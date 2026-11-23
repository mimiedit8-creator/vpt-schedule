import aiosqlite
from pathlib import Path
from app.config import settings

_db: aiosqlite.Connection | None = None

async def connect():
    global _db
    Path(settings.database_path).parent.mkdir(parents=True, exist_ok=True)
    _db = await aiosqlite.connect(settings.database_path)
    _db.row_factory = aiosqlite.Row
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.execute("PRAGMA synchronous=NORMAL")
    await _db.executescript('''
    CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        messaging_enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_users_username ON users(username COLLATE NOCASE);
    CREATE TABLE IF NOT EXISTS blocks(
        blocker_id INTEGER NOT NULL,
        blocked_id INTEGER NOT NULL,
        PRIMARY KEY(blocker_id, blocked_id)
    );
    CREATE TABLE IF NOT EXISTS schedule_cache(
        day TEXT PRIMARY KEY,
        body TEXT NOT NULL,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS tracks(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        number TEXT NOT NULL,
        last_status TEXT,
        last_location TEXT,
        updated_at TEXT,
        UNIQUE(user_id, number)
    );
    CREATE INDEX IF NOT EXISTS idx_tracks_user ON tracks(user_id);
    ''')
    await _db.commit()

def db():
    if _db is None:
        raise RuntimeError("Database is not connected")
    return _db

async def close():
    global _db
    if _db:
        await _db.close()
        _db = None
