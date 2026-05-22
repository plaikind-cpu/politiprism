import os
import json
import anthropic
from models import get_db
from ingestion import brave_news_search

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
MODEL = "claude-sonnet-4-20250514"

# ── Step 1: Extract discrete verifiable claims ───────────────────────────────

def extract_claims(statement_text, politician_name):
    prompt = f"""You are a strict filter extracting only fact-checkable claims from news text about {politician_name}.

A fact-checkable claim must meet ALL of these criteria:
1. Directly attributed to {politician_name} speaking or writing (quoted or paraphrased from their words)
2. A specific factual assertion about the real world — something that is objectively TRUE or FALSE
3. Checkable against independent evidence (statistics, historical facts, documented events, scientific consensus)

REJECT anything that is:
- An opinion, value judgment, or characterization ("it's a disaster", "it's a disgrace", "I'd be happy to do it")
- A future intention or promise ("I will send troops", "we're going to build")
- A hypothetical or rhetorical statement ("if Jesus was counting the votes...")
- A vague or unmeasurable assertion ("we're doing great", "the best ever")
- A statement by administration officials, aides, or spokespeople — only {politician_name} directly
- A fact ABOUT {politician_name} reported by journalists (poll numbers about them, their approval ratings, economic stats about their performance) — only facts {politician_name} themselves asserted
- Something already in the list below (avoid duplicates)

GOOD examples of checkable claims:
- "The US trade deficit with China is $500 billion" (specific measurable fact)
- "NATO members agreed to 5% GDP spending at the last summit" (documented event)
- "The 14th Amendment has guaranteed birthright citizenship since 1868" (historical fact)

BAD examples (reject these):
- "I'd be happy to do it" (intention/opinion)
- "Colbert is finally finished" (characterization)
- "We have total control" (vague assertion)
- "No American leader has done this in 50 years" (vague, hard to check)
- "He would have won California if Jesus was counting" (hypothetical/rhetorical)

Return ONLY a JSON array of claim strings — just the factual substance, no attribution prefix.
If no qualifying claims exist, return [].
No preamble, no markdown, no explanation.

Text to analyze:
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
        return [c for c in claims if isinstance(c, str) and len(c) > 15]
    except Exception as e:
        print(f"Claim extraction error: {e}")
        return []

# ── Step 1b: Deduplicate claims within today's run ───────────────────────────

def is_duplicate_claim(claim_text, politician_id, date_str):
    conn = get_db()
    existing = conn.execute("""
        SELECT c.claim_text FROM claims c
        JOIN statements s ON c.statement_id = s.id
        WHERE s.politician_id = ? AND DATE(c.checked_at) = ?
    """, (politician_id, date_str)).fetchall()
    conn.close()

    if not existing:
        return False

    claim_words = set(claim_text.lower().split())
    if not claim_words:
        return False

    for row in existing:
        ex_words = set(row["claim_text"].lower().split())
        overlap = len(claim_words & ex_words) / len(claim_words)
        if overlap > 0.70:
            return True
    return False

# ── Step 2: Search for evidence ──────────────────────────────────────────────

def search_for_claim(claim_text):
    results = brave_news_search(claim_text[:120])
    return [{
        "title": r.get("title", ""),
        "url": r.get("url", ""),
        "snippet": r.get("description", "")
    } for r in results[:5]]

# ── Step 3: Fact-check the substance ─────────────────────────────────────────

def render_verdict(claim_text, politician_name, evidence):
    evidence_block = "\n".join([
        f"- [{e['title']}]({e['url']}): {e['snippet']}" for e in evidence
    ]) or "No supporting evidence found."

    prompt = f"""You are a rigorous, nonpartisan fact-checker.

{politician_name} asserted the following factual claim:
"{claim_text}"

Your ONLY job: evaluate whether this factual assertion is accurate.
Do NOT evaluate whether {politician_name} said it — assume they did.
Do NOT comment on whether it was appropriate to say.
Focus ONLY on whether the underlying facts are correct.

Evidence from independent sources:
{evidence_block}

Respond ONLY with this JSON object:
{{
  "verdict": "TRUE" | "FALSE" | "MISLEADING" | "UNVERIFIABLE",
  "confidence": "HIGH" | "MEDIUM" | "LOW",
  "explanation": "2-3 sentences on whether the factual claim is accurate, citing specific evidence. Start with what the evidence shows, not with who said what.",
  "citations": [{{"title": "...", "url": "...", "snippet": "..."}}]
}}

Verdict definitions:
- TRUE = evidence confirms the factual assertion
- FALSE = evidence contradicts the factual assertion  
- MISLEADING = technically accurate but missing context that significantly changes the meaning
- UNVERIFIABLE = insufficient independent evidence to confirm or deny

No preamble, no markdown fences. JSON only."""

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
        conn.commit()
        added += 1

    conn.close()
    print(f"  Added {added} unique claims")
    return added
