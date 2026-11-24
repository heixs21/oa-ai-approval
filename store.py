"""本地状态表，用 SQLite 即可。换 MySQL 只需改连接。"""
import sqlite3
import threading
from datetime import datetime

import config as C

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS approval (
    request_id   TEXT NOT NULL,
    node_id      TEXT NOT NULL,
    workflow_id  TEXT,
    request_name TEXT,
    status       TEXT,
    decision     TEXT,
    confidence   REAL,
    remark       TEXT,
    attach_count INTEGER DEFAULT 0,
    folder       TEXT,
    retry_count  INTEGER DEFAULT 0,
    log_count    INTEGER DEFAULT 0,
    err_msg      TEXT,
    create_time  TEXT,
    update_time  TEXT,
    PRIMARY KEY (request_id, node_id)
);
"""


def _migrate(conn: sqlite3.Connection):
    """给老库补字段，避免升级时报错。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(approval)")}
    if "log_count" not in cols:
        conn.execute("ALTER TABLE approval ADD COLUMN log_count INTEGER DEFAULT 0")
    conn.commit()


def _conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn"):
        conn = sqlite3.connect(str(C.DB_PATH), timeout=15)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
        _local.conn = conn
    return _local.conn


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get(request_id: str, node_id: str) -> dict | None:
    cur = _conn().execute(
        "SELECT status, decision, remark, retry_count, log_count, update_time "
        "FROM approval WHERE request_id=? AND node_id=?",
        (request_id, node_id),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "status": row[0],
        "decision": row[1],
        "remark": row[2],
        "retry_count": row[3],
        "log_count": row[4] or 0,
        "update_time": row[5],
    }


def is_submitted(request_id: str, node_id: str) -> bool:
    rec = get(request_id, node_id)
    return bool(rec and rec["status"] == "SUBMITTED")


def save(
    request_id: str,
    node_id: str,
    status: str,
    *,
    workflow_id: str = "",
    request_name: str = "",
    decision: str = "",
    confidence: float = 0.0,
    remark: str = "",
    attach_count: int = 0,
    folder: str = "",
    err_msg: str = "",
    log_count: int = 0,
):
    conn = _conn()
    now = _now()
    conn.execute(
        """
        INSERT INTO approval (request_id, node_id, workflow_id, request_name, status,
                              decision, confidence, remark, attach_count, folder,
                              retry_count, log_count, err_msg, create_time, update_time)
        VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?,?,?)
        ON CONFLICT(request_id, node_id) DO UPDATE SET
            status=excluded.status,
            decision=excluded.decision,
            confidence=excluded.confidence,
            remark=excluded.remark,
            attach_count=excluded.attach_count,
            folder=excluded.folder,
            log_count=excluded.log_count,
            err_msg=excluded.err_msg,
            update_time=excluded.update_time
        """,
        (request_id, node_id, workflow_id, request_name, status, decision,
         confidence, remark, attach_count, folder, log_count, err_msg, now, now),
    )
    conn.commit()


def bump_retry(request_id: str, node_id: str, err: str = "") -> int:
    conn = _conn()
    conn.execute(
        "UPDATE approval SET retry_count=retry_count+1, err_msg=?, update_time=? "
        "WHERE request_id=? AND node_id=?",
        (err[:900], _now(), request_id, node_id),
    )
    conn.commit()
    rec = get(request_id, node_id)
    return (rec or {}).get("retry_count", 0)


def stats() -> dict:
    cur = _conn().execute("SELECT status, COUNT(*) FROM approval GROUP BY status")
    return dict(cur.fetchall())
