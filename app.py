from flask import Flask, Response, render_template, request, redirect, session, url_for
from memberlist import members as default_members
from payment_report import (
    build_payment_report_pdf,
    clean_date,
    competition_week,
    competition_week_label,
    format_comp_date,
)

import datetime as dt
import smtplib
import json
import os
import random
import re
import secrets
import sqlite3
from contextlib import closing
from email.message import EmailMessage
from functools import wraps
from werkzeug.security import check_password_hash, generate_password_hash


app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "pool_comp.sqlite3")
SECRET_KEY_FILE = os.path.join(BASE_DIR, ".flask_secret_key")
OLD_STATE_FILE = os.path.join(BASE_DIR, "bracket_state.json")
MEMBERS_FILE = os.path.join(BASE_DIR, "members.json")
KNOWN_PLAYERS_FILE = os.path.join(BASE_DIR, "known_players.json")
DEFAULT_ELO = 1000
ELO_K_FACTOR = 32
APP_STATE_ID = 1
TEAM_SEPARATOR_RE = re.compile(r"(?:[&+/,.]|\s{2,})")
RATING_TEAM_SEPARATOR_RE = re.compile(r"(?:[&+/,]|\s{2,})")
REGISTRATION_STATE_ID = 1
FINANCE_STATE_ID = 1
BRACKET_SETTINGS_ID = 1
REGISTRATION_FIELDS = ("new_players", "late_players", "buybacks")
SIDE_ROUNDS = [32, 16, 8, 4, 2, 1]
GAME_ROUND_LABELS = {
    0: "First Round",
    1: "Second Round",
    2: "Third Round",
    3: "Quarter Final",
    4: "Semi Final",
    "final": "Final",
}
GAME_ROUND_SHORT_LABELS = {
    0: "First",
    1: "Second",
    2: "Third",
    3: "Quarter",
    4: "Semi",
    "final": "Final",
}
GAME_ROUND_NUMBER_LABELS = {
    0: "First Round",
    1: "Second Round",
    2: "Third Round",
    3: "Fourth Round",
    4: "Fifth Round",
    5: "Sixth Round",
}
ROUND_ROBIN_FINAL_SHORT_LABEL = "RR Final"
ROUND_ORDER = [0, 1, 2, 3, 4, "final"]
TABLE_COUNT = 3
PRESET_WINNINGS = {
    32: {1: 45.0, 2: 30.0, 3: 15.0},
    48: {1: 50.0, 2: 35.0, 3: 25.0},
    64: {1: 60.0, 2: 45.0, 3: 30.0},
}
PAYOUT_PLACES = (1, 2, 3)
EXECUTIVE_ROLES = (
    "Executive",
    "President",
    "Vice President",
    "Treasurer",
    "Secretary",
    "Social Media Manager",
)
DEFAULT_BRACKET_SETTINGS = {
    "highlight_color": "#FF7900",
    "remove_hover_color": "#ff7c7c",
    "table_badge_background": "#FF7900",
    "table_badge_text": "#050505",
}
BRACKET_COLOR_PRESETS = [
    {"name": "Orange", "value": "#FF7900"},
    {"name": "Green", "value": "#7CFF7C"},
    {"name": "Blue", "value": "#4DA3FF"},
    {"name": "Pink", "value": "#FF5FA2"},
    {"name": "Yellow", "value": "#FFD447"},
    {"name": "White", "value": "#f2f2f2"},
]
CSS_COLOR_RE = re.compile(
    r"^(#[0-9A-Fa-f]{3}(?:[0-9A-Fa-f]{3})?|rgb\(\s*(?:25[0-5]|2[0-4]\d|1?\d?\d)\s*,\s*(?:25[0-5]|2[0-4]\d|1?\d?\d)\s*,\s*(?:25[0-5]|2[0-4]\d|1?\d?\d)\s*\))$"
)


def load_secret_key():
    configured_key = os.environ.get("POOL_APP_SECRET_KEY")

    if configured_key:
        return configured_key

    if os.path.exists(SECRET_KEY_FILE):
        with open(SECRET_KEY_FILE, "r", encoding="utf-8") as f:
            key = f.read().strip()
            if key:
                return key

    key = secrets.token_urlsafe(48)

    with open(SECRET_KEY_FILE, "w", encoding="utf-8") as f:
        f.write(key)

    return key


app.secret_key = load_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)


def competition_context_for_date(date_key=None):
    comp_date = clean_date(date_key or most_recent_tuesday_key())
    return {
        "date": comp_date.isoformat(),
        "date_label": format_comp_date(comp_date),
        "week": competition_week(comp_date),
        "week_label": competition_week_label(comp_date),
        "display": f"{format_comp_date(comp_date)} ({competition_week_label(comp_date)})",
    }


@app.context_processor
def inject_competition_context():
    return {
        "competition": competition_context_for_date(),
        "executive_username": session.get("executive_username", ""),
        "executive_player_name": session.get("executive_player_name", ""),
        "executive_role": session.get("executive_role", EXECUTIVE_ROLES[0]),
    }


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
        ensure_registration_state_schema(conn)
        ensure_finance_state_schema(conn)
        ensure_bracket_settings_schema(conn)
        ensure_executive_users_schema(conn)
        ensure_executive_requests_schema(conn)
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


def ensure_executive_users_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS executive_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            player_name TEXT NOT NULL DEFAULT '',
            executive_role TEXT NOT NULL DEFAULT 'Executive',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_login_at TEXT
        )
    """)
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(executive_users)").fetchall()
    }

    if "player_name" not in columns:
        conn.execute("ALTER TABLE executive_users ADD COLUMN player_name TEXT NOT NULL DEFAULT ''")

    if "executive_role" not in columns:
        conn.execute("ALTER TABLE executive_users ADD COLUMN executive_role TEXT NOT NULL DEFAULT 'Executive'")

    migrate_known_executive_usernames(conn)


def migrate_known_executive_usernames(conn):
    conn.execute(
        """
        UPDATE executive_users
        SET username = ?
        WHERE username = ?
          AND NOT EXISTS (
              SELECT 1 FROM executive_users existing
              WHERE existing.username = ?
          )
        """,
        ("rockateeer12@gmail.com", "bludclawg", "rockateeer12@gmail.com")
    )


def ensure_executive_requests_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS executive_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            player_name TEXT NOT NULL,
            executive_role TEXT NOT NULL DEFAULT 'Executive',
            status TEXT NOT NULL DEFAULT 'pending',
            requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            resolved_at TEXT,
            resolved_by_username TEXT NOT NULL DEFAULT ''
        )
    """)


def clean_username(username):
    return str(username or "").strip().casefold()


def clean_email(value):
    email = clean_username(value)

    if not email or "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        return ""

    return email


def clean_executive_role(role):
    clean = str(role or "").strip()
    return clean if clean in EXECUTIVE_ROLES else EXECUTIVE_ROLES[0]


def executive_user_count():
    with closing(get_db()) as conn:
        ensure_executive_users_schema(conn)
        row = conn.execute("SELECT COUNT(*) AS count FROM executive_users").fetchone()
        return int(row["count"] or 0)


def get_executive_user(username):
    clean = clean_username(username)

    if not clean:
        return None

    with closing(get_db()) as conn:
        ensure_executive_users_schema(conn)
        return conn.execute(
            """
            SELECT id, username, password_hash, player_name, executive_role
            FROM executive_users
            WHERE username = ?
            """,
            (clean,)
        ).fetchone()


def get_executive_user_by_id(user_id):
    try:
        clean_id = int(user_id)
    except (TypeError, ValueError):
        return None

    with closing(get_db()) as conn:
        ensure_executive_users_schema(conn)
        return conn.execute(
            """
            SELECT id, username, password_hash, player_name, executive_role
            FROM executive_users
            WHERE id = ?
            """,
            (clean_id,)
        ).fetchone()


def create_executive_user(username, password, player_name="", role=None):
    clean = clean_email(username)

    if not clean:
        return False, "Enter a valid email address."

    if len(clean) > 80:
        return False, "Use a username under 80 characters."

    if len(password or "") < 8:
        return False, "Use a password with at least 8 characters."

    canonical_player_name = canonical_known_player_name(player_name)

    if player_name and not canonical_player_name:
        return False, "Choose a known player from the list."

    clean_role = clean_executive_role(role)

    try:
        with closing(get_db()) as conn:
            ensure_executive_users_schema(conn)
            conn.execute(
                """
                INSERT INTO executive_users (
                    username, password_hash, player_name, executive_role
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    clean,
                    generate_password_hash(password),
                    canonical_player_name,
                    clean_role,
                )
            )
            conn.commit()
    except sqlite3.IntegrityError:
        return False, "That username already exists."

    return True, clean


def pending_executive_request_for_email(email):
    clean = clean_email(email)

    if not clean:
        return None

    with closing(get_db()) as conn:
        ensure_executive_requests_schema(conn)
        return conn.execute(
            """
            SELECT *
            FROM executive_requests
            WHERE email = ?
              AND status = 'pending'
            ORDER BY requested_at DESC
            LIMIT 1
            """,
            (clean,)
        ).fetchone()


def pending_executive_requests():
    with closing(get_db()) as conn:
        ensure_executive_requests_schema(conn)
        return conn.execute(
            """
            SELECT *
            FROM executive_requests
            WHERE status = 'pending'
            ORDER BY requested_at ASC
            """
        ).fetchall()


def create_executive_request(email, password, player_name, role):
    clean = clean_email(email)

    if not clean:
        return False, "Enter a valid email address."

    if len(password or "") < 8:
        return False, "Use a password with at least 8 characters."

    canonical_player_name = canonical_known_player_name(player_name)

    if not canonical_player_name:
        return False, "Choose your known player name from the list."

    clean_role = clean_executive_role(role)

    if get_executive_user(clean):
        return False, "An executive account already exists for that email."

    if pending_executive_request_for_email(clean):
        return False, "There is already a pending request for that email."

    with closing(get_db()) as conn:
        ensure_executive_requests_schema(conn)
        conn.execute(
            """
            INSERT INTO executive_requests (
                email, password_hash, player_name, executive_role
            ) VALUES (?, ?, ?, ?)
            """,
            (
                clean,
                generate_password_hash(password),
                canonical_player_name,
                clean_role,
            )
        )
        conn.commit()

    request_info = {
        "email": clean,
        "player_name": canonical_player_name,
        "role": clean_role,
    }
    send_executive_request_emails(request_info)
    return True, request_info


def resolve_executive_request(request_id, action, resolver_username):
    try:
        clean_request_id = int(request_id)
    except (TypeError, ValueError):
        return False, "Request was not found."

    if action not in {"approve", "deny"}:
        return False, "Choose approve or deny."

    with closing(get_db()) as conn:
        ensure_executive_users_schema(conn)
        ensure_executive_requests_schema(conn)
        request_row = conn.execute(
            """
            SELECT *
            FROM executive_requests
            WHERE id = ?
              AND status = 'pending'
            """,
            (clean_request_id,)
        ).fetchone()

        if not request_row:
            return False, "Request was not found."

        if action == "approve":
            if conn.execute(
                "SELECT 1 FROM executive_users WHERE username = ?",
                (request_row["email"],)
            ).fetchone():
                return False, "An account already exists for that email."

            conn.execute(
                """
                INSERT INTO executive_users (
                    username, password_hash, player_name, executive_role
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    request_row["email"],
                    request_row["password_hash"],
                    request_row["player_name"],
                    clean_executive_role(request_row["executive_role"]),
                )
            )

        status = "approved" if action == "approve" else "denied"
        conn.execute(
            """
            UPDATE executive_requests
            SET status = ?,
                resolved_at = CURRENT_TIMESTAMP,
                resolved_by_username = ?
            WHERE id = ?
            """,
            (status, resolver_username or "", clean_request_id)
        )
        conn.commit()

    request_info = {
        "email": request_row["email"],
        "player_name": request_row["player_name"],
        "role": clean_executive_role(request_row["executive_role"]),
    }
    send_executive_resolution_email(request_info, status)
    return True, status


def executive_email_recipients():
    with closing(get_db()) as conn:
        ensure_executive_users_schema(conn)
        rows = conn.execute(
            """
            SELECT username
            FROM executive_users
            ORDER BY username COLLATE NOCASE
            """
        ).fetchall()

    return [
        row["username"]
        for row in rows
        if clean_email(row["username"])
    ]


def send_email(to_addresses, subject, body):
    recipients = [
        clean_email(address)
        for address in to_addresses
        if clean_email(address)
    ]

    if not recipients:
        return False

    smtp_host = os.environ.get("POOL_APP_SMTP_HOST", "").strip()
    smtp_port = int(os.environ.get("POOL_APP_SMTP_PORT", "587") or 587)
    smtp_user = os.environ.get("POOL_APP_SMTP_USER", "").strip()
    smtp_password = os.environ.get("POOL_APP_SMTP_PASSWORD", "")
    from_address = clean_email(
        os.environ.get("POOL_APP_EMAIL_FROM", "").strip()
        or smtp_user
        or "pool-app@example.local"
    )

    if not smtp_host:
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_address
    message["To"] = ", ".join(recipients)
    message.set_content(body)

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as smtp:
            if os.environ.get("POOL_APP_SMTP_TLS", "1") != "0":
                smtp.starttls()
            if smtp_user:
                smtp.login(smtp_user, smtp_password)
            smtp.send_message(message)
    except Exception:
        return False

    return True


def executive_request_email_body(request_info, prefix):
    return (
        f"{prefix}\n\n"
        f"Email: {request_info['email']}\n"
        f"Name: {request_info['player_name']}\n"
        f"Requested role: {request_info['role']}\n"
    )


def send_executive_request_emails(request_info):
    body = executive_request_email_body(
        request_info,
        "A new executive account request has been submitted."
    )
    send_email(
        executive_email_recipients(),
        "New executive account request",
        body,
    )
    send_email(
        [request_info["email"]],
        "Executive account request received",
        executive_request_email_body(
            request_info,
            "Your executive account request has been received and is waiting for approval."
        ),
    )


def send_executive_resolution_email(request_info, status):
    if status == "approved":
        prefix = "Your executive account request has been approved. You can now log in."
        subject = "Executive account approved"
    else:
        prefix = "Your executive account request has been denied."
        subject = "Executive account request denied"

    send_email(
        [request_info["email"]],
        subject,
        executive_request_email_body(request_info, prefix),
    )


def mark_executive_login(user_id):
    with closing(get_db()) as conn:
        conn.execute(
            """
            UPDATE executive_users
            SET last_login_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (user_id,)
        )
        conn.commit()


def login_executive(user):
    session.clear()
    session["executive_user_id"] = int(user["id"])
    session["executive_username"] = user["username"]
    session["executive_player_name"] = user["player_name"] or ""
    session["executive_role"] = clean_executive_role(user["executive_role"])
    mark_executive_login(user["id"])


def is_executive_logged_in():
    return bool(session.get("executive_user_id"))


def current_executive_user():
    return get_executive_user_by_id(session.get("executive_user_id"))


def executive_profiles():
    with closing(get_db()) as conn:
        ensure_executive_users_schema(conn)
        rows = conn.execute(
            """
            SELECT username, player_name, executive_role
            FROM executive_users
            WHERE player_name != ''
            ORDER BY player_name COLLATE NOCASE, username COLLATE NOCASE
            """
        ).fetchall()

    return [
        {
            "username": row["username"],
            "player_name": row["player_name"],
            "role": clean_executive_role(row["executive_role"]),
        }
        for row in rows
    ]


def executive_profiles_by_player():
    profiles = {}

    for profile in executive_profiles():
        profiles.setdefault(profile["player_name"].casefold(), []).append(profile)

    return profiles


def update_current_executive_profile(player_name, role):
    user_id = session.get("executive_user_id")

    if not user_id:
        return False, "Executive login required"

    canonical_player_name = canonical_known_player_name(player_name)

    if player_name and not canonical_player_name:
        return False, "Choose a known player from the list."

    clean_role = clean_executive_role(role)

    with closing(get_db()) as conn:
        ensure_executive_users_schema(conn)
        conn.execute(
            """
            UPDATE executive_users
            SET player_name = ?,
                executive_role = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (canonical_player_name, clean_role, int(user_id))
        )
        conn.commit()

    session["executive_role"] = clean_role
    session["executive_player_name"] = canonical_player_name
    return True, {
        "player_name": canonical_player_name,
        "role": clean_role,
    }


def executive_login_required(route):
    @wraps(route)
    def wrapped(*args, **kwargs):
        if is_executive_logged_in():
            return route(*args, **kwargs)

        if request.method != "GET":
            if wants_json_response() or request.is_json:
                return {"success": False, "error": "Executive login required"}, 401

        return redirect(url_for("executive_login", next=request.full_path))

    return wrapped


def clean_next_url(next_url):
    candidate = str(next_url or "").strip()

    if not candidate.startswith("/") or candidate.startswith("//"):
        return url_for("executive_games")

    return candidate


def quote_sqlite_identifier(identifier):
    return '"' + str(identifier).replace('"', '""') + '"'


def database_diagnostics(table_name=None):
    with closing(get_db()) as conn:
        table_names = [
            row["name"]
            for row in conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type IN ('table', 'view')
                  AND name NOT LIKE 'sqlite_%'
                ORDER BY name COLLATE NOCASE
                """
            ).fetchall()
        ]
        tables = []

        for name in table_names:
            row = conn.execute(
                f"SELECT COUNT(*) AS row_count FROM {quote_sqlite_identifier(name)}"
            ).fetchone()
            tables.append({"name": name, "row_count": int(row["row_count"] or 0)})

        selected_table = table_name if table_name in table_names else (table_names[0] if table_names else "")
        schema = []
        columns = []
        rows = []

        if selected_table:
            quoted = quote_sqlite_identifier(selected_table)
            schema = [dict(row) for row in conn.execute(f"PRAGMA table_info({quoted})").fetchall()]
            columns = [column["name"] for column in schema]
            rows = [
                {
                    column: "NULL" if row[column] is None else str(row[column])
                    for column in columns
                }
                for row in conn.execute(f"SELECT * FROM {quoted}").fetchall()
            ]

    return {
        "tables": tables,
        "selected_table": selected_table,
        "schema": schema,
        "columns": columns,
        "rows": rows,
    }


def clean_css_color(value, fallback):
    color = str(value or "").strip()

    if CSS_COLOR_RE.fullmatch(color):
        return color

    return fallback


def wants_json_response():
    return (
        request.form.get("_ajax") == "1"
        or request.args.get("_ajax") == "1"
        or request.headers.get("X-Requested-With") == "fetch"
    )


def ensure_bracket_settings_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bracket_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            highlight_color TEXT NOT NULL DEFAULT '#FF7900',
            remove_hover_color TEXT NOT NULL DEFAULT '#ff7c7c',
            table_badge_background TEXT NOT NULL DEFAULT '#FF7900',
            table_badge_text TEXT NOT NULL DEFAULT '#050505',
            version INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(bracket_settings)").fetchall()
    }

    if "version" not in columns:
        conn.execute("ALTER TABLE bracket_settings ADD COLUMN version INTEGER NOT NULL DEFAULT 0")

    conn.execute(
        """
        INSERT OR IGNORE INTO bracket_settings (
            id, highlight_color, remove_hover_color,
            table_badge_background, table_badge_text, version, updated_at
        ) VALUES (?, ?, ?, ?, ?, 0, CURRENT_TIMESTAMP)
        """,
        (
            BRACKET_SETTINGS_ID,
            DEFAULT_BRACKET_SETTINGS["highlight_color"],
            DEFAULT_BRACKET_SETTINGS["remove_hover_color"],
            DEFAULT_BRACKET_SETTINGS["table_badge_background"],
            DEFAULT_BRACKET_SETTINGS["table_badge_text"],
        )
    )


def ensure_registration_state_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS registration_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            new_players TEXT NOT NULL DEFAULT '',
            late_players TEXT NOT NULL DEFAULT '',
            buybacks TEXT NOT NULL DEFAULT '',
            version INTEGER NOT NULL DEFAULT 0,
            last_client_id TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        INSERT OR IGNORE INTO registration_state (
            id, new_players, late_players, buybacks,
            version, last_client_id, updated_at
        ) VALUES (?, '', '', '', 0, '', CURRENT_TIMESTAMP)
    """, (REGISTRATION_STATE_ID,))


def ensure_finance_state_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS finance_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            entry_fee REAL NOT NULL DEFAULT 0,
            winnings_total REAL NOT NULL DEFAULT 0,
            payments TEXT NOT NULL DEFAULT '{}',
            payout_mode TEXT NOT NULL DEFAULT 'preset',
            comp_size INTEGER NOT NULL DEFAULT 64,
            first_winnings REAL NOT NULL DEFAULT 60,
            second_winnings REAL NOT NULL DEFAULT 45,
            third_winnings REAL NOT NULL DEFAULT 30,
            winners TEXT NOT NULL DEFAULT '{}',
            comp_date TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)

    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(finance_state)").fetchall()
    }
    finance_columns = {
        "payout_mode": "TEXT NOT NULL DEFAULT 'preset'",
        "comp_size": "INTEGER NOT NULL DEFAULT 64",
        "first_winnings": "REAL NOT NULL DEFAULT 60",
        "second_winnings": "REAL NOT NULL DEFAULT 45",
        "third_winnings": "REAL NOT NULL DEFAULT 30",
        "winners": "TEXT NOT NULL DEFAULT '{}'",
        "comp_date": "TEXT NOT NULL DEFAULT ''",
    }

    for column, definition in finance_columns.items():
        if column not in columns:
            conn.execute(f"ALTER TABLE finance_state ADD COLUMN {column} {definition}")

    conn.execute("""
        INSERT OR IGNORE INTO finance_state (
            id, entry_fee, winnings_total, payments, payout_mode, comp_size,
            first_winnings, second_winnings, third_winnings, winners, comp_date, updated_at
        ) VALUES (?, 0, 0, '{}', 'preset', 64, 60, 45, 30, '{}', '', CURRENT_TIMESTAMP)
    """, (FINANCE_STATE_ID,))

    conn.execute("""
        CREATE TABLE IF NOT EXISTS comp_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            comp_date TEXT NOT NULL,
            semester_key TEXT NOT NULL,
            year_key TEXT NOT NULL,
            placement INTEGER NOT NULL,
            winner_name TEXT NOT NULL,
            base_winnings REAL NOT NULL DEFAULT 0,
            adjusted_winnings REAL NOT NULL DEFAULT 0,
            is_buyback INTEGER NOT NULL DEFAULT 0,
            is_halved INTEGER NOT NULL DEFAULT 0,
            half_reason TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(comp_date, placement)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS finance_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            comp_date TEXT NOT NULL UNIQUE,
            semester_key TEXT NOT NULL,
            year_key TEXT NOT NULL,
            total_players INTEGER NOT NULL DEFAULT 0,
            paid_count INTEGER NOT NULL DEFAULT 0,
            total_income REAL NOT NULL DEFAULT 0,
            winnings_total REAL NOT NULL DEFAULT 0,
            profit_loss REAL NOT NULL DEFAULT 0,
            payments TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)


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
            active_tables TEXT NOT NULL DEFAULT '{}',
            replacement_slots TEXT NOT NULL,
            champion TEXT NOT NULL DEFAULT '',
            round_robin_scores TEXT NOT NULL DEFAULT '{}',
            late_players INTEGER NOT NULL DEFAULT 0,
            buybacks INTEGER NOT NULL DEFAULT 0,
            state_version INTEGER NOT NULL DEFAULT 0,
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
            active_tables, replacement_slots, champion, round_robin_scores,
            late_players, buybacks, state_version, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET
            left_slots = excluded.left_slots,
            right_slots = excluded.right_slots,
            advancements = excluded.advancements,
            active_matches = excluded.active_matches,
            active_tables = excluded.active_tables,
            replacement_slots = excluded.replacement_slots,
            champion = excluded.champion,
            round_robin_scores = excluded.round_robin_scores,
            late_players = excluded.late_players,
            buybacks = excluded.buybacks,
            state_version = app_state.state_version + 1,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            APP_STATE_ID,
            json_dumps_compact(state.get("left", [])),
            json_dumps_compact(state.get("right", [])),
            json_dumps_compact(state.get("advancements", {})),
            json_dumps_compact(state.get("active_matches", [])),
            json_dumps_compact(state.get("active_tables", {})),
            json_dumps_compact(state.get("replacement_slots", [])),
            state.get("champion", ""),
            json_dumps_compact(state.get("round_robin_scores", {})),
            int(counts.get("late_players", 0)),
            int(counts.get("buybacks", 0)),
        )
    )


def ensure_app_state_schema(conn):
    columns = app_state_table_columns(conn)

    if not columns:
        create_app_state_table(conn)
        return

    base_typed_columns = {
        "id", "left_slots", "right_slots", "advancements", "active_matches",
        "replacement_slots", "late_players", "buybacks", "updated_at"
    }

    if base_typed_columns.issubset(columns):
        if "champion" not in columns:
            conn.execute("ALTER TABLE app_state ADD COLUMN champion TEXT NOT NULL DEFAULT ''")

        if "state_version" not in columns:
            conn.execute("ALTER TABLE app_state ADD COLUMN state_version INTEGER NOT NULL DEFAULT 0")

        if "round_robin_scores" not in columns:
            conn.execute("ALTER TABLE app_state ADD COLUMN round_robin_scores TEXT NOT NULL DEFAULT '{}'")

        if "active_tables" not in columns:
            conn.execute("ALTER TABLE app_state ADD COLUMN active_tables TEXT NOT NULL DEFAULT '{}'")

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


def has_full_player_name(name):
    parts = str(name or "").strip().split()

    if len(parts) < 2:
        return False

    last_name = re.sub(r"[^A-Za-z0-9]", "", parts[-1].rstrip("."))
    return len(last_name) > 1


def clean_elo_player_name(name):
    clean_name = clean_player_name(name)

    if not clean_name or RATING_TEAM_SEPARATOR_RE.search(clean_name):
        return ""

    if not has_full_player_name(clean_name):
        return ""

    return clean_name


def get_or_create_player(conn, name):
    clean_name = clean_elo_player_name(name)
    if not clean_name:
        return None

    if not is_known_player_alias(clean_name):
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


def unique_names(names):
    clean_names = []
    seen_names = set()

    for value in names:
        name = clean_known_player_name(value)
        key = name.casefold()

        if name and key not in seen_names:
            clean_names.append(name)
            seen_names.add(key)

    return clean_names


def clean_known_player_name(value):
    name = clean_player_name(value)

    if not name or TEAM_SEPARATOR_RE.search(name):
        return ""

    if not has_full_player_name(name):
        return ""

    return name


def known_player_aliases():
    aliases = set()

    for name in load_known_player_names():
        aliases.add(name.casefold())

        short_name = clean_player_name(bracket_name(name))
        if short_name:
            aliases.add(short_name.casefold())

    return aliases


def is_known_player_alias(name):
    clean_name = clean_elo_player_name(name)

    if not clean_name:
        return False

    return clean_name.casefold() in known_player_aliases()


def sort_names(names):
    return sorted(names, key=lambda name: name.casefold())


def load_known_player_names():
    known_names = []

    if os.path.exists(KNOWN_PLAYERS_FILE):
        try:
            with open(KNOWN_PLAYERS_FILE, "r", encoding="utf-8") as f:
                stored_known_players = json.load(f)

            if isinstance(stored_known_players, list):
                known_names.extend(stored_known_players)
        except json.JSONDecodeError:
            pass

    known_names.extend(load_members())

    return sort_names(unique_names(known_names))


def canonical_known_player_name(value):
    clean = clean_known_player_name(value)

    if not clean:
        return ""

    clean_key = clean.casefold()

    for name in load_known_player_names():
        if name.casefold() == clean_key:
            return name

    return ""


def save_known_player_names(names):
    clean_names = sort_names(unique_names(names))

    with open(KNOWN_PLAYERS_FILE, "w", encoding="utf-8") as f:
        json.dump(clean_names, f, indent=2)

    return clean_names


def add_known_players(names):
    existing = load_known_player_names()
    return save_known_player_names(existing + list(names))


def get_known_players():
    member_keys = {name.casefold() for name in load_members()}
    executive_map = executive_profiles_by_player()

    return [
        {
            "name": name,
            "is_member": name.casefold() in member_keys,
            "executives": executive_map.get(name.casefold(), []),
        }
        for name in load_known_player_names()
    ]


def load_known_non_member_names():
    member_keys = {name.casefold() for name in load_members()}

    return [
        name
        for name in load_known_player_names()
        if name.casefold() not in member_keys
    ]


def load_state():
    with closing(get_db()) as conn:
        row = conn.execute(
            """
            SELECT left_slots, right_slots, advancements, active_matches,
                   active_tables, replacement_slots, champion, late_players,
                   buybacks, round_robin_scores, state_version
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
            "active_tables": json_loads_or_default(row["active_tables"], {}),
            "replacement_slots": json_loads_or_default(row["replacement_slots"], []),
            "champion": row["champion"] or "",
            "round_robin_scores": json_loads_or_default(row["round_robin_scores"], {}),
            "_version": int(row["state_version"] or 0),
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


def load_bracket_settings():
    with closing(get_db()) as conn:
        ensure_bracket_settings_schema(conn)
        row = conn.execute(
            """
            SELECT highlight_color, remove_hover_color,
                   table_badge_background, table_badge_text, version
            FROM bracket_settings
            WHERE id = ?
            """,
            (BRACKET_SETTINGS_ID,)
        ).fetchone()

    settings = dict(DEFAULT_BRACKET_SETTINGS)

    if row:
        for key, fallback in DEFAULT_BRACKET_SETTINGS.items():
            settings[key] = clean_css_color(row[key], fallback)
        settings["_version"] = int(row["version"] or 0)
    else:
        settings["_version"] = 0

    return settings


def save_bracket_settings(form):
    values = {
        key: clean_css_color(form.get(key), fallback)
        for key, fallback in DEFAULT_BRACKET_SETTINGS.items()
    }

    with closing(get_db()) as conn:
        ensure_bracket_settings_schema(conn)
        conn.execute(
            """
            UPDATE bracket_settings
            SET highlight_color = ?,
                remove_hover_color = ?,
                table_badge_background = ?,
                table_badge_text = ?,
                version = version + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                values["highlight_color"],
                values["remove_hover_color"],
                values["table_badge_background"],
                values["table_badge_text"],
                BRACKET_SETTINGS_ID,
            )
        )
        conn.commit()

    return values


def clean_registration_client_id(client_id):
    return str(client_id or "")[:80]


def registration_state_from_row(row):
    state = {field: "" for field in REGISTRATION_FIELDS}

    if row:
        state.update({
            "new_players": row["new_players"] or "",
            "late_players": row["late_players"] or "",
            "buybacks": row["buybacks"] or "",
            "version": int(row["version"] or 0),
            "last_client_id": row["last_client_id"] or "",
            "updated_at": row["updated_at"] or "",
        })
    else:
        state.update({
            "version": 0,
            "last_client_id": "",
            "updated_at": "",
        })

    return state


def load_registration_state_with_conn(conn):
    ensure_registration_state_schema(conn)
    row = conn.execute(
        """
        SELECT new_players, late_players, buybacks, version,
               last_client_id, updated_at
        FROM registration_state
        WHERE id = ?
        """,
        (REGISTRATION_STATE_ID,)
    ).fetchone()

    return registration_state_from_row(row)


def load_registration_state():
    with closing(get_db()) as conn:
        return load_registration_state_with_conn(conn)


def clean_money_value(value):
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return 0.0

    return round(max(amount, 0.0), 2)


def today_key():
    return dt.date.today().isoformat()


def clean_date_key(value, fallback=None):
    fallback = fallback or most_recent_tuesday_key()

    try:
        return dt.date.fromisoformat(str(value or "")).isoformat()
    except ValueError:
        return fallback


def most_recent_tuesday_key(date_key=None):
    try:
        current_date = dt.date.fromisoformat(str(date_key or today_key()))
    except ValueError:
        current_date = dt.date.today()

    days_since_tuesday = (current_date.weekday() - 1) % 7
    return (current_date - dt.timedelta(days=days_since_tuesday)).isoformat()


def tuesday_week_options(selected_date_key=None, weeks_back=18, weeks_forward=6):
    anchor = dt.date.fromisoformat(most_recent_tuesday_key())
    selected_key = clean_date_key(selected_date_key)
    options = []

    for offset in range(-weeks_back, weeks_forward + 1):
        comp_date = anchor + dt.timedelta(days=offset * 7)
        context = competition_context_for_date(comp_date.isoformat())
        options.append({
            "date": context["date"],
            "label": f"{context['date_label']} ({context['week_label']})",
        })

    if selected_key not in {option["date"] for option in options}:
        context = competition_context_for_date(selected_key)
        options.append({
            "date": context["date"],
            "label": f"{context['date_label']} ({context['week_label']})",
        })

    return sorted(options, key=lambda option: option["date"], reverse=True)


def semester_key_for_date(date_key=None):
    try:
        comp_date = dt.date.fromisoformat(str(date_key or today_key()))
    except ValueError:
        comp_date = dt.date.today()

    semester = 1 if comp_date.month <= 6 else 2
    return f"{comp_date.year}-S{semester}"


def year_key_for_date(date_key=None):
    try:
        comp_date = dt.date.fromisoformat(str(date_key or today_key()))
    except ValueError:
        comp_date = dt.date.today()

    return str(comp_date.year)


def clean_comp_size(value, fallback=64):
    try:
        size = int(value)
    except (TypeError, ValueError):
        size = int(fallback or 64)

    if size in PRESET_WINNINGS:
        return size

    if size <= 32:
        return 32

    if size <= 48:
        return 48

    return 64


def default_comp_size_for_players(players):
    entry_count = len({player["slot_id"] for player in players})
    return clean_comp_size(entry_count)


def clean_payout_mode(value):
    return "custom" if str(value or "").strip() == "custom" else "preset"


def clean_winner_map(value):
    source = value if isinstance(value, dict) else json_loads_or_default(value, {})
    winners = {}

    for place in PAYOUT_PLACES:
        name = str(source.get(str(place), source.get(place, "")) or "").strip()
        winners[str(place)] = name

    return winners


def load_finance_state():
    with closing(get_db()) as conn:
        ensure_finance_state_schema(conn)
        row = conn.execute(
            """
            SELECT entry_fee, winnings_total, payments, payout_mode,
                   comp_size, first_winnings, second_winnings,
                   third_winnings, winners, comp_date
            FROM finance_state
            WHERE id = ?
            """,
            (FINANCE_STATE_ID,)
        ).fetchone()

    if not row:
        return {
            "entry_fee": 0.0,
            "winnings_total": 0.0,
            "payments": {},
            "payout_mode": "preset",
            "comp_size": 64,
            "prizes": {1: 60.0, 2: 45.0, 3: 30.0},
            "winners": clean_winner_map({}),
            "comp_date": most_recent_tuesday_key(),
        }

    return {
        "entry_fee": clean_money_value(row["entry_fee"]),
        "winnings_total": clean_money_value(row["winnings_total"]),
        "payments": json_loads_or_default(row["payments"], {}),
        "payout_mode": clean_payout_mode(row["payout_mode"]),
        "comp_size": clean_comp_size(row["comp_size"]),
        "prizes": {
            1: clean_money_value(row["first_winnings"]),
            2: clean_money_value(row["second_winnings"]),
            3: clean_money_value(row["third_winnings"]),
        },
        "winners": clean_winner_map(row["winners"]),
        "comp_date": clean_date_key(row["comp_date"]),
    }


def save_finance_state(entry_fee, payments, payout_mode, comp_size, prizes, winners, comp_date):
    clean_payments = {
        str(key): bool(value)
        for key, value in payments.items()
        if str(key).strip() and bool(value)
    }
    clean_prizes = {
        place: clean_money_value(prizes.get(place, 0))
        for place in PAYOUT_PLACES
    }
    clean_winners = clean_winner_map(winners)
    payout_mode = clean_payout_mode(payout_mode)
    comp_size = clean_comp_size(comp_size)
    comp_date = clean_date_key(comp_date)

    with closing(get_db()) as conn:
        ensure_finance_state_schema(conn)
        conn.execute(
            """
            UPDATE finance_state
            SET entry_fee = ?,
                winnings_total = ?,
                payments = ?,
                payout_mode = ?,
                comp_size = ?,
                first_winnings = ?,
                second_winnings = ?,
                third_winnings = ?,
                winners = ?,
                comp_date = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                clean_money_value(entry_fee),
                clean_money_value(sum(clean_prizes.values())),
                json_dumps_compact(clean_payments),
                payout_mode,
                comp_size,
                clean_prizes[1],
                clean_prizes[2],
                clean_prizes[3],
                json_dumps_compact(clean_winners),
                comp_date,
                FINANCE_STATE_ID,
            )
        )
        conn.commit()


def save_registration_state_with_conn(conn, updates, client_id=""):
    current = load_registration_state_with_conn(conn)
    values = {
        field: str(updates.get(field, current[field]) or "")
        for field in REGISTRATION_FIELDS
    }

    conn.execute(
        """
        UPDATE registration_state
        SET new_players = ?,
            late_players = ?,
            buybacks = ?,
            version = version + 1,
            last_client_id = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            values["new_players"],
            values["late_players"],
            values["buybacks"],
            clean_registration_client_id(client_id),
            REGISTRATION_STATE_ID,
        )
    )

    return load_registration_state_with_conn(conn)


def save_registration_state(updates, client_id=""):
    with closing(get_db()) as conn:
        state = save_registration_state_with_conn(conn, updates, client_id)
        conn.commit()
        return state


def touch_registration_state(client_id=""):
    save_registration_state({}, client_id)


def claim_registration_state(known_version, client_id, updates):
    with closing(get_db()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        current_state = load_registration_state_with_conn(conn)

        if is_stale_registration_state(known_version, client_id, current_state):
            conn.rollback()
            return False

        save_registration_state_with_conn(conn, updates, client_id)
        conn.commit()
        return True


def registration_form_updates(form):
    return {
        field: form.get(field, "")
        for field in REGISTRATION_FIELDS
        if field in form
    }


def is_stale_registration_state(known_version, client_id, current_state):
    try:
        known_version = int(known_version)
    except (TypeError, ValueError):
        return False

    return (
        known_version < int(current_state.get("version", 0))
        and current_state.get("last_client_id", "") != clean_registration_client_id(client_id)
    )


def shorten_name(full_name, buyback=False):
    parts = full_name.strip().split()

    if not parts:
        return ""

    if len(parts) == 1:
        short = parts[0]
    else:
        short = f"{parts[0]} {parts[-1][:2]}."

    return f"{short}*" if buyback else short


def split_team_players(line):
    players = []

    for name in TEAM_SEPARATOR_RE.split(str(line or "")):
        clean_name = name.strip()

        if clean_name:
            players.append(clean_name)

    return players


def first_name(name):
    parts = str(name or "").strip().split()
    return parts[0] if parts else ""


def comp_entry_players(entry):
    return [
        name.strip()
        for name in str(entry or "").strip().rstrip("*").split("/")
        if name.strip()
    ]


def bracket_name(entry, buyback=False):
    players = split_team_players(entry)

    if len(players) > 1:
        short = " / ".join(first_name(player) for player in players if first_name(player))
    else:
        short = shorten_name(entry)

    return f"{short}*" if buyback and short else short


def player_names_from_entries(entries):
    players = []

    for entry in entries:
        players.extend(split_team_players(entry))

    return players


def known_player_names_from_entries(entries):
    return [
        name
        for name in (clean_known_player_name(entry) for entry in entries)
        if name
    ]


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
        "active_tables": {},
        "replacement_slots": [],
        "champion": "",
        "round_robin_scores": {},
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
    state.setdefault("active_tables", {})
    if not isinstance(state["active_tables"], dict):
        state["active_tables"] = {}
    state.setdefault("replacement_slots", [])
    state.setdefault("champion", "")
    state.setdefault("round_robin_scores", {})
    if not isinstance(state["round_robin_scores"], dict):
        state["round_robin_scores"] = {}
    state.setdefault("counts", {})
    state["counts"].setdefault("late_players", 0)
    state["counts"].setdefault("buybacks", 0)

    return state


def prune_ineligible_elo_players():
    with closing(get_db()) as conn:
        rows = conn.execute("SELECT name FROM players").fetchall()
        names_to_remove = [
            row["name"]
            for row in rows
            if not is_known_player_alias(row["name"])
        ]

        if names_to_remove:
            conn.executemany(
                "DELETE FROM players WHERE name = ?",
                [(name,) for name in names_to_remove]
            )
            conn.commit()


init_db()
prune_ineligible_elo_players()


def get_register_counts():
    state = normalize_state(load_state() or empty_state())
    all_slots = state.get("left", []) + state.get("right", [])

    return {
        "total_players": sum(1 for name in all_slots if name.strip()),
        "late_players": state.get("counts", {}).get("late_players", 0),
        "buybacks": state.get("counts", {}).get("buybacks", 0),
    }


def member_aliases_by_status():
    members = load_members()
    aliases = {}
    first_name_counts = {}

    for member in members:
        first = first_name(member).casefold()
        if first:
            first_name_counts[first] = first_name_counts.get(first, 0) + 1

    for member in members:
        clean_member = clean_player_name(member)
        if not clean_member:
            continue

        aliases[clean_member.casefold()] = True

        short_name = clean_player_name(bracket_name(clean_member))
        if short_name:
            aliases[short_name.casefold()] = True

        first = first_name(clean_member)
        if first and first_name_counts.get(first.casefold(), 0) == 1:
            aliases[first.casefold()] = True

    return aliases


def active_comp_players(state=None):
    state = normalize_state(state or load_state() or empty_state())
    member_aliases = member_aliases_by_status()
    players = []
    seen_keys = set()

    for slot_index, entry in enumerate(state.get("left", []) + state.get("right", [])):
        if not str(entry or "").strip():
            continue

        slot_id = f"{'L' if slot_index < 32 else 'R'}-0-{slot_index if slot_index < 32 else slot_index - 32}"

        for player_index, player_name in enumerate(comp_entry_players(entry)):
            payment_key = f"{slot_id}:{player_index}:{player_name.casefold()}"

            if payment_key in seen_keys:
                continue

            seen_keys.add(payment_key)
            players.append({
                "key": payment_key,
                "slot_id": slot_id,
                "player_index": player_index,
                "entry": entry,
                "name": player_name,
                "is_member": player_name.casefold() in member_aliases,
            })

    return players


def is_doubles_comp(players):
    return any("/" in player["entry"] for player in players)


def payment_fee_for_player(player, doubles_comp=False):
    if doubles_comp:
        return 3.0

    if str(player.get("entry", "")).strip().endswith("*"):
        return 4.0

    if player.get("is_member"):
        return 3.0

    return 4.0


def winner_choice_name(entry):
    return str(entry.get("entry", "") or "").strip()


def winner_choice_key(name):
    return clean_player_name(name).casefold()


def payout_prizes_for_state(finance_state, players):
    comp_size = clean_comp_size(
        finance_state.get("comp_size"),
        default_comp_size_for_players(players)
    )
    mode = clean_payout_mode(finance_state.get("payout_mode"))

    if mode == "preset":
        prizes = dict(PRESET_WINNINGS[comp_size])
    else:
        prizes = {
            place: clean_money_value(finance_state.get("prizes", {}).get(place, 0))
            for place in PAYOUT_PLACES
        }

    return comp_size, mode, prizes


def semester_win_count(conn, winner_name, semester_key, current_comp_date=None):
    names = [
        clean_player_name(name).casefold()
        for name in comp_entry_players(winner_name)
    ]
    if not names:
        names = [clean_player_name(winner_name).casefold()]

    rows = conn.execute(
        """
        SELECT winner_name
        FROM comp_results
        WHERE placement = 1
          AND semester_key = ?
          AND (? IS NULL OR comp_date <> ?)
          AND winner_name <> ''
        """,
        (semester_key, current_comp_date, current_comp_date)
    ).fetchall()
    count = 0

    for row in rows:
        result_names = [
            clean_player_name(name).casefold()
            for name in comp_entry_players(row["winner_name"])
        ] or [clean_player_name(row["winner_name"]).casefold()]

        if any(name and name in result_names for name in names):
            count += 1

    return count


def payout_rows_for_winners(winners, prizes, entries=None, comp_date=None):
    comp_date = comp_date or today_key()
    semester_key = semester_key_for_date(comp_date)
    entry_lookup = {
        winner_choice_key(winner_choice_name(entry)): entry
        for entry in (entries or [])
    }
    rows = []

    with closing(get_db()) as conn:
        ensure_finance_state_schema(conn)

        for place in PAYOUT_PLACES:
            winner_name = str(winners.get(str(place), "") or "").strip()
            base_winnings = clean_money_value(prizes.get(place, 0))
            entry = entry_lookup.get(winner_choice_key(winner_name), {})
            is_buyback = (
                str(winner_name).strip().endswith("*")
                or str(entry.get("entry", "")).strip().endswith("*")
            )
            semester_wins = semester_win_count(
                conn,
                winner_name,
                semester_key,
                comp_date
            ) if winner_name else 0
            repeat_halved = place == 1 and winner_name and semester_wins + 1 >= 3
            half_reasons = []

            if is_buyback:
                half_reasons.append("buyback")

            if repeat_halved:
                half_reasons.append("third semester win")

            is_halved = bool(half_reasons)
            adjusted_winnings = round(
                base_winnings / 2 if is_halved else base_winnings,
                2
            )

            rows.append({
                "place": place,
                "label": {1: "1st", 2: "2nd", 3: "3rd"}[place],
                "winner_name": winner_name,
                "base_winnings": base_winnings,
                "adjusted_winnings": adjusted_winnings,
                "is_buyback": is_buyback,
                "is_halved": is_halved,
                "half_reason": ", ".join(half_reasons),
                "semester_wins_before": semester_wins,
            })

    return rows


def completed_round_robin_scoreboard(state=None):
    state = normalize_state(state or load_state() or empty_state())
    scoreboard = round_robin_scoreboard_for_state(state)

    if not scoreboard.get("active"):
        return None

    players = scoreboard.get("players", [])
    total_score = sum(int(player.get("rank_score", 0)) for player in players)

    if len(players) == 3 and total_score == 3:
        return scoreboard

    return None


def automatic_winners_from_round_robin(state=None):
    scoreboard = completed_round_robin_scoreboard(state)

    if not scoreboard:
        return {}, False

    players = scoreboard["players"]
    scores = [int(player.get("rank_score", 0)) for player in players]
    is_three_way_tie = len(set(scores)) == 1

    if is_three_way_tie:
        return {
            str(place): players[index]["name"]
            for index, place in enumerate(PAYOUT_PLACES)
        }, True

    ordered_players = sorted(
        players,
        key=lambda player: (
            -int(player.get("rank_score", 0)),
            player["name"].casefold()
        )
    )

    return {
        str(place): ordered_players[index]["name"]
        for index, place in enumerate(PAYOUT_PLACES)
    }, False


def payout_rows_for_round_robin_tie(winners, prizes, entries=None, comp_date=None):
    split_amount = round(
        sum(clean_money_value(prizes.get(place, 0)) for place in PAYOUT_PLACES) / 3,
        2
    )
    split_prizes = {place: split_amount for place in PAYOUT_PLACES}
    rows = payout_rows_for_winners(winners, split_prizes, entries, comp_date)

    for row in rows:
        row["label"] = "Tie"
        row["base_winnings"] = split_amount
        row["adjusted_winnings"] = split_amount
        row["is_halved"] = False
        row["half_reason"] = "3-way tie split"

    return rows


def save_comp_results_and_snapshot(summary):
    comp_date = clean_date_key(summary.get("comp_date"))
    semester_key = semester_key_for_date(comp_date)
    year_key = year_key_for_date(comp_date)

    with closing(get_db()) as conn:
        ensure_finance_state_schema(conn)

        for row in summary["payout_rows"]:
            if not row["winner_name"]:
                conn.execute(
                    "DELETE FROM comp_results WHERE comp_date = ? AND placement = ?",
                    (comp_date, row["place"])
                )
                continue

            conn.execute(
                """
                INSERT INTO comp_results (
                    comp_date, semester_key, year_key, placement, winner_name,
                    base_winnings, adjusted_winnings, is_buyback, is_halved,
                    half_reason, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(comp_date, placement) DO UPDATE SET
                    semester_key = excluded.semester_key,
                    year_key = excluded.year_key,
                    winner_name = excluded.winner_name,
                    base_winnings = excluded.base_winnings,
                    adjusted_winnings = excluded.adjusted_winnings,
                    is_buyback = excluded.is_buyback,
                    is_halved = excluded.is_halved,
                    half_reason = excluded.half_reason,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    comp_date,
                    semester_key,
                    year_key,
                    row["place"],
                    row["winner_name"],
                    row["base_winnings"],
                    row["adjusted_winnings"],
                    int(row["is_buyback"]),
                    int(row["is_halved"]),
                    row["half_reason"],
                )
            )

        conn.execute(
            """
            INSERT INTO finance_snapshots (
                comp_date, semester_key, year_key, total_players, paid_count,
                total_income, winnings_total, profit_loss, payments, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(comp_date) DO UPDATE SET
                semester_key = excluded.semester_key,
                year_key = excluded.year_key,
                total_players = excluded.total_players,
                paid_count = excluded.paid_count,
                total_income = excluded.total_income,
                winnings_total = excluded.winnings_total,
                profit_loss = excluded.profit_loss,
                payments = excluded.payments,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                comp_date,
                semester_key,
                year_key,
                summary["total_players"],
                summary["paid_count"],
                summary["total_income"],
                summary["winnings_total"],
                summary["profit_loss"],
                json_dumps_compact({
                    player["name"]: player["paid"]
                    for player in summary["players"]
                }),
            )
        )
        conn.commit()


def payment_report_history():
    with closing(get_db()) as conn:
        ensure_finance_state_schema(conn)
        snapshots = [
            dict(row)
            for row in conn.execute(
                """
                SELECT comp_date, semester_key, year_key, total_players,
                       paid_count, total_income, winnings_total, profit_loss
                FROM finance_snapshots
                ORDER BY comp_date DESC
                """
            ).fetchall()
        ]
        winners = [
            dict(row)
            for row in conn.execute(
                """
                SELECT comp_date, semester_key, placement, winner_name,
                       base_winnings, adjusted_winnings, is_buyback,
                       is_halved, half_reason
                FROM comp_results
                ORDER BY comp_date DESC, placement ASC
                """
            ).fetchall()
        ]

    def grouped_total(key):
        totals = {}
        for row in snapshots:
            group_key = row[key]
            bucket = totals.setdefault(group_key, {
                "income": 0.0,
                "winnings": 0.0,
                "profit_loss": 0.0,
            })
            bucket["income"] = round(bucket["income"] + row["total_income"], 2)
            bucket["winnings"] = round(bucket["winnings"] + row["winnings_total"], 2)
            bucket["profit_loss"] = round(bucket["profit_loss"] + row["profit_loss"], 2)

        return [
            {"key": group_key, **values}
            for group_key, values in sorted(totals.items(), reverse=True)
        ]

    return {
        "snapshots": snapshots,
        "winners": winners,
        "winner_grid": winner_history_grid(),
        "semester_totals": grouped_total("semester_key"),
        "year_totals": grouped_total("year_key"),
    }


def winner_history_grid(limit=30):
    with closing(get_db()) as conn:
        ensure_finance_state_schema(conn)
        rows = conn.execute(
            """
            SELECT comp_date, placement, winner_name, adjusted_winnings,
                   half_reason
            FROM comp_results
            WHERE winner_name <> ''
            ORDER BY comp_date DESC, placement ASC
            """
        ).fetchall()

    weeks = []
    week_lookup = {}

    for row in rows:
        comp_date = row["comp_date"]

        if comp_date not in week_lookup:
            week = {
                "comp_date": comp_date,
                "places": {
                    place: {
                        "winner_name": "",
                        "adjusted_winnings": 0.0,
                        "half_reason": "",
                    }
                    for place in PAYOUT_PLACES
                }
            }
            week_lookup[comp_date] = week
            weeks.append(week)

        week_lookup[comp_date]["places"][row["placement"]] = {
            "winner_name": row["winner_name"],
            "adjusted_winnings": clean_money_value(row["adjusted_winnings"]),
            "half_reason": row["half_reason"] or "",
        }

    return weeks[:limit]


def finance_entries_from_players(players):
    entries = []
    entry_by_slot = {}

    for player in players:
        slot_id = player["slot_id"]

        if slot_id not in entry_by_slot:
            entry = {
                "slot_id": slot_id,
                "entry": player["entry"],
                "players": [],
                "total_fee": 0.0,
            }
            entry_by_slot[slot_id] = entry
            entries.append(entry)

        entry_by_slot[slot_id]["players"].append(player)
        entry_by_slot[slot_id]["total_fee"] = round(
            entry_by_slot[slot_id]["total_fee"] + player["fee"],
            2
        )

    return entries


def finance_summary():
    finance_state = load_finance_state()
    state = normalize_state(load_state() or empty_state())
    players = active_comp_players()
    doubles_comp = is_doubles_comp(players)
    default_size = default_comp_size_for_players(players)
    comp_size, payout_mode, prizes = payout_prizes_for_state(finance_state, players)
    comp_date = clean_date_key(finance_state.get("comp_date"))
    automatic_winners, round_robin_three_way_tie = automatic_winners_from_round_robin(state)
    winners = automatic_winners or clean_winner_map(finance_state.get("winners"))
    active_keys = {player["key"] for player in players}
    payments = {
        key: paid
        for key, paid in finance_state.get("payments", {}).items()
        if key in active_keys
    }
    paid_count = sum(1 for paid in payments.values() if paid)

    for player in players:
        player["paid"] = bool(payments.get(player["key"]))
        player["fee"] = payment_fee_for_player(player, doubles_comp)

    total_income = round(
        sum(player["fee"] for player in players if player["paid"]),
        2
    )
    entries = finance_entries_from_players(players)
    if round_robin_three_way_tie:
        payout_rows = payout_rows_for_round_robin_tie(winners, prizes, entries, comp_date)
    else:
        payout_rows = payout_rows_for_winners(winners, prizes, entries, comp_date)

    winnings_total = clean_money_value(
        sum(row["adjusted_winnings"] for row in payout_rows)
    )

    return {
        "players": players,
        "entries": entries,
        "winner_choices": [
            winner_choice_name(entry)
            for entry in entries
            if winner_choice_name(entry)
        ],
        "doubles_comp": doubles_comp,
        "preset_winnings": PRESET_WINNINGS,
        "payout_mode": payout_mode,
        "comp_size": comp_size,
        "default_comp_size": default_size,
        "comp_date": comp_date,
        "comp_date_display": competition_context_for_date(comp_date)["display"],
        "prizes": prizes,
        "winners": winners,
        "automatic_winners": bool(automatic_winners),
        "round_robin_three_way_tie": round_robin_three_way_tie,
        "payout_rows": payout_rows,
        "winnings_total": winnings_total,
        "paid_count": paid_count,
        "unpaid_count": len(players) - paid_count,
        "total_players": len(players),
        "total_income": total_income,
        "profit_loss": round(total_income - winnings_total, 2),
        "winner_history": winner_history_grid(),
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

        if not winner or not loser:
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
                        DEFAULT_ELO, DEFAULT_ELO, DEFAULT_ELO, DEFAULT_ELO
                    )
                )
                match_result_id = cursor.lastrowid
            except sqlite3.IntegrityError:
                existing = conn.execute(
                    """
                    SELECT id
                    FROM match_results
                    WHERE match_id = ? AND winner_name = ? AND loser_name = ?
                    """,
                    (match_id, winner_name, loser_name)
                ).fetchone()

                return True, {
                    "already_recorded": True,
                    "elo_skipped": True,
                    "match_result_id": existing["id"] if existing else None,
                    "winner_name": winner_name,
                    "loser_name": loser_name,
                }

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
                    DEFAULT_ELO, DEFAULT_ELO, DEFAULT_ELO, DEFAULT_ELO
                )
            )
            conn.commit()

            return True, {
                "already_recorded": False,
                "elo_skipped": True,
                "match_result_id": match_result_id,
                "winner_name": winner_name,
                "loser_name": loser_name,
            }

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


def match_results_by_match_id(match_ids):
    clean_match_ids = [
        str(match_id or "").strip()
        for match_id in match_ids
        if str(match_id or "").strip()
    ]

    if not clean_match_ids:
        return {}

    placeholders = ",".join("?" for _ in clean_match_ids)

    with closing(get_db()) as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM match_results
            WHERE match_id IN ({placeholders})
            ORDER BY id DESC
            """,
            clean_match_ids
        ).fetchall()

    results = {}

    for row in rows:
        results.setdefault(row["match_id"], dict(row))

    return results


def undo_match_result_row(conn, row):
    conn.execute(
        """
        UPDATE players
        SET elo = ?,
            games_played = MAX(games_played - 1, 0),
            wins = MAX(wins - 1, 0),
            updated_at = CURRENT_TIMESTAMP
        WHERE name = ?
        """,
        (row["winner_elo_before"], row["winner_name"])
    )
    conn.execute(
        """
        UPDATE players
        SET elo = ?,
            games_played = MAX(games_played - 1, 0),
            losses = MAX(losses - 1, 0),
            updated_at = CURRENT_TIMESTAMP
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
        (row["id"],)
    )
    conn.execute("DELETE FROM match_results WHERE id = ?", (row["id"],))


def undo_match_results_for_match_ids(match_ids):
    clean_match_ids = [
        str(match_id or "").strip()
        for match_id in match_ids
        if str(match_id or "").strip()
    ]

    if not clean_match_ids:
        return 0

    placeholders = ",".join("?" for _ in clean_match_ids)

    with closing(get_db()) as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM match_results
            WHERE match_id IN ({placeholders})
            ORDER BY id DESC
            """,
            clean_match_ids
        ).fetchall()

        for row in rows:
            undo_match_result_row(conn, row)

        conn.commit()

    return len(rows)


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


def normalize_active_matches(state):
    state["active_matches"] = [
        str(match_id)
        for match_id in state.get("active_matches", [])
        if str(match_id).strip()
    ]


def normalize_active_tables(state):
    normalize_active_matches(state)
    active_match_ids = set(state["active_matches"])
    clean_tables = {}
    used_tables = set()

    for match_id, table_number in state.get("active_tables", {}).items():
        match_id = str(match_id or "").strip()

        try:
            table_number = int(table_number)
        except (TypeError, ValueError):
            continue

        if (
            match_id in active_match_ids
            and 1 <= table_number <= TABLE_COUNT
            and table_number not in used_tables
        ):
            clean_tables[match_id] = table_number
            used_tables.add(table_number)

    state["active_tables"] = clean_tables

    for match_id in state["active_matches"]:
        if match_id in state["active_tables"]:
            continue

        for table_number in range(1, TABLE_COUNT + 1):
            if table_number not in used_tables:
                state["active_tables"][match_id] = table_number
                used_tables.add(table_number)
                break


def next_available_table(state):
    normalize_active_tables(state)
    used_tables = set(state["active_tables"].values())

    for table_number in range(1, TABLE_COUNT + 1):
        if table_number not in used_tables:
            return table_number

    return None


def available_tables(state):
    normalize_active_tables(state)
    used_tables = set(state["active_tables"].values())
    return [
        table_number
        for table_number in range(1, TABLE_COUNT + 1)
        if table_number not in used_tables
    ]


def match_id_for_side_round(side, round_index, top_slot_index):
    return f"{side}-{round_index}-{top_slot_index}"


def parse_side_match_id(match_id):
    parts = str(match_id or "").split("-")

    if len(parts) != 3:
        return None

    side, round_text, top_slot_text = parts

    if side not in {"L", "R"}:
        return None

    try:
        round_index = int(round_text)
        top_slot_index = int(top_slot_text)
    except ValueError:
        return None

    if not 0 <= round_index <= 4:
        return None

    if top_slot_index % 2 != 0 or not 0 <= top_slot_index < SIDE_ROUNDS[round_index]:
        return None

    return side, round_index, top_slot_index


def is_final_match_id(match_id):
    return str(match_id or "") == "F-0-0"


def match_slot_ids(match_id):
    if is_final_match_id(match_id):
        return ["L-5-0", "R-5-0"]

    parsed = parse_side_match_id(match_id)
    if not parsed:
        return []

    side, round_index, top_slot_index = parsed
    return [
        f"{side}-{round_index}-{top_slot_index}",
        f"{side}-{round_index}-{top_slot_index + 1}",
    ]


def match_target_slot_id(match_id):
    if is_final_match_id(match_id):
        return "champion"

    parsed = parse_side_match_id(match_id)
    if not parsed:
        return None

    side, round_index, top_slot_index = parsed
    return f"{side}-{round_index + 1}-{top_slot_index // 2}"


def match_id_for_slot(slot_id):
    parts = str(slot_id or "").split("-")

    if len(parts) != 3:
        return None

    side, round_text, slot_text = parts

    if side not in {"L", "R"}:
        return None

    try:
        round_index = int(round_text)
        slot_index = int(slot_text)
    except ValueError:
        return None

    if round_index == 5:
        return "F-0-0"

    if not 0 <= round_index <= 4:
        return None

    top_slot_index = slot_index if slot_index % 2 == 0 else slot_index - 1
    return match_id_for_side_round(side, round_index, top_slot_index)


def slot_name(state, slot_id):
    parts = str(slot_id or "").split("-")

    if len(parts) != 3:
        return ""

    side, round_text, slot_text = parts

    try:
        round_index = int(round_text)
        slot_index = int(slot_text)
    except ValueError:
        return ""

    state = normalize_state(state)

    if round_index == 0:
        first_round = state["left"] if side == "L" else state["right"]
        fallback = first_round[slot_index] if 0 <= slot_index < len(first_round) else ""
        return state.get("advancements", {}).get(slot_id, fallback) or ""

    return state.get("advancements", {}).get(slot_id, "") or ""


def all_match_ids():
    match_ids = []

    for round_index in range(5):
        for side in ("L", "R"):
            for top_slot_index in range(0, SIDE_ROUNDS[round_index], 2):
                match_ids.append(match_id_for_side_round(side, round_index, top_slot_index))

    match_ids.append("F-0-0")
    return match_ids


def slot_ids_for_round(round_index):
    if not 0 <= round_index <= 5:
        return []

    return [
        f"{side}-{round_index}-{slot_index}"
        for side in ("L", "R")
        for slot_index in range(SIDE_ROUNDS[round_index])
    ]


def round_participants(state, round_index):
    participants = []

    for slot_id in slot_ids_for_round(round_index):
        name = slot_name(state, slot_id)

        if str(name or "").strip():
            participants.append({
                "slot_id": slot_id,
                "name": name,
            })

    return participants


def downstream_match_ids(match_id):
    match_ids = []
    current_match_id = match_id

    while current_match_id:
        match_ids.append(current_match_id)
        target_slot_id = match_target_slot_id(current_match_id)

        if target_slot_id in {None, "champion"}:
            break

        current_match_id = match_id_for_slot(target_slot_id)

        if current_match_id in match_ids:
            break

    return match_ids


def is_round_robin_match_id(match_id):
    return str(match_id or "").startswith("RR|")


def round_robin_match_id(round_index, first_slot_id, second_slot_id):
    return f"RR|{round_index}|{first_slot_id}|{second_slot_id}"


def parse_round_robin_match_id(match_id):
    parts = str(match_id or "").split("|")

    if len(parts) != 4 or parts[0] != "RR":
        return None

    try:
        round_index = int(parts[1])
    except ValueError:
        return None

    if not 0 <= round_index <= 5:
        return None

    first_slot_id = parts[2]
    second_slot_id = parts[3]

    if first_slot_id not in slot_ids_for_round(round_index):
        return None

    if second_slot_id not in slot_ids_for_round(round_index):
        return None

    if first_slot_id == second_slot_id:
        return None

    return round_index, first_slot_id, second_slot_id


def game_from_match_id(state, match_id):
    state = normalize_state(state)
    slot_ids = match_slot_ids(match_id)

    if len(slot_ids) != 2:
        return None

    player_names = [slot_name(state, slot_id) for slot_id in slot_ids]
    target_slot_id = match_target_slot_id(match_id)
    winner_name = state.get("champion", "") if target_slot_id == "champion" else slot_name(state, target_slot_id)
    parsed = parse_side_match_id(match_id)
    round_key = "final" if is_final_match_id(match_id) else parsed[1]

    return {
        "id": match_id,
        "round_key": round_key,
        "round_label": GAME_ROUND_LABELS[round_key],
        "slot_ids": slot_ids,
        "players": [
            {"slot_id": slot_ids[0], "name": player_names[0]},
            {"slot_id": slot_ids[1], "name": player_names[1]},
        ],
        "winner_name": winner_name,
        "played": bool(str(winner_name or "").strip()),
        "active": match_id in state.get("active_matches", []),
        "table_number": state.get("active_tables", {}).get(match_id),
        "ready": all(str(name or "").strip() for name in player_names),
        "has_players": any(str(name or "").strip() for name in player_names),
    }


def normal_games_for_state(state):
    return [
        game
        for game in (game_from_match_id(state, match_id) for match_id in all_match_ids())
        if game and game["has_players"]
    ]


def prior_rounds_are_complete(state, round_index):
    for game in normal_games_for_state(state):
        round_key = game["round_key"]

        if isinstance(round_key, int) and round_key < round_index and not game["played"]:
            return False

    return True


def detected_round_robin_final(state):
    state = normalize_state(state)

    for round_index in range(1, 5):
        participants = round_participants(state, round_index)

        if len(participants) == 3 and prior_rounds_are_complete(state, round_index):
            return {
                "round_index": round_index,
                "participants": participants,
            }

    return None


def round_robin_label(round_index):
    return f"Round Robin Final ({GAME_ROUND_NUMBER_LABELS[round_index]})"


def round_robin_group_key(round_index):
    return f"rr-{round_index}"


def round_robin_pairings(participants):
    return [
        (participants[0], participants[1]),
        (participants[0], participants[2]),
        (participants[1], participants[2]),
    ]


def round_robin_match_ids_for_detection(detection):
    return [
        round_robin_match_id(
            detection["round_index"],
            first["slot_id"],
            second["slot_id"],
        )
        for first, second in round_robin_pairings(detection["participants"])
    ]


def round_robin_score_key(detection):
    participants = [
        [participant["slot_id"], participant["name"]]
        for participant in detection["participants"]
    ]

    return json_dumps_compact({
        "round": detection["round_index"],
        "participants": participants,
    })


def clean_round_robin_score(value):
    try:
        score = int(value)
    except (TypeError, ValueError):
        score = 0

    return max(0, min(score, 2))


def round_robin_scores_from_results(detection, results):
    scores = {
        participant["slot_id"]: 0
        for participant in detection["participants"]
    }
    name_by_slot = {
        participant["slot_id"]: participant["name"]
        for participant in detection["participants"]
    }

    for result in results.values():
        parsed = parse_round_robin_match_id(result["match_id"])

        if not parsed:
            continue

        _, first_slot_id, second_slot_id = parsed

        for slot_id in (first_slot_id, second_slot_id):
            if name_by_slot.get(slot_id) == result["winner_name"]:
                scores[slot_id] += 1

    return {
        slot_id: clean_round_robin_score(score)
        for slot_id, score in scores.items()
    }


def round_robin_scores_for_detection(state, detection, results=None):
    state = normalize_state(state)
    key = round_robin_score_key(detection)
    stored_scores = state.get("round_robin_scores", {}).get(key)
    slot_ids = [
        participant["slot_id"]
        for participant in detection["participants"]
    ]

    if isinstance(stored_scores, dict):
        return {
            slot_id: clean_round_robin_score(stored_scores.get(slot_id, 0))
            for slot_id in slot_ids
        }

    if results is None:
        results = match_results_by_match_id(
            round_robin_match_ids_for_detection(detection)
        )

    return round_robin_scores_from_results(detection, results)


def set_round_robin_scores_for_detection(state, detection, scores):
    state.setdefault("round_robin_scores", {})
    key = round_robin_score_key(detection)
    state["round_robin_scores"][key] = {
        participant["slot_id"]: clean_round_robin_score(
            scores.get(participant["slot_id"], 0)
        )
        for participant in detection["participants"]
    }


def adjust_round_robin_score(state, detection, slot_id, delta):
    slot_id = str(slot_id or "")
    participant_slot_ids = {
        participant["slot_id"]
        for participant in detection["participants"]
    }

    if slot_id not in participant_slot_ids:
        return False

    scores = round_robin_scores_for_detection(state, detection)
    scores[slot_id] = clean_round_robin_score(scores.get(slot_id, 0) + delta)
    set_round_robin_scores_for_detection(state, detection, scores)
    return True


def clear_round_robin_scores_for_detection(state, detection):
    state.setdefault("round_robin_scores", {})
    state["round_robin_scores"].pop(round_robin_score_key(detection), None)


def round_robin_game_from_match_id(state, match_id, result=None):
    parsed = parse_round_robin_match_id(match_id)

    if not parsed:
        return None

    round_index, first_slot_id, second_slot_id = parsed
    first_name = slot_name(state, first_slot_id)
    second_name = slot_name(state, second_slot_id)

    if not first_name or not second_name:
        return None

    result = result or match_results_by_match_id([match_id]).get(match_id)
    winner_name = result["winner_name"] if result else ""

    return {
        "id": match_id,
        "display_id": "Round Robin",
        "round_key": round_robin_group_key(round_index),
        "round_label": round_robin_label(round_index),
        "is_round_robin": True,
        "slot_ids": [first_slot_id, second_slot_id],
        "players": [
            {"slot_id": first_slot_id, "name": first_name},
            {"slot_id": second_slot_id, "name": second_name},
        ],
        "winner_name": winner_name,
        "played": bool(winner_name),
        "active": match_id in state.get("active_matches", []),
        "table_number": state.get("active_tables", {}).get(match_id),
        "ready": True,
        "has_players": True,
    }


def round_robin_games_for_detection(state, detection):
    pairings = round_robin_pairings(detection["participants"])
    match_ids = round_robin_match_ids_for_detection(detection)
    results = match_results_by_match_id(match_ids)
    games = []

    for index, (first, second) in enumerate(pairings, start=1):
        match_id = round_robin_match_id(
            detection["round_index"],
            first["slot_id"],
            second["slot_id"],
        )
        game = round_robin_game_from_match_id(
            state,
            match_id,
            results.get(match_id)
        )

        if game:
            game["display_id"] = f"Round Robin Game {index}"
            games.append(game)

    return games


def round_robin_scoreboard_for_state(state, detection=None):
    detection = detection or detected_round_robin_final(state)

    if not detection:
        return {
            "active": False,
            "title": "Round Robin Final",
            "players": [],
            "played": 0,
            "total": 0,
        }

    match_ids = round_robin_match_ids_for_detection(detection)
    results = match_results_by_match_id(match_ids)
    score_values = round_robin_scores_for_detection(state, detection, results)
    players = {
        participant["slot_id"]: {
            "slot_id": participant["slot_id"],
            "name": participant["name"],
            "wins": 0,
            "losses": 0,
            "played": 0,
            "place": "",
            "rank_score": score_values.get(participant["slot_id"], 0),
        }
        for participant in detection["participants"]
    }

    for result in results.values():
        winner_name = result["winner_name"]
        loser_name = result["loser_name"]
        parsed = parse_round_robin_match_id(result["match_id"])
        match_slot_ids = parsed[1:] if parsed else ()
        winner_slot_id = next(
            (
                slot_id for slot_id in match_slot_ids
                if players.get(slot_id, {}).get("name") == winner_name
            ),
            None
        )
        loser_slot_id = next(
            (
                slot_id for slot_id in match_slot_ids
                if players.get(slot_id, {}).get("name") == loser_name
            ),
            None
        )

        if winner_slot_id in players:
            players[winner_slot_id]["wins"] += 1
            players[winner_slot_id]["played"] += 1

        if loser_slot_id in players:
            players[loser_slot_id]["losses"] += 1
            players[loser_slot_id]["played"] += 1

    score_rows = []

    score_rows.extend(players.values())

    score_rows.sort(
        key=lambda player: (
            -player["rank_score"],
            player["name"].casefold()
        )
    )

    place_labels = {
        1: "1st",
        2: "2nd",
        3: "3rd",
    }
    has_scores = any(player["rank_score"] for player in score_rows)
    last_score = None
    current_place = 0

    for index, player in enumerate(score_rows):
        if has_scores and player["rank_score"] != last_score:
            current_place = index + 1
            last_score = player["rank_score"]

        player["place"] = place_labels.get(current_place, "") if has_scores else ""
        player["score"] = f"{player['rank_score']}W"

    total_score = sum(player["rank_score"] for player in score_rows)
    three_way_tie = (
        len(score_rows) == 3
        and total_score == 3
        and len({player["rank_score"] for player in score_rows}) == 1
    )

    return {
        "active": True,
        "title": round_robin_label(detection["round_index"]),
        "players": score_rows,
        "played": len(results),
        "total": len(match_ids),
        "score_total": total_score,
        "is_complete": total_score == 3,
        "three_way_tie": three_way_tie,
    }


def all_game_groups_for_state(state):
    normalize_active_tables(state)
    groups = []
    round_robin = detected_round_robin_final(state)
    round_robin_index = round_robin["round_index"] if round_robin else None

    for round_key in ROUND_ORDER:
        if round_key == round_robin_index:
            groups.append({
                "key": round_robin_group_key(round_robin_index),
                "label": round_robin_label(round_robin_index),
                "short_label": ROUND_ROBIN_FINAL_SHORT_LABEL,
                "scoreboard": round_robin_scoreboard_for_state(state, round_robin),
                "games": round_robin_games_for_detection(state, round_robin),
            })
            continue

        games = [
            game
            for game in normal_games_for_state(state)
            if game["round_key"] == round_key
        ]

        if games:
            groups.append({
                "key": str(round_key),
                "label": GAME_ROUND_LABELS[round_key],
                "short_label": GAME_ROUND_SHORT_LABELS[round_key],
                "games": games,
            })

    return groups


def apply_game_filter(games, game_filter):
    if game_filter == "played":
        return [game for game in games if game["played"]]

    if game_filter == "unplayed":
        return [game for game in games if not game["played"]]

    return games


def grouped_games_for_state(state, game_filter="all"):
    groups = []

    for group in all_game_groups_for_state(state):
        games = apply_game_filter(group["games"], game_filter)

        if games:
            groups.append({
                **group,
                "games": games,
            })

    return groups


def games_for_state(state):
    return [
        game
        for group in all_game_groups_for_state(state)
        for game in group["games"]
    ]


def game_for_match_id(state, match_id):
    if is_round_robin_match_id(match_id):
        return round_robin_game_from_match_id(state, match_id)

    return game_from_match_id(state, match_id)


def round_jump_links_for_groups(groups):
    links = []

    for group in groups:
        games = [
            game
            for game in group["games"]
            if not game["played"]
        ]

        if not games:
            continue

        links.append({
            "key": group["key"],
            "label": group["label"],
            "short_label": group["short_label"],
            "count": len(games),
        })

    return links


def update_round_robin_score_in_state(state, slot_id, delta):
    state = normalize_state(state)
    detection = detected_round_robin_final(state)

    if not detection:
        return False, "Round robin final was not found"

    try:
        delta = int(delta)
    except (TypeError, ValueError):
        return False, "Score change was not valid"

    if not adjust_round_robin_score(state, detection, slot_id, delta):
        return False, "Finalist was not found"

    return True, state


def reset_round_robin_final_in_state(state):
    state = normalize_state(state)
    detection = detected_round_robin_final(state)

    if not detection:
        return False, "Round robin final was not found"

    match_ids = round_robin_match_ids_for_detection(detection)
    undo_match_results_for_match_ids(match_ids)
    clear_round_robin_scores_for_detection(state, detection)
    normalize_active_tables(state)
    state["active_matches"] = [
        match_id
        for match_id in state["active_matches"]
        if match_id not in match_ids
    ]

    for match_id in match_ids:
        state["active_tables"].pop(match_id, None)

    return True, state


def start_game_in_state(state, match_id, table_number=None):
    state = normalize_state(state)
    game = game_for_match_id(state, match_id)

    if not game:
        return False, "Game was not found"

    if game["played"]:
        return False, "That game has already been played"

    if not game["ready"]:
        return False, "Both players must be known before a game can start"

    normalize_active_tables(state)

    if match_id not in state["active_matches"]:
        open_tables = available_tables(state)

        if len(open_tables) > 1:
            try:
                table_number = int(table_number)
            except (TypeError, ValueError):
                return False, "Choose a table before starting this game"

            if table_number not in open_tables:
                return False, "That table is already in use"
        else:
            table_number = open_tables[0] if open_tables else None

        if table_number is None:
            return False, f"All {TABLE_COUNT} tables are already in use"

        state["active_matches"].append(match_id)
        state["active_tables"][match_id] = table_number
    elif match_id not in state["active_tables"]:
        open_tables = available_tables(state)
        table_number = open_tables[0] if open_tables else None

        if table_number is None:
            return False, f"All {TABLE_COUNT} tables are already in use"

        state["active_tables"][match_id] = table_number

    return True, state


def win_game_in_state(state, match_id, winner_slot_id):
    state = normalize_state(state)
    game = game_for_match_id(state, match_id)

    if not game:
        return False, "Game was not found"

    if game["played"]:
        return False, "Reset this game before recording a different winner"

    if not game["ready"]:
        return False, "Both players must be known before a winner can be recorded"

    player_by_slot = {
        player["slot_id"]: player["name"]
        for player in game["players"]
    }

    winner_name = player_by_slot.get(winner_slot_id, "")

    if not winner_name:
        return False, "Winner was not found"

    loser_name = next(
        (name for slot_id, name in player_by_slot.items() if slot_id != winner_slot_id),
        ""
    )

    success, result = record_match_result(match_id, winner_name, loser_name)

    if not success:
        return False, result

    if is_round_robin_match_id(match_id):
        detection = detected_round_robin_final(state)

        if detection:
            adjust_round_robin_score(state, detection, winner_slot_id, 1)

    target_slot_id = match_target_slot_id(match_id)

    if target_slot_id == "champion":
        state["champion"] = winner_name
    elif target_slot_id:
        state.setdefault("advancements", {})
        state["advancements"][target_slot_id] = winner_name

    normalize_active_tables(state)
    state["active_matches"] = [
        active_match_id
        for active_match_id in state["active_matches"]
        if active_match_id != match_id
    ]
    state["active_tables"].pop(match_id, None)

    return True, state


def reset_game_in_state(state, match_id):
    state = normalize_state(state)
    game = game_for_match_id(state, match_id)

    if not game:
        normalize_active_tables(state)
        if match_id in state["active_matches"] or match_id in state["active_tables"]:
            state["active_matches"] = [
                active_match_id
                for active_match_id in state["active_matches"]
                if active_match_id != match_id
            ]
            state["active_tables"].pop(match_id, None)
            return True, state

        return False, "Game was not found"

    if is_round_robin_match_id(match_id):
        detection = detected_round_robin_final(state)
        winner_slot_id = next(
            (
                player["slot_id"]
                for player in game["players"]
                if player["name"] and player["name"] == game.get("winner_name")
            ),
            ""
        )

        if detection and winner_slot_id:
            adjust_round_robin_score(state, detection, winner_slot_id, -1)

        undo_match_results_for_match_ids([match_id])
        normalize_active_tables(state)
        state["active_matches"] = [
            active_match_id
            for active_match_id in state["active_matches"]
            if active_match_id != match_id
        ]
        state["active_tables"].pop(match_id, None)
        return True, state

    impacted_match_ids = downstream_match_ids(match_id)
    round_robin = detected_round_robin_final(state)
    parsed_match = parse_side_match_id(match_id)

    if round_robin and parsed_match:
        side, round_index, top_slot_index = parsed_match

        if round_index <= round_robin["round_index"]:
            impacted_match_ids.extend(round_robin_match_ids_for_detection(round_robin))
            clear_round_robin_scores_for_detection(state, round_robin)

    undo_match_results_for_match_ids(impacted_match_ids)

    state.setdefault("advancements", {})
    normalize_active_tables(state)
    state["active_matches"] = [
        active_match_id
        for active_match_id in state["active_matches"]
        if active_match_id not in impacted_match_ids
    ]
    for impacted_match_id in impacted_match_ids:
        state["active_tables"].pop(impacted_match_id, None)

    for impacted_match_id in impacted_match_ids:
        target_slot_id = match_target_slot_id(impacted_match_id)

        if target_slot_id == "champion":
            state["champion"] = ""
        elif target_slot_id:
            state["advancements"][target_slot_id] = ""

    return True, state


def delete_first_round_game_in_state(state, match_id):
    state = normalize_state(state)
    parsed = parse_side_match_id(match_id)

    if not parsed:
        return False, "Game was not found"

    side, round_index, top_slot_index = parsed

    if round_index != 0:
        return False, "Only first-round games can be deleted"

    reset_success, reset_result = reset_game_in_state(state, match_id)

    if not reset_success:
        return False, reset_result

    state = reset_result
    removed_any = False

    for slot_id in match_slot_ids(match_id):
        if slot_name(state, slot_id):
            success, result = remove_first_round_player_from_state(state, slot_id)

            if not success:
                return False, result

            removed_any = True

    if not removed_any:
        return False, "That first-round game is already empty"

    return True, state


def delete_first_round_player_in_state(state, match_id, slot_id):
    state = normalize_state(state)
    parsed = parse_side_match_id(match_id)

    if not parsed:
        return False, "Game was not found"

    side, round_index, top_slot_index = parsed

    if round_index != 0:
        return False, "Only first-round players can be deleted"

    if slot_id not in match_slot_ids(match_id):
        return False, "That player does not belong to this game"

    reset_success, reset_result = reset_game_in_state(state, match_id)

    if not reset_success:
        return False, reset_result

    remove_success, remove_result = remove_first_round_player_from_state(
        reset_result,
        slot_id
    )

    if not remove_success:
        return False, remove_result

    return True, reset_result


@app.route("/")
def bracket():
    state = load_state()

    if state is None:
        state = empty_state()
        save_state(state)
    else:
        state = normalize_state(state)

    normalize_active_tables(state)
    state["round_robin_scoreboard"] = round_robin_scoreboard_for_state(state)

    return render_template(
        "bracket.html",
        bracket_data=state,
        bracket_settings=load_bracket_settings()
    )


@app.route("/executive_login", methods=["GET", "POST"])
def executive_login():
    has_executive_users = executive_user_count() > 0
    next_url = clean_next_url(request.args.get("next") or request.form.get("next"))
    error = ""
    message = request.args.get("message", "")

    if request.method == "POST":
        username = clean_username(request.form.get("username"))
        password = request.form.get("password", "")

        if has_executive_users:
            user = get_executive_user(username)

            if user and check_password_hash(user["password_hash"], password):
                login_executive(user)
                return redirect(next_url)

            error = "Username or password is incorrect."
        else:
            success, result = create_executive_user(
                username,
                password,
                request.form.get("player_name", ""),
                request.form.get("executive_role", EXECUTIVE_ROLES[0])
            )

            if success:
                user = get_executive_user(result)
                login_executive(user)
                return redirect(next_url)

            error = result

    return render_template(
        "executive_login.html",
        setup_required=not has_executive_users,
        error=error,
        message=message,
        next_url=next_url,
        known_player_names=load_known_player_names(),
        executive_roles=EXECUTIVE_ROLES,
    )


@app.route("/executive_logout", methods=["POST"])
def executive_logout():
    session.clear()
    return redirect(url_for("executive_login"))


@app.route("/executive_signup", methods=["GET", "POST"])
def executive_signup():
    if executive_user_count() == 0:
        return redirect(url_for("executive_login", next=request.args.get("next") or url_for("executive_games")))

    error = ""

    if request.method == "POST":
        success, result = create_executive_request(
            request.form.get("email", ""),
            request.form.get("password", ""),
            request.form.get("player_name", ""),
            request.form.get("executive_role", EXECUTIVE_ROLES[0])
        )

        if success:
            return redirect(url_for(
                "executive_login",
                message="Your request has been sent to the current executives."
            ))

        error = result

    return render_template(
        "executive_signup.html",
        error=error,
        known_player_names=load_known_player_names(),
        executive_roles=EXECUTIVE_ROLES,
    )


@app.route("/executive_requests", methods=["GET", "POST"])
@executive_login_required
def executive_requests_route():
    message = ""
    error = ""

    if request.method == "POST":
        success, result = resolve_executive_request(
            request.form.get("request_id"),
            request.form.get("action"),
            session.get("executive_username", "")
        )

        if success:
            message = f"Request {result}."
        else:
            error = result

    return render_template(
        "executive_requests.html",
        requests=pending_executive_requests(),
        message=message,
        error=error,
    )


@app.route("/database")
@app.route("/database/<table_name>")
@executive_login_required
def database_browser(table_name=None):
    return render_template(
        "database.html",
        **database_diagnostics(table_name),
    )


@app.route("/bracket_settings", methods=["GET", "POST"])
@executive_login_required
def bracket_settings():
    if request.method == "POST":
        if request.form.get("action") == "reset":
            settings = save_bracket_settings(DEFAULT_BRACKET_SETTINGS)
        else:
            settings = save_bracket_settings(request.form)

        if wants_json_response():
            return {**settings, "_version": load_bracket_settings().get("_version", 0)}

        return redirect(url_for("bracket_settings"))

    if wants_json_response():
        return load_bracket_settings()

    return render_template(
        "bracket_settings.html",
        settings=load_bracket_settings(),
        defaults=DEFAULT_BRACKET_SETTINGS,
        presets=BRACKET_COLOR_PRESETS,
        executive_profiles=executive_profiles(),
    )


@app.route("/bracket_state")
def bracket_state_route():
    state = normalize_state(load_state() or empty_state())
    normalize_active_tables(state)
    state["round_robin_scoreboard"] = round_robin_scoreboard_for_state(state)
    state["bracket_settings_version"] = load_bracket_settings().get("_version", 0)
    return state


@app.route("/executive_games")
@executive_login_required
def executive_games():
    state = normalize_state(load_state() or empty_state())
    game_filter = request.args.get("filter", "all")

    if game_filter not in {"all", "unplayed", "played"}:
        game_filter = "all"

    groups = grouped_games_for_state(state, game_filter)
    games = [game for group in groups for game in group["games"]]
    all_games = games_for_state(state)
    open_tables = available_tables(state)

    return render_template(
        "executive_games.html",
        groups=groups,
        round_jump_links=round_jump_links_for_groups(groups),
        game_filter=game_filter,
        available_tables=open_tables,
        needs_table_choice=len(open_tables) > 1,
        counts={
            "all": len(all_games),
            "unplayed": sum(1 for game in all_games if not game["played"]),
            "played": sum(1 for game in all_games if game["played"]),
            "visible": len(games),
        },
        bracket_version=state.get("_version", 0),
        executive_profiles=executive_profiles(),
    )


@app.route("/game_action", methods=["POST"])
@executive_login_required
def game_action_route():
    action = request.form.get("action", "")
    match_id = request.form.get("match_id", "")
    game_filter = request.form.get("filter", "all")
    state = normalize_state(load_state() or empty_state())

    if action == "start":
        success, result = start_game_in_state(
            state,
            match_id,
            request.form.get("table_number")
        )
    elif action == "win":
        success, result = win_game_in_state(
            state,
            match_id,
            request.form.get("winner_slot_id", "")
        )
    elif action == "reset":
        success, result = reset_game_in_state(state, match_id)
    elif action == "round_robin_score":
        success, result = update_round_robin_score_in_state(
            state,
            request.form.get("slot_id", ""),
            request.form.get("score_delta", "0")
        )
    elif action == "reset_round_robin_final":
        success, result = reset_round_robin_final_in_state(state)
    elif action == "delete_first_round_player":
        success, result = delete_first_round_player_in_state(
            state,
            match_id,
            request.form.get("slot_id", "")
        )
    elif action == "delete_first_round":
        success, result = delete_first_round_game_in_state(state, match_id)
    else:
        success, result = False, "Unknown game action"

    if success:
        save_state(result)

    return redirect(url_for("executive_games", filter=game_filter))


@app.route("/registration_state", methods=["GET", "POST"])
def registration_state_route():
    if request.method == "POST" and not is_executive_logged_in():
        return {"success": False, "error": "Executive login required"}, 401

    if request.method == "POST":
        incoming = request.get_json(silent=True) or {}
        registration_client_id = clean_registration_client_id(
            incoming.get("client_id", "")
        )
        updates = {
            field: incoming.get(field, "")
            for field in REGISTRATION_FIELDS
            if field in incoming
        }

        with closing(get_db()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            current_state = load_registration_state_with_conn(conn)

            if is_stale_registration_state(
                incoming.get("known_version"),
                registration_client_id,
                current_state
            ):
                conn.rollback()
                return {**current_state, "refresh": True}, 409

            if updates:
                current_state = save_registration_state_with_conn(
                    conn,
                    updates,
                    registration_client_id
                )

            conn.commit()

            return current_state

    return load_registration_state()


@app.route("/payments", methods=["GET", "POST"])
@executive_login_required
def payments():
    if request.method == "POST":
        active_players = active_comp_players()
        state = normalize_state(load_state() or empty_state())
        paid_keys = set(request.form.getlist("paid_player"))
        comp_date = clean_date_key(request.form.get("comp_date"))
        payments = {
            player["key"]: player["key"] in paid_keys
            for player in active_players
        }
        payout_mode = clean_payout_mode(request.form.get("payout_mode"))
        comp_size = clean_comp_size(
            request.form.get("comp_size"),
            default_comp_size_for_players(active_players)
        )

        if payout_mode == "preset":
            prizes = dict(PRESET_WINNINGS[comp_size])
        else:
            prizes = {
                1: request.form.get("first_winnings", "0"),
                2: request.form.get("second_winnings", "0"),
                3: request.form.get("third_winnings", "0"),
            }

        winners = {
            str(place): request.form.get(f"winner_{place}", "")
            for place in PAYOUT_PLACES
        }
        automatic_winners, _ = automatic_winners_from_round_robin(state)

        if automatic_winners:
            winners = automatic_winners

        save_finance_state(
            0,
            payments,
            payout_mode,
            comp_size,
            prizes,
            winners,
            comp_date
        )
        summary = finance_summary()
        save_comp_results_and_snapshot(summary)

        if request.headers.get("X-Requested-With") == "fetch":
            return summary

        return redirect(url_for("payments"))

    summary = finance_summary()
    return render_template(
        "payments.html",
        summary=summary,
        report_week_options=tuesday_week_options(summary.get("comp_date")),
    )


@app.route("/payments/export.pdf")
@executive_login_required
def payments_pdf():
    summary = finance_summary()
    report_date = clean_date_key(request.args.get("comp_date") or summary.get("comp_date"))
    summary = {
        **summary,
        "comp_date": report_date,
        "comp_date_display": competition_context_for_date(report_date)["display"],
    }
    save_comp_results_and_snapshot(summary)
    history = payment_report_history()
    pdf_bytes = build_payment_report_pdf(summary, history, report_date)

    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={
            "Content-Disposition": "attachment; filename=pool-payments-report.pdf"
        }
    )


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST" and not is_executive_logged_in():
        return redirect(url_for("executive_login", next=url_for("register")))

    if request.method == "POST":
        action = request.form.get("action")
        registration_client_id = clean_registration_client_id(
            request.form.get("registration_client_id", "")
        )
        registration_updates = registration_form_updates(request.form)

        if not claim_registration_state(
            request.form.get("registration_version"),
            registration_client_id,
            registration_updates
        ):
            return redirect(url_for("register"))

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
            save_registration_state(
                {field: "" for field in REGISTRATION_FIELDS},
                registration_client_id
            )
            return redirect(url_for("register"))

        if action == "generate":
            player_entries = [
                (name, bracket_name(name))
                for name in new_players
            ]

            random.shuffle(player_entries)

            player_entries = player_entries[:64]
            players = [short_name for _, short_name in player_entries]

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

            add_known_players(known_player_names_from_entries(raw_name for raw_name, _ in player_entries))
            ensure_players_exist(players)
            save_state(state)
            save_registration_state(
                {field: "" for field in REGISTRATION_FIELDS},
                registration_client_id
            )

            return redirect(url_for("register"))

        if action == "add_late":
            state = normalize_state(load_state() or empty_state())

            player_entries = [
                (name, bracket_name(name))
                for name in late_players
            ]

            random.shuffle(player_entries)

            available_slots = sum(
                1 for i in range(64)
                if is_first_round_slot_empty(state, i)
            )
            added_count = min(len(player_entries), available_slots)
            added_entries = player_entries[:added_count]
            players_to_add = [short_name for _, short_name in added_entries]

            state = add_players_to_empty_slots(state, players_to_add)
            state["counts"]["late_players"] += added_count

            add_known_players(known_player_names_from_entries(raw_name for raw_name, _ in added_entries))
            ensure_players_exist(players_to_add)
            save_state(state)
            registration_updates["late_players"] = ""
            save_registration_state(registration_updates, registration_client_id)

            return redirect(url_for("register"))

        if action == "add_buybacks":
            state = normalize_state(load_state() or empty_state())

            player_entries = [
                (name, bracket_name(name, buyback=True))
                for name in buybacks
            ]

            random.shuffle(player_entries)

            available_slots = sum(
                1 for i in range(64)
                if is_first_round_slot_empty(state, i)
            )
            added_count = min(len(player_entries), available_slots)
            added_entries = player_entries[:added_count]
            players_to_add = [short_name for _, short_name in added_entries]

            state = add_players_to_empty_slots(state, players_to_add)
            state["counts"]["buybacks"] += added_count

            add_known_players(known_player_names_from_entries(raw_name for raw_name, _ in added_entries))
            ensure_players_exist(players_to_add)
            save_state(state)
            registration_updates["buybacks"] = ""
            save_registration_state(registration_updates, registration_client_id)

            return redirect(url_for("register"))

    counts = get_register_counts()
    registration_state = load_registration_state()
    target_field = "late_players" if counts["total_players"] > 0 else "new_players"

    return render_template(
        "register.html",
        members=load_members(),
        known_players=get_known_players(),
        counts=counts,
        initial_signups=player_names_from_entries(
            parse_textarea(registration_state.get(target_field, ""))
        ),
        registration_state=registration_state,
        rankings=get_rankings(),
        has_initial_comp=counts["total_players"] > 0
    )


@app.route("/members", methods=["GET", "POST"])
@executive_login_required
def edit_members():
    if request.method == "POST":
        edited_members = parse_textarea(request.form.get("members", ""))
        edited_known_non_members = parse_textarea(
            request.form.get("known_non_members", "")
        )
        save_members(edited_members)
        save_known_player_names(edited_members + edited_known_non_members)
        touch_registration_state()
        return redirect(url_for("register"))

    members = load_members()
    known_non_members = load_known_non_member_names()

    return render_template(
        "members.html",
        members_text="\n".join(members),
        member_count=len(members),
        known_non_members_text="\n".join(known_non_members),
        known_non_member_count=len(known_non_members)
    )


@app.route("/remove_first_round_player", methods=["POST"])
@executive_login_required
def remove_first_round_player_route():
    incoming = request.get_json() or {}
    slot_id = incoming.get("slot_id", "")

    state = normalize_state(load_state() or empty_state())
    success, result = remove_first_round_player_from_state(state, slot_id)

    if not success:
        return {"success": False, "error": result}, 400

    save_state(state)
    touch_registration_state()

    return {
        "success": True,
        "removed_player": result,
        "slot_id": slot_id
    }


@app.route("/record_match_result", methods=["POST"])
@executive_login_required
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
@executive_login_required
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
@executive_login_required
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
    incoming["champion"] = incoming.get("champion", old_state.get("champion", ""))
    incoming["round_robin_scores"] = old_state.get("round_robin_scores", {})
    incoming["active_tables"] = old_state.get("active_tables", {})

    save_state(incoming)

    return {"success": True}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
