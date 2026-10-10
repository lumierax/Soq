"""Small durable, single-process ledger. No credentials are ever persisted."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


def now_ms() -> int:
    return int(time.time() * 1000)


class Ledger:
    def __init__(self, path: str):
        if path != ':memory:':
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(Path(path).expanduser()) if path != ':memory:' else path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS positions (
                id TEXT PRIMARY KEY, mode TEXT NOT NULL, symbol TEXT NOT NULL,
                state TEXT NOT NULL, opened_at INTEGER NOT NULL, closed_at INTEGER,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS intents (
                client_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                state TEXT NOT NULL, payload TEXT NOT NULL, time INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS candles (
                mode TEXT NOT NULL, symbol TEXT NOT NULL, candle INTEGER NOT NULL,
                PRIMARY KEY (mode, symbol, candle)
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, time INTEGER NOT NULL,
                type TEXT NOT NULL, message TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cashflows (
                ref TEXT PRIMARY KEY, mode TEXT NOT NULL, time INTEGER NOT NULL,
                kind TEXT NOT NULL, amount REAL NOT NULL
            );
        ''')
        self.db.commit()

    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row['value']) if row else default

    def set(self, key: str, value: Any) -> None:
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, json.dumps(value, allow_nan=False)))

    def save_position(self, position: dict) -> None:
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?,?)', (
                position['id'], position['mode'], position['symbol'], position['state'],
                position['openedAt'], position.get('closedAt'), json.dumps(position, allow_nan=False),
            ))

    def positions(self, *, active: bool = True, mode: str | None = None) -> list[dict]:
        sql = 'SELECT payload FROM positions WHERE state ' + ("!= 'closed'" if active else "= 'closed'")
        args = []
        if mode:
            sql += ' AND mode=?'
            args.append(mode)
        sql += ' ORDER BY opened_at DESC'
        return [json.loads(row['payload']) for row in self.db.execute(sql, args).fetchall()]

    def intent(self, client_id: str, kind: str, payload: dict, state: str = 'pending') -> None:
        with self.db:
            self.db.execute('INSERT INTO intents VALUES (?,?,?,?,?)',
                            (client_id, kind, state, json.dumps(payload, allow_nan=False), now_ms()))

    def complete_intent(self, client_id: str, state: str = 'confirmed') -> None:
        with self.db:
            self.db.execute('UPDATE intents SET state=? WHERE client_id=?', (state, client_id))

    def pending_intents(self) -> list[dict]:
        return [dict(row, payload=json.loads(row['payload'])) for row in
                self.db.execute("SELECT * FROM intents WHERE state='pending'").fetchall()]

    def get_intent(self, client_id: str) -> dict | None:
        row = self.db.execute('SELECT * FROM intents WHERE client_id=?', (client_id,)).fetchone()
        return dict(row, payload=json.loads(row['payload'])) if row else None

    def consume_candle(self, mode: str, symbol: str, candle: int) -> bool:
        with self.db:
            cursor = self.db.execute('INSERT OR IGNORE INTO candles VALUES (?,?,?)', (mode, symbol, candle))
        return cursor.rowcount == 1

    def candle_used(self, mode: str, symbol: str, candle: int) -> bool:
        return self.db.execute('SELECT 1 FROM candles WHERE mode=? AND symbol=? AND candle=?',
                               (mode, symbol, candle)).fetchone() is not None

    def event(self, kind: str, message: str) -> None:
        with self.db:
            self.db.execute('INSERT INTO events(time,type,message) VALUES (?,?,?)', (now_ms(), kind, message))

    def events(self, limit: int = 30) -> list[dict]:
        return [dict(row) for row in self.db.execute('SELECT * FROM events ORDER BY id DESC LIMIT ?', (limit,))]

    def cashflow(self, ref: str, mode: str, kind: str, amount: float, at: int | None = None) -> None:
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO cashflows VALUES (?,?,?,?,?)',
                            (ref, mode, at or now_ms(), kind, amount))

    def cash_total(self, mode: str, since: int = 0) -> float:
        return float(self.db.execute('SELECT COALESCE(SUM(amount),0) AS value FROM cashflows WHERE mode=? AND time>=?',
                                     (mode, since)).fetchone()['value'])

    def close(self) -> None:
        self.db.close()
