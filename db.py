import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    data_type: str
    not_null: bool = False
    default_value: Any = None
    primary_key: bool = False


@dataclass(frozen=True)
class TableSpec:
    name: str
    columns: tuple[ColumnSpec, ...]
    create_sql: str


MESSAGE_EVENTS_TABLE = TableSpec(
    name="message_events",
    columns=(
        ColumnSpec("id", "INTEGER", primary_key=True),
        ColumnSpec("chat_id", "INTEGER", not_null=True),
        ColumnSpec("user_id", "INTEGER"),
        ColumnSpec("user_name", "TEXT"),
        ColumnSpec("message_text", "TEXT", not_null=True),
        ColumnSpec("created_at", "TEXT", not_null=True, default_value="CURRENT_TIMESTAMP"),
    ),
    create_sql="""
CREATE TABLE message_events (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    user_id INTEGER,
    user_name TEXT,
    message_text TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
""".strip(),
)

SUMMARY_POSITIONS_TABLE = TableSpec(
    name="summary_positions",
    columns=(
        ColumnSpec("id", "INTEGER", primary_key=True),
        ColumnSpec("chat_id", "INTEGER", not_null=True),
        ColumnSpec("user_id", "INTEGER", not_null=True),
        ColumnSpec("start_message_id", "INTEGER", not_null=True),
        ColumnSpec("created_at", "TEXT", not_null=True, default_value="CURRENT_TIMESTAMP"),
    ),
    create_sql="""
CREATE TABLE summary_positions (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    start_message_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(chat_id, user_id)
);
""".strip(),
)


def connect_sqlite(db_path: str) -> tuple[sqlite3.Connection, bool]:
    db_exists = os.path.exists(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    created_new = not db_exists
    if created_new:
        logger.info("SQLite file does not exist and will be initialized: %s", db_path)
    else:
        logger.info("Using existing SQLite file: %s", db_path)
    return conn, created_new


def ensure_table(conn: sqlite3.Connection, table_spec: TableSpec) -> None:
    table_exists = _table_exists(conn, table_spec.name)
    if not table_exists:
        logger.info("Table %s does not exist. Creating it.", table_spec.name)
        conn.execute(table_spec.create_sql)
        conn.commit()
        return

    actual_columns = _load_table_columns(conn, table_spec.name)
    if _schema_matches(actual_columns, table_spec.columns):
        logger.debug("Table %s schema is up to date.", table_spec.name)
        return

    logger.warning("Table %s schema mismatch detected. Applying migration.", table_spec.name)
    _migrate_table(conn, table_spec, actual_columns)


def setup_echo_feature(conn: sqlite3.Connection) -> None:
    ensure_table(conn, MESSAGE_EVENTS_TABLE)


def setup_summary_feature(conn: sqlite3.Connection) -> None:
    ensure_table(conn, SUMMARY_POSITIONS_TABLE)


def record_message_event(
    conn: sqlite3.Connection,
    chat_id: int | None,
    user_id: int | None,
    user_name: str | None,
    message_text: str,
) -> None:
    conn.execute(
        """
        INSERT INTO message_events (chat_id, user_id, user_name, message_text)
        VALUES (?, ?, ?, ?);
        """,
        (chat_id, user_id, user_name, message_text),
    )
    conn.commit()


def get_latest_message_id(conn: sqlite3.Connection, chat_id: int) -> int:
    row = conn.execute(
        """
        SELECT MAX(id)
        FROM message_events
        WHERE chat_id = ?;
        """,
        (chat_id,),
    ).fetchone()
    if row is None or row[0] is None:
        return 0
    return int(row[0])


def set_summary_start(
    conn: sqlite3.Connection,
    chat_id: int,
    user_id: int,
    start_message_id: int,
) -> bool:
    cursor = conn.execute(
        """
        INSERT INTO summary_positions (chat_id, user_id, start_message_id)
        VALUES (?, ?, ?)
        ON CONFLICT(chat_id, user_id) DO NOTHING;
        """,
        (chat_id, user_id, start_message_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def get_summary_start(
    conn: sqlite3.Connection,
    chat_id: int,
    user_id: int,
) -> int | None:
    row = conn.execute(
        """
        SELECT start_message_id
        FROM summary_positions
        WHERE chat_id = ? AND user_id = ?;
        """,
        (chat_id, user_id),
    ).fetchone()
    if row is None:
        return None
    return int(row[0])


def clear_summary_start(conn: sqlite3.Connection, chat_id: int, user_id: int) -> None:
    conn.execute(
        """
        DELETE FROM summary_positions
        WHERE chat_id = ? AND user_id = ?;
        """,
        (chat_id, user_id),
    )
    conn.commit()


def count_messages_since(conn: sqlite3.Connection, chat_id: int, start_message_id: int) -> int:
    row = conn.execute(
        """
        SELECT COUNT(1)
        FROM message_events
        WHERE chat_id = ? AND id > ?;
        """,
        (chat_id, start_message_id),
    ).fetchone()
    if row is None:
        return 0
    return int(row[0])


def fetch_recent_messages(
    conn: sqlite3.Connection, chat_id: int, limit: int
) -> list[tuple[int, int | None, str | None, str]]:
    rows = conn.execute(
        """
        SELECT id, user_id, user_name, message_text
        FROM message_events
        WHERE chat_id = ?
        ORDER BY id DESC
        LIMIT ?;
        """,
        (chat_id, limit),
    ).fetchall()
    rows.reverse()
    return [(int(row[0]), row[1], row[2], str(row[3])) for row in rows]


def fetch_messages_since(
    conn: sqlite3.Connection,
    chat_id: int,
    start_message_id: int,
    limit: int | None = None,
) -> list[tuple[int, int | None, str | None, str]]:
    sql = """
        SELECT id, user_id, user_name, message_text
        FROM message_events
        WHERE chat_id = ? AND id > ?
        ORDER BY id ASC
    """
    params: list[Any] = [chat_id, start_message_id]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, tuple(params)).fetchall()
    return [(int(row[0]), row[1], row[2], str(row[3])) for row in rows]


def get_latest_user_name(conn: sqlite3.Connection, chat_id: int, user_id: int) -> str | None:
    row = conn.execute(
        """
        SELECT user_name
        FROM message_events
        WHERE chat_id = ? AND user_id = ? AND user_name IS NOT NULL AND user_name <> ''
        ORDER BY id DESC
        LIMIT 1;
        """,
        (chat_id, user_id),
    ).fetchone()
    if row is None:
        return None
    return str(row[0])


def list_group_chat_ids(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute(
        """
        SELECT DISTINCT chat_id
        FROM message_events
        WHERE chat_id < 0
        ORDER BY chat_id;
        """
    ).fetchall()
    return [int(row[0]) for row in rows]


def list_summary_positions_by_chat(conn: sqlite3.Connection, chat_id: int) -> list[tuple[int, int]]:
    rows = conn.execute(
        """
        SELECT user_id, start_message_id
        FROM summary_positions
        WHERE chat_id = ?
        ORDER BY id ASC;
        """,
        (chat_id,),
    ).fetchall()
    return [(int(row[0]), int(row[1])) for row in rows]


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?;",
        (table_name,),
    ).fetchone()
    return row is not None


def _load_table_columns(conn: sqlite3.Connection, table_name: str) -> tuple[ColumnSpec, ...]:
    rows = conn.execute(f"PRAGMA table_info('{table_name}');").fetchall()
    columns: list[ColumnSpec] = []
    for row in rows:
        columns.append(
            ColumnSpec(
                name=row[1],
                data_type=(row[2] or "").upper(),
                not_null=bool(row[3]),
                default_value=row[4],
                primary_key=bool(row[5]),
            )
        )
    return tuple(columns)


def _schema_matches(actual: tuple[ColumnSpec, ...], expected: tuple[ColumnSpec, ...]) -> bool:
    if len(actual) != len(expected):
        return False
    for actual_col, expected_col in zip(actual, expected):
        if actual_col.name != expected_col.name:
            return False
        if actual_col.data_type != expected_col.data_type.upper():
            return False
        if actual_col.not_null != expected_col.not_null:
            return False
        if not _defaults_match(actual_col.default_value, expected_col.default_value):
            return False
        if actual_col.primary_key != expected_col.primary_key:
            return False
    return True


def _defaults_match(actual: Any, expected: Any) -> bool:
    if actual is None and expected is None:
        return True
    if actual is None or expected is None:
        return False
    return str(actual).strip("'\"").upper() == str(expected).strip("'\"").upper()


def _migrate_table(
    conn: sqlite3.Connection,
    table_spec: TableSpec,
    actual_columns: tuple[ColumnSpec, ...],
) -> None:
    backup_table = f"{table_spec.name}__legacy_{int(time.time())}"
    old_columns = {column.name for column in actual_columns}
    target_columns = [column.name for column in table_spec.columns]
    shared_columns = [column for column in target_columns if column in old_columns]

    conn.execute("BEGIN;")
    try:
        conn.execute(f"ALTER TABLE {table_spec.name} RENAME TO {backup_table};")
        conn.execute(table_spec.create_sql)
        if shared_columns:
            column_list = ", ".join(shared_columns)
            conn.execute(
                f"""
                INSERT INTO {table_spec.name} ({column_list})
                SELECT {column_list}
                FROM {backup_table};
                """
            )
        conn.execute(f"DROP TABLE {backup_table};")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
