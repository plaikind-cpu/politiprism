from datetime import datetime, timezone
from apscheduler.schedulers.background import BackgroundScheduler
from models import get_db
from ingestion import fetch_statements_for_politician, get_unprocessed_statements, mark_statement_processed
from factcheck import process_statement
import atexit

def run_daily_pipeline():
    print("=== PolitiPrism daily pipeline starting ===")
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # 1. Fetch news for all active politicians
    conn = get_db()
    politicians = conn.execute(
        "SELECT * FROM politicians WHERE active = 1"
    ).fetchall()
    conn.close()

    for politician in politicians:
        print(f"Fetching statements for {politician['name']}...")
        count = fetch_statements_for_politician(politician)
        print(f"  Added {count} new statements")

    # 2. Fact-check all unprocessed statements
    statements = get_unprocessed_statements()
    print(f"Processing {len(statements)} unprocessed statements...")

    for statement in statements:
        try:
            process_statement(statement, date_str)
            mark_statement_processed(statement["id"])
        except Exception as e:
            print(f"  Error processing statement {statement['id']}: {e}")

    print("=== Pipeline complete ===")

def start_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(run_daily_pipeline, "cron", hour=7, minute=0)
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown())
    return scheduler
