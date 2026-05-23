import os
import json
import secrets
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from models import init_db, get_db
from scheduler import start_scheduler, run_daily_pipeline

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "paul@pklmedialab.com")

# ── Auth helpers ──────────────────────────────────────────────────────────────

def is_logged_in():
    return session.get("user_email") is not None

def require_login(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_logged_in():
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def is_admin():
    return session.get("user_email") == ADMIN_EMAIL

# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        conn.close()

        if not user:
            flash("That email is not on the invite list.")
            return render_template("login.html")

        # Generate magic link token
        token = secrets.token_urlsafe(32)
        expires = (datetime.utcnow() + timedelta(hours=1)).isoformat()
        conn = get_db()
        conn.execute("UPDATE users SET token = ?, token_expires = ? WHERE email = ?",
                     (token, expires, email))
        conn.commit()
        conn.close()

        magic_url = url_for("magic_login", token=token, _external=True)
        print(f"MAGIC LINK for {email}: {magic_url}")  # Railway logs — no email infra needed yet
        flash(f"Magic link generated. Check Railway logs (or console) for the link.")
        return render_template("login.html")

    return render_template("login.html")

@app.route("/magic/<token>")
def magic_login(token):
    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE token = ?", (token,)
    ).fetchone()
    conn.close()

    if not user:
        return "Invalid or expired link.", 403
    if datetime.utcnow().isoformat() > user["token_expires"]:
        return "Link expired. Please request a new one.", 403

    session["user_email"] = user["email"]
    conn = get_db()
    conn.execute("UPDATE users SET token = NULL, token_expires = NULL WHERE email = ?",
                 (user["email"],))
    conn.commit()
    conn.close()
    return redirect(url_for("digest"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ── Main digest view ──────────────────────────────────────────────────────────

@app.route("/")
@require_login
def digest():
    date_str = request.args.get("date", datetime.utcnow().strftime("%Y-%m-%d"))
    conn = get_db()

    # Get all politicians
    politicians = conn.execute(
        "SELECT * FROM politicians WHERE active = 1"
    ).fetchall()

    # Get claims checked on the requested date, grouped by politician
    results = []
    for pol in politicians:
        claims = conn.execute("""
            SELECT c.*, s.source_url, s.source_title, s.raw_text
            FROM claims c
            JOIN statements s ON c.statement_id = s.id
            WHERE s.politician_id = ?
              AND DATE(c.checked_at) = ?
            ORDER BY c.checked_at DESC
        """, (pol["id"], date_str)).fetchall()

        if claims:
            results.append({
                "politician": pol,
                "claims": [dict(c) for c in claims]
            })

    conn.close()

    # Parse citations JSON for template
    for group in results:
        for claim in group["claims"]:
            try:
                claim["citations"] = json.loads(claim["citations"] or "[]")
            except:
                claim["citations"] = []

    return render_template("digest.html",
                           results=results,
                           date=date_str,
                           is_admin=is_admin())

# ── Admin panel ───────────────────────────────────────────────────────────────


# PDF export
@app.route("/digest.pdf")
@require_login
def digest_pdf():
    from fpdf import FPDF
    from flask import Response

    date_str = request.args.get("date", datetime.utcnow().strftime("%Y-%m-%d"))
    conn = get_db()
    politicians = conn.execute("SELECT * FROM politicians WHERE active = 1").fetchall()
    results = []
    for pol in politicians:
        claims = conn.execute("""
            SELECT c.*, s.source_url, s.source_title
            FROM claims c
            JOIN statements s ON c.statement_id = s.id
            WHERE s.politician_id = ? AND DATE(c.checked_at) = ?
            ORDER BY c.checked_at DESC
        """, (pol["id"], date_str)).fetchall()
        if claims:
            results.append({"politician": pol, "claims": [dict(c) for c in claims]})
    conn.close()
    for group in results:
        for claim in group["claims"]:
            try:
                claim["citations"] = json.loads(claim["citations"] or "[]")
            except:
                claim["citations"] = []

    VERDICT_COLORS = {
        "TRUE":         (30, 100, 60),
        "FALSE":        (180, 40, 40),
        "MISLEADING":   (180, 110, 20),
        "UNVERIFIABLE": (100, 100, 120),
    }

    def safe(text):
        if not text:
            return ""
        return text.encode("latin-1", "replace").decode("latin-1")

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_margins(18, 15, 18)

    # Title
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_text_color(40, 40, 80)
    pdf.cell(0, 10, "PolitiPrism - Fact-Check Digest", ln=True)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(120, 120, 140)
    pdf.cell(0, 6, f"{date_str}  |  politiprism.app  |  Generated {datetime.utcnow().strftime('%B %d, %Y %H:%M UTC')}", ln=True)
    pdf.ln(6)

    # Summary table
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(40, 40, 80)
    pdf.cell(0, 8, "Summary", ln=True)
    pdf.set_draw_color(180, 180, 200)
    pdf.line(18, pdf.get_y(), 192, pdf.get_y())
    pdf.ln(3)

    col_w = [42, 98, 28, 24]
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_fill_color(230, 230, 240)
    pdf.set_text_color(60, 60, 80)
    for header, w in zip(["Politician", "Claim", "Verdict", "Confidence"], col_w):
        pdf.cell(w, 7, header, border=1, fill=True)
    pdf.ln()

    pdf.set_font("Helvetica", "", 7.5)
    for group in results:
        pol_name = safe(group["politician"]["name"])
        # Group header row
        pdf.set_fill_color(240, 240, 248)
        pdf.set_text_color(60, 60, 100)
        pdf.set_font("Helvetica", "B", 7.5)
        pdf.cell(sum(col_w), 6, f"  {pol_name.upper()}  -  {safe(group['politician']['role'])}", border=1, fill=True, ln=True)
        pdf.set_font("Helvetica", "", 7.5)
        for claim in group["claims"]:
            verdict = claim.get("verdict", "UNVERIFIABLE")
            color = VERDICT_COLORS.get(verdict, (100, 100, 120))
            pdf.set_text_color(60, 60, 80)
            pdf.cell(col_w[0], 6, pol_name, border=1)
            # Claim text — truncate if needed
            claim_txt = safe(claim.get("claim_text", ""))
            if len(claim_txt) > 95:
                claim_txt = claim_txt[:92] + "..."
            pdf.cell(col_w[1], 6, claim_txt, border=1)
            pdf.set_text_color(*color)
            pdf.set_font("Helvetica", "B", 7.5)
            pdf.cell(col_w[2], 6, verdict, border=1)
            pdf.set_text_color(100, 100, 120)
            pdf.set_font("Helvetica", "", 7.5)
            pdf.cell(col_w[3], 6, safe(claim.get("confidence", "")), border=1)
            pdf.ln()

    # Detailed section
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(40, 40, 80)
    pdf.cell(0, 8, "Detailed Fact-Checks", ln=True)
    pdf.set_draw_color(180, 180, 200)
    pdf.line(18, pdf.get_y(), 192, pdf.get_y())
    pdf.ln(4)

    for group in results:
        pol_name = safe(group["politician"]["name"])
        pol_role = safe(group["politician"]["role"])
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(40, 40, 80)
        pdf.cell(0, 7, f"{pol_name}  -  {pol_role}", ln=True)
        pdf.set_draw_color(200, 200, 220)
        pdf.line(18, pdf.get_y(), 192, pdf.get_y())
        pdf.ln(3)

        for claim in group["claims"]:
            verdict = claim.get("verdict", "UNVERIFIABLE")
            color = VERDICT_COLORS.get(verdict, (100, 100, 120))
            conf = claim.get("confidence", "")

            # Verdict badge line
            pdf.set_font("Helvetica", "B", 9)
            pdf.set_text_color(*color)
            pdf.cell(30, 6, verdict, ln=False)
            pdf.set_font("Helvetica", "", 8)
            pdf.set_text_color(140, 140, 160)
            pdf.cell(0, 6, f"{conf} confidence", ln=True)

            # Claim text
            pdf.set_font("Helvetica", "I", 9)
            pdf.set_text_color(50, 50, 70)
            pdf.multi_cell(0, 5, safe(f'"{claim.get("claim_text", "")}"'))
            pdf.ln(1)

            # Explanation
            pdf.set_font("Helvetica", "", 8.5)
            pdf.set_text_color(80, 80, 100)
            pdf.multi_cell(0, 5, safe(claim.get("explanation", "")))

            # Source
            if claim.get("source_title") or claim.get("source_url"):
                pdf.set_font("Helvetica", "", 7.5)
                pdf.set_text_color(80, 80, 180)
                src = claim.get("source_title") or claim.get("source_url") or ""
                # Label Truth Social vs news
                if "Truth Social" in src or "trumpstruth" in (claim.get("source_url") or ""):
                    src_label = f"Source: Truth Social — {src[:80]}"
                elif "whitehouse.gov" in (claim.get("source_url") or ""):
                    src_label = f"Source: White House Transcript"
                else:
                    src_label = f"Source: {src[:80]}"
                pdf.cell(0, 5, safe(src_label), ln=True)

            # Citations
            for cite in claim.get("citations", [])[:2]:
                if cite.get("title"):
                    pdf.set_font("Helvetica", "", 7)
                    pdf.set_text_color(100, 100, 180)
                    pdf.cell(0, 4, safe(f"  - {cite['title']}"), ln=True)

            pdf.ln(4)
            pdf.set_draw_color(220, 220, 230)
            pdf.line(18, pdf.get_y(), 192, pdf.get_y())
            pdf.ln(3)

        pdf.ln(2)

    pdf_bytes = bytes(pdf.output())
    filename = f"PolitiPrism_{date_str}.pdf"
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

@app.route("/admin")
@require_login
def admin():
    if not is_admin():
        return "Access denied.", 403
    conn = get_db()
    politicians = conn.execute("SELECT * FROM politicians ORDER BY name").fetchall()
    users = conn.execute("SELECT * FROM users ORDER BY email").fetchall()
    conn.close()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    return render_template("admin.html", politicians=politicians, users=users, today=today)

@app.route("/admin/politician/add", methods=["POST"])
@require_login
def add_politician():
    if not is_admin():
        return "Access denied.", 403
    name = request.form.get("name", "").strip()
    role = request.form.get("role", "").strip()
    terms = request.form.get("search_terms", "").strip()
    if name and terms:
        conn = get_db()
        conn.execute("INSERT INTO politicians (name, role, search_terms) VALUES (?, ?, ?)",
                     (name, role, terms))
        conn.commit()
        conn.close()
    return redirect(url_for("admin"))

@app.route("/admin/politician/toggle/<int:pol_id>")
@require_login
def toggle_politician(pol_id):
    if not is_admin():
        return "Access denied.", 403
    conn = get_db()
    pol = conn.execute("SELECT active FROM politicians WHERE id = ?", (pol_id,)).fetchone()
    if pol:
        conn.execute("UPDATE politicians SET active = ? WHERE id = ?",
                     (0 if pol["active"] else 1, pol_id))
        conn.commit()
    conn.close()
    return redirect(url_for("admin"))

@app.route("/admin/user/add", methods=["POST"])
@require_login
def add_user():
    if not is_admin():
        return "Access denied.", 403
    email = request.form.get("email", "").strip().lower()
    if email:
        conn = get_db()
        try:
            conn.execute("INSERT INTO users (email) VALUES (?)", (email,))
            conn.commit()
        except:
            pass  # already exists
        conn.close()
    return redirect(url_for("admin"))

@app.route("/admin/user/remove/<int:user_id>")
@require_login
def remove_user(user_id):
    if not is_admin():
        return "Access denied.", 403
    conn = get_db()
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("admin"))

# ── Manual trigger (admin only) ───────────────────────────────────────────────

@app.route("/admin/run-pipeline")
@require_login
def trigger_pipeline():
    if not is_admin():
        return "Access denied.", 403
    import threading
    threading.Thread(target=run_daily_pipeline, daemon=True).start()
    flash("Pipeline started — check Railway logs for progress.")
    return redirect(url_for("admin"))



# ── Pipeline status API ───────────────────────────────────────────────────────

@app.route("/admin/pipeline-status")
@require_login
def pipeline_status():
    conn = get_db()
    row = conn.execute("SELECT * FROM pipeline_status WHERE id=1").fetchone()
    conn.close()
    if not row:
        return jsonify({"running": 0, "stage": "idle"})
    return jsonify(dict(row))

# ── Clear data (admin only) ───────────────────────────────────────────────────

@app.route("/admin/clear-today")
@require_login
def clear_today():
    if not is_admin():
        return "Access denied.", 403
    date_str = request.args.get("date", datetime.utcnow().strftime("%Y-%m-%d"))
    conn = get_db()
    # Delete claims for today
    conn.execute("""
        DELETE FROM claims WHERE id IN (
            SELECT c.id FROM claims c
            JOIN statements s ON c.statement_id = s.id
            WHERE DATE(c.checked_at) = ?
        )
    """, (date_str,))
    # Delete statements fetched today and reset processed flag
    conn.execute("DELETE FROM statements WHERE DATE(fetched_at) = ?", (date_str,))
    conn.commit()
    conn.close()
    flash(f"Cleared all data for {date_str}. Run the pipeline to re-fetch.")
    return redirect(url_for("admin"))

@app.route("/admin/clear-all")
@require_login
def clear_all():
    if not is_admin():
        return "Access denied.", 403
    conn = get_db()
    conn.execute("DELETE FROM claims")
    conn.execute("DELETE FROM statements")
    conn.commit()
    conn.close()
    flash("All claims and statements cleared. Politicians and users preserved.")
    return redirect(url_for("admin"))

# ── Startup ───────────────────────────────────────────────────────────────────

init_db()

# Seed admin user
conn = get_db()
try:
    conn.execute("INSERT OR IGNORE INTO users (email) VALUES (?)", (ADMIN_EMAIL,))
    conn.commit()
except:
    pass
conn.close()

start_scheduler()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
