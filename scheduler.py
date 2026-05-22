from datetime import datetime, timezone
from apscheduler.schedulers.background import BackgroundScheduler
from models import get_db
from ingestion import fetch_statements_for_politician, get_unprocessed_statements, mark_statement_processed
from factcheck import process_statement
import atexit

def set_status(running, stage, statements_total=0, statements_done=0, claims_found=0):
    try:
        conn = get_db()
        now = datetime.now(timezone.utc).isoformat()
        if running:
            conn.execute("""
                UPDATE pipeline_status SET
                    running=?, stage=?, statements_total=?,
                    statements_done=?, claims_found=?, started_at=?
                WHERE id=1
            """, (1, stage, statements_total, statements_done, claims_found, now))
        else:
            conn.execute("""
                UPDATE pipeline_status SET
                    running=0, stage=?, statements_total=?,
                    statements_done=?, claims_found=?, finished_at=?
                WHERE id=1
            """, (stage, statements_total, statements_done, claims_found, now))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Status update error: {e}")

def get_pipeline_running():
    try:
        conn = get_db()
        row = conn.execute("SELECT running FROM pipeline_status WHERE id=1").fetchone()
        conn.close()
        return row["running"] if row else 0
    except:
        return 0

def run_daily_pipeline():
    if get_pipeline_running():
        print("Pipeline already running — skipping.")
        return

    print("=== PolitiPrism daily pipeline starting ===")
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    claims_found = 0

    set_status(True, "Fetching news...", 0, 0, 0)

    # 1. Fetch news for all active politicians
    conn = get_db()
    politicians = conn.execute("SELECT * FROM politicians WHERE active = 1").fetchall()
    conn.close()

    for politician in politicians:
        set_status(True, f"Fetching: {politician['name']}", 0, 0, claims_found)
        print(f"Fetching statements for {politician['name']}...")
        count = fetch_statements_for_politician(politician)
        print(f"  Added {count} new statements")

    # 2. Fact-check all unprocessed statements
    statements = get_unprocessed_statements()
    total = len(statements)
    print(f"Processing {total} unprocessed statements...")
    set_status(True, f"Checking {total} statements...", total, 0, 0)

    for i, statement in enumerate(statements):
        pol_name = statement["politician_name"]
        set_status(True, f"Checking {pol_name} ({i+1}/{total})", total, i, claims_found)
        try:
            new_claims = process_statement(statement, date_str)
            claims_found += new_claims
            mark_statement_processed(statement["id"])
        except Exception as e:
            print(f"  Error processing statement {statement['id']}: {e}")

    set_status(False, f"Done — {claims_found} claims checked", total, total, claims_found)
    print("=== Pipeline complete ===")

def start_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(run_daily_pipeline, "cron", hour=7, minute=0)
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown())
    return scheduler
