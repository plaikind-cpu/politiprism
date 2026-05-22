import os
import requests
import json
import re
from datetime import datetime, timedelta
from models import get_db

BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY")
BRAVE_NEWS_URL = "https://api.search.brave.com/res/v1/news/search"

ARTICLE_FETCH_TIMEOUT = 10
MAX_ARTICLE_CHARS = 8000  # enough for a full news article, not too large for Claude

def fetch_statements_for_politician(politician):
    """
    Search Brave News, fetch full article text for each result,
    store as statements for claim extraction.
    Returns count of new statements added.
    """
    search_terms = [t.strip() for t in politician["search_terms"].split(",")]
    added = 0

    for term in search_terms:
        results = brave_news_search(term)
        for article in results:
            url = article.get("url", "")
            title = article.get("title", "")

            if not url:
                continue

            # Skip paywalled or problematic domains
            if any(d in url for d in ["wsj.com", "ft.com", "nytimes.com/subscription"]):
                continue

            # Deduplicate by URL
            if is_duplicate_url(url, politician["id"]):
                continue

            # Try to fetch full article text
            full_text = fetch_article_text(url)

            if full_text and len(full_text) > 200:
                raw_text = full_text
            else:
                # Fall back to snippet if fetch fails
                description = article.get("description", "")
                if not description or len(description) < 40:
                    continue
                raw_text = f"{title}. {description}"

            store_statement(
                politician_id=politician["id"],
                raw_text=raw_text[:MAX_ARTICLE_CHARS],
                source_url=url,
                source_title=title
            )
            added += 1

    return added

def fetch_article_text(url):
    """
    Fetch a URL and extract readable text content.
    Returns plain text string or None on failure.
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; PolitiPrism/1.0; fact-checking bot)",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        }
        resp = requests.get(url, headers=headers, timeout=ARTICLE_FETCH_TIMEOUT,
                            allow_redirects=True)
        if resp.status_code != 200:
            return None

        html = resp.text

        # Strip scripts, styles, nav elements
        html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<nav[^>]*>.*?</nav>', '', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<header[^>]*>.*?</header>', '', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<footer[^>]*>.*?</footer>', '', html, flags=re.DOTALL | re.IGNORECASE)

        # Extract text from paragraph tags (article body)
        paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', html, re.DOTALL | re.IGNORECASE)
        text = ' '.join(paragraphs)

        # Strip remaining HTML tags
        text = re.sub(r'<[^>]+>', '', text)

        # Clean up whitespace and HTML entities
        text = re.sub(r'&amp;', '&', text)
        text = re.sub(r'&lt;', '<', text)
        text = re.sub(r'&gt;', '>', text)
        text = re.sub(r'&quot;', '"', text)
        text = re.sub(r'&#39;', "'", text)
        text = re.sub(r'&nbsp;', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()

        return text if len(text) > 100 else None

    except Exception as e:
        print(f"    Article fetch failed for {url}: {e}")
        return None

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
        "freshness": "pd",
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
        SELECT s.*, p.name as politician_name, p.id as politician_id
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
