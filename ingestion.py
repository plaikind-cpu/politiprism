import os
import re
import json
import requests
from datetime import datetime, timedelta, timezone
from models import get_db

from politifact import ingest_politifact

BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY")
BRAVE_NEWS_URL = "https://api.search.brave.com/res/v1/news/search"

WH_REMARKS_API  = "https://www.whitehouse.gov/wp-json/wp/v2/posts"
WH_REMARKS_SLUG = "remarks"  # category slug

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PolitiPrism/1.0; fact-checking bot)",
    "Accept": "text/html,application/xhtml+xml,application/json",
}

# ── Primary source: whitehouse.gov/remarks ────────────────────────────────────

def fetch_wh_remarks_today(politician):
    """
    Pull today's Remarks from whitehouse.gov WordPress REST API.
    Only returns posts from the last 24 hours attributed to the politician.
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    added = 0

    try:
        # Get category ID for 'remarks'
        cat_resp = requests.get(
            "https://www.whitehouse.gov/wp-json/wp/v2/categories",
            params={"slug": "remarks", "per_page": 5},
            headers=HEADERS, timeout=10
        )
        if not cat_resp.text.strip():
            print("  WH API: empty response")
            return 0
        cats = cat_resp.json()
        if not cats:
            print("  WH API: remarks category not found")
            return 0
        cat_id = cats[0]["id"]

        # Fetch recent posts in that category
        posts_resp = requests.get(
            WH_REMARKS_API,
            params={
                "categories": cat_id,
                "per_page": 20,
                "orderby": "date",
                "order": "desc",
                "after": since,
            },
            headers=HEADERS, timeout=10
        )
        posts = posts_resp.json()
        if not isinstance(posts, list):
            print(f"  WH API unexpected response: {str(posts)[:100]}")
            return 0

        pol_name = politician["name"].split()[1].lower()  # e.g. "trump", "vance", "rubio"

        for post in posts:
            title = post.get("title", {}).get("rendered", "")
            url   = post.get("link", "")

            # Only include posts mentioning this politician
            if pol_name not in title.lower() and pol_name not in url.lower():
                continue

            if is_duplicate_url(url, politician["id"]):
                continue

            # Fetch full transcript text
            content_html = post.get("content", {}).get("rendered", "")
            text = html_to_text(content_html)

            if not text or len(text) < 100:
                # Fall back to fetching the page directly
                text = fetch_page_text(url)

            if text and len(text) > 100:
                store_statement(
                    politician_id=politician["id"],
                    raw_text=text[:12000],  # full transcript, generously sized
                    source_url=url,
                    source_title=strip_html(title)
                )
                added += 1
                print(f"  WH Remarks: stored '{strip_html(title)[:70]}'")

    except Exception as e:
        print(f"  WH API error: {e}")

    return added

# ── Secondary source: Brave News (press pool quotes, interviews) ──────────────

def fetch_news_quotes(politician):
    """
    Search for direct quote articles from press pool coverage.
    Fetches full article text for each result.
    """
    # Tight queries that specifically find quote-focused articles
    name = politician["name"].split()[-1]  # Trump, Vance, Rubio
    queries = [
        f'"{name} told reporters"',
        f'"{name} said in a speech"',
        f'"{name} said at a press conference"',
    ]

    added = 0
    SKIP_DOMAINS = ["wsj.com", "ft.com", "washingtonpost.com"]

    for query in queries:
        results = brave_news_search(query)
        for article in results:
            url   = article.get("url", "")
            title = article.get("title", "")

            if not url or is_duplicate_url(url, politician["id"]):
                continue
            if any(d in url for d in SKIP_DOMAINS):
                continue

            text = fetch_page_text(url)
            if not text:
                desc = article.get("description", "")
                if len(desc) < 40:
                    continue
                text = f"{title}. {desc}"

            if len(text) > 100:
                store_statement(
                    politician_id=politician["id"],
                    raw_text=text[:12000],
                    source_url=url,
                    source_title=title
                )
                added += 1

    return added


TRUTH_SOCIAL_RSS = "https://www.trumpstruth.org/feed"

# ── Third source: Truth Social via trumpstruth.org RSS ────────────────────────

def fetch_truth_social(politician):
    """
    Pull Trump's Truth Social posts from trumpstruth.org RSS archive.
    Only applicable to Donald Trump.
    """
    if "trump" not in politician["name"].lower():
        return 0

    # Use date-filtered URL for past 24 hours
    since_date = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d")
    url = f"{TRUTH_SOCIAL_RSS}?start_date={since_date}"

    added = 0
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            print(f"  Truth Social RSS: HTTP {resp.status_code}")
            return 0

        # Parse RSS XML manually (no external library needed)
        xml = resp.text
        items = re.findall(r'<item>(.*?)</item>', xml, re.DOTALL)
        print(f"  Truth Social: found {len(items)} posts")

        for item in items:
            post_url   = re.search(r'<link>(.*?)</link>', item)
            post_title = re.search(r'<title>(.*?)</title>', item)
            post_desc  = re.search(r'<description>(.*?)</description>', item, re.DOTALL)

            post_url   = post_url.group(1).strip()   if post_url   else ""
            post_title = post_title.group(1).strip()  if post_title else ""
            post_text  = post_desc.group(1).strip()   if post_desc  else ""

            # Strip CDATA wrappers and HTML
            post_text = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', post_text, flags=re.DOTALL)
            post_text = html_to_text(post_text) or post_text

            if not post_text or len(post_text) < 20:
                continue
            if is_duplicate_url(post_url, politician["id"]):
                continue

            # Skip pure campaign attack posts — no factual claims to check
            attack_signals = ["voted against", "voted for allowing", "voted to allow",
                              "endorsed", "endorsed crooked", "RINO", "Radical Left",
                              "witch hunt", "election interference", "fake news",
                              "corrupt", "crooked", "do nothing"]
            post_lower = post_text.lower()
            if sum(1 for s in attack_signals if s.lower() in post_lower) >= 2:
                print(f"  [SKIP campaign post] {post_text[:60]}...")
                continue

            # Label it clearly as a Truth Social post
            raw_text = f"[Truth Social post by Donald Trump]\n\n{post_text}"
            store_statement(
                politician_id=politician["id"],
                raw_text=raw_text[:12000],
                source_url=post_url or url,
                source_title=f"Truth Social: {post_text[:80]}..."
            )
            added += 1

    except Exception as e:
        print(f"  Truth Social RSS error: {e}")

    return added

# ── Orchestrator ──────────────────────────────────────────────────────────────

def fetch_statements_for_politician(politician):
    """
    Four sources in priority order:
    1. PolitiFact              — editorially curated, pre-verified claims (best signal)
    2. whitehouse.gov/remarks  — official transcripts (ground truth)
    3. trumpstruth.org RSS     — Truth Social posts (direct Trump words)
    4. Brave News              — press pool quotes from interviews/gaggles
    """
    pf_count   = ingest_politifact(politician)
    wh_count   = fetch_wh_remarks_today(politician)
    ts_count   = fetch_truth_social(politician)
    news_count = fetch_news_quotes(politician)
    print(f"  Sources: {pf_count} PolitiFact + {wh_count} WH transcripts + {ts_count} Truth Social + {news_count} news articles")
    return pf_count + wh_count + ts_count + news_count

# ── Brave News helper ─────────────────────────────────────────────────────────

def brave_news_search(query):
    if not BRAVE_API_KEY:
        print("WARNING: BRAVE_API_KEY not set")
        return []
    try:
        resp = requests.get(
            BRAVE_NEWS_URL,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": BRAVE_API_KEY
            },
            params={
                "q": query,
                "count": 5,
                "freshness": "pd",
                "text_decorations": False,
                "search_lang": "en",
                "country": "US"
            },
            timeout=10
        )
        resp.raise_for_status()
        return resp.json().get("results", [])
    except Exception as e:
        print(f"Brave search error for '{query}': {e}")
        return []

# ── Page text fetcher ─────────────────────────────────────────────────────────

def fetch_page_text(url):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
        if resp.status_code != 200:
            return None
        return html_to_text(resp.text)
    except Exception as e:
        print(f"    Fetch failed {url[:60]}: {e}")
        return None

def html_to_text(html):
    """Strip HTML and return clean readable text."""
    # Remove scripts, styles, nav
    for tag in ["script", "style", "nav", "header", "footer", "aside"]:
        html = re.sub(rf'<{tag}[^>]*>.*?</{tag}>', '', html,
                      flags=re.DOTALL | re.IGNORECASE)
    # Extract paragraphs
    paras = re.findall(r'<p[^>]*>(.*?)</p>', html, re.DOTALL | re.IGNORECASE)
    text = ' '.join(paras) if paras else html
    # Strip tags and clean entities
    text = re.sub(r'<[^>]+>', '', text)
    for ent, char in [('&amp;','&'),('&lt;','<'),('&gt;','>'),
                       ('&quot;','"'),('&#39;',"'"),('&nbsp;',' ')]:
        text = text.replace(ent, char)
    text = re.sub(r'\s+', ' ', text).strip()
    return text if len(text) > 50 else None

def strip_html(text):
    return re.sub(r'<[^>]+>', '', text).strip()

# ── DB helpers ────────────────────────────────────────────────────────────────

def is_duplicate_url(url, politician_id):
    conn = get_db()
    since = (datetime.utcnow() - timedelta(days=1)).isoformat()
    row = conn.execute("""
        SELECT id FROM statements
        WHERE politician_id = ? AND source_url = ? AND fetched_at > ?
    """, (politician_id, url, since)).fetchone()
    conn.close()
    return row is not None

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
