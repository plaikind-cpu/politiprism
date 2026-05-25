"""
politifact.py — PolitiFact ingestion source

Fetches Trump fact-checks by scraping the ruling-specific list pages
which render statements in plain text without requiring JavaScript.
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

# Scrape each ruling type separately — these pages render content server-side
RULING_URLS = [
    ("false",       "FALSE",       "https://www.politifact.com/factchecks/list/?ruling=false&speaker=donald-trump"),
    ("pants-fire",  "FALSE",       "https://www.politifact.com/factchecks/list/?ruling=pants-fire&speaker=donald-trump"),
    ("barely-true", "MISLEADING",  "https://www.politifact.com/factchecks/list/?ruling=barely-true&speaker=donald-trump"),
    ("half-true",   "MISLEADING",  "https://www.politifact.com/factchecks/list/?ruling=half-true&speaker=donald-trump"),
    ("mostly-true", "TRUE",        "https://www.politifact.com/factchecks/list/?ruling=mostly-true&speaker=donald-trump"),
    ("true",        "TRUE",        "https://www.politifact.com/factchecks/list/?ruling=true&speaker=donald-trump"),
]

RULING_LABEL = {
    "false":       "False",
    "pants-fire":  "Pants on Fire",
    "barely-true": "Mostly False",
    "half-true":   "Half True",
    "mostly-true": "Mostly True",
    "true":        "True",
}

def fetch_ruling_page(url):
    """Fetch a PolitiFact ruling-filtered page and extract statements."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return []
        return parse_statements(resp.text)
    except Exception as e:
        print(f"    PolitiFact fetch error: {e}")
        return []

def parse_statements(html):
    """
    Extract statements from a PolitiFact list page.
    Pattern: 'stated on DATE in CONTEXT: "STATEMENT"'
    """
    results = []

    # Remove scripts and styles first
    html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL|re.IGNORECASE)
    html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL|re.IGNORECASE)

    # Extract all text
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'&quot;', '"', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&#x27;', "'", text)
    text = re.sub(r'&[a-z]+;', ' ', text)
    text = re.sub(r'\s+', ' ', text)

    # Find "stated on DATE in CONTEXT: STATEMENT" patterns
    pattern = re.findall(
        r'stated on ([A-Z][a-z]+ \d+, \d{4}) in ([^:]{5,80}):\s*["\u201c]([^"\u201d]{20,300})["\u201d]',
        text
    )

    for date_str, context, statement in pattern:
        statement = statement.strip()
        context   = context.strip()
        if len(statement) < 15:
            continue
        results.append({
            "date":    date_str,
            "context": context,
            "statement": statement,
        })

    # Also try without quotes (some statements aren't quoted)
    pattern2 = re.findall(
        r'stated on ([A-Z][a-z]+ \d+, \d{4}) in ([^:\n]{5,80}):\s+([A-Z][^\.]{20,250}\.)',
        text
    )
    seen = {r["statement"] for r in results}
    for date_str, context, statement in pattern2:
        statement = statement.strip()
        if statement not in seen and len(statement) > 20:
            results.append({
                "date":    date_str,
                "context": context.strip(),
                "statement": statement,
            })
            seen.add(statement)

    return results[:20]  # cap per ruling type

def fetch_all_checks(days_back=90, max_per_ruling=15):
    """
    Fetch fact-checks across all ruling types.
    Returns list of dicts with statement, verdict, ruling_label, date, context.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days_back)
    all_checks = []
    seen_statements = set()

    for ruling, verdict, url in RULING_URLS:
        label = RULING_LABEL[ruling]
        print(f"    Fetching PolitiFact [{label}]...")
        items = fetch_ruling_page(url)

        for item in items[:max_per_ruling]:
            stmt = item["statement"]
            if stmt in seen_statements:
                continue

            # Parse date for filtering
            try:
                dt = datetime.strptime(item["date"], "%B %d, %Y").replace(tzinfo=timezone.utc)
                if dt < since:
                    continue
            except:
                pass  # include if date parse fails

            seen_statements.add(stmt)
            all_checks.append({
                "statement":    stmt,
                "ruling":       ruling,
                "ruling_label": label,
                "verdict":      verdict,
                "date":         item["date"],
                "context":      item["context"],
                "url":          url,
            })

    print(f"    PolitiFact: parsed {len(all_checks)} total checks")
    return all_checks

def ingest_politifact(politician):
    """
    Daily ingestion — fetch recent PolitiFact checks and store as statements.
    Pre-seeds claim_registry with PolitiFact verdicts.
    """
    if "trump" not in politician["name"].lower():
        return 0

    print("  Fetching PolitiFact checks (last 7 days)...")
    checks = fetch_all_checks(days_back=7, max_per_ruling=5)
    print(f"  PolitiFact daily: {len(checks)} checks found")

    return _store_checks(checks, politician)

def bulk_import_training_data(days_back=90, max_items=100):
    """
    One-time bulk import of PolitiFact historical checks as training examples.
    """
    print(f"Bulk importing PolitiFact training data (last {days_back} days)...")
    checks = fetch_all_checks(days_back=days_back, max_per_ruling=20)
    print(f"Found {len(checks)} checks to import")

    conn = get_db()
    imported = 0

    for check in checks[:max_items]:
        stmt = check["statement"]

        # Store as training example in claim_feedback
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
            0,
            stmt,
            "politifact_verified",
            check["context"],
            1,  # RELEVANT — PolitiFact chose to check it
            f"PolitiFact rated '{check['ruling_label']}' on {check['date']} in {check['context']}",
            "politifact_import"
        ))
        conn.commit()

        # Also pre-seed claim_registry
        from learning import fingerprint as fp_fn
        fp = fp_fn(stmt)
        conn.execute("""
            INSERT OR IGNORE INTO claim_registry
                (claim_fingerprint, raw_quote, search_query, verdict, verdict_summary)
            VALUES (?, ?, ?, ?, ?)
        """, (
            fp, stmt,
            stmt[:80],
            check["verdict"],
            f"PolitiFact rated this '{check['ruling_label']}' ({check['date']})"
        ))
        conn.commit()
        imported += 1

    conn.close()
    print(f"Imported {imported} PolitiFact training examples")
    return imported

def _store_checks(checks, politician):
    """Store PolitiFact checks as statements for pipeline processing."""
    conn = get_db()
    added = 0

    for check in checks:
        url  = check["url"] + "#" + re.sub(r'[^a-z0-9]', '-', check["statement"][:40].lower())
        stmt = check["statement"]

        existing = conn.execute(
            "SELECT id FROM statements WHERE source_url = ?", (url,)
        ).fetchone()
        if existing:
            continue

        raw_text = (
            f"[PolitiFact fact-check — Donald Trump stated on {check['date']} "
            f"in {check['context']}]\n\n"
            f'"{stmt}"\n\n'
            f"PolitiFact ruling: {check['ruling_label']}"
        )

        conn.execute("""
            INSERT INTO statements (politician_id, raw_text, source_url, source_title, processed)
            VALUES (?, ?, ?, ?, 0)
        """, (
            politician["id"],
            raw_text[:8000],
            url,
            f"PolitiFact [{check['ruling_label']}]: {stmt[:80]}"
        ))
        conn.commit()

        # Pre-seed claim_registry
        from learning import fingerprint as fp_fn
        fp = fp_fn(stmt)
        conn.execute("""
            INSERT OR IGNORE INTO claim_registry
                (claim_fingerprint, raw_quote, search_query, verdict, verdict_summary)
            VALUES (?, ?, ?, ?, ?)
        """, (
            fp, stmt, stmt[:80], check["verdict"],
            f"PolitiFact rated '{check['ruling_label']}' on {check['date']}"
        ))
        conn.commit()
        added += 1

    conn.close()
    return added
