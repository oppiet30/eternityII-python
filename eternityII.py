import time
import json
import mysql.connector
from datetime import datetime

WORKER_ID = 1   # set per-machine
HEARTBEAT_INTERVAL = 30
JOB_POLL_INTERVAL = 5

def db():
    return mysql.connector.connect(
        host="localhost",
        user="solver",
        password="solverpass",
        database="eternity2"
    )

def heartbeat():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE workers
        SET last_heartbeat = NOW()
        WHERE worker_id = %s
    """, (WORKER_ID,))
    conn.commit()
    cur.close()
    conn.close()

def fetch_jobs():
    conn = db()
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT *
        FROM jobs
        WHERE assigned_worker = %s
          AND status = 'running'
        ORDER BY started_at ASC
    """, (WORKER_ID,))
    jobs = cur.fetchall()
    cur.close()
    conn.close()
    return jobs

def mark_failed(job_id, reason="worker error"):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE jobs
        SET status='failed', finished_at=NOW()
        WHERE job_id=%s
    """, (job_id,))
    conn.commit()
    cur.close()
    conn.close()

def complete_job(job_id, score, depth, duration):
    conn = db()
    cur = conn.cursor()
    cur.execute("CALL proc_complete_job(%s,%s,%s,%s)",
                (job_id, score, depth, duration))
    conn.commit()
    cur.close()
    conn.close()

def run_solver(prefix, parameters):
    """
    Placeholder for your actual Eternity II solver.
    Replace this with your C/Python solver call.
    """
    print(f"Running solver on prefix {prefix} with params {parameters}")

    # Simulate work
    time.sleep(3)

    # Fake results for demonstration
    return {
        "score": 460,
        "depth": 12,
        "duration": 3
    }

def worker_loop():
    print("Worker loop started.")

    last_heartbeat = 0

    while True:
        now = time.time()

        # Heartbeat
        if now - last_heartbeat > HEARTBEAT_INTERVAL:
            heartbeat()
            last_heartbeat = now

        # Fetch jobs
        jobs = fetch_jobs()

        if not jobs:
            time.sleep(JOB_POLL_INTERVAL)
            continue

        for job in jobs:
            job_id = job["job_id"]
            prefix = job["prefix"]
            params = json.loads(job["parameters"]) if job["parameters"] else {}

            try:
                print(f"Worker {WORKER_ID} running job {job_id}")

                result = run_solver(prefix, params)

                complete_job(
                    job_id,
                    result["score"],
                    result["depth"],
                    result["duration"]
                )

                print(f"Job {job_id} completed.")

            except Exception as e:
                print(f"Job {job_id} failed: {e}")
                mark_failed(job_id)

        time.sleep(JOB_POLL_INTERVAL)

if __name__ == "__main__":
    worker_loop()

