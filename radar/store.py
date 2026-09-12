"""SQLite 상태 저장 — 가격 스냅샷(단기 변동률 계산용) + 알림 중복 방지."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .config import DATA_DIR

DB_PATH: Path = DATA_DIR / "radar.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS px (
    ts     INTEGER NOT NULL,
    market TEXT    NOT NULL,
    symbol TEXT    NOT NULL,
    price  REAL    NOT NULL,
    qvol   REAL,
    PRIMARY KEY (ts, market, symbol)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS px_sym ON px(market, symbol, ts);

CREATE TABLE IF NOT EXISTS alerts (
    kind   TEXT    NOT NULL,
    key    TEXT    NOT NULL,
    ts     INTEGER NOT NULL,
    payload TEXT,
    PRIMARY KEY (kind, key)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS runs (
    ts     INTEGER PRIMARY KEY,
    note   TEXT
);
"""


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(_SCHEMA)
    return con


def save_snapshot(con: sqlite3.Connection, market: str,
                  rows: list[tuple[str, float, float | None]], ts: int | None = None) -> int:
    ts = ts or int(time.time())
    con.executemany(
        "INSERT OR REPLACE INTO px(ts, market, symbol, price, qvol) VALUES (?,?,?,?,?)",
        [(ts, market, s, p, q) for s, p, q in rows],
    )
    con.commit()
    return ts


def prune(con: sqlite3.Connection, keep_hours: int = 8) -> None:
    cutoff = int(time.time()) - keep_hours * 3600
    con.execute("DELETE FROM px WHERE ts < ?", (cutoff,))
    con.execute("DELETE FROM alerts WHERE ts < ?", (int(time.time()) - 30 * 86400,))
    con.commit()


def price_at(con: sqlite3.Connection, market: str, minutes_ago: int,
             tolerance_min: int = 6) -> dict[str, tuple[float, float | None]]:
    """minutes_ago 분 전에 가장 가까운 스냅샷을 통째로 가져온다. {symbol: (price, qvol)}"""
    target = int(time.time()) - minutes_ago * 60
    row = con.execute(
        "SELECT ts FROM px WHERE market=? AND ts<=? ORDER BY ts DESC LIMIT 1", (market, target)
    ).fetchone()
    if not row:
        return {}
    ts = row[0]
    if abs(ts - target) > tolerance_min * 60 + 60:
        return {}
    cur = con.execute("SELECT symbol, price, qvol FROM px WHERE market=? AND ts=?", (market, ts))
    return {s: (p, q) for s, p, q in cur.fetchall()}


def recently_alerted(con: sqlite3.Connection, kind: str, key: str, cooldown_min: int) -> bool:
    row = con.execute("SELECT ts FROM alerts WHERE kind=? AND key=?", (kind, key)).fetchone()
    return bool(row) and (int(time.time()) - row[0]) < cooldown_min * 60


def mark_alerted(con: sqlite3.Connection, kind: str, key: str, payload: str = "") -> None:
    con.execute(
        "INSERT OR REPLACE INTO alerts(kind, key, ts, payload) VALUES (?,?,?,?)",
        (kind, key, int(time.time()), payload[:2000]),
    )
    con.commit()


def known_keys(con: sqlite3.Connection, kind: str, within_hours: int) -> set[str]:
    cutoff = int(time.time()) - within_hours * 3600
    return {r[0] for r in con.execute(
        "SELECT key FROM alerts WHERE kind=? AND ts>=?", (kind, cutoff)).fetchall()}
