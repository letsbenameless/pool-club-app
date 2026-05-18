from flask import Flask, render_template, request, redirect, url_for
from memberlist import members

import json
import os
import random


app = Flask(__name__)

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bracket_state.json")


def load_state():
    if not os.path.exists(STATE_FILE):
        return None

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return None


def save_state(state):
    temp_file = STATE_FILE + ".tmp"

    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

    os.replace(temp_file, STATE_FILE)

def shorten_name(full_name, buyback=False):
    parts = full_name.strip().split()

    if not parts:
        return ""

    if len(parts) == 1:
        short = parts[0]
    else:
        short = f"{parts[0]} {parts[-1][0]}."

    return f"{short}*" if buyback else short


def parse_textarea(text):
    return [
        name.strip()
        for name in text.splitlines()
        if name.strip()
    ]


def pad_to_64(players):
    players = players[:64]
    return players + [""] * (64 - len(players))


def empty_state():
    return {
        "left": [""] * 32,
        "right": [""] * 32,
        "advancements": {},
        "active_matches": [],
        "replacement_slots": [],
        "counts": {
            "late_players": 0,
            "buybacks": 0
        }
    }


def normalize_state(state):
    if not state:
        return empty_state()

    state.setdefault("left", [""] * 32)
    state.setdefault("right", [""] * 32)
    state.setdefault("advancements", {})
    state.setdefault("active_matches", [])
    state.setdefault("replacement_slots", [])
    state.setdefault("counts", {})
    state["counts"].setdefault("late_players", 0)
    state["counts"].setdefault("buybacks", 0)

    return state


def get_register_counts():
    state = normalize_state(load_state() or empty_state())
    all_slots = state.get("left", []) + state.get("right", [])

    return {
        "total_players": sum(1 for name in all_slots if name.strip()),
        "late_players": state.get("counts", {}).get("late_players", 0),
        "buybacks": state.get("counts", {}).get("buybacks", 0),
    }


def slot_parts_from_global_index(index):
    side = "L" if index < 32 else "R"
    slot_index = index if index < 32 else index - 32
    return side, slot_index, f"{side}-0-{slot_index}"


def is_first_round_slot_empty(state, index):
    first_round = state["left"] + state["right"]
    side, slot_index, slot_id = slot_parts_from_global_index(index)
    current_name = state.get("advancements", {}).get(slot_id, first_round[index])
    return not str(current_name or "").strip()


def add_players_to_empty_slots(state, players_to_add):
    first_round = state["left"] + state["right"]

    state.setdefault("advancements", {})
    state.setdefault("replacement_slots", [])

    preferred_indexes = []
    for index in state.get("replacement_slots", []):
        if isinstance(index, int) and 0 <= index < 64 and is_first_round_slot_empty(state, index):
            if index not in preferred_indexes:
                preferred_indexes.append(index)

    normal_empty_indexes = []
    for i in range(64):
        if i in preferred_indexes:
            continue

        if is_first_round_slot_empty(state, i):
            normal_empty_indexes.append(i)

    empty_indexes = preferred_indexes + normal_empty_indexes
    used_indexes = []

    for name, index in zip(players_to_add, empty_indexes):
        first_round[index] = name

        side, slot_index, slot_id = slot_parts_from_global_index(index)
        state["advancements"][slot_id] = name
        used_indexes.append(index)

    state["replacement_slots"] = [
        index for index in state.get("replacement_slots", [])
        if index not in used_indexes and is_first_round_slot_empty(state, index)
    ]

    state["left"] = first_round[:32]
    state["right"] = first_round[32:64]

    return state


def remove_first_round_player_from_state(state, slot_id):
    parts = slot_id.split("-")

    if len(parts) != 3:
        return False, "Invalid slot id"

    side, round_index, slot_index_text = parts

    if side not in {"L", "R"} or round_index != "0":
        return False, "Only first-round players can be removed"

    try:
        slot_index = int(slot_index_text)
    except ValueError:
        return False, "Invalid slot index"

    if not 0 <= slot_index < 32:
        return False, "Slot index out of range"

    global_index = slot_index if side == "L" else slot_index + 32
    first_round = state["left"] + state["right"]

    existing_name = state.get("advancements", {}).get(slot_id, first_round[global_index])

    if not str(existing_name or "").strip():
        return False, "That first-round slot is already empty"

    first_round[global_index] = ""
    state["left"] = first_round[:32]
    state["right"] = first_round[32:64]

    state.setdefault("advancements", {})
    state["advancements"][slot_id] = ""

    state.setdefault("replacement_slots", [])
    if global_index not in state["replacement_slots"]:
        state["replacement_slots"].append(global_index)

    # If this removed player had already been copied into later rounds, leave those
    # results alone. This keeps right-click removal safe for fixing first-round
    # sign-up/vacancy mistakes without rewriting match history.
    return True, existing_name


@app.route("/")
def bracket():
    state = load_state()

    if state is None:
        state = empty_state()
        save_state(state)
    else:
        state = normalize_state(state)

    return render_template("bracket.html", bracket_data=state)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        action = request.form.get("action")

        selected_members = request.form.getlist("members")

        new_players = parse_textarea(
            request.form.get("new_players", "")
        )

        late_players = parse_textarea(
            request.form.get("late_players", "")
        )

        buybacks = parse_textarea(
            request.form.get("buybacks", "")
        )

        if action == "generate":
            players = [
                shorten_name(name)
                for name in selected_members + new_players
            ]

            random.shuffle(players)

            slots = pad_to_64(players)

            state = {
                "left": slots[:32],
                "right": slots[32:64],
                "advancements": {},
                "active_matches": [],
                "replacement_slots": [],
                "counts": {
                    "late_players": 0,
                    "buybacks": 0
                }
            }

            save_state(state)

            return redirect(url_for("bracket"))

        if action == "add_late":
            state = normalize_state(load_state() or empty_state())

            players_to_add = [
                shorten_name(name)
                for name in late_players
            ]

            random.shuffle(players_to_add)

            available_slots = sum(
                1 for i in range(64)
                if is_first_round_slot_empty(state, i)
            )
            added_count = min(len(players_to_add), available_slots)

            state = add_players_to_empty_slots(state, players_to_add)
            state["counts"]["late_players"] += added_count

            save_state(state)

            return redirect(url_for("bracket"))

        if action == "add_buybacks":
            state = normalize_state(load_state() or empty_state())

            players_to_add = [
                shorten_name(name, buyback=True)
                for name in buybacks
            ]

            random.shuffle(players_to_add)

            available_slots = sum(
                1 for i in range(64)
                if is_first_round_slot_empty(state, i)
            )
            added_count = min(len(players_to_add), available_slots)

            state = add_players_to_empty_slots(state, players_to_add)
            state["counts"]["buybacks"] += added_count

            save_state(state)

            return redirect(url_for("bracket"))

    return render_template(
        "register.html",
        members=members,
        counts=get_register_counts()
    )


@app.route("/remove_first_round_player", methods=["POST"])
def remove_first_round_player_route():
    incoming = request.get_json() or {}
    slot_id = incoming.get("slot_id", "")

    state = normalize_state(load_state() or empty_state())
    success, result = remove_first_round_player_from_state(state, slot_id)

    if not success:
        return {"success": False, "error": result}, 400

    save_state(state)

    return {
        "success": True,
        "removed_player": result,
        "slot_id": slot_id
    }


@app.route("/save_bracket", methods=["POST"])
def save_bracket_route():
    incoming = request.get_json()

    if not incoming:
        return {"success": False, "error": "No JSON received"}, 400

    old_state = normalize_state(load_state() or empty_state())

    incoming_names = incoming.get("left", []) + incoming.get("right", [])
    old_names = old_state.get("left", []) + old_state.get("right", [])

    incoming_has_players = any(name for name in incoming_names)
    old_has_players = any(name for name in old_names)

    if old_has_players and not incoming_has_players:
        return {
            "success": False,
            "error": "Refused to overwrite populated bracket with empty bracket"
        }, 400

    incoming = normalize_state(incoming)

    # Browser saves only describe bracket UI state. Preserve server-side
    # registration metadata so a bracket click cannot wipe counts or vacancies.
    incoming["counts"] = old_state.get("counts", {"late_players": 0, "buybacks": 0})
    incoming["replacement_slots"] = old_state.get("replacement_slots", [])

    save_state(incoming)

    return {"success": True}

if __name__ == "__main__":
    app.run(debug=True)