
#!/usr/bin/env python3
"""Serve the dashboard and a Gemini-powered workbook data assistant."""

from __future__ import annotations

import json
import os
import random
import re
import secrets
import smtplib
import sqlite3
import threading
import time
from contextlib import closing
from email.message import EmailMessage
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

import bcrypt

from init_db import rebuild_database, sync_employee_workbook


APP_DIR = Path(__file__).resolve().parent


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


load_env_file(APP_DIR / ".env")
DB_PATH = Path(os.environ.get("DASHBOARD_DB_PATH", APP_DIR / "chatbot_data.db"))
AUTH_DB_PATH = Path(os.environ.get("DASHBOARD_AUTH_DB_PATH", APP_DIR / "auth_data.db"))
MAX_REQUEST_BYTES = 65_536
MAX_QUESTION_CHARS = 5_000
MAX_HISTORY_MESSAGES = 12
MAX_QUERY_ROWS = 200
MAX_TOOL_RESULT_BYTES = 48_000
MAX_TOOL_ROUNDS = 4
GEMINI_MAX_ATTEMPTS = 4
GEMINI_RETRYABLE_STATUS = {429, 500, 503, 504}
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
BCRYPT_ROUNDS = 12
OTP_LENGTH = 6
OTP_TTL_SECONDS = 10 * 60
OTP_MAX_ATTEMPTS = 5
OTP_LOCK = threading.Lock()
OTP_STORE: dict[str, dict[str, Any]] = {}
SESSION_TTL_SECONDS = 12 * 60 * 60
SESSION_COOKIE = "dashboard_session"
ADMIN_UNLOCK_PASSWORD = "20032003"
SESSION_LOCK = threading.Lock()
SESSIONS: dict[str, dict[str, Any]] = {}
AUTH_PAGES = {"/login.html", "/signup.html"}
PUBLIC_STATIC_PREFIXES = ("/assets/",)
ROLE_OPTIONS = {
    "Administrator",
    "HR Manager",
    "Department Director",
    "Project Manager",
    "Employee",
}
AISHAH_ADMIN_EMAILS = {"aishahabunaja@gmail.com"}
AISHAH_ADMIN_NAMES = {"aishahabunaja", "aishah abunaja", "aishah_abunaja"}
ROLE_PAGES = {
    "Administrator": {"index.html", "departments.html", "employees.html", "projects.html", "tasks.html", "meetings.html", "weekly-updates.html", "activity-log.html", "lists.html", "admin.html"},
    "HR Manager": {"index.html", "departments.html", "employees.html", "projects.html", "tasks.html", "meetings.html", "weekly-updates.html"},
    "Department Director": {"index.html", "departments.html", "employees.html", "projects.html", "tasks.html", "meetings.html", "weekly-updates.html"},
    "Project Manager": {"index.html", "projects.html", "tasks.html", "meetings.html", "weekly-updates.html"},
    "Employee": {"index.html", "employees.html", "projects.html", "tasks.html", "meetings.html", "weekly-updates.html"},
}
PAGE_BY_TABLE = {
    "data_departments": "departments.html",
    "data_employees": "employees.html",
    "data_projects": "projects.html",
    "data_tasks": "tasks.html",
    "data_meetings": "meetings.html",
    "data_weekly_updates": "weekly-updates.html",
    "data_activity_log": "activity-log.html",
    "data_lists": "lists.html",
}

SYSTEM_INSTRUCTION = """
You are the data assistant embedded in the Enterprise Overview dashboard.

The attached SQL schema is generated from sample_data.xlsx. Answer questions about this dashboard and its workbook only.
- For every workbook-data question, use query_workbook before answering. Never rely on assumptions or memorized values.
- Write read-only SQLite SELECT queries using the exact table and column names in the schema.
- You may call query_workbook repeatedly to inspect values, join tables, aggregate, compare, filter, or verify a result.
- Join sheets through matching ID columns when needed. Do not assume similarly named people or projects are identical without IDs.
- Dates are stored as ISO text (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS), so SQLite date functions can be used.
- Distinguish department-reported headcount from the smaller sample of employee records.
- Treat weekly updates as observations, not necessarily cumulative progress.
- If a query is truncated, use narrower queries or pagination before claiming a complete list.
- If the requested data is absent, say so. Never invent, estimate, use general knowledge, or access the internet.
- Never request an entire sheet when an aggregate or filtered query can answer the question. Select only necessary columns and rows.
- Format long answers for readability. Use a short introduction followed by Markdown bullets or a numbered list, with one record per line. Never return a long comma-separated wall of text.
- If the question is unrelated to the dashboard or workbook, explain briefly that you can only answer questions about this dashboard's sample_data.xlsx data.
- Match the user's language. Give a direct answer and include IDs when they prevent ambiguity.
""".strip()

QUERY_TOOL = {
    "functionDeclarations": [
        {
            "name": "query_workbook",
            "description": "Run a read-only SQLite SELECT query against the workbook tables. Use this for all facts, records, calculations, filtering, grouping, comparisons, and joins. Results include column names, rows, and whether they were truncated.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "One read-only SQLite SELECT or WITH...SELECT statement using exact schema identifiers.",
                    }
                },
                "required": ["sql"],
            },
        }
    ]
}


def hash_password(password: str) -> str:
    """Return a bcrypt hash for a plaintext password."""
    if not isinstance(password, str) or not password:
        raise ValueError("Password must be a non-empty string")
    password_bytes = password.encode("utf-8")
    return bcrypt.hashpw(password_bytes, bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a plaintext password against a bcrypt hash."""
    if not isinstance(password, str) or not isinstance(password_hash, str):
        return False
    if not password or not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def normalize_email(email: str) -> str:
    normalized = " ".join(str(email or "").strip().lower().split())
    if "@" not in normalized or normalized.startswith("@") or normalized.endswith("@"):
        raise ValueError("A valid email address is required")
    return normalized


def generate_otp() -> str:
    upper = 10 ** OTP_LENGTH
    return f"{secrets.randbelow(upper):0{OTP_LENGTH}d}"


def gmail_credentials() -> tuple[str, str]:
    username = os.environ.get("GMAIL_ADDRESS", "").strip()
    app_password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    if not username or not app_password:
        raise RuntimeError("GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be configured")
    return username, app_password


def store_otp(email: str, otp: str) -> None:
    OTP_STORE[email] = {
        "hash": hash_password(otp),
        "expires_at": time.time() + OTP_TTL_SECONDS,
        "attempts": 0,
    }


def send_otp_email(email: str, otp: str) -> None:
    username, app_password = gmail_credentials()
    message = EmailMessage()
    message["Subject"] = "Your Enterprise Dashboard verification code"
    message["From"] = username
    message["To"] = email
    message.set_content(
        "Use this verification code to continue signing in to the Enterprise Dashboard:\n\n"
        f"{otp}\n\n"
        "This code expires in 10 minutes. If you did not request it, you can ignore this email."
    )
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as smtp:
        smtp.login(username, app_password)
        smtp.send_message(message)


def request_otp(email: str) -> None:
    normalized_email = normalize_email(email)
    otp = generate_otp()
    with OTP_LOCK:
        store_otp(normalized_email, otp)
    try:
        send_otp_email(normalized_email, otp)
    except Exception:
        with OTP_LOCK:
            OTP_STORE.pop(normalized_email, None)
        raise


def verify_otp(email: str, otp: str) -> bool:
    normalized_email = normalize_email(email)
    candidate = str(otp or "").strip()
    if not candidate.isdigit() or len(candidate) != OTP_LENGTH:
        return False
    with OTP_LOCK:
        record = OTP_STORE.get(normalized_email)
        if not record:
            return False
        if time.time() > record["expires_at"]:
            OTP_STORE.pop(normalized_email, None)
            return False
        if record["attempts"] >= OTP_MAX_ATTEMPTS:
            OTP_STORE.pop(normalized_email, None)
            return False
        record["attempts"] += 1
        password_hash = record["hash"]
    verified = verify_password(candidate, password_hash)
    if verified:
        with OTP_LOCK:
            OTP_STORE.pop(normalized_email, None)
    return verified


def open_auth_database() -> sqlite3.Connection:
    connection = sqlite3.connect(AUTH_DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def ensure_auth_database() -> None:
    with closing(open_auth_database()) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                email TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                department TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                email_verified INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            """
        )
        columns = {row[1] for row in connection.execute("PRAGMA table_info(users)").fetchall()}
        if "role" not in columns:
            connection.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'Employee'")
        connection.commit()


def normalize_role(role: str) -> str:
    clean_role = " ".join(str(role or "").split())
    if clean_role not in ROLE_OPTIONS:
        raise ValueError("A valid role is required")
    return clean_role


def normalize_person_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name or "").casefold())


def signup_profile_from_workbook(name: str, email: str) -> dict[str, str]:
    normalized_email = normalize_email(email)
    clean_name = " ".join(str(name or "").split())
    if not clean_name:
        raise ValueError("Full name is required")
    if normalized_email in AISHAH_ADMIN_EMAILS or normalize_person_name(clean_name) in {normalize_person_name(item) for item in AISHAH_ADMIN_NAMES}:
        return {
            "name": clean_name,
            "email": normalized_email,
            "department": "Executive Office",
            "role": "Administrator",
        }
    ensure_database_current()
    with closing(open_database()) as connection:
        row = connection.execute(
            """
            SELECT employee_name, email, department, job_title, level
            FROM data_employees
            WHERE email = ? OR lower(employee_name) = lower(?)
            LIMIT 1
            """,
            (normalized_email, clean_name),
        ).fetchone()
    if row is None:
        raise ValueError("Your employee record was not found. Ask an administrator to add you first.")
    workbook_name = " ".join(str(row["employee_name"] or clean_name).split())
    department = " ".join(str(row["department"] or "").split())
    if not department:
        raise ValueError("Your employee record is missing a department. Ask an administrator to update it.")
    title_values = {str(row["job_title"] or "").strip(), str(row["level"] or "").strip()}
    role = "Administrator" if "Administrator" in title_values else "Employee"
    return {
        "name": workbook_name,
        "email": normalized_email,
        "department": department,
        "role": role,
    }


def user_by_email(email: str) -> sqlite3.Row | None:
    normalized_email = normalize_email(email)
    ensure_auth_database()
    with closing(open_auth_database()) as connection:
        return connection.execute(
            "SELECT email, name, department, role, password_hash, email_verified, created_at FROM users WHERE email = ?",
            (normalized_email,),
        ).fetchone()


def create_user(name: str, email: str, password_hash: str, department: str, role: str = "Employee") -> None:
    normalized_email = normalize_email(email)
    clean_name = " ".join(str(name or "").split())
    clean_department = " ".join(str(department or "").split())
    clean_role = normalize_role(role)
    if not clean_name:
        raise ValueError("Full name is required")
    if not clean_department:
        raise ValueError("Department is required")
    ensure_auth_database()
    with closing(open_auth_database()) as connection:
        try:
            connection.execute(
                """
                INSERT INTO users (email, name, department, role, password_hash, email_verified, created_at)
                VALUES (?, ?, ?, ?, ?, 1, datetime('now'))
                """,
                (normalized_email, clean_name, clean_department, clean_role, password_hash),
            )
            connection.commit()
        except sqlite3.IntegrityError as error:
            raise ValueError("An account already exists for this email") from error


def create_user_with_password(name: str, email: str, password: str, department: str, role: str = "Employee") -> None:
    if not isinstance(password, str) or len(password) < 8:
        raise ValueError("Password must be at least 8 characters")
    create_user(name, email, hash_password(password), department, role)


def signup_user(name: str, email: str, password: str) -> None:
    profile = signup_profile_from_workbook(name, email)
    normalized_email = profile["email"]
    if user_by_email(normalized_email):
        raise ValueError("An account already exists for this email")
    if not isinstance(password, str) or len(password) < 8:
        raise ValueError("Password must be at least 8 characters")
    password_hash = hash_password(password)
    otp = generate_otp()
    with OTP_LOCK:
        OTP_STORE[normalized_email] = {
            "hash": hash_password(otp),
            "expires_at": time.time() + OTP_TTL_SECONDS,
            "attempts": 0,
            "signup": {
                "name": profile["name"],
                "email": normalized_email,
                "department": profile["department"],
                "role": profile["role"],
                "password_hash": password_hash,
            },
        }
    try:
        send_otp_email(normalized_email, otp)
    except Exception:
        with OTP_LOCK:
            OTP_STORE.pop(normalized_email, None)
        raise


def verify_signup_otp(email: str, otp: str) -> bool:
    normalized_email = normalize_email(email)
    candidate = str(otp or "").strip()
    if not candidate.isdigit() or len(candidate) != OTP_LENGTH:
        return False
    with OTP_LOCK:
        record = OTP_STORE.get(normalized_email)
        if not record or "signup" not in record:
            return False
        if time.time() > record["expires_at"]:
            OTP_STORE.pop(normalized_email, None)
            return False
        if record["attempts"] >= OTP_MAX_ATTEMPTS:
            OTP_STORE.pop(normalized_email, None)
            return False
        record["attempts"] += 1
        password_hash = record["hash"]
        signup = dict(record["signup"])
    if not verify_password(candidate, password_hash):
        return False
    create_user(signup["name"], signup["email"], signup["password_hash"], signup["department"], signup["role"])
    with OTP_LOCK:
        OTP_STORE.pop(normalized_email, None)
    return True


def update_user_password(email: str, password_hash: str) -> None:
    normalized_email = normalize_email(email)
    ensure_auth_database()
    with closing(open_auth_database()) as connection:
        connection.execute(
            "UPDATE users SET password_hash = ? WHERE email = ?",
            (password_hash, normalized_email),
        )
        connection.commit()


def list_users() -> list[dict[str, Any]]:
    ensure_auth_database()
    with closing(open_auth_database()) as connection:
        rows = connection.execute(
            "SELECT email, name, department, role, email_verified, created_at FROM users ORDER BY created_at DESC, email ASC"
        ).fetchall()
    return [dict(row) for row in rows]


def update_user_record(email: str, name: str, department: str, role: str, password: str | None = None) -> None:
    normalized_email = normalize_email(email)
    clean_name = " ".join(str(name or "").split())
    clean_department = " ".join(str(department or "").split())
    clean_role = normalize_role(role)
    if not clean_name:
        raise ValueError("Full name is required")
    if not clean_department:
        raise ValueError("Department is required")
    ensure_auth_database()
    with closing(open_auth_database()) as connection:
        if password:
            if len(str(password)) < 8:
                raise ValueError("Password must be at least 8 characters")
            cursor = connection.execute(
                "UPDATE users SET name = ?, department = ?, role = ?, password_hash = ? WHERE email = ?",
                (clean_name, clean_department, clean_role, hash_password(str(password)), normalized_email),
            )
        else:
            cursor = connection.execute(
                "UPDATE users SET name = ?, department = ?, role = ? WHERE email = ?",
                (clean_name, clean_department, clean_role, normalized_email),
            )
        if cursor.rowcount == 0:
            raise ValueError("User not found")
        connection.commit()


def delete_user_record(email: str) -> None:
    normalized_email = normalize_email(email)
    ensure_auth_database()
    with closing(open_auth_database()) as connection:
        cursor = connection.execute("DELETE FROM users WHERE email = ?", (normalized_email,))
        if cursor.rowcount == 0:
            raise ValueError("User not found")
        connection.commit()


def change_user_role(email: str, role: str) -> None:
    normalized_email = normalize_email(email)
    clean_role = normalize_role(role)
    ensure_auth_database()
    with closing(open_auth_database()) as connection:
        cursor = connection.execute("UPDATE users SET role = ? WHERE email = ?", (clean_role, normalized_email))
        if cursor.rowcount == 0:
            raise ValueError("User not found")
        connection.commit()


def workbook_missing_user(error: ValueError) -> bool:
    return str(error) == "User not found"


def request_password_reset(email: str) -> None:
    normalized_email = normalize_email(email)
    if user_by_email(normalized_email) is None:
        raise ValueError("There's no account with this email. Create an account first.")
    otp = generate_otp()
    with OTP_LOCK:
        OTP_STORE[normalized_email] = {
            "hash": hash_password(otp),
            "expires_at": time.time() + OTP_TTL_SECONDS,
            "attempts": 0,
            "password_reset": True,
        }
    try:
        send_otp_email(normalized_email, otp)
    except Exception:
        with OTP_LOCK:
            OTP_STORE.pop(normalized_email, None)
        raise


def reset_password(email: str, otp: str, password: str) -> bool:
    normalized_email = normalize_email(email)
    if not isinstance(password, str) or len(password) < 8:
        raise ValueError("Password must be at least 8 characters")
    candidate = str(otp or "").strip()
    if not candidate.isdigit() or len(candidate) != OTP_LENGTH:
        return False
    with OTP_LOCK:
        record = OTP_STORE.get(normalized_email)
        if not record or not record.get("password_reset"):
            return False
        if time.time() > record["expires_at"]:
            OTP_STORE.pop(normalized_email, None)
            return False
        if record["attempts"] >= OTP_MAX_ATTEMPTS:
            OTP_STORE.pop(normalized_email, None)
            return False
        record["attempts"] += 1
        password_hash = record["hash"]
    if not verify_password(candidate, password_hash):
        return False
    update_user_password(normalized_email, hash_password(password))
    with OTP_LOCK:
        OTP_STORE.pop(normalized_email, None)
    return True


def authenticate_user(email: str, password: str) -> sqlite3.Row | None:
    user = user_by_email(email)
    if user is None or not user["email_verified"]:
        return None
    if not verify_password(str(password or ""), user["password_hash"]):
        return None
    return user


def create_session(email: str) -> str:
    token = secrets.token_urlsafe(32)
    with SESSION_LOCK:
        SESSIONS[token] = {"email": normalize_email(email), "expires_at": time.time() + SESSION_TTL_SECONDS, "admin_unlocked": False}
    return token


def session_record(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    with SESSION_LOCK:
        session = SESSIONS.get(token)
        if not session:
            return None
        if time.time() > session["expires_at"]:
            SESSIONS.pop(token, None)
            return None
        session["expires_at"] = time.time() + SESSION_TTL_SECONDS
        return dict(session)


def session_user(token: str | None) -> dict[str, str] | None:
    session = session_record(token)
    if session is None:
        return None
    email = session["email"]
    user = user_by_email(email)
    if user is None:
        return None
    return {"email": user["email"], "name": user["name"], "department": user["department"], "role": user["role"], "admin_unlocked": bool(session.get("admin_unlocked"))}


def unlock_admin_session(token: str | None, password: str) -> bool:
    if str(password or "") != ADMIN_UNLOCK_PASSWORD:
        return False
    if not token:
        return False
    with SESSION_LOCK:
        session = SESSIONS.get(token)
        if not session:
            return False
        session["admin_unlocked"] = True
        session["expires_at"] = time.time() + SESSION_TTL_SECONDS
        return True


def destroy_session(token: str | None) -> None:
    if not token:
        return
    with SESSION_LOCK:
        SESSIONS.pop(token, None)


def find_workspace() -> Path:
    data_dir = os.environ.get("DASHBOARD_DATA_DIR", "").strip()
    if data_dir:
        candidate = Path(data_dir)
        if (candidate / "sample_data.xlsx").is_file():
            return candidate
    for parent in (APP_DIR, *APP_DIR.parents):
        if (parent / "sample_data.xlsx").is_file():
            return parent
    raise FileNotFoundError("Workspace containing sample_data.xlsx was not found")


WORKSPACE = find_workspace()
WORKBOOK_PATH = WORKSPACE / "sample_data.xlsx"
PAGES_DIR = WORKSPACE / "dashboard_pages"
DASHBOARD_PATH = PAGES_DIR / "index.html"
STATIC_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}
SYNC_LOCK = threading.Lock()
SYNC_ERROR = ""


def workbook_metadata() -> dict[str, str]:
    if not DB_PATH.is_file():
        return {}
    try:
        with closing(sqlite3.connect(DB_PATH, timeout=10)) as connection:
            return dict(connection.execute("SELECT key, value FROM workbook_meta").fetchall())
    except sqlite3.Error:
        return {}


def ensure_database_current() -> dict[str, str]:
    """Atomically rebuild SQLite when the workbook's size or mtime changes."""
    global SYNC_ERROR
    source_stat = WORKBOOK_PATH.stat()
    expected_mtime = str(source_stat.st_mtime_ns)
    expected_size = str(source_stat.st_size)
    metadata = workbook_metadata()
    if metadata.get("source_modified_ns") == expected_mtime and metadata.get("source_size") == expected_size:
        return metadata
    with SYNC_LOCK:
        metadata = workbook_metadata()
        if metadata.get("source_modified_ns") == expected_mtime and metadata.get("source_size") == expected_size:
            return metadata
        try:
            result = rebuild_database(WORKBOOK_PATH, DB_PATH)
            SYNC_ERROR = ""
            print(f"Workbook synchronized: {result['sheets']} sheets, {result['rows']} rows")
        except (OSError, ValueError, sqlite3.Error) as error:
            SYNC_ERROR = str(error)
            if not DB_PATH.is_file():
                raise
            print(f"Workbook synchronization failed; serving the previous database: {error}")
        return workbook_metadata()


def freshness_payload() -> dict[str, Any]:
    metadata = ensure_database_current()
    source_stat = WORKBOOK_PATH.stat()
    with closing(open_database()) as connection:
        row_count = connection.execute("SELECT COALESCE(SUM(row_count), 0) FROM workbook_sheets").fetchone()[0]
    return {
        "in_sync": metadata.get("source_modified_ns") == str(source_stat.st_mtime_ns),
        "last_synced": metadata.get("last_synced"),
        "workbook_modified": metadata.get("source_modified"),
        "workbook_size": source_stat.st_size,
        "workbook_rows": row_count,
        "sync_error": SYNC_ERROR or None,
    }


def open_database() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.set_progress_handler(lambda: 1, 2_000_000)
    return connection


def workbook_schema() -> str:
    with closing(open_database()) as connection:
        sheets = connection.execute(
            "SELECT sheet_name, table_name, row_count FROM workbook_sheets ORDER BY rowid"
        ).fetchall()
        columns = connection.execute(
            "SELECT table_name, source_name, column_name, data_type FROM workbook_columns ORDER BY table_name, ordinal"
        ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for column in columns:
        grouped.setdefault(column["table_name"], []).append(column)
    blocks = []
    for sheet in sheets:
        definitions = ", ".join(
            f'"{column["column_name"]}" {column["data_type"]} /* {column["source_name"]} */'
            for column in grouped.get(sheet["table_name"], [])
        )
        blocks.append(
            f'Sheet "{sheet["sheet_name"]}" ({sheet["row_count"]} rows):\n'
            f'CREATE TABLE "{sheet["table_name"]}" ({definitions});'
        )
    return "\n\n".join(blocks)


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def table_catalog() -> dict[str, dict[str, Any]]:
    """Describe every workbook table: sheet name, row count, and column metadata.

    Used by the dashboard pages (via /api/tables and /api/data/<table>) to render
    charts, tables, and search dynamically from whatever is currently in the
    database, instead of hardcoding sheet names or column labels in the frontend.
    """
    with closing(open_database()) as connection:
        sheets = connection.execute(
            "SELECT sheet_name, table_name, row_count FROM workbook_sheets ORDER BY rowid"
        ).fetchall()
        columns = connection.execute(
            "SELECT table_name, source_name, column_name, data_type FROM workbook_columns ORDER BY table_name, ordinal"
        ).fetchall()
    grouped: dict[str, list[dict[str, str]]] = {}
    for column in columns:
        grouped.setdefault(column["table_name"], []).append(
            {"key": column["column_name"], "label": column["source_name"], "type": column["data_type"]}
        )
    return {
        sheet["table_name"]: {
            "sheet_name": sheet["sheet_name"],
            "table_name": sheet["table_name"],
            "row_count": sheet["row_count"],
            "columns": grouped.get(sheet["table_name"], []),
        }
        for sheet in sheets
    }


def allowed_pages(user: dict[str, str] | None) -> set[str]:
    role = user["role"] if user else "Employee"
    return ROLE_PAGES.get(role, ROLE_PAGES["Employee"])


def can_access_page(page: str, user: dict[str, str] | None) -> bool:
    return page in allowed_pages(user)


def page_for_table(table_name: str) -> str | None:
    return PAGE_BY_TABLE.get(table_name)


def row_matches_department(row: dict[str, Any], department: str, project_departments: dict[str, str]) -> bool:
    if "department" in row or "department_name" in row:
        return row.get("department") == department or row.get("department_name") == department
    project_id = row.get("project_id")
    return bool(project_id and project_departments.get(str(project_id)) == department)


def row_matches_employee(row: dict[str, Any], user: dict[str, str], employee_id: str | None) -> bool:
    user_name = user["name"]
    user_email = user["email"]
    if "email" in row or "employee_id" in row:
        return row.get("email") == user_email or bool(employee_id and row.get("employee_id") == employee_id)
    return any(row.get(key) == user_name for key in ("employee_name", "employee", "assigned_to", "organizer", "owner"))


def employee_id_for_user(user: dict[str, str]) -> str | None:
    try:
        with closing(open_database()) as connection:
            row = connection.execute(
                "SELECT employee_id FROM data_employees WHERE email = ? OR employee_name = ? LIMIT 1",
                (user["email"], user["name"]),
            ).fetchone()
    except sqlite3.Error:
        return None
    return row["employee_id"] if row else None


def project_departments() -> dict[str, str]:
    try:
        with closing(open_database()) as connection:
            rows = connection.execute("SELECT project_id, department FROM data_projects").fetchall()
    except sqlite3.Error:
        return {}
    return {str(row["project_id"]): row["department"] for row in rows}


def filter_rows_for_user(table_name: str, rows: list[dict[str, Any]], user: dict[str, str] | None) -> list[dict[str, Any]]:
    if user is None:
        return []
    role = user["role"]
    page = page_for_table(table_name)
    if page and not can_access_page(page, user):
        return []
    if role in {"Administrator", "HR Manager", "Project Manager"}:
        return rows
    if role == "Department Director":
        departments_by_project = project_departments()
        return [row for row in rows if row_matches_department(row, user["department"], departments_by_project)]
    if role == "Employee":
        employee_id = employee_id_for_user(user)
        if table_name == "data_employees":
            return [row for row in rows if row_matches_employee(row, user, employee_id)][:1]
        if table_name == "data_tasks":
            return [row for row in rows if row_matches_employee(row, user, employee_id)]
        if table_name == "data_meetings":
            return [row for row in rows if row_matches_employee(row, user, employee_id)]
        return rows
    return []


def fetch_table(table_name: str, user: dict[str, str] | None = None) -> dict[str, Any] | None:
    """Return every row of a whitelisted workbook table as plain JSON records.

    table_name is only ever used after confirming it's a key of table_catalog(),
    i.e. an identifier that init_db.py itself generated - never raw user input.
    """
    catalog = table_catalog()
    meta = catalog.get(table_name)
    if meta is None:
        return None
    with closing(open_database()) as connection:
        cursor = connection.execute(f"SELECT * FROM {quote_identifier(table_name)}")
        column_keys = [item[0] for item in cursor.description]
        rows = [dict(zip(column_keys, row)) for row in cursor.fetchall()]
    if user is not None:
        rows = filter_rows_for_user(table_name, rows, user)
    return {
        "table_name": table_name,
        "sheet_name": meta["sheet_name"],
        "columns": meta["columns"],
        "rows": rows,
        "row_count": len(rows),
    }


PAGE_BY_SHEET = {
    "Departments": "departments.html",
    "Employees": "employees.html",
    "Projects": "projects.html",
    "Tasks": "tasks.html",
    "Meetings": "meetings.html",
    "Weekly Updates": "weekly-updates.html",
    "Activity Log": "activity-log.html",
    "Lists": "lists.html",
}


def global_search(query: str, user: dict[str, str] | None = None, limit: int = 60) -> dict[str, Any]:
    normalized = " ".join(query.casefold().split())
    if len(normalized) < 2:
        return {"query": query, "total": 0, "results": []}
    results: list[dict[str, Any]] = []
    total = 0
    for table_name, metadata in table_catalog().items():
        table = fetch_table(table_name, user)
        if table is None:
            continue
        columns = metadata["columns"]
        for row in table["rows"]:
            searchable = " ".join(str(row.get(column["key"], "")) for column in columns).casefold()
            if normalized not in searchable:
                continue
            total += 1
            if len(results) >= limit:
                continue
            preferred = next((row.get(key) for key in ("employee_name", "project_name", "task_name", "department_name", "meeting_type", "activity_type", "update_id") if row.get(key)), None)
            populated = [(column["label"], row.get(column["key"])) for column in columns if row.get(column["key"]) not in (None, "")]
            title = preferred or (populated[0][1] if populated else metadata["sheet_name"])
            details = [f"{label}: {value}" for label, value in populated if value != title][:3]
            results.append({
                "sheet_name": metadata["sheet_name"],
                "table_name": table_name,
                "page": PAGE_BY_SHEET.get(metadata["sheet_name"], "index.html"),
                "title": str(title),
                "details": details,
            })
    return {"query": query, "total": total, "results": results, "truncated": total > len(results)}


def query_workbook(sql: str) -> dict[str, Any]:
    statement = str(sql).strip()
    if not statement or not statement.casefold().startswith(("select", "with")):
        return {"error": "Only a SELECT or WITH...SELECT statement is allowed."}
    try:
        with closing(open_database()) as connection:
            cursor = connection.execute(statement)
            if cursor.description is None:
                return {"error": "The query did not return rows."}
            columns = [item[0] for item in cursor.description]
            fetched = cursor.fetchmany(MAX_QUERY_ROWS + 1)
    except sqlite3.Error as error:
        return {"error": f"SQLite query error: {error}"}
    truncated = len(fetched) > MAX_QUERY_ROWS
    rows = []
    result_bytes = 0
    for row in fetched[:MAX_QUERY_ROWS]:
        values = [row[column] for column in columns]
        row_bytes = len(json.dumps(values, ensure_ascii=False).encode("utf-8"))
        if rows and result_bytes + row_bytes > MAX_TOOL_RESULT_BYTES:
            truncated = True
            break
        rows.append(values)
        result_bytes += row_bytes
    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
    }


def retry_delay(error: HTTPError, attempt: int) -> float:
    retry_after = error.headers.get("Retry-After") if error.headers else None
    try:
        return min(15.0, max(0.0, float(retry_after))) if retry_after else 0.0
    except ValueError:
        return 0.0


def gemini_request(contents: list[dict[str, Any]], force_tool: bool) -> dict[str, Any]:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured on the server")
    payload: dict[str, Any] = {
        "system_instruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
        "contents": contents,
        "tools": [QUERY_TOOL],
        "generationConfig": {
            "temperature": 0.1,
            "topP": 0.8,
            "maxOutputTokens": 8192,
        },
    }
    if force_tool:
        payload["toolConfig"] = {
            "functionCallingConfig": {
                "mode": "ANY",
                "allowedFunctionNames": ["query_workbook"],
            }
        }
    encoded_payload = json.dumps(payload).encode("utf-8")
    for attempt in range(GEMINI_MAX_ATTEMPTS):
        request = Request(
            GEMINI_API_URL,
            data=encoded_payload,
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            method="POST",
        )
        try:
            with urlopen(request, timeout=90) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            if error.code not in GEMINI_RETRYABLE_STATUS or attempt == GEMINI_MAX_ATTEMPTS - 1:
                if error.code == 503:
                    raise RuntimeError("Gemini is temporarily busy. Please try again in a moment.") from error
                raise RuntimeError(f"Gemini API returned HTTP {error.code}") from error
            delay = retry_delay(error, attempt) or min(8.0, (2 ** attempt) + random.random())
            print(f"Gemini HTTP {error.code}; retrying in {delay:.1f}s ({attempt + 2}/{GEMINI_MAX_ATTEMPTS})")
            time.sleep(delay)
        except URLError as error:
            raise RuntimeError(f"Could not reach the Gemini API: {error.reason}") from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("Gemini returned an invalid response") from error
    raise RuntimeError("Gemini request failed")


def response_content(result: dict[str, Any]) -> dict[str, Any]:
    try:
        return result["candidates"][0]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise RuntimeError("Gemini returned an unexpected response") from error


def text_from_content(content: dict[str, Any]) -> str:
    return "".join(part.get("text", "") for part in content.get("parts", [])).strip()


def normalize_history(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value[-MAX_HISTORY_MESSAGES:]:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        text = item.get("text")
        if role not in {"user", "model"} or not isinstance(text, str) or not text.strip():
            continue
        result.append({"role": role, "parts": [{"text": text.strip()[:MAX_QUESTION_CHARS]}]})
    return result


def call_gemini(question: str, history: Any = None) -> str:
    prompt = f"WORKBOOK SQL SCHEMA\n===================\n{workbook_schema()}\n\nCURRENT QUESTION\n================\n{question}"
    contents = normalize_history(history)
    contents.append({"role": "user", "parts": [{"text": prompt}]})

    for round_number in range(MAX_TOOL_ROUNDS + 1):
        # Do not force a tool call: forcing it on every first round made Gemini invoke
        # query_workbook even for greetings or out-of-scope questions. The system
        # instruction already tells it to use query_workbook for workbook-data questions,
        # and it can still call the tool as many times as it needs (up to MAX_TOOL_ROUNDS).
        result = gemini_request(contents, force_tool=False)
        content = response_content(result)
        contents.append(content)  # Preserve function IDs and Gemini thought signatures exactly.
        calls = [part["functionCall"] for part in content.get("parts", []) if "functionCall" in part]
        if not calls:
            answer = text_from_content(content)
            if answer:
                return answer
            raise RuntimeError("Gemini returned an empty response")
        if round_number >= MAX_TOOL_ROUNDS:
            raise RuntimeError("Gemini exceeded the workbook query limit")

        response_parts = []
        for call in calls:
            name = call.get("name", "")
            arguments = call.get("args") or {}
            if name == "query_workbook":
                tool_result = query_workbook(arguments.get("sql", ""))
            else:
                tool_result = {"error": f"Unknown function: {name}"}
            function_response: dict[str, Any] = {
                "name": name,
                "response": {"result": tool_result},
            }
            if call.get("id"):
                function_response["id"] = call["id"]
            response_parts.append({"functionResponse": function_response})
        contents.append({"role": "user", "parts": response_parts})
    raise RuntimeError("Gemini did not complete the answer")


class DashboardHandler(BaseHTTPRequestHandler):
    def session_token(self) -> str | None:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def current_user(self) -> dict[str, str] | None:
        return session_user(self.session_token())

    def is_public_path(self, path: str) -> bool:
        return path in AUTH_PAGES or path.startswith(PUBLIC_STATIC_PREFIXES)

    def send_redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def cors_origin(self) -> str:
        origin = self.headers.get("Origin", "")
        if origin == "null":
            return origin
        if origin.startswith(("http://127.0.0.1:", "http://localhost:")):
            return origin
        return "*"

    def send_cors_headers(self) -> None:
        origin = self.cors_origin()
        self.send_header("Access-Control-Allow-Origin", origin)
        if origin != "*":
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Vary", "Origin")

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def send_json_with_session(self, status: int, payload: dict[str, Any], token: str) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL_SECONDS}")
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def send_json_clearing_session(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def send_logout_redirect(self) -> None:
        self.send_response(303)
        self.send_header("Location", "/login.html")
        self.send_header("Set-Cookie", f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
        self.send_header("Content-Length", "0")
        self.send_cors_headers()
        self.end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def serve_static(self, path: str) -> bool:
        relative = path.lstrip("/")
        if not relative:
            return False
        candidate = (PAGES_DIR / relative).resolve()
        try:
            candidate.relative_to(PAGES_DIR.resolve())
        except ValueError:
            return False
        content_type = STATIC_CONTENT_TYPES.get(candidate.suffix.casefold())
        if content_type is None or not candidate.is_file():
            return False
        if candidate.suffix.casefold() == ".html" and path not in AUTH_PAGES:
            user = self.current_user()
            if user is None:
                self.send_redirect(f"/login.html?next={quote(path)}")
                return True
            if candidate.name == "admin.html" and user["role"] != "Administrator":
                self.send_redirect("/index.html")
                return True
            if not can_access_page(candidate.name, user):
                self.send_redirect("/index.html")
                return True
        body = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return True

    def read_json_body(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(400, {"error": "Invalid content length"})
            return None
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self.send_json(400, {"error": "Request is empty or too large"})
            return None
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(400, {"error": "Invalid JSON request"})
            return None
        if not isinstance(payload, dict):
            self.send_json(400, {"error": "JSON request must be an object"})
            return None
        return payload

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path in AUTH_PAGES and self.current_user() is not None:
            self.send_redirect("/index.html")
            return
        if path == "/auth/me":
            user = self.current_user()
            if user is None:
                self.send_json(401, {"authenticated": False})
                return
            self.send_json(200, {"authenticated": True, "user": user})
            return
        if path == "/health" or path.startswith("/api/"):
            if path.startswith("/api/") and self.current_user() is None:
                self.send_json(401, {"error": "Authentication required"})
                return
            try:
                ensure_database_current()
            except (OSError, ValueError, sqlite3.Error) as error:
                self.send_json(503, {"error": f"Workbook synchronization failed: {error}"})
                return
        if path in {"/", "/index.html"}:
            if self.current_user() is None:
                self.send_redirect("/login.html?next=/index.html")
                return
            body = DASHBOARD_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/health":
            with closing(open_database()) as connection:
                sheet_count = connection.execute("SELECT COUNT(*) FROM workbook_sheets").fetchone()[0]
                row_count = connection.execute("SELECT COALESCE(SUM(row_count), 0) FROM workbook_sheets").fetchone()[0]
            self.send_json(200, {
                "status": "ok",
                "scope": "workbook-only",
                "model": GEMINI_MODEL,
                "gemini_configured": bool(os.environ.get("GEMINI_API_KEY", "").strip()),
                "workbook_sheets": sheet_count,
                "workbook_rows": row_count,
            })
            return
        if path == "/api/tables":
            user = self.current_user()
            tables = [
                metadata
                for table_name, metadata in table_catalog().items()
                if can_access_page(page_for_table(table_name) or "", user)
            ]
            self.send_json(200, {"tables": tables})
            return
        if path == "/api/freshness":
            self.send_json(200, freshness_payload())
            return
        if path == "/api/search":
            query = parse_qs(parsed.query).get("q", [""])[0].strip()
            if len(query) < 2:
                self.send_json(200, {"query": query, "total": 0, "results": []})
                return
            self.send_json(200, global_search(query, self.current_user()))
            return
        if path == "/admin/users":
            user = self.current_user()
            if user is None:
                self.send_json(401, {"error": "Authentication required"})
                return
            if user["role"] != "Administrator":
                self.send_json(403, {"error": "Administrator access required"})
                return
            session = session_record(self.session_token())
            if not session or not session.get("admin_unlocked"):
                self.send_json(403, {"error": "Admin password required"})
                return
            self.send_json(200, {"users": list_users()})
            return
        if path.startswith("/api/data/"):
            table_name = path[len("/api/data/"):]
            result = fetch_table(table_name, self.current_user())
            if result is None:
                self.send_json(404, {"error": f"Unknown table: {table_name}"})
                return
            self.send_json(200, result)
            return
        if self.serve_static(path):
            return
        self.send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/auth/signup":
            payload = self.read_json_body()
            if payload is None:
                return
            try:
                signup_user(
                    payload.get("name", ""),
                    payload.get("email", ""),
                    payload.get("password", ""),
                )
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
                return
            except RuntimeError as error:
                self.send_json(503, {"error": str(error)})
                return
            except smtplib.SMTPAuthenticationError:
                self.send_json(503, {"error": "Gmail rejected the app password. Generate a new Gmail App Password and update GMAIL_APP_PASSWORD."})
                return
            except (OSError, smtplib.SMTPException) as error:
                print(f"Signup OTP email error: {error}")
                self.send_json(503, {"error": "Could not send verification code"})
                return
            self.send_json(200, {"ok": True, "message": "Verification code sent"})
            return
        if path == "/auth/verify-signup":
            payload = self.read_json_body()
            if payload is None:
                return
            try:
                verified = verify_signup_otp(payload.get("email", ""), payload.get("otp", ""))
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
                return
            if not verified:
                self.send_json(400, {"error": "Invalid or expired verification code"})
                return
            user = user_by_email(payload.get("email", ""))
            token = create_session(user["email"])
            self.send_json_with_session(200, {"ok": True, "user": {"email": user["email"], "name": user["name"], "department": user["department"], "role": user["role"]}}, token)
            return
        if path == "/auth/login":
            payload = self.read_json_body()
            if payload is None:
                return
            try:
                email = payload.get("email", "")
                if user_by_email(email) is None:
                    self.send_json(404, {"error": "There's no account with this email. Create an account first."})
                    return
                user = authenticate_user(email, payload.get("password", ""))
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
                return
            if user is None:
                self.send_json(401, {"error": "Invalid email or password"})
                return
            token = create_session(user["email"])
            self.send_json_with_session(200, {"ok": True, "user": {"email": user["email"], "name": user["name"], "department": user["department"], "role": user["role"]}}, token)
            return
        if path == "/auth/logout":
            destroy_session(self.session_token())
            self.send_logout_redirect()
            return
        if path == "/auth/admin-unlock":
            payload = self.read_json_body()
            if payload is None:
                return
            user = self.current_user()
            if user is None:
                self.send_json(401, {"error": "Authentication required"})
                return
            if user["role"] != "Administrator":
                self.send_json(403, {"error": "Administrator access required"})
                return
            if not unlock_admin_session(self.session_token(), payload.get("password", "")):
                self.send_json(401, {"error": "Invalid admin password"})
                return
            self.send_json(200, {"ok": True, "message": "Admin access granted"})
            return
        if path == "/auth/request-otp":
            payload = self.read_json_body()
            if payload is None:
                return
            try:
                request_otp(payload.get("email", ""))
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
                return
            except RuntimeError as error:
                self.send_json(503, {"error": str(error)})
                return
            except smtplib.SMTPAuthenticationError:
                self.send_json(503, {"error": "Gmail rejected the app password. Generate a new Gmail App Password and update GMAIL_APP_PASSWORD."})
                return
            except (OSError, smtplib.SMTPException) as error:
                print(f"OTP email error: {error}")
                self.send_json(503, {"error": "Could not send verification code"})
                return
            self.send_json(200, {"ok": True, "message": "Verification code sent"})
            return
        if path == "/auth/request-password-reset":
            payload = self.read_json_body()
            if payload is None:
                return
            try:
                request_password_reset(payload.get("email", ""))
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
                return
            except RuntimeError as error:
                self.send_json(503, {"error": str(error)})
                return
            except smtplib.SMTPAuthenticationError:
                self.send_json(503, {"error": "Gmail rejected the app password. Generate a new Gmail App Password and update GMAIL_APP_PASSWORD."})
                return
            except (OSError, smtplib.SMTPException) as error:
                print(f"Password reset OTP email error: {error}")
                self.send_json(503, {"error": "Could not send verification code"})
                return
            self.send_json(200, {"ok": True, "message": "Verification code sent"})
            return
        if path == "/auth/reset-password":
            payload = self.read_json_body()
            if payload is None:
                return
            try:
                reset = reset_password(
                    payload.get("email", ""),
                    payload.get("otp", ""),
                    payload.get("password", ""),
                )
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
                return
            if not reset:
                self.send_json(400, {"error": "Invalid or expired verification code"})
                return
            self.send_json(200, {"ok": True, "message": "Password updated"})
            return
        if path == "/admin/users":
            user = self.current_user()
            if user is None:
                self.send_json(401, {"error": "Authentication required"})
                return
            if user["role"] != "Administrator":
                self.send_json(403, {"error": "Administrator access required"})
                return
            session = session_record(self.session_token())
            if not session or not session.get("admin_unlocked"):
                self.send_json(403, {"error": "Admin password required"})
                return
            payload = self.read_json_body()
            if payload is None:
                return
            action = str(payload.get("action", "")).strip().lower()
            try:
                if action == "create":
                    email = payload.get("email", "")
                    normalize_email(email)
                    if not str(payload.get("name", "")).strip():
                        raise ValueError("Full name is required")
                    if not str(payload.get("department", "")).strip():
                        raise ValueError("Department is required")
                    sync_employee_workbook(
                        WORKBOOK_PATH,
                        action="create",
                        employee={
                            "employee_id": payload.get("employee_id", ""),
                            "employee_name": payload.get("name", ""),
                            "email": email,
                            "department_id": payload.get("department_id", ""),
                            "department": payload.get("department", ""),
                            "job_title": payload.get("job_title", ""),
                            "level": payload.get("level", ""),
                            "manager": payload.get("manager", ""),
                            "location": payload.get("location", ""),
                            "hire_date": payload.get("hire_date", ""),
                            "employment_status": payload.get("employment_status", "Active"),
                        },
                    )
                    ensure_database_current()
                    self.send_json(200, {"ok": True})
                    return
                if action == "update":
                    email = payload.get("email", "")
                    employee_payload = {
                        "employee_id": payload.get("employee_id", ""),
                        "employee_name": payload.get("name", ""),
                        "email": email,
                        "department_id": payload.get("department_id", ""),
                        "department": payload.get("department", ""),
                        "job_title": payload.get("job_title", ""),
                        "level": payload.get("level", ""),
                        "manager": payload.get("manager", ""),
                        "location": payload.get("location", ""),
                        "hire_date": payload.get("hire_date", ""),
                        "employment_status": payload.get("employment_status", "Active"),
                    }
                    try:
                        sync_employee_workbook(WORKBOOK_PATH, action="update", employee=employee_payload)
                    except ValueError as error:
                        if not workbook_missing_user(error):
                            raise
                        sync_employee_workbook(WORKBOOK_PATH, action="create", employee=employee_payload)
                    ensure_database_current()
                    existing_user = user_by_email(email)
                    if existing_user is not None:
                        update_user_record(
                            email,
                            payload.get("name", ""),
                            payload.get("department", ""),
                            existing_user["role"],
                            None,
                        )
                    self.send_json(200, {"ok": True})
                    return
                if action == "delete":
                    email = payload.get("email", "")
                    try:
                        sync_employee_workbook(
                            WORKBOOK_PATH,
                            action="delete",
                            employee={"email": email},
                        )
                    except ValueError as error:
                        if not workbook_missing_user(error):
                            raise
                    ensure_database_current()
                    if user_by_email(email) is not None:
                        delete_user_record(email)
                    self.send_json(200, {"ok": True})
                    return
                if action == "make_admin":
                    email = payload.get("email", "")
                    existing_user = user_by_email(email)
                    employee_payload = {
                        "employee_id": payload.get("employee_id", ""),
                        "employee_name": payload.get("name", ""),
                        "email": email,
                        "department_id": payload.get("department_id", ""),
                        "department": payload.get("department", ""),
                        "job_title": "Administrator",
                        "level": "Administrator",
                        "manager": payload.get("manager", ""),
                        "location": payload.get("location", ""),
                        "hire_date": payload.get("hire_date", ""),
                        "employment_status": payload.get("employment_status", "Active"),
                    }
                    try:
                        sync_employee_workbook(
                            WORKBOOK_PATH,
                            action="promote",
                            employee={"email": email, "role": "Administrator"},
                        )
                    except ValueError as error:
                        if not workbook_missing_user(error):
                            raise
                        sync_employee_workbook(
                            WORKBOOK_PATH,
                            action="create",
                            employee=employee_payload,
                        )
                    ensure_database_current()
                    if existing_user is not None:
                        change_user_role(email, "Administrator")
                    self.send_json(200, {"ok": True})
                    return
                if action == "remove_admin":
                    email = payload.get("email", "")
                    existing_user = user_by_email(email)
                    employee_payload = {
                        "employee_id": payload.get("employee_id", ""),
                        "employee_name": payload.get("name", ""),
                        "email": email,
                        "department_id": payload.get("department_id", ""),
                        "department": payload.get("department", ""),
                        "job_title": "Employee",
                        "level": "Employee",
                        "manager": payload.get("manager", ""),
                        "location": payload.get("location", ""),
                        "hire_date": payload.get("hire_date", ""),
                        "employment_status": payload.get("employment_status", "Active"),
                    }
                    try:
                        sync_employee_workbook(WORKBOOK_PATH, action="update", employee=employee_payload)
                    except ValueError as error:
                        if not workbook_missing_user(error):
                            raise
                        sync_employee_workbook(WORKBOOK_PATH, action="create", employee=employee_payload)
                    ensure_database_current()
                    if existing_user is not None:
                        change_user_role(email, "Employee")
                    self.send_json(200, {"ok": True})
                    return
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
                return
            self.send_json(400, {"error": "Invalid admin action"})
            return
        if path == "/auth/verify-otp":
            payload = self.read_json_body()
            if payload is None:
                return
            try:
                verified = verify_otp(payload.get("email", ""), payload.get("otp", ""))
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
                return
            self.send_json(200, {"verified": verified})
            return
        if path != "/ask":
            self.send_json(404, {"error": "Not found"})
            return
        if self.current_user() is None:
            self.send_json(401, {"error": "Authentication required"})
            return
        payload = self.read_json_body()
        if payload is None:
            return
        question = payload.get("question")
        if not isinstance(question, str) or not question.strip():
            self.send_json(400, {"error": "Question is empty"})
            return
        question = question.strip()
        if len(question) > MAX_QUESTION_CHARS:
            self.send_json(400, {"error": "Question is too long"})
            return
        try:
            ensure_database_current()
            reply = call_gemini(question, payload.get("history"))
        except (OSError, ValueError, sqlite3.Error) as error:
            self.send_json(503, {"error": f"Workbook synchronization failed: {error}"})
            return
        except RuntimeError as error:
            print(f"Dashboard assistant error: {error}")
            self.send_json(503, {"error": str(error)})
            return
        self.send_json(200, {"reply": reply})

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")


def main() -> None:
    ensure_auth_database()
    ensure_database_current()
    address = (os.environ.get("DASHBOARD_HOST", "127.0.0.1"), int(os.environ.get("DASHBOARD_PORT", "8000")))
    print(f"Dashboard chatbot running at http://{address[0]}:{address[1]}")
    print(f"Gemini model: {GEMINI_MODEL}")
    print(f"Gemini key configured: {bool(os.environ.get('GEMINI_API_KEY', '').strip())}")
    ThreadingHTTPServer(address, DashboardHandler).serve_forever()


if __name__ == "__main__":
    main()
