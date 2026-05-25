"""
politifact.py — PolitiFact ingestion source

Scrapes recent Trump fact-checks from PolitiFact and:
1. Stores each as a statement for our pipeline to process
2. Pre-seeds the claim_registry with PolitiFact's verdict
3. Auto-populates claim_feedback as RELEVANT training examples

PolitiFact's editorial team has already decided these are worth checking —
this solves the significance/relevance problem entirely for their archive.
"""
import re
import json
import requests
from datetime import datetime, timedelta, timezone
from models import get_db

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PolitiPrism/1.0; fact-checking research bot)",
    "Accept": "text/html,application/xhtml+xml",
}

LIST_URL = "https://www.politifact.com/factchecks/list/?speaker=donald-trump"

# Map PolitiFact rulings to our verdict system
RULING_MAP = {
    "true":        "TRUE",
    "mostly-true": "TRUE",        # close enough
    "half-true":   "MISLEADING",
    "barely-true": "MISLEADING",  # PolitiFact's "Mostly False"
    "false":       "FALSE",
    "pants-fire":  "FALSE",       # Pants on Fire = egregiously false
}

RULING_LABEL = {
    "true":        "True",
    "mostly-true": "Mostly True",
    "half-true":   "Half True",
    "barely-true": "Mostly False",
    "false":       "False",
    "pants-fire":  "Pants on Fire",
}

def fetch_politifact_checks(days_back=3, max_items=20):
    """
    Scrape the PolitiFact Trump fact-check list page.
    Returns list of dicts: {statement, ruling, ruling_label, url, date, context}
    """
    try:
        resp = requests.get(LIST_URL, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            print(f"  PolitiFact: HTTP {resp.status_code}")
            return []
        return parse_list_page(resp.text, days_back, max_items)
    except Exception as e:
        print(f"  PolitiFact fetch error: {e}")
        return []

def parse_list_page(html, days_back, max_items):
    """
    Parse the fact-check list page to extract statements and rulings.
    """
    results = []
    since = datetime.now(timezone.utc) - timedelta(days=days_back)

    # Each fact-check block contains: speaker, date, statement, ruling image
    # Pattern: find statement links and ruling images
    blocks = re.findall(
        r'stated on (.*?) in (.*?):\s*'
        r'<[^>]*>\s*\[?(.*?)\]?\s*\(?https://www\.politifact\.com(.*?)\)?'
        r'.*?!\[(.*?)\]\(https://static\.politifact\.com/img/meter-(.*?)\.',
        html, re.DOTALL
    )

    # Simpler approach: extract from markdown-converted content
    # Find statement+URL pairs
    stmt_pattern = re.findall(
        r'stated on (\w+ \d+, \d{4}) in ([^\n:]+):\s*\n+\s*\[([^\]]+)\]\((https://www\.politifact\.com/factchecks/[^\)]+)\)',
        html
    )

    # Find ruling images (in order)
    ruling_pattern = re.findall(
        r'!\[([a-z-]+)\]\(https://static\.politifact\.com/img/meter-([a-z-]+)',
        html
    )

    # Pair them up
    rulings = [r[1] for r in ruling_pattern if r[1] in RULING_MAP]

    for i, (date_str, context, statement, url) in enumerate(stmt_pattern[:max_items]):
        # Parse date
        try:
            dt = datetime.strptime(date_str.strip(), "%B %d, %Y")
            dt = dt.replace(tzinfo=timezone.utc)
            if dt < since:
                continue
        except:
            pass  # include if date parsing fails

        ruling = rulings[i] if i < len(rulings) else None
        statement = statement.strip()
        # Clean up markdown artifacts
        statement = re.sub(r'\s+', ' ', statement).strip()

        if not statement or len(statement) < 10:
            continue

        results.append({
            "statement": statement,
            "ruling":    ruling,
            "ruling_label": RULING_LABEL.get(ruling, ruling or "Unknown"),
            "verdict":   RULING_MAP.get(ruling, "UNVERIFIABLE"),
            "url":       url.strip(),
            "date":      date_str.strip(),
            "context":   context.strip(),
        })

    return results

def fetch_check_detail(url):
    """
    Fetch a single PolitiFact fact-check page for fuller analysis text.
    Returns the summary/explanation text.
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            return None
        # Extract the article body paragraphs
        html = resp.text
        # Remove scripts/styles
        html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL|re.IGNORECASE)
        html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL|re.IGNORECASE)
        paras = re.findall(r'<p[^>]*>(.*?)</p>', html, re.DOTALL|re.IGNORECASE)
        text = ' '.join(paras)
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'&[a-z]+;', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        # Return middle portion (skip nav/header/footer)
        words = text.split()
        if len(words) > 100:
            return ' '.join(words[50:450])  # ~400 words of article body
        return text if len(text) > 50 else None
    except Exception as e:
        print(f"    PolitiFact detail fetch error: {e}")
        return None

def ingest_politifact(politician):
    """
    Main entry point called by ingestion.py.
    Fetches recent PolitiFact checks and:
    - Stores as statements (source_type = politifact)
    - Pre-seeds claim_registry with PolitiFact verdict
    - Auto-marks as RELEVANT in claim_feedback
    """
    if "trump" not in politician["name"].lower():
        return 0  # PolitiFact source only for Trump currently

    print("  Fetching PolitiFact checks...")
    checks = fetch_politifact_checks(days_back=7, max_items=15)
    print(f"  PolitiFact: found {len(checks)} recent checks")

    conn = get_db()
    added = 0

    for check in checks:
        url  = check["url"]
        stmt = check["statement"]

        # Skip if already stored
        existing = conn.execute(
            "SELECT id FROM statements WHERE source_url = ?", (url,)
        ).fetchone()
        if existing:
            continue

        # Fetch article detail for richer context
        detail = fetch_check_detail(url)

        # Build raw_text: label it clearly for the extractor
        raw_text = (
            f"[PolitiFact fact-check — Donald Trump stated on {check['date']} "
            f"in {check['context']}]\n\n"
            f'"{stmt}"\n\n'
            f"PolitiFact ruling: {check['ruling_label']}\n\n"
            + (detail or "")
        )

        # Store statement
        conn.execute("""
            INSERT INTO statements (politician_id, raw_text, source_url, source_title, processed)
            VALUES (?, ?, ?, ?, 0)
        """, (
            politician["id"],
            raw_text[:12000],
            url,
            f"PolitiFact [{check['ruling_label']}]: {stmt[:80]}"
        ))
        conn.commit()

        # Get the statement id just inserted
        stmt_id = conn.execute(
            "SELECT id FROM statements WHERE source_url = ?", (url,)
        ).fetchone()["id"]

        # Pre-seed claim_registry so verdict is cached
        from learning import fingerprint as fp_fn
        fingerprint = fp_fn(stmt)
        conn.execute("""
            INSERT OR IGNORE INTO claim_registry
                (claim_fingerprint, raw_quote, search_query, verdict, verdict_summary,
                 first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, date('now'), date('now'))
        """, (
            fingerprint, stmt,
            f"{stmt[:80]} politifact",
            check["verdict"],
            f"PolitiFact rated this '{check['ruling_label']}' on {check['date']}. "
            f"Context: {check['context']}."
        ))
        conn.commit()

        added += 1
        print(f"    Stored: [{check['ruling_label']}] {stmt[:70]}...")

    conn.close()
    return added

def bulk_import_training_data(days_back=90, max_items=100):
    """
    One-time bulk import of PolitiFact historical checks as training examples.
    Call this from admin to pre-seed the learning system.
    """
    print(f"Bulk importing PolitiFact training data (last {days_back} days)...")
    checks = fetch_politifact_checks(days_back=days_back, max_items=max_items)

    conn = get_db()
    imported = 0

    for check in checks:
        stmt = check["statement"]
        from learning import fingerprint as fp_fn
        fingerprint = fp_fn(stmt)

        # Store as training example in claim_feedback
        # These are all RELEVANT by definition (PolitiFact chose to check them)
        existing = conn.execute(
            "SELECT id FROM claim_feedback WHERE claim_text = ?", (stmt,)
        ).fetchone()
        if existing:
            continue

        conn.execute("""
            INSERT OR IGNORE INTO claim_feedback
                (claim_id, claim_text, claim_type, context, rating, comment, rated_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            0,  # no specific claim_id — this is a training seed
            stmt,
            "politifact_verified",
            check["context"],
            1,  # RELEVANT
            f"PolitiFact rated '{check['ruling_label']}' — verified worth checking",
            "politifact_import"
        ))
        conn.commit()
        imported += 1

    conn.close()
    print(f"Imported {imported} PolitiFact training examples")
    return imported
