import sqlite3
import os

DB_PATH = os.environ.get("DB_PATH", "/tmp/politiprism.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()

    # Politicians being tracked
    c.execute("""
        CREATE TABLE IF NOT EXISTS politicians (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            role TEXT,
            search_terms TEXT NOT NULL,  -- comma-separated search queries
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # Raw statements fetched from news sources
    c.execute("""
        CREATE TABLE IF NOT EXISTS statements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            politician_id INTEGER NOT NULL,
            raw_text TEXT NOT NULL,
            source_url TEXT,
            source_title TEXT,
            fetched_at TEXT DEFAULT (datetime('now')),
            processed INTEGER DEFAULT 0,
            FOREIGN KEY (politician_id) REFERENCES politicians(id)
        )
    """)

    # Discrete claims extracted from each statement
    c.execute("""
        CREATE TABLE IF NOT EXISTS claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            statement_id INTEGER NOT NULL,
            claim_text TEXT NOT NULL,
            verdict TEXT,           -- TRUE / FALSE / MISLEADING / UNVERIFIABLE
            confidence TEXT,        -- HIGH / MEDIUM / LOW
            explanation TEXT,
            citations TEXT,         -- JSON array of {url, title, snippet}
            checked_at TEXT,
            FOREIGN KEY (statement_id) REFERENCES statements(id)
        )
    """)

    # Invited users (magic-link auth)
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            token TEXT,
            token_expires TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # Seed default politicians if table is empty
    c.execute("SELECT COUNT(*) FROM politicians")
    if c.fetchone()[0] == 0:
        seed_politicians(c)

    conn.commit()
    conn.close()

def seed_politicians(c):
    defaults = [
        ("Donald Trump", "President", "Trump said,Trump claims,Trump announced,Trump declared"),
        ("JD Vance", "Vice President", "Vance said,Vance claims,Vance announced"),
        ("Marco Rubio", "Secretary of State", "Rubio said,Rubio claims,Rubio announced"),
    ]
    for name, role, terms in defaults:
        c.execute(
            "INSERT INTO politicians (name, role, search_terms) VALUES (?, ?, ?)",
            (name, role, terms)
        )
