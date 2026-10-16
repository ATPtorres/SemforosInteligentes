"""
SmartTraffic - Capa de persistencia (SQLite).
Un unico hilo de conexion protegido con un lock, ya que SQLite no se lleva
bien con escrituras concurrentes desde varios hilos.
"""

import sqlite3
import threading
import time
from typing import List, Dict, Any

_conn: sqlite3.Connection | None = None
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS telemetry (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    access    TEXT NOT NULL,
    distance  REAL,
    vehicles  INTEGER,
    light     TEXT,
    occupied  INTEGER,
    mode      TEXT
);
CREATE INDEX IF NOT EXISTS idx_telemetry_ts ON telemetry(ts);
CREATE INDEX IF NOT EXISTS idx_telemetry_access_ts ON telemetry(access, ts);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    kind    TEXT NOT NULL,
    access  TEXT,
    detail  TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


def init(path: str) -> None:
    global _conn
    with _lock:
        _conn = sqlite3.connect(path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA)
        _conn.commit()


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def _execute(sql: str, params: tuple = ()) -> None:
    with _lock:
        if _conn is None:
            return
        _conn.execute(sql, params)
        _conn.commit()


def _query(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    with _lock:
        if _conn is None:
            return []
        rows = _conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


# ------------------------------- ESCRITURAS --------------------------------

def save_telemetry(access: str, distance: float | None, vehicles: int,
                   light: str, occupied: int, mode: str) -> None:
    _execute(
        "INSERT INTO telemetry (ts, access, distance, vehicles, light, occupied, mode) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (time.time(), access, distance, vehicles, light, occupied, mode),
    )


def save_event(kind: str, access: str | None = None, detail: str = "") -> None:
    _execute(
        "INSERT INTO events (ts, kind, access, detail) VALUES (?, ?, ?, ?)",
        (time.time(), kind, access, detail),
    )


def prune(keep_seconds: int = 86400) -> None:
    """Borra datos con mas de 1 dia para que el archivo no crezca sin limite."""
    limit = time.time() - keep_seconds
    _execute("DELETE FROM telemetry WHERE ts < ?", (limit,))
    _execute("DELETE FROM events WHERE ts < ?", (limit,))


# -------------------------------- LECTURAS ---------------------------------

def recent_telemetry(limit: int = 100) -> List[Dict[str, Any]]:
    return _query(
        "SELECT ts, access, distance, vehicles, light, occupied, mode "
        "FROM telemetry ORDER BY id DESC LIMIT ?",
        (limit,),
    )


def recent_events(limit: int = 50) -> List[Dict[str, Any]]:
    return _query(
        "SELECT ts, kind, access, detail FROM events ORDER BY id DESC LIMIT ?",
        (limit,),
    )


def arrivals_in_window(window_s: float) -> Dict[str, int]:
    """Cuenta cuantos vehiculos llegaron en los ultimos `window_s` segundos."""
    rows = _query(
        "SELECT access, COUNT(*) AS n FROM telemetry "
        "WHERE ts >= ? AND occupied = 1 GROUP BY access",
        (time.time() - window_s,),
    )
    return {r["access"]: int(r["n"]) for r in rows}


def summary(window_s: float = 60.0) -> List[Dict[str, Any]]:
    """Resumen por acceso: ultima distancia, maximo vehiculos y ocupaciones."""
    since = time.time() - window_s
    rows = _query(
        "SELECT access, "
        "  COUNT(*) AS samples, "
        "  AVG(distance) AS avg_distance, "
        "  SUM(occupied) AS occupied_samples, "
        "  MAX(vehicles) AS max_vehicles, "
        "  MIN(vehicles) AS min_vehicles "
        "FROM telemetry WHERE ts >= ? GROUP BY access",
        (since,),
    )
    return rows
