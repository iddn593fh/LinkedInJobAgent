"""The run, shared by `debug` (on a scratch copy of the DB) and `fetch` (on the real one).

  merge -> dedup against sent -> store every card
  -> A. LLM triage (titles, big batches)
  -> enrich triage survivors (LinkedIn posting)
  -> B. LLM judge (posting excerpts)
  -> to_send: every "send" verdict not yet notified

Stages B and A work from DB queues, not just today's cards, so jobs a previous run could not
finish (LLM budget spent, LinkedIn blocked) are picked up first by the next one.
"""
import db
from enrich import fetch_posting
from judge import judge, triage
from linkedin_http import Stop
from parsing import fingerprint, parse_salary


def merge_cards(cards):
    """One card per job ID across emails and searches; the first copy wins, sources accumulate.
    Email cards go first so the alert's salary line is kept when a search card lacks one."""
    by_id = {}
    for c in cards:
        src = c.get("source")
        if c["job_id"] in by_id:
            kept = by_id[c["job_id"]]
            if src and src not in kept["sources"]:
                kept["sources"].append(src)
            for k in ("company", "location", "salary_text", "posted"):
                kept[k] = kept.get(k) or c.get(k)
            continue
        kept = dict(c)
        kept["sources"] = [src] if src else []
        by_id[c["job_id"]] = kept
    return list(by_id.values())


def prepare(cards, conn):
    """Add fingerprint and parsed salary; mark cards already sent (by job ID or fingerprint)."""
    for c in cards:
        c["fingerprint"] = fingerprint(c.get("company"), c.get("title"), c.get("location"))
        c["salary_min"], c["salary_max"] = parse_salary(c.get("salary_text"))
        c["already_sent"] = db.is_notified(conn, c["job_id"], c["fingerprint"])
    return cards


def store(conn, cards, run_id):
    for c in cards:
        db.upsert_seen(conn, c, run_id, commit=False)
    conn.commit()


def run_stages(conn, li_client, llm_client, llm_cfg, opts, log=lambda *a: None):
    """Triage, enrich, judge from the DB queues. Returns counters for the run row."""
    stats = {"triaged": 0, "triage_pass": 0, "enriched": 0, "judged": 0, "send": 0}

    if llm_client is not None:
        todo = db.pending_triage(conn, opts.backlog_days)
        log(f"triage: {len(todo)} job(s) queued")

        def on_triage(j, label):
            db.save_triage(conn, j["fingerprint"], label)
            stats["triaged"] += 1
            stats["triage_pass"] += label != "no"
        triage(llm_client, todo, llm_cfg, on_triage)

    if opts.enrich and li_client is not None:
        todo = db.pending_enrich(conn, opts.backlog_days)
        log(f"enrich: {len(todo)} job(s) queued, cap {opts.enrich_cap}")
        for j in todo[:opts.enrich_cap]:
            try:
                info = fetch_posting(li_client, j["job_id"])
            except Stop:
                log(f"enrich: stopped, {li_client.stop_reason()}")
                break
            if info is None and li_client.throttled:
                continue                      # 429s, not a dead posting: retry next run
            db.save_enrichment(conn, j["job_id"], info)
            stats["enriched"] += info is not None

    if llm_client is not None:
        todo = db.pending_judge(conn, opts.backlog_days, opts.title_only_after_hours)
        log(f"judge: {len(todo)} job(s) queued")

        def on_verdict(j, v):
            db.save_verdict(conn, j["fingerprint"], v)
            stats["judged"] += 1
            stats["send"] += v["verdict"] == "send"
        judge(llm_client, todo, llm_cfg, on_verdict)

    return stats
