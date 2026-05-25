"""
learning.py — Feedback-driven significance learning

Reads user ratings + comments from claim_feedback and formats them
as rich few-shot examples for the significance scorer prompt.
"""
from models import get_db

# ── Fetch examples for prompt ─────────────────────────────────────────────────

def get_feedback_examples(limit=30):
    """
    Returns recent rated claims formatted as few-shot prompt examples.
    Includes editor comments when present — these are the richest signal.
    """
    conn = get_db()
    rows = conn.execute("""
        SELECT claim_text, claim_type, context, rating, comment, rated_by
        FROM claim_feedback
        WHERE rating IS NOT NULL OR comment IS NOT NULL
        ORDER BY
            CASE WHEN rated_by = 'politifact_import' THEN 1 ELSE 0 END,
            rated_at DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()

    if not rows:
        return None, 0

    relevant     = [r for r in rows if r["rating"] ==  1]
    not_relevant = [r for r in rows if r["rating"] == -1]
    commented    = [r for r in rows if r["comment"] and r["comment"].strip()]

    examples = []

    # Comments first — richest signal
    for r in commented[:10]:
        label = "RELEVANT" if r["rating"] == 1 else "NOT RELEVANT" if r["rating"] == -1 else "COMMENTED"
        entry = f'{label}: "{r["claim_text"]}"\n  Editor note: "{r["comment"].strip()}"'
        if r["claim_type"]:
            entry += f'\n  Type: {r["claim_type"]}'
        examples.append(entry)

    # Then binary ratings
    for i in range(max(len(relevant), len(not_relevant))):
        if i < len(relevant):
            r = relevant[i]
            if not r["comment"]:  # already included above if commented
                examples.append(
                    f'RELEVANT: "{r["claim_text"]}"\n'
                    f'  Type: {r["claim_type"] or "unknown"}'
                )
        if i < len(not_relevant):
            r = not_relevant[i]
            if not r["comment"]:
                examples.append(
                    f'NOT RELEVANT: "{r["claim_text"]}"\n'
                    f'  Type: {r["claim_type"] or "unknown"}'
                )

    return "\n\n".join(examples[:30]), len(rows)

def get_learning_stats():
    """Returns stats for the admin learning dashboard."""
    conn = get_db()
    total_claims  = conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
    total_rated   = conn.execute(
        "SELECT COUNT(*) FROM claim_feedback WHERE rating IS NOT NULL"
    ).fetchone()[0]
    total_commented = conn.execute(
        "SELECT COUNT(*) FROM claim_feedback WHERE comment IS NOT NULL AND comment != ''"
    ).fetchone()[0]
    pf_seeds = conn.execute(
        "SELECT COUNT(*) FROM claim_feedback WHERE rated_by = 'politifact_import'"
    ).fetchone()[0]
    relevant      = conn.execute(
        "SELECT COUNT(*) FROM claim_feedback WHERE rating = 1"
    ).fetchone()[0]
    not_relevant  = conn.execute(
        "SELECT COUNT(*) FROM claim_feedback WHERE rating = -1"
    ).fetchone()[0]
    sub_claims    = conn.execute(
        "SELECT COUNT(*) FROM sub_claim_queue"
    ).fetchone()[0]

    recent = conn.execute("""
        SELECT rating FROM claim_feedback
        WHERE rating IS NOT NULL
        ORDER BY rated_at DESC LIMIT 20
    """).fetchall()
    recent_relevant = sum(1 for r in recent if r["rating"] == 1)
    recent_accuracy = round(recent_relevant / len(recent) * 100) if recent else 0

    recent_comments = conn.execute("""
        SELECT claim_text, comment, rating FROM claim_feedback
        WHERE comment IS NOT NULL AND comment != ''
        ORDER BY rated_at DESC LIMIT 5
    """).fetchall()

    nr_examples = conn.execute("""
        SELECT claim_text, claim_type, comment FROM claim_feedback
        WHERE rating = -1 ORDER BY rated_at DESC LIMIT 5
    """).fetchall()

    conn.close()

    return {
        "total_claims":     total_claims,
        "total_rated":      total_rated,
        "total_commented":  total_commented,
        "relevant":         relevant,
        "not_relevant":     not_relevant,
        "sub_claims":       sub_claims,
        "pct_rated":        round(total_rated / total_claims * 100) if total_claims else 0,
        "recent_accuracy":  recent_accuracy,
        "recent_total":     len(recent),
        "recent_comments":  [dict(r) for r in recent_comments],
        "nr_examples":      [dict(r) for r in nr_examples],
        "ready_for_auto":   total_rated >= 50 and recent_accuracy >= 80,
    }

def store_feedback(claim_id, rating, comment=None, sub_claim=None, rated_by="admin"):
    """
    Store feedback with optional comment and sub-claim.
    rating can be 1, -1, or None (comment only).
    """
    conn = get_db()

    claim = conn.execute("""
        SELECT c.claim_text, c.confidence as claim_type, c.explanation as context,
               s.source_url, s.politician_id
        FROM claims c
        JOIN statements s ON c.statement_id = s.id
        WHERE c.id = ?
    """, (claim_id,)).fetchone()

    if not claim:
        conn.close()
        return False

    # Upsert — allow updating existing feedback
    conn.execute("""
        INSERT INTO claim_feedback
            (claim_id, claim_text, claim_type, context, rating, comment, sub_claim, rated_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(claim_id) DO UPDATE SET
            rating    = excluded.rating,
            comment   = excluded.comment,
            sub_claim = excluded.sub_claim,
            rated_at  = datetime('now')
    """, (claim_id, claim["claim_text"], claim["claim_type"],
          claim["context"], rating, comment, sub_claim, rated_by))

    # Update claim's user_rating
    if rating == 1:
        label = "relevant"
    elif rating == -1:
        label = "not_relevant"
    else:
        label = "commented"
    conn.execute("UPDATE claims SET user_rating = ? WHERE id = ?", (label, claim_id))

    # Queue sub-claim if provided
    if sub_claim and sub_claim.strip():
        conn.execute("""
            INSERT INTO sub_claim_queue
                (parent_claim_id, sub_claim_text, source_url, source_title, politician_id)
            SELECT ?, ?, s.source_url, s.source_title, s.politician_id
            FROM claims c JOIN statements s ON c.statement_id = s.id
            WHERE c.id = ?
        """, (claim_id, sub_claim.strip(), claim_id))

    conn.commit()
    conn.close()
    return True
