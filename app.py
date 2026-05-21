from flask import Flask, render_template, request, redirect, url_for
from memberlist import members as default_members

import json
import os
import random
import sqlite3
from contextlib import closing


app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "pool_comp.sqlite3")
OLD_STATE_FILE = os.path.join(BASE_DIR, "bracket_state.json")
MEMBERS_FILE = os.path.join(BASE_DIR, "members.json")
DEFAULT_ELO = 1000
ELO_K_FACTOR = 32
APP_STATE_ID = 1


def get_db():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db():
    with closing(get_db()) as conn:
        ensure_app_state_schema(conn)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                elo REAL NOT NULL DEFAULT 1000,
                games_played INTEGER NOT NULL DEFAULT 0,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS match_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id TEXT NOT NULL,
                winner_name TEXT NOT NULL,
                loser_name TEXT NOT NULL,
                winner_elo_before REAL NOT NULL,
                loser_elo_before REAL NOT NULL,
                winner_elo_after REAL NOT NULL,
                loser_elo_after REAL NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(match_id, winner_name, loser_name)
            );

            CREATE TABLE IF NOT EXISTS game_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_result_id INTEGER,
                match_id TEXT NOT NULL,
                winner_name TEXT NOT NULL,
                loser_name TEXT NOT NULL,
                winner_elo_before REAL NOT NULL,
                loser_elo_before REAL NOT NULL,
                winner_elo_after REAL NOT NULL,
                loser_elo_after REAL NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                undone_at TEXT,
                FOREIGN KEY(match_result_id) REFERENCES match_results(id) ON DELETE SET NULL,
                UNIQUE(match_result_id)
            );
        """)
        conn.execute("""
            INSERT OR IGNORE INTO game_history (
                match_result_id, match_id, winner_name, loser_name,
                winner_elo_before, loser_elo_before,
                winner_elo_after, loser_elo_after, created_at
            )
            SELECT id, match_id, winner_name, loser_name,
                   winner_elo_before, loser_elo_before,
                   winner_elo_after, loser_elo_after, created_at
            FROM match_results
        """)
        conn.commit()


def json_dumps_compact(value):
    return json.dumps(value, separators=(",", ":"))


def json_loads_or_default(value, default):
    if value is None:
        return default

    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def create_app_state_table(conn):
    conn.execute("""
        CREATE TABLE app_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            left_slots TEXT NOT NULL,
            right_slots TEXT NOT NULL,
            advancements TEXT NOT NULL,
            active_matches TEXT NOT NULL,
            replacement_slots TEXT NOT NULL,
            late_players INTEGER NOT NULL DEFAULT 0,
            buybacks INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)


def app_state_table_columns(conn):
    return {
        row["name"]
        for row in conn.execute("PRAGMA table_info(app_state)").fetchall()
    }


def save_state_with_conn(conn, state):
    state = normalize_state(state)
    counts = state.get("counts", {})

    conn.execute(
        """
        INSERT INTO app_state (
            id, left_slots, right_slots, advancements, active_matches,
            replacement_slots, late_players, buybacks, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET
            left_slots = excluded.left_slots,
            right_slots = excluded.right_slots,
            advancements = excluded.advancements,
            active_matches = excluded.active_matches,
            replacement_slots = excluded.replacement_slots,
            late_players = excluded.late_players,
            buybacks = excluded.buybacks,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            APP_STATE_ID,
            json_dumps_compact(state.get("left", [])),
            json_dumps_compact(state.get("right", [])),
            json_dumps_compact(state.get("advancements", {})),
            json_dumps_compact(state.get("active_matches", [])),
            json_dumps_compact(state.get("replacement_slots", [])),
            int(counts.get("late_players", 0)),
            int(counts.get("buybacks", 0)),
        )
    )


def ensure_app_state_schema(conn):
    columns = app_state_table_columns(conn)

    if not columns:
        create_app_state_table(conn)
        return

    typed_columns = {
        "id", "left_slots", "right_slots", "advancements", "active_matches",
        "replacement_slots", "late_players", "buybacks", "updated_at"
    }

    if typed_columns.issubset(columns):
        return

    legacy_state = None

    if {"key", "value"}.issubset(columns):
        row = conn.execute(
            "SELECT value FROM app_state WHERE key = 'bracket_state'"
        ).fetchone()

        if row:
            legacy_state = json_loads_or_default(row["value"], None)

    conn.execute("ALTER TABLE app_state RENAME TO app_state_legacy")
    create_app_state_table(conn)

    if legacy_state:
        save_state_with_conn(conn, legacy_state)

    conn.execute("DROP TABLE app_state_legacy")


def clean_player_name(name):
    return str(name or "").strip().rstrip("*").strip()


def get_or_create_player(conn, name):
    clean_name = clean_player_name(name)
    if not clean_name:
        return None

    conn.execute(
        "INSERT OR IGNORE INTO players (name, elo) VALUES (?, ?)",
        (clean_name, DEFAULT_ELO)
    )
    return conn.execute(
        "SELECT * FROM players WHERE name = ?",
        (clean_name,)
    ).fetchone()


def ensure_players_exist(names):
    with closing(get_db()) as conn:
        for name in names:
            if clean_player_name(name):
                get_or_create_player(conn, name)
        conn.commit()


def load_members():
    if os.path.exists(MEMBERS_FILE):
        try:
            with open(MEMBERS_FILE, "r", encoding="utf-8") as f:
                stored_members = json.load(f)

            if isinstance(stored_members, list):
                return [
                    str(name).strip()
                    for name in stored_members
                    if str(name).strip()
                ]
        except json.JSONDecodeError:
            pass

    return list(default_members)


def save_members(members):
    clean_members = []
    seen_members = set()

    for member in members:
        name = str(member or "").strip()
        key = name.casefold()

        if name and key not in seen_members:
            clean_members.append(name)
            seen_members.add(key)

    with open(MEMBERS_FILE, "w", encoding="utf-8") as f:
        json.dump(clean_members, f, indent=2)

    return clean_members


def load_state():
    with closing(get_db()) as conn:
        row = conn.execute(
            """
            SELECT left_slots, right_slots, advancements, active_matches,
                   replacement_slots, late_players, buybacks
            FROM app_state
            WHERE id = ?
            """,
            (APP_STATE_ID,)
        ).fetchone()

    if row:
        return normalize_state({
            "left": json_loads_or_default(row["left_slots"], [""] * 32),
            "right": json_loads_or_default(row["right_slots"], [""] * 32),
            "advancements": json_loads_or_default(row["advancements"], {}),
            "active_matches": json_loads_or_default(row["active_matches"], []),
            "replacement_slots": json_loads_or_default(row["replacement_slots"], []),
            "counts": {
                "late_players": row["late_players"],
                "buybacks": row["buybacks"],
            }
        })

    # One-time migration from the old JSON save file, if it exists.
    if os.path.exists(OLD_STATE_FILE):
        try:
            with open(OLD_STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            save_state(state)
            return state
        except json.JSONDecodeError:
            return None

    return None


def save_state(state):
    with closing(get_db()) as conn:
        save_state_with_conn(conn, state)
        conn.commit()

def shorten_name(full_name, buyback=False):
    parts = full_name.strip().split()

    if not parts:
        return ""

    if len(parts) == 1:
        short = parts[0]
    else:
        short = f"{parts[0]} {parts[-1][:2]}."

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


init_db()


def get_register_counts():
    state = normalize_state(load_state() or empty_state())
    all_slots = state.get("left", []) + state.get("right", [])

    return {
        "total_players": sum(1 for name in all_slots if name.strip()),
        "late_players": state.get("counts", {}).get("late_players", 0),
        "buybacks": state.get("counts", {}).get("buybacks", 0),
    }

def calculate_elo(winner_elo, loser_elo, k_factor=ELO_K_FACTOR):
    expected_winner = 1 / (1 + 10 ** ((loser_elo - winner_elo) / 400))
    expected_loser = 1 / (1 + 10 ** ((winner_elo - loser_elo) / 400))

    winner_after = winner_elo + k_factor * (1 - expected_winner)
    loser_after = loser_elo + k_factor * (0 - expected_loser)

    return round(winner_after, 2), round(loser_after, 2)


def record_match_result(match_id, winner_name, loser_name):
    winner_name = clean_player_name(winner_name)
    loser_name = clean_player_name(loser_name)
    match_id = str(match_id or "").strip()

    if not match_id or not winner_name or not loser_name:
        return False, "Missing match id, winner, or loser"

    if winner_name == loser_name:
        return False, "Winner and loser cannot be the same player"

    with closing(get_db()) as conn:
        winner = get_or_create_player(conn, winner_name)
        loser = get_or_create_player(conn, loser_name)

        winner_after, loser_after = calculate_elo(winner["elo"], loser["elo"])

        try:
            cursor = conn.execute(
                """
                INSERT INTO match_results (
                    match_id, winner_name, loser_name,
                    winner_elo_before, loser_elo_before,
                    winner_elo_after, loser_elo_after
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    match_id, winner_name, loser_name,
                    winner["elo"], loser["elo"],
                    winner_after, loser_after
                )
            )
            match_result_id = cursor.lastrowid
        except sqlite3.IntegrityError:
            return True, {
                "already_recorded": True,
                "winner_name": winner_name,
                "loser_name": loser_name,
                "winner_elo": round(winner["elo"], 2),
                "loser_elo": round(loser["elo"], 2),
            }

        conn.execute(
            """
            UPDATE players
            SET elo = ?, games_played = games_played + 1, wins = wins + 1, updated_at = CURRENT_TIMESTAMP
            WHERE name = ?
            """,
            (winner_after, winner_name)
        )
        conn.execute(
            """
            UPDATE players
            SET elo = ?, games_played = games_played + 1, losses = losses + 1, updated_at = CURRENT_TIMESTAMP
            WHERE name = ?
            """,
            (loser_after, loser_name)
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO game_history (
                match_result_id, match_id, winner_name, loser_name,
                winner_elo_before, loser_elo_before,
                winner_elo_after, loser_elo_after
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                match_result_id, match_id, winner_name, loser_name,
                winner["elo"], loser["elo"], winner_after, loser_after
            )
        )
        conn.commit()

    return True, {
        "already_recorded": False,
        "match_result_id": match_result_id,
        "winner_name": winner_name,
        "loser_name": loser_name,
        "winner_elo_before": round(winner["elo"], 2),
        "loser_elo_before": round(loser["elo"], 2),
        "winner_elo_after": winner_after,
        "loser_elo_after": loser_after,
    }


def get_rankings():
    with closing(get_db()) as conn:
        rows = conn.execute(
            """
            SELECT name, elo, games_played, wins, losses
            FROM players
            ORDER BY elo DESC, wins DESC, name ASC
            """
        ).fetchall()

    return [dict(row) for row in rows]


def get_game_history(limit=100):
    with closing(get_db()) as conn:
        rows = conn.execute(
            """
            SELECT id, match_result_id, match_id, winner_name, loser_name,
                   winner_elo_before, loser_elo_before,
                   winner_elo_after, loser_elo_after, created_at, undone_at
            FROM game_history
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (limit,)
        ).fetchall()

    return [dict(row) for row in rows]


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

        new_players = parse_textarea(
            request.form.get("new_players", "")
        )

        late_players = parse_textarea(
            request.form.get("late_players", "")
        )

        buybacks = parse_textarea(
            request.form.get("buybacks", "")
        )

        if action == "end_comp":
            save_state(empty_state())
            return redirect(url_for("register"))

        if action == "generate":
            players = [
                shorten_name(name)
                for name in new_players
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

            ensure_players_exist(players)
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

            ensure_players_exist(players_to_add[:added_count])
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

            ensure_players_exist(players_to_add[:added_count])
            save_state(state)

            return redirect(url_for("bracket"))

    counts = get_register_counts()

    return render_template(
        "register.html",
        members=load_members(),
        counts=counts,
        initial_signups=[],
        rankings=get_rankings(),
        has_initial_comp=counts["total_players"] > 0
    )


@app.route("/members", methods=["GET", "POST"])
def edit_members():
    if request.method == "POST":
        edited_members = parse_textarea(request.form.get("members", ""))
        save_members(edited_members)
        return redirect(url_for("register"))

    return render_template(
        "members.html",
        members_text="\n".join(load_members()),
        member_count=len(load_members())
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


@app.route("/record_match_result", methods=["POST"])
def record_match_result_route():
    incoming = request.get_json() or {}

    success, result = record_match_result(
        incoming.get("match_id", ""),
        incoming.get("winner_name", ""),
        incoming.get("loser_name", "")
    )

    if not success:
        return {"success": False, "error": result}, 400

    return {"success": True, **result}


@app.route("/rankings")
def rankings_route():
    return {"players": get_rankings()}


@app.route("/game_history")
def game_history_route():
    return {"games": get_game_history()}


@app.route("/undo_match_result", methods=["POST"])
def undo_match_result_route():
    incoming = request.get_json() or {}

    try:
        result_id = int(incoming.get("match_result_id"))
    except (TypeError, ValueError):
        return {"success": False, "error": "Missing match result id"}, 400

    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT * FROM match_results WHERE id = ?",
            (result_id,)
        ).fetchone()

        if not row:
            return {"success": False, "error": "Match result was not found"}, 404

        conn.execute(
            """
            UPDATE players
            SET elo = ?, games_played = MAX(games_played - 1, 0), wins = MAX(wins - 1, 0), updated_at = CURRENT_TIMESTAMP
            WHERE name = ?
            """,
            (row["winner_elo_before"], row["winner_name"])
        )
        conn.execute(
            """
            UPDATE players
            SET elo = ?, games_played = MAX(games_played - 1, 0), losses = MAX(losses - 1, 0), updated_at = CURRENT_TIMESTAMP
            WHERE name = ?
            """,
            (row["loser_elo_before"], row["loser_name"])
        )
        conn.execute(
            """
            UPDATE game_history
            SET undone_at = CURRENT_TIMESTAMP
            WHERE match_result_id = ?
            """,
            (result_id,)
        )
        conn.execute("DELETE FROM match_results WHERE id = ?", (result_id,))
        conn.commit()

    return {"success": True}

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
