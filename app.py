from flask import Flask, render_template, request, redirect, url_for
from memberlist import members

import json
import os
import random
import threading


app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "bracket_state.json")
STATE_LOCK = threading.Lock()


def empty_state():
    return {
        "left": [""] * 32,
        "right": [""] * 32,
        "advancements": {},
        "active_matches": [],
        "champion": "Champion",
        "revision": 0,
    }


def normalize_state(state):
    """Keep the saved JSON shape stable even if old/partial data is loaded."""
    base = empty_state()

    if not isinstance(state, dict):
        return base

    left = state.get("left", [])
    right = state.get("right", [])

    if not isinstance(left, list):
        left = []
    if not isinstance(right, list):
        right = []

    base["left"] = (left + [""] * 32)[:32]
    base["right"] = (right + [""] * 32)[:32]

    advancements = state.get("advancements", {})
    base["advancements"] = advancements if isinstance(advancements, dict) else {}

    active_matches = state.get("active_matches", [])
    base["active_matches"] = active_matches if isinstance(active_matches, list) else []

    champion = state.get("champion", "Champion")
    base["champion"] = champion if isinstance(champion, str) and champion else "Champion"

    try:
        base["revision"] = int(state.get("revision", 0))
    except (TypeError, ValueError):
        base["revision"] = 0

    return base


def load_state_unlocked():
    if not os.path.exists(STATE_FILE):
        return empty_state()

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return normalize_state(json.load(f))
    except (json.JSONDecodeError, OSError):
        return empty_state()


def load_state():
    with STATE_LOCK:
        return load_state_unlocked()


def save_state_unlocked(state):
    state = normalize_state(state)
    temp_file = STATE_FILE + ".tmp"

    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

    os.replace(temp_file, STATE_FILE)
    return state


def save_new_revision_unlocked(state):
    old_state = load_state_unlocked()
    state = normalize_state(state)
    state["revision"] = old_state.get("revision", 0) + 1
    return save_state_unlocked(state)


def save_new_revision(state):
    with STATE_LOCK:
        return save_new_revision_unlocked(state)


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


def add_players_to_empty_slots(state, players_to_add):
    """
    Add players to empty first-round slots only.
    Existing first-round names, advancements, active matches, and champion are preserved.
    """
    state = normalize_state(state)
    first_round = state["left"] + state["right"]

    used_names = {
        name.strip().lower()
        for name in first_round + list(state["advancements"].values())
        if isinstance(name, str) and name.strip()
    }

    clean_players = []
    for name in players_to_add:
        key = name.strip().lower()
        if key and key not in used_names:
            clean_players.append(name)
            used_names.add(key)

    empty_indexes = []

    for i in range(64):
        side = "L" if i < 32 else "R"
        slot_index = i if i < 32 else i - 32
        slot_id = f"{side}-0-{slot_index}"

        current_name = state["advancements"].get(slot_id, first_round[i])

        if not current_name:
            empty_indexes.append(i)

    for name, index in zip(clean_players, empty_indexes):
        first_round[index] = name

        side = "L" if index < 32 else "R"
        slot_index = index if index < 32 else index - 32
        slot_id = f"{side}-0-{slot_index}"

        state["advancements"][slot_id] = name

    state["left"] = first_round[:32]
    state["right"] = first_round[32:64]

    return state


@app.route("/")
def bracket():
    state = load_state()

    if state is None:
        state = save_new_revision(empty_state())

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

            state = empty_state()
            state["left"] = slots[:32]
            state["right"] = slots[32:64]
            state["advancements"] = {
                f"L-0-{i}": name for i, name in enumerate(state["left"]) if name
            } | {
                f"R-0-{i}": name for i, name in enumerate(state["right"]) if name
            }

            save_new_revision(state)

            return redirect(url_for("bracket"))

        if action == "add_late":
            players_to_add = [
                shorten_name(name)
                for name in late_players
            ]

            with STATE_LOCK:
                state = load_state_unlocked()
                state = add_players_to_empty_slots(state, players_to_add)
                save_new_revision_unlocked(state)

            return redirect(url_for("bracket"))

        if action == "add_buybacks":
            players_to_add = [
                shorten_name(name, buyback=True)
                for name in buybacks
            ]

            with STATE_LOCK:
                state = load_state_unlocked()
                state = add_players_to_empty_slots(state, players_to_add)
                save_new_revision_unlocked(state)

            return redirect(url_for("bracket"))

    return render_template(
        "register.html",
        members=members,
        initial_signups=[]
    )


@app.route("/save_bracket", methods=["POST"])
def save_bracket_route():
    incoming = request.get_json()

    if not incoming:
        return {"success": False, "error": "No JSON received"}, 400

    with STATE_LOCK:
        old_state = load_state_unlocked()
        incoming = normalize_state(incoming)

        incoming_revision = incoming.get("revision", 0)
        old_revision = old_state.get("revision", 0)

        if incoming_revision < old_revision:
            return {
                "success": False,
                "error": "Stale browser state refused. Refreshing will load the latest bracket.",
                "revision": old_revision,
            }, 409

        incoming_names = incoming.get("left", []) + incoming.get("right", [])
        old_names = old_state.get("left", []) + old_state.get("right", [])

        incoming_has_players = any(name for name in incoming_names)
        old_has_players = any(name for name in old_names)

        if old_has_players and not incoming_has_players:
            return {
                "success": False,
                "error": "Refused to overwrite populated bracket with empty bracket",
                "revision": old_revision,
            }, 400

        saved = save_new_revision_unlocked(incoming)

    return {"success": True, "revision": saved["revision"]}


if __name__ == "__main__":
    app.run(debug=True)
