"""
learning.py — Feedback-driven significance learning

Reads user ratings from claim_feedback and formats them as
few-shot examples for the significance scorer prompt.
Also provides analytics on learning progress.
"""
import json
from models import get_db

# ── Fetch examples for prompt ─────────────────────────────────────────────────

def get_feedback_examples(limit=30):
    """
    Returns recent rated claims formatted as few-shot prompt examples.
    Balances relevant and not-relevant examples.
    """
    conn = get_db()
    rows = conn.execute("""
        SELECT claim_text, claim_type, context, rating
        FROM claim_feedback
        ORDER BY rated_at DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()

    if not rows:
        return None, 0  # no examples yet

    relevant     = [r for r in rows if r["rating"] == 1]
    not_relevant = [r for r in rows if r["rating"] == -1]

    examples = []
    # Interleave relevant and not-relevant for balance
    for i in range(max(len(relevant), len(not_relevant))):
        if i < len(relevant):
            r = relevant[i]
            examples.append(
                f'RELEVANT: "{r["claim_text"]}"\n'
                f'  Type: {r["claim_type"] or "unknown"}\n'
                f'  Context: {r["context"] or "unknown"}'
            )
        if i < len(not_relevant):
            r = not_relevant[i]
            examples.append(
                f'NOT RELEVANT: "{r["claim_text"]}"\n'
                f'  Type: {r["claim_type"] or "unknown"}\n'
                f'  Context: {r["context"] or "unknown"}'
            )

    return "\n\n".join(examples[:30]), len(rows)

def get_learning_stats():
    """Returns stats for the admin learning dashboard."""
    conn = get_db()

    total_claims = conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
    total_rated  = conn.execute("SELECT COUNT(*) FROM claim_feedback").fetchone()[0]
    relevant     = conn.execute(
        "SELECT COUNT(*) FROM claim_feedback WHERE rating = 1"
    ).fetchone()[0]
    not_relevant = conn.execute(
        "SELECT COUNT(*) FROM claim_feedback WHERE rating = -1"
    ).fetchone()[0]

    # Recent trend — last 20 ratings
    recent = conn.execute("""
        SELECT rating FROM claim_feedback
        ORDER BY rated_at DESC LIMIT 20
    """).fetchall()
    recent_relevant = sum(1 for r in recent if r["rating"] == 1)
    recent_accuracy = round(recent_relevant / len(recent) * 100) if recent else 0

    # Most common not-relevant patterns
    nr_examples = conn.execute("""
        SELECT claim_text, claim_type FROM claim_feedback
        WHERE rating = -1 ORDER BY rated_at DESC LIMIT 5
    """).fetchall()

    conn.close()

    return {
        "total_claims":    total_claims,
        "total_rated":     total_rated,
        "relevant":        relevant,
        "not_relevant":    not_relevant,
        "pct_rated":       round(total_rated / total_claims * 100) if total_claims else 0,
        "recent_accuracy": recent_accuracy,
        "recent_total":    len(recent),
        "nr_examples":     [dict(r) for r in nr_examples],
        "ready_for_auto":  total_rated >= 50 and recent_accuracy >= 80,
    }

def store_feedback(claim_id, rating, rated_by="admin"):
    """Store a rating and update the claim's user_rating field."""
    conn = get_db()

    # Get claim details for the feedback record
    claim = conn.execute("""
        SELECT c.claim_text, c.confidence as claim_type, c.explanation as context
        FROM claims c WHERE c.id = ?
    """, (claim_id,)).fetchone()

    if not claim:
        conn.close()
        return False

    # Upsert feedback (allow changing mind)
    conn.execute("""
        INSERT INTO claim_feedback (claim_id, claim_text, claim_type, context, rating, rated_by)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT DO NOTHING
    """, (claim_id, claim["claim_text"], claim["claim_type"],
          claim["context"], rating, rated_by))

    # Also update the claim's own rating field
    conn.execute(
        "UPDATE claims SET user_rating = ? WHERE id = ?",
        ("relevant" if rating == 1 else "not_relevant", claim_id)
    )

    conn.commit()
    conn.close()
    return True
