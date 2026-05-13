from flask import Flask, render_template, request, redirect, url_for
from memberlist import members

import json
import os
import random


app = Flask(__name__)

STATE_FILE = "bracket_state.json"


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
        "active_matches": []
    }


def add_players_to_empty_slots(state, players_to_add):
    first_round = state["left"] + state["right"]

    state.setdefault("advancements", {})

    empty_indexes = []

    for i in range(64):
        side = "L" if i < 32 else "R"
        slot_index = i if i < 32 else i - 32
        slot_id = f"{side}-0-{slot_index}"

        current_name = state["advancements"].get(slot_id, first_round[i])

        if not current_name:
            empty_indexes.append(i)

    for name, index in zip(players_to_add, empty_indexes):
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
        state = empty_state()
        save_state(state)

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
                "active_matches": []
            }

            save_state(state)

            return redirect(url_for("bracket"))

        if action == "add_late":
            state = load_state() or empty_state()

            players_to_add = [
                shorten_name(name)
                for name in late_players
            ]

            state = add_players_to_empty_slots(state, players_to_add)

            save_state(state)

            return redirect(url_for("bracket"))

        if action == "add_buybacks":
            state = load_state() or empty_state()

            players_to_add = [
                shorten_name(name, buyback=True)
                for name in buybacks
            ]

            state = add_players_to_empty_slots(state, players_to_add)

            save_state(state)

            return redirect(url_for("bracket"))

    return render_template(
        "register.html",
        members=members
    )


@app.route("/save_bracket", methods=["POST"])
def save_bracket_route():
    incoming = request.get_json()

    if not incoming:
        return {"success": False, "error": "No JSON received"}, 400

    old_state = load_state() or empty_state()

    incoming_names = incoming.get("left", []) + incoming.get("right", [])
    old_names = old_state.get("left", []) + old_state.get("right", [])

    incoming_has_players = any(name for name in incoming_names)
    old_has_players = any(name for name in old_names)

    if old_has_players and not incoming_has_players:
        return {
            "success": False,
            "error": "Refused to overwrite populated bracket with empty bracket"
        }, 400

    save_state(incoming)

    return {"success": True}

if __name__ == "__main__":
    app.run(debug=True)