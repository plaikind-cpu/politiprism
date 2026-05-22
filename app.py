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

@app.route("/admin")
@require_login
def admin():
    if not is_admin():
        return "Access denied.", 403
    conn = get_db()
    politicians = conn.execute("SELECT * FROM politicians ORDER BY name").fetchall()
    users = conn.execute("SELECT * FROM users ORDER BY email").fetchall()
    conn.close()
    return render_template("admin.html", politicians=politicians, users=users)

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
