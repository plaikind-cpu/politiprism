import os
import re
import json
import anthropic
from models import get_db
from ingestion import brave_news_search

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
MODEL = "claude-sonnet-4-20250514"

# ── Helpers ───────────────────────────────────────────────────────────────────

def call_claude(prompt, max_tokens=800):
    resp = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = resp.content[0].text.strip()
    return raw.replace("```json", "").replace("```", "").strip()

def fingerprint(text):
    """Normalize a claim string for deduplication."""
    return re.sub(r'[^a-z0-9 ]', '', text.lower().strip())

# ── Change 1: Groundability pre-filter ───────────────────────────────────────

def is_groundable_source(source_text, source_type):
    """
    Returns True only if the source contains direct verbatim quotes
    from the politician. Skips paraphrased news articles.
    """
    # WH transcripts and Truth Social are always direct quotes
    if source_type in ("wh_transcript", "truth_social"):
        return True

    prompt = f"""You are evaluating whether a text source contains direct, verbatim or 
near-verbatim quotations attributed to Donald Trump — words he actually spoke or wrote.

Source type: {source_type}
Source text: {source_text[:3000]}

Return JSON only:
{{
  "contains_direct_quotes": true or false,
  "confidence": "high" or "medium" or "low",
  "reason": "one sentence explanation"
}}

Rules:
- News articles that only paraphrase or summarize do NOT contain direct quotes.
- News articles with a blockquoted or quoted passage DO contain direct quotes.
- If confidence is "low", treat as false."""

    try:
        raw = call_claude(prompt, max_tokens=200)
        data = json.loads(raw)
        if data.get("confidence") == "low":
            return False
        return data.get("contains_direct_quotes", False)
    except Exception as e:
        print(f"    Groundability check error: {e}")
        return False  # conservative — skip on error

# ── Change 2a: Quote extraction ───────────────────────────────────────────────

def extract_raw_quotes(source_text, politician_name):
    """
    Step 2a: Extract only verbatim or near-verbatim quotes where the
    politician makes a specific assertion about the real world.
    """
    prompt = f"""You are extracting direct quotations from a politician's speech or writing.

Source text:
{source_text[:8000]}

Extract every discrete sentence or clause where {politician_name} makes a specific 
assertion about the real world — something that could be true or false.

Return JSON only — an array of raw quote objects:
[
  {{
    "raw_quote": "exact words from the text",
    "context": "one sentence describing what he was talking about"
  }}
]

Strict rules:
- Only include words {politician_name} actually said or wrote. No paraphrasing.
- If you cannot put quotation marks around it and source it directly to him, exclude it.
- Exclude rhetorical questions, jokes, and expressions of intent ("we're going to...").
- Exclude attacks or accusations about other named people ("Congressman X voted against Y").
- Exclude vague boasts ("we're doing great", "the best ever", "nobody has done more").
- Maximum 10 quotes per source. Choose the most specific and concrete.
- If there are no qualifying quotes, return []."""

    try:
        raw = call_claude(prompt, max_tokens=1200)
        quotes = json.loads(raw)
        if not isinstance(quotes, list):
            return []
        return quotes
    except Exception as e:
        print(f"    Quote extraction error: {e}")
        return []

# ── Change 2b: Claim scoring ──────────────────────────────────────────────────

def score_claim(raw_quote, context):
    """
    Step 2b: Score each extracted quote. Only verifiable claims with
    no reject_reason proceed to Brave Search + verdict.
    """
    prompt = f"""You are evaluating whether a politician's statement contains a verifiable factual claim.

Statement: "{raw_quote}"
Context: "{context}"

Return JSON only:
{{
  "claim_type": "historical_fact" or "statistical_claim" or "event_claim" or "opinion" or "prediction" or "vague",
  "specific_entity": "the person, place, number, or event being asserted (or null)",
  "specific_value": "the specific thing being claimed about it (or null)",
  "verifiable": true or false,
  "search_query": "a 6-10 word web search that would find confirming or refuting evidence (or null)",
  "reject_reason": null or "opinion" or "vague" or "prediction" or "third_party_accusation" or "unverifiable"
}}

Rules:
- "verifiable" is true only if a search engine could plausibly return evidence that 
  directly confirms or refutes the claim.
- If specific_entity or specific_value are null, set verifiable to false.
- opinion, vague, prediction, and third_party_accusation always get a non-null reject_reason.
- "The best ever" or "nobody has done more" = opinion unless it names a specific measurable metric.
- Statistical claims must include an actual number to be verifiable.
- "We have the best economy" = opinion, reject.
- "Unemployment is 3.9%" = statistical_claim, verifiable."""

    try:
        raw = call_claude(prompt, max_tokens=300)
        return json.loads(raw)
    except Exception as e:
        print(f"    Claim scoring error: {e}")
        return {"verifiable": False, "reject_reason": "unverifiable"}

# ── Change 3: Claim registry ──────────────────────────────────────────────────

def check_registry(fp):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM claim_registry WHERE claim_fingerprint = ?", (fp,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None

def update_registry(fp, raw_quote, search_query, verdict, summary):
    conn = get_db()
    conn.execute("""
        INSERT INTO claim_registry 
            (claim_fingerprint, raw_quote, search_query, verdict, verdict_summary)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(claim_fingerprint) DO UPDATE SET
            last_seen = date('now'),
            occurrence_count = occurrence_count + 1,
            verdict = excluded.verdict,
            verdict_summary = excluded.verdict_summary
    """, (fp, raw_quote, search_query, verdict, summary))
    conn.commit()
    conn.close()

# ── Evidence search ───────────────────────────────────────────────────────────

def search_for_claim(search_query):
    results = brave_news_search(search_query)
    return [{
        "title": r.get("title", ""),
        "url": r.get("url", ""),
        "snippet": r.get("description", "")
    } for r in results[:5]]

# ── Verdict rendering (unchanged — working well) ──────────────────────────────

def render_verdict(claim_text, politician_name, evidence):
    evidence_block = "\n".join([
        f"- [{e['title']}]({e['url']}): {e['snippet']}" for e in evidence
    ]) or "No supporting evidence found."

    prompt = f"""You are a rigorous, nonpartisan fact-checker.

{politician_name} stated: "{claim_text}"

Evaluate whether this factual assertion is accurate.
Do NOT evaluate whether {politician_name} said it — assume they did.
Focus ONLY on whether the underlying facts are correct.

Evidence:
{evidence_block}

Respond ONLY with this JSON:
{{
  "verdict": "TRUE" or "FALSE" or "MISLEADING" or "UNVERIFIABLE",
  "confidence": "HIGH" or "MEDIUM" or "LOW",
  "explanation": "2-3 sentences on whether the factual claim is accurate, citing specific evidence.",
  "citations": [{{"title": "...", "url": "...", "snippet": "..."}}]
}}

Citation rules:
- Only include sources that directly address the factual claim
- Do NOT include sources merely because they mention {politician_name}
- If no source directly addresses the claim, return empty citations array

Verdict definitions:
- TRUE = evidence confirms the assertion
- FALSE = evidence contradicts the assertion
- MISLEADING = technically accurate but missing critical context
- UNVERIFIABLE = insufficient independent evidence"""

    try:
        raw = call_claude(prompt, max_tokens=600)
        return json.loads(raw)
    except Exception as e:
        print(f"    Verdict error: {e}")
        return {"verdict": "UNVERIFIABLE", "confidence": "LOW",
                "explanation": "Could not complete fact-check.", "citations": []}

# ── Full pipeline for one statement ──────────────────────────────────────────

def process_statement(statement, date_str):
    # Convert sqlite3.Row to plain dict so .get() works throughout
    statement = dict(statement)
    statement_id   = statement["id"]
    politician_id  = statement["politician_id"]
    politician_name = statement["politician_name"]
    raw_text       = statement["raw_text"]
    source_url     = statement.get("source_url", "") or ""

    # Determine source type for groundability check
    if "whitehouse.gov" in source_url:
        source_type = "wh_transcript"
    elif "trumpstruth.org" in source_url or "Truth Social" in raw_text:
        source_type = "truth_social"
    else:
        source_type = "news_article"

    # Change 1: Groundability pre-filter
    if not is_groundable_source(raw_text, source_type):
        print(f"  [SKIP non-grounded source] {source_url[:60]}")
        return 0

    # Change 2a: Extract raw quotes
    print(f"  Extracting quotes from statement {statement_id} ({source_type})...")
    quotes = extract_raw_quotes(raw_text, politician_name)
    print(f"  Found {len(quotes)} raw quotes")

    conn = get_db()
    added = 0

    for q in quotes:
        raw_quote = q.get("raw_quote", "").strip()
        context   = q.get("context", "")

        if not raw_quote or len(raw_quote) < 10:
            continue

        # Change 2b: Score the claim
        score = score_claim(raw_quote, context)
        if score.get("reject_reason") or not score.get("verifiable"):
            print(f"    [REJECT {score.get('reject_reason','unverifiable')}] {raw_quote[:60]}...")
            continue

        search_query = score.get("search_query") or raw_quote[:80]
        fp = fingerprint(raw_quote)

        # Change 3: Check claim registry
        cached = check_registry(fp)
        if cached:
            print(f"    [CACHED {cached['verdict']}] {raw_quote[:60]}...")
            # Store in claims table using cached verdict
            conn.execute("""
                INSERT INTO claims
                    (statement_id, claim_text, verdict, confidence, explanation, citations, checked_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            """, (statement_id, raw_quote, cached["verdict"], "HIGH",
                  f"[Cached] {cached['verdict_summary']}", "[]"))
            conn.commit()
            update_registry(fp, raw_quote, search_query, cached["verdict"], cached["verdict_summary"])
            added += 1
            continue

        # New claim — run full pipeline
        print(f"    Checking: {raw_quote[:80]}...")
        evidence     = search_for_claim(search_query)
        verdict_data = render_verdict(raw_quote, politician_name, evidence)

        verdict  = verdict_data.get("verdict", "UNVERIFIABLE")
        summary  = verdict_data.get("explanation", "")

        conn.execute("""
            INSERT INTO claims
                (statement_id, claim_text, verdict, confidence, explanation, citations, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
        """, (statement_id, raw_quote, verdict,
              verdict_data.get("confidence", "LOW"), summary,
              json.dumps(verdict_data.get("citations", []))))
        conn.commit()

        # Register in claim registry
        update_registry(fp, raw_quote, search_query, verdict, summary)
        added += 1

    conn.close()
    print(f"  Added {added} verified claims")
    return added
