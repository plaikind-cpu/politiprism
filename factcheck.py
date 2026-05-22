import os
import json
import requests
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
- Each claim must be a specific, checkable factual assertion (not an opinion or prediction)
- If no qualifying direct statements exist in the text, return an empty array []

Return ONLY a JSON array of claim strings. No preamble, no markdown, no explanation.
Example: ["Trump said the trade deficit has been cut by half", "Trump claimed tariffs brought in $200 billion"]

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

# ── Step 2: Verify a single claim via Brave Search ───────────────────────────

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

# ── Step 3: Render a verdict given claim + evidence ──────────────────────────

def render_verdict(claim_text, politician_name, evidence):
    evidence_block = "\n".join([
        f"- [{e['title']}]({e['url']}): {e['snippet']}" for e in evidence
    ])

    prompt = f"""You are a rigorous, nonpartisan fact-checker.

Claim (attributed to {politician_name}):
"{claim_text}"

Evidence from news sources:
{evidence_block if evidence_block else "No supporting evidence found."}

Evaluate the claim and respond ONLY with a JSON object in this exact format:
{{
  "verdict": "TRUE" | "FALSE" | "MISLEADING" | "UNVERIFIABLE",
  "confidence": "HIGH" | "MEDIUM" | "LOW",
  "explanation": "2-3 sentence explanation citing specific evidence",
  "citations": [
    {{"title": "...", "url": "...", "snippet": "..."}}
  ]
}}

Rules:
- Be strictly factual. Do not inject political opinion.
- UNVERIFIABLE = not enough evidence to confirm or deny
- MISLEADING = technically true but missing important context
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

def process_statement(statement):
    statement_id = statement["id"]
    politician_name = statement["politician_name"]
    raw_text = statement["raw_text"]

    print(f"  Extracting claims from statement {statement_id}...")
    claims = extract_claims(raw_text, politician_name)
    print(f"  Found {len(claims)} claims")

    conn = get_db()
    for claim_text in claims:
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
    conn.close()
