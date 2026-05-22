import os
import requests
import json
from datetime import datetime, timedelta
from models import get_db

BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY")
BRAVE_NEWS_URL = "https://api.search.brave.com/res/v1/news/search"

def fetch_statements_for_politician(politician):
    """
    Search Brave News for each of a politician's search terms.
    Store new (non-duplicate) statements in the DB.
    Returns count of new statements added.
    """
    search_terms = [t.strip() for t in politician["search_terms"].split(",")]
    added = 0

    for term in search_terms:
        results = brave_news_search(term)
        for article in results:
            url = article.get("url", "")
            title = article.get("title", "")
            description = article.get("description", "")

            if not description or len(description) < 40:
                continue

            # Deduplicate by source URL
            if not is_duplicate_url(url, politician["id"]):
                store_statement(
                    politician_id=politician["id"],
                    raw_text=f"{title}. {description}",
                    source_url=url,
                    source_title=title
                )
                added += 1

    return added

def brave_news_search(query):
    """Call Brave News Search API and return article list."""
    if not BRAVE_API_KEY:
        print("WARNING: BRAVE_API_KEY not set")
        return []

    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": BRAVE_API_KEY
    }
    params = {
        "q": query,
        "count": 10,
        "freshness": "pd",  # past day
        "text_decorations": False,
        "search_lang": "en",
        "country": "US"
    }

    try:
        resp = requests.get(BRAVE_NEWS_URL, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data.get("results", [])
    except Exception as e:
        print(f"Brave search error for '{query}': {e}")
        return []

def is_duplicate_url(url, politician_id):
    """Check if we already stored this article for this politician today."""
    conn = get_db()
    c = conn.cursor()
    since = (datetime.utcnow() - timedelta(days=1)).isoformat()
    c.execute("""
        SELECT id FROM statements
        WHERE politician_id = ? AND source_url = ? AND fetched_at > ?
    """, (politician_id, url, since))
    result = c.fetchone()
    conn.close()
    return result is not None

def store_statement(politician_id, raw_text, source_url, source_title):
    conn = get_db()
    conn.execute("""
        INSERT INTO statements (politician_id, raw_text, source_url, source_title)
        VALUES (?, ?, ?, ?)
    """, (politician_id, raw_text, source_url, source_title))
    conn.commit()
    conn.close()

def get_unprocessed_statements():
    conn = get_db()
    rows = conn.execute("""
        SELECT s.*, p.name as politician_name
        FROM statements s
        JOIN politicians p ON s.politician_id = p.id
        WHERE s.processed = 0
        ORDER BY s.fetched_at DESC
    """).fetchall()
    conn.close()
    return rows

def mark_statement_processed(statement_id):
    conn = get_db()
    conn.execute("UPDATE statements SET processed = 1 WHERE id = ?", (statement_id,))
    conn.commit()
    conn.close()
