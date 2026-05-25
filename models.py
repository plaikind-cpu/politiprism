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

    # Invited users with password auth
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT,
            token TEXT,
            token_expires TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    # Add password_hash column if upgrading existing DB
    try:
        c.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
    except:
        pass  # column already exists

    # Pipeline run status
    c.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_status (
            id INTEGER PRIMARY KEY CHECK (id = 1),  -- singleton row
            running INTEGER DEFAULT 0,
            stage TEXT DEFAULT '',
            statements_total INTEGER DEFAULT 0,
            statements_done INTEGER DEFAULT 0,
            claims_found INTEGER DEFAULT 0,
            started_at TEXT,
            finished_at TEXT
        )
    """)
    # Ensure the singleton row exists
    c.execute("INSERT OR IGNORE INTO pipeline_status (id) VALUES (1)")

    # Claim registry — deduplication and caching across runs
    c.execute("""
        CREATE TABLE IF NOT EXISTS claim_registry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_fingerprint TEXT UNIQUE,
            raw_quote TEXT,
            search_query TEXT,
            verdict TEXT,
            verdict_summary TEXT,
            first_seen DATE DEFAULT (date('now')),
            last_seen DATE DEFAULT (date('now')),
            occurrence_count INTEGER DEFAULT 1
        )
    """)

    # User feedback on claim relevance — drives learning loop
    c.execute("""
        CREATE TABLE IF NOT EXISTS claim_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id INTEGER NOT NULL,
            claim_text TEXT NOT NULL,
            claim_type TEXT,
            context TEXT,
            rating INTEGER,           -- 1=relevant, -1=not relevant, NULL=comment only
            comment TEXT,             -- editor's note explaining the rating
            sub_claim TEXT,           -- optional sub-claim to queue for checking
            rated_by TEXT,
            rated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (claim_id) REFERENCES claims(id)
        )
    """)
    # Migrate claim_feedback — remove UNIQUE constraint on claim_id, use claim_text instead
    try:
        # Check if old constraint exists by trying to insert two rows with same claim_id
        c.execute("INSERT INTO claim_feedback (claim_id, claim_text, rating) VALUES (0, '__test1__', 1)")
        c.execute("INSERT INTO claim_feedback (claim_id, claim_text, rating) VALUES (0, '__test2__', 1)")
        c.execute("DELETE FROM claim_feedback WHERE claim_text IN ('__test1__', '__test2__')")
        # If we got here, no UNIQUE on claim_id — good
    except Exception:
        # UNIQUE constraint on claim_id exists — migrate the table
        print("Migrating claim_feedback table to remove claim_id UNIQUE constraint...")
        c.execute("ALTER TABLE claim_feedback RENAME TO claim_feedback_old")
        c.execute("""
            CREATE TABLE claim_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                claim_id INTEGER NOT NULL,
                claim_text TEXT NOT NULL UNIQUE,
                claim_type TEXT,
                context TEXT,
                rating INTEGER,
                comment TEXT,
                sub_claim TEXT,
                rated_by TEXT,
                rated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        c.execute("""
            INSERT OR IGNORE INTO claim_feedback
                (claim_id, claim_text, claim_type, context, rating, comment, sub_claim, rated_by, rated_at)
            SELECT claim_id, claim_text, claim_type, context, rating, comment, sub_claim, rated_by, rated_at
            FROM claim_feedback_old
        """)
        c.execute("DROP TABLE claim_feedback_old")
        print("Migration complete.")

    # Ensure unique index on claim_text
    try:
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_claim_text ON claim_feedback(claim_text)")
    except:
        pass

    # Add comment/sub_claim columns if upgrading
    for col in ["comment TEXT", "sub_claim TEXT"]:
        try:
            c.execute(f"ALTER TABLE claim_feedback ADD COLUMN {col}")
        except:
            pass
    # Sub-claims queued by editor for fact-checking
    c.execute("""
        CREATE TABLE IF NOT EXISTS sub_claim_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_claim_id INTEGER NOT NULL,
            sub_claim_text TEXT NOT NULL,
            source_url TEXT,
            source_title TEXT,
            politician_id INTEGER,
            status TEXT DEFAULT 'pending',  -- pending / checked
            queued_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (parent_claim_id) REFERENCES claims(id)
        )
    """)

    # Add significance columns to claims if upgrading
    for col in ["significance", "significance_reason", "user_rating"]:
        try:
            c.execute(f"ALTER TABLE claims ADD COLUMN {col} TEXT")
        except:
            pass

    # Seed default politicians if table is empty
    c.execute("SELECT COUNT(*) FROM politicians")
    if c.fetchone()[0] == 0:
        seed_politicians(c)

    conn.commit()
    conn.close()

def seed_politicians(c):
    # search_terms used only for Brave News secondary source
    defaults = [
        ("Donald Trump", "President", "Trump told reporters,Trump said in a speech,Trump said at a press conference"),
        ("JD Vance", "Vice President", "Vance told reporters,Vance said in a speech,Vance said at a press conference"),
        ("Marco Rubio", "Secretary of State", "Rubio told reporters,Rubio said in a speech,Rubio said at a press conference"),
    ]
    for name, role, terms in defaults:
        c.execute(
            "INSERT INTO politicians (name, role, search_terms) VALUES (?, ?, ?)",
            (name, role, terms)
        )
