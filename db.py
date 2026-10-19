"""All database access for jobagent. Swapping to Postgres should only touch this file."""
import json
import os
import sqlite3
from datetime import datetime, timezone

DEFAULT_DB = "/opt/jobagent/jobagent.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,          -- LinkedIn numeric job ID; this is the job_key
    fingerprint   TEXT NOT NULL,             -- sha256(company|normalized_title|location)
    title         TEXT,
    company       TEXT,
    location      TEXT,
    url           TEXT,
    tier          INTEGER,
    yoe_min       INTEGER,
    salary_min    INTEGER,
    salary_max    INTEGER,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    notified_at   TEXT,
    verdict_json  TEXT,
    verdict_at    TEXT
);
CREATE INDEX IF NOT EXISTS jobs_fingerprint ON jobs(fingerprint);

CREATE TABLE IF NOT EXISTS agent_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    hours            INTEGER,
    emails           INTEGER DEFAULT 0,
    cards            INTEGER DEFAULT 0,
    title_pass       INTEGER DEFAULT 0,
    new              INTEGER DEFAULT 0,
    tier_pass        INTEGER DEFAULT 0,
    prefilter_pass   INTEGER DEFAULT 0,
    llm              INTEGER DEFAULT 0,
    notified         INTEGER DEFAULT 0,
    truncated_alerts INTEGER DEFAULT 0,
    enriched         INTEGER DEFAULT 0,
    error            TEXT
);
"""

RUN_COUNTERS = ("emails", "cards", "title_pass", "new", "tier_pass", "prefilter_pass",
                "llm", "notified", "truncated_alerts", "enriched")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_path():
    return os.environ.get("JOBAGENT_DB", DEFAULT_DB)


def connect(path=None, readonly=False):
    path = path or db_path()
    if readonly:
        if not os.path.exists(path):
            return None
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


# --- dedup -----------------------------------------------------------------

def is_notified(conn, job_id, fingerprint):
    """True if this exact job ID, or any job with the same content fingerprint, was already sent."""
    if conn is None:
        return False
    row = conn.execute(
        "SELECT 1 FROM jobs WHERE (fingerprint = ? OR job_id = ?) AND notified_at IS NOT NULL LIMIT 1",
        (fingerprint, job_id),
    ).fetchone()
    return row is not None


def upsert_seen(conn, job):
    """Record that a job card was seen. Never touches notified_at or the cached verdict."""
    ts = now()
    conn.execute(
        """INSERT INTO jobs (job_id, fingerprint, title, company, location, url, tier,
                             yoe_min, salary_min, salary_max, first_seen_at, last_seen_at)
           VALUES (:job_id, :fingerprint, :title, :company, :location, :url, :tier,
                   :yoe_min, :salary_min, :salary_max, :ts, :ts)
           ON CONFLICT(job_id) DO UPDATE SET
               last_seen_at = excluded.last_seen_at,
               fingerprint  = excluded.fingerprint,
               tier         = excluded.tier,
               yoe_min      = COALESCE(excluded.yoe_min, jobs.yoe_min),
               salary_min   = COALESCE(excluded.salary_min, jobs.salary_min),
               salary_max   = COALESCE(excluded.salary_max, jobs.salary_max)""",
        {
            "job_id": job["job_id"], "fingerprint": job["fingerprint"],
            "title": job.get("title"), "company": job.get("company"),
            "location": job.get("location"), "url": job.get("url"),
            "tier": job.get("tier"), "yoe_min": job.get("yoe_min"),
            "salary_min": job.get("salary_min"), "salary_max": job.get("salary_max"),
            "ts": ts,
        },
    )
    conn.commit()


def mark_notified(conn, job_keys):
    """Set notified_at for each job_key (LinkedIn job ID). Returns rows updated."""
    ts = now()
    n = 0
    for key in job_keys:
        cur = conn.execute(
            "UPDATE jobs SET notified_at = ? WHERE job_id = ? AND notified_at IS NULL", (ts, key))
        n += cur.rowcount
    conn.commit()
    return n


# --- LLM verdict cache -------------------------------------------------------

def get_cached_verdict(conn, fingerprint):
    """Most recent verdict for any job sharing this fingerprint, or None."""
    if conn is None:
        return None
    row = conn.execute(
        """SELECT verdict_json FROM jobs WHERE fingerprint = ? AND verdict_json IS NOT NULL
           ORDER BY verdict_at DESC LIMIT 1""",
        (fingerprint,),
    ).fetchone()
    return json.loads(row["verdict_json"]) if row else None


def save_verdict(conn, job_key, verdict):
    conn.execute(
        "UPDATE jobs SET verdict_json = ?, verdict_at = ? WHERE job_id = ?",
        (json.dumps(verdict), now(), job_key),
    )
    conn.commit()


# --- run bookkeeping ---------------------------------------------------------

def start_run(conn, hours):
    cur = conn.execute("INSERT INTO agent_runs (started_at, hours) VALUES (?, ?)", (now(), hours))
    conn.commit()
    return cur.lastrowid


def finish_run(conn, run_id, stats, error=None):
    cols = [c for c in RUN_COUNTERS if c in stats]
    sets = ", ".join(f"{c} = ?" for c in cols)
    conn.execute(
        f"UPDATE agent_runs SET {sets}{', ' if sets else ''}finished_at = ?, error = ? WHERE id = ?",
        [stats[c] for c in cols] + [now(), error, run_id],
    )
    conn.commit()


def add_to_latest_run(conn, column, amount):
    if column not in RUN_COUNTERS:
        raise ValueError(column)
    conn.execute(
        f"UPDATE agent_runs SET {column} = {column} + ? "
        "WHERE id = (SELECT MAX(id) FROM agent_runs)", (amount,))
    conn.commit()


def run_history(conn, limit=30):
    if conn is None:
        return []
    rows = conn.execute("SELECT * FROM agent_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
