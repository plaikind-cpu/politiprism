import os
import json
import anthropic
from models import get_db
from ingestion import brave_news_search

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
MODEL = "claude-sonnet-4-20250514"

# ── Step 1: Extract discrete verifiable claims from a statement ──────────────

def extract_claims(statement_text, politician_name):
    prompt = f"""You are analyzing a news excerpt for direct spoken statements by {politician_name}.

Your task: extract only verifiable factual claims that {politician_name} personally SAID, STATED, or CLAIMED out loud — in a speech, interview, press conference, social media post, or direct quote.

STRICT RULES:
- Only include claims where the text explicitly attributes the words to {politician_name} speaking (e.g. "Trump said...", "Trump claimed...", "Trump told reporters...", "Trump posted...", "according to Trump...")
- Do NOT include actions taken by the administration, policy changes, or things that happened TO {politician_name}
- Do NOT include statements by aides, officials, or spokespeople on behalf of {politician_name}
- Do NOT include reporter paraphrases of general policy — only direct attribution of spoken/written words
- Each claim must be a specific, checkable factual assertion (not an opinion, prediction, or value judgment)
- Strip out the attribution prefix — return only the substance of the claim itself
- If no qualifying direct statements exist in the text, return an empty array []

Return ONLY a JSON array of claim strings. No preamble, no markdown, no explanation.
Example: ["The trade deficit has been cut by half", "Tariffs have brought in $200 billion in revenue"]

Text:
{statement_text}"""

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        claims = json.loads(raw)
        return [c for c in claims if isinstance(c, str) and len(c) > 10]
    except Exception as e:
        print(f"Claim extraction error: {e}")
        return []

# ── Step 1b: Deduplicate claims against those already checked today ───────────

def is_duplicate_claim(claim_text, politician_id, date_str):
    """Return True if a semantically similar claim was already checked today."""
    conn = get_db()
    existing = conn.execute("""
        SELECT c.claim_text FROM claims c
        JOIN statements s ON c.statement_id = s.id
        WHERE s.politician_id = ? AND DATE(c.checked_at) = ?
    """, (politician_id, date_str)).fetchall()
    conn.close()

    if not existing:
        return False

    existing_texts = [r["claim_text"].lower().strip() for r in existing]
    claim_lower = claim_text.lower().strip()

    # Exact or near-exact match (handles minor phrasing variants)
    for ex in existing_texts:
        # Check significant word overlap (>70% of words shared)
        claim_words = set(claim_lower.split())
        ex_words = set(ex.split())
        if len(claim_words) == 0:
            continue
        overlap = len(claim_words & ex_words) / len(claim_words)
        if overlap > 0.70:
            return True
    return False

# ── Step 2: Search for evidence about the claim ──────────────────────────────

def search_for_claim(claim_text):
    results = brave_news_search(claim_text[:120])
    snippets = []
    for r in results[:5]:
        snippets.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("description", "")
        })
    return snippets

# ── Step 3: Fact-check the substance of the claim ───────────────────────────

def render_verdict(claim_text, politician_name, evidence):
    evidence_block = "\n".join([
        f"- [{e['title']}]({e['url']}): {e['snippet']}" for e in evidence
    ])

    prompt = f"""You are a rigorous, nonpartisan fact-checker evaluating whether a claim is factually accurate.

{politician_name} made the following claim:
"{claim_text}"

Your job is to evaluate whether the SUBSTANCE of this claim is TRUE or FALSE — not whether {politician_name} said it (assume they did). Focus entirely on whether the factual assertion itself is accurate.

Evidence from independent news sources:
{evidence_block if evidence_block else "No supporting evidence found."}

Respond ONLY with a JSON object in this exact format:
{{
  "verdict": "TRUE" | "FALSE" | "MISLEADING" | "UNVERIFIABLE",
  "confidence": "HIGH" | "MEDIUM" | "LOW",
  "explanation": "2-3 sentences evaluating the factual accuracy of the claim itself, citing specific evidence that supports or contradicts it",
  "citations": [
    {{"title": "...", "url": "...", "snippet": "..."}}
  ]
}}

Verdict definitions:
- TRUE = the factual assertion is accurate based on available evidence
- FALSE = the factual assertion contradicts available evidence
- MISLEADING = technically accurate but omits critical context that changes its meaning
- UNVERIFIABLE = insufficient independent evidence to confirm or deny the factual claim

Rules:
- Never evaluate whether {politician_name} made the statement — assume they did
- Be strictly factual. No political opinion.
- Citations must only come from the evidence provided above
- No preamble, no markdown fences, just the JSON object"""

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(raw)
    except Exception as e:
        print(f"Verdict error: {e}")
        return {
            "verdict": "UNVERIFIABLE",
            "confidence": "LOW",
            "explanation": "Could not complete fact-check due to an error.",
            "citations": []
        }

# ── Step 4: Full pipeline for one statement ──────────────────────────────────

def process_statement(statement, date_str):
    from datetime import datetime
    statement_id = statement["id"]
    politician_id = statement["politician_id"]
    politician_name = statement["politician_name"]
    raw_text = statement["raw_text"]

    print(f"  Extracting claims from statement {statement_id}...")
    claims = extract_claims(raw_text, politician_name)
    print(f"  Found {len(claims)} claims")

    conn = get_db()
    added = 0
    for claim_text in claims:
        if is_duplicate_claim(claim_text, politician_id, date_str):
            print(f"    [SKIP duplicate] {claim_text[:60]}...")
            continue

        print(f"    Checking: {claim_text[:80]}...")
        evidence = search_for_claim(claim_text)
        verdict_data = render_verdict(claim_text, politician_name, evidence)

        conn.execute("""
            INSERT INTO claims
                (statement_id, claim_text, verdict, confidence, explanation, citations, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
        """, (
            statement_id,
            claim_text,
            verdict_data.get("verdict", "UNVERIFIABLE"),
            verdict_data.get("confidence", "LOW"),
            verdict_data.get("explanation", ""),
            json.dumps(verdict_data.get("citations", []))
        ))
        conn.commit()  # Commit after each claim so dedup sees it immediately
        added += 1

    conn.close()
    print(f"  Added {added} unique claims")
