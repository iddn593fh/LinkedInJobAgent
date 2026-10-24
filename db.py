"""All database access for jobagent. Swapping to Postgres should only touch this file."""
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

DEFAULT_DB = "/opt/jobagent/jobagent.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,          -- LinkedIn numeric job ID; this is the job_key
    fingerprint   TEXT NOT NULL,             -- sha256(company|normalized_title), see parsing.py
    title         TEXT,
    company       TEXT,
    location      TEXT,
    url           TEXT,
    yoe_min       INTEGER,                   -- parsed hints for the LLM, never filters
    salary_min    INTEGER,
    salary_max    INTEGER,
    salary_text   TEXT,
    posted        TEXT,
    seniority_level TEXT,
    description   TEXT,
    enriched_at   TEXT,
    enrich_failed INTEGER DEFAULT 0,
    triage        TEXT,                      -- LLM stage A: yes | maybe | no
    triage_at     TEXT,
    verdict_json  TEXT,                      -- LLM stage B
    verdict_at    TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    notified_at   TEXT
);
CREATE INDEX IF NOT EXISTS jobs_fingerprint ON jobs(fingerprint);

-- Every time a job card is seen: which run, from which email or search.
CREATE TABLE IF NOT EXISTS sightings (
    run_id   INTEGER NOT NULL,
    job_id   TEXT NOT NULL,
    source   TEXT NOT NULL,
    PRIMARY KEY (run_id, job_id, source)
);

-- LLM usage per UTC day (free-tier daily limits reset at midnight UTC).
CREATE TABLE IF NOT EXISTS llm_usage (
    day      TEXT PRIMARY KEY,
    requests INTEGER NOT NULL DEFAULT 0,
    tokens   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    hours            INTEGER,
    emails           INTEGER DEFAULT 0,
    raw_cards        INTEGER DEFAULT 0,
    search_requests  INTEGER DEFAULT 0,
    searches_capped  INTEGER DEFAULT 0,
    cards            INTEGER DEFAULT 0,      -- unique jobs this run
    new              INTEGER DEFAULT 0,      -- ...not already sent
    email_cards      INTEGER DEFAULT 0,      -- unique jobs seen in alert emails
    email_only       INTEGER DEFAULT 0,      -- ...of which no search returned
    email_only_pass  INTEGER DEFAULT 0,      -- ...of which the LLM said send
    triaged          INTEGER DEFAULT 0,
    triage_pass      INTEGER DEFAULT 0,
    enriched         INTEGER DEFAULT 0,
    judged           INTEGER DEFAULT 0,
    send             INTEGER DEFAULT 0,
    notified         INTEGER DEFAULT 0,
    llm_requests     INTEGER DEFAULT 0,
    llm_tokens       INTEGER DEFAULT 0,
    pending          INTEGER DEFAULT 0,      -- jobs left for the next run (LLM budget / enrichment)
    truncated_alerts INTEGER DEFAULT 0,
    stopped          TEXT,                   -- why LinkedIn or LLM work stopped early, if it did
    error            TEXT
);
"""

RUN_COUNTERS = ("emails", "raw_cards", "search_requests", "searches_capped", "cards", "new",
                "email_cards", "email_only", "email_only_pass", "triaged", "triage_pass",
                "enriched", "judged", "send", "notified", "llm_requests", "llm_tokens", "pending",
                "truncated_alerts", "stopped")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


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
    _migrate(conn)
    return conn


SCHEMA_VERSION = 1


def _migrate(conn):
    """One-off data migrations, tracked in PRAGMA user_version.

    v1 (2026-10-24): fingerprints no longer include location. Recompute every row so jobs
    already sent in one city dedupe against the same role posted elsewhere."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        from parsing import fingerprint
        rows = conn.execute("SELECT job_id, company, title FROM jobs").fetchall()
        conn.executemany("UPDATE jobs SET fingerprint = ? WHERE job_id = ?",
                         [(fingerprint(r["company"], r["title"]), r["job_id"]) for r in rows])
    if version < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()


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


JOB_FIELDS = ("fingerprint", "title", "company", "location", "url", "yoe_min", "salary_min",
              "salary_max", "salary_text", "posted")


def upsert_seen(conn, job, run_id=None, commit=True):
    """Record a job card and where it was seen. A job new to the DB inherits the triage and
    verdict of any earlier job with the same fingerprint (a repost), so it is not judged again.
    Never touches notified_at."""
    ts = now()
    row = {k: job.get(k) for k in JOB_FIELDS}
    row.update(job_id=job["job_id"], ts=ts)
    cols = ", ".join(JOB_FIELDS)
    vals = ", ".join(f":{k}" for k in JOB_FIELDS)
    conn.execute(
        f"""INSERT INTO jobs (job_id, {cols}, first_seen_at, last_seen_at)
            VALUES (:job_id, {vals}, :ts, :ts)
            ON CONFLICT(job_id) DO UPDATE SET
                fingerprint  = excluded.fingerprint,
                salary_text  = COALESCE(jobs.salary_text, excluded.salary_text),
                posted       = COALESCE(jobs.posted, excluded.posted),
                last_seen_at = excluded.last_seen_at""",
        row)
    _inherit(conn, job["job_id"], row["fingerprint"])
    if run_id is not None:
        for src in job.get("sources") or [job.get("source") or "unknown"]:
            conn.execute("INSERT OR IGNORE INTO sightings (run_id, job_id, source) VALUES (?, ?, ?)",
                         (run_id, job["job_id"], src))
    if commit:
        conn.commit()


def _inherit(conn, job_id, fingerprint):
    """Copy triage / verdict from the latest other job with this fingerprint, where missing."""
    for col, at in (("triage", "triage_at"), ("verdict_json", "verdict_at")):
        src = conn.execute(
            f"""SELECT {col}, {at} FROM jobs WHERE fingerprint = ? AND job_id != ?
                AND {col} IS NOT NULL ORDER BY {at} DESC LIMIT 1""", (fingerprint, job_id)).fetchone()
        if src:
            conn.execute(f"UPDATE jobs SET {col} = ?, {at} = ? WHERE job_id = ? AND {col} IS NULL",
                         (src[0], src[1], job_id))


def get_job(conn, job_id):
    row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    return _job(row) if row else None


def _job(row):
    d = dict(row)
    d["verdict"] = json.loads(d["verdict_json"]) if d.get("verdict_json") else None
    return d


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


# --- work queues (everything not sent, seen within the backlog window) ----------

def _cutoff(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


_OPEN = """notified_at IS NULL AND last_seen_at >= :cutoff
           AND NOT EXISTS (SELECT 1 FROM jobs s WHERE s.fingerprint = jobs.fingerprint
                           AND s.notified_at IS NOT NULL)"""


def _one_per_fingerprint(rows):
    out, seen = [], set()
    for r in rows:
        if r["fingerprint"] not in seen:
            seen.add(r["fingerprint"])
            out.append(_job(r))
    return out


def pending_triage(conn, days):
    rows = conn.execute(f"SELECT * FROM jobs WHERE triage IS NULL AND {_OPEN} ORDER BY first_seen_at",
                        {"cutoff": _cutoff(days)}).fetchall()
    return _one_per_fingerprint(rows)


def pending_enrich(conn, days):
    """Triage survivors still without a description: 'yes' before 'maybe', oldest first."""
    rows = conn.execute(
        f"""SELECT * FROM jobs WHERE triage IN ('yes', 'maybe') AND verdict_json IS NULL
            AND enriched_at IS NULL AND enrich_failed = 0 AND {_OPEN}
            ORDER BY triage = 'maybe', first_seen_at""", {"cutoff": _cutoff(days)}).fetchall()
    return _one_per_fingerprint(rows)


def pending_judge(conn, days, title_only_after_hours):
    """Triage survivors without a verdict. Unenriched ones wait for enrichment until they are
    older than title_only_after_hours (or enrichment failed), then are judged on the card alone."""
    rows = conn.execute(
        f"""SELECT * FROM jobs WHERE triage IN ('yes', 'maybe') AND verdict_json IS NULL AND {_OPEN}
            AND (enriched_at IS NOT NULL OR enrich_failed = 1 OR first_seen_at < :wait)
            ORDER BY triage = 'maybe', first_seen_at""",
        {"cutoff": _cutoff(days), "wait": _cutoff(title_only_after_hours / 24)}).fetchall()
    return _one_per_fingerprint(rows)


def count_pending(conn, days):
    row = conn.execute(
        f"""SELECT COUNT(*) FROM jobs WHERE {_OPEN} AND
            (triage IS NULL OR (triage IN ('yes', 'maybe') AND verdict_json IS NULL))""",
        {"cutoff": _cutoff(days)}).fetchone()
    return row[0]


def to_send(conn):
    """Every job the LLM said to send that has not reached Telegram yet, oldest first.
    Includes leftovers from a run that crashed before mark-notified."""
    rows = conn.execute(
        """SELECT * FROM jobs WHERE verdict_json IS NOT NULL AND notified_at IS NULL
           AND json_extract(verdict_json, '$.verdict') = 'send'
           AND NOT EXISTS (SELECT 1 FROM jobs s WHERE s.fingerprint = jobs.fingerprint
                           AND s.notified_at IS NOT NULL)
           ORDER BY first_seen_at""").fetchall()
    return _one_per_fingerprint(rows)    # one message per job


def locations(conn, fingerprint):
    """Every location this role has been seen in (one message lists them all)."""
    rows = conn.execute(
        "SELECT DISTINCT location FROM jobs WHERE fingerprint = ? AND location IS NOT NULL "
        "ORDER BY first_seen_at", (fingerprint,)).fetchall()
    return [r[0] for r in rows]


def save_triage(conn, fingerprint, label):
    """Applies to every unjudged row of this job (same fingerprint), so duplicates cost one call."""
    conn.execute("UPDATE jobs SET triage = ?, triage_at = ? WHERE fingerprint = ? AND triage IS NULL",
                 (label, now(), fingerprint))
    conn.commit()


def save_enrichment(conn, job_id, info):
    if info is None:
        conn.execute("UPDATE jobs SET enrich_failed = 1 WHERE job_id = ?", (job_id,))
    else:
        conn.execute(
            """UPDATE jobs SET description = ?, seniority_level = ?, yoe_min = ?,
                   salary_min = COALESCE(?, salary_min), salary_max = COALESCE(?, salary_max),
                   enriched_at = ? WHERE job_id = ?""",
            (info["description"], info["seniority_level"], info["yoe_min"], info["salary_min"],
             info["salary_max"], now(), job_id))
    conn.commit()


def save_verdict(conn, fingerprint, verdict):
    conn.execute(
        "UPDATE jobs SET verdict_json = ?, verdict_at = ? WHERE fingerprint = ? AND verdict_json IS NULL",
        (json.dumps(verdict), now(), fingerprint),
    )
    conn.commit()


# --- LLM usage ---------------------------------------------------------------

def llm_usage_today(conn):
    if conn is None:
        return 0, 0
    row = conn.execute("SELECT requests, tokens FROM llm_usage WHERE day = ?", (today(),)).fetchone()
    return (row["requests"], row["tokens"]) if row else (0, 0)


def add_llm_usage(conn, requests, tokens):
    conn.execute(
        """INSERT INTO llm_usage (day, requests, tokens) VALUES (?, ?, ?)
           ON CONFLICT(day) DO UPDATE SET requests = requests + excluded.requests,
                                          tokens = tokens + excluded.tokens""",
        (today(), requests, tokens))
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


def email_value(conn, runs=30):
    """Is the alert email still pulling its weight? Over the last `runs` runs: unique jobs seen
    in emails, how many of those no search returned, and how many email-only jobs were sent."""
    if conn is None:
        return None
    ids = "SELECT id FROM agent_runs ORDER BY id DESC LIMIT ?"
    row = conn.execute(f"""
        WITH email_jobs AS (SELECT DISTINCT job_id FROM sightings
                            WHERE source = 'email' AND run_id IN ({ids})),
             searched AS (SELECT DISTINCT job_id FROM sightings WHERE source LIKE 'search:%')
        SELECT COUNT(*) AS email_jobs,
               SUM(e.job_id NOT IN (SELECT job_id FROM searched)) AS email_only,
               SUM(e.job_id NOT IN (SELECT job_id FROM searched) AND j.notified_at IS NOT NULL)
                   AS email_only_sent
        FROM email_jobs e JOIN jobs j ON j.job_id = e.job_id""", (runs,)).fetchone()
    return {k: row[k] or 0 for k in ("email_jobs", "email_only", "email_only_sent")}


def run_history(conn, limit=30):
    if conn is None:
        return []
    rows = conn.execute("SELECT * FROM agent_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
