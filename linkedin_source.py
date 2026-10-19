#!/opt/jobagent/venv/bin/python
"""jobagent CLI.

  debug           full run on a scratch copy of the DB: prints every step, keeps nothing
                  (LLM stages only with --llm; their usage still counts against today's budget)
  fetch           full run on the real DB, prints JSON {stats, candidates} for n8n
  mark-notified   read job_keys from stdin, one per line, set notified_at
  stats           funnel history, one row per run, plus today's LLM budget
  llm-check       one tiny LLM request: verifies key/model, prints the provider's rate limits
"""
import argparse
import html
import json
import os
import sqlite3
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
from email_parser import parse_alert_html
from imap_source import Mailbox, load_env, read_local_file
from linkedin_http import GuestClient, parse_range
from llm import LLMClient, load_config
from logutil import log
from pipeline import merge_cards, prepare, run_stages, store
from search_source import load_searches, run_searches


def add_run_args(p):
    p.add_argument("--hours", type=int, default=24)
    p.add_argument("--source", choices=("all", "search", "email"), default="all",
                   help="where cards come from (default: guest search + alert emails)")
    p.add_argument("--max-pages", type=int, default=5,
                   help="result pages (10 jobs each) per search unless searches.yaml sets max_pages; "
                        "results are relevance-ranked, so later pages are mostly noise")
    p.add_argument("--page-delay", type=parse_range, default="10-20",
                   help="seconds between LinkedIn requests, random in range (default 10-20)")
    p.add_argument("--search-delay", type=parse_range, default="30-60",
                   help="seconds before the first page of each new search (default 30-60)")
    p.add_argument("--max-minutes", type=float, default=60,
                   help="wall-clock budget for all LinkedIn requests (searches + enrichment)")
    p.add_argument("--max-429", type=int, default=60,
                   help="LinkedIn 429s tolerated per run before stopping (each request is retried "
                        "up to 3 times 5-15s apart; 3 requests in a row failing also stops the run; "
                        "403/999 stop at once)")
    p.add_argument("--no-enrich", dest="enrich", action="store_false",
                   help="skip fetching full postings for triage survivors")
    p.add_argument("--enrich-cap", type=int, default=70,
                   help="max postings fetched per run (2026-10-19 live run: 77 triage survivors)")
    p.add_argument("--backlog-days", type=float, default=3,
                   help="unfinished jobs seen within this many days are retried")
    p.add_argument("--title-only-after-hours", type=float, default=30,
                   help="a triage survivor still not enriched after this long is judged on its card")


def load_emails(args):
    if getattr(args, "file", None):
        return [read_local_file(f) for f in args.file]
    with Mailbox(readonly=True) as mb:
        return mb.alert_emails(args.hours)


def collect(args, client):
    """Cards from every enabled source. Returns (cards, report) where report has
    per-email and per-search details for display and run stats."""
    report = {"emails": [], "searches": [], "truncated": 0}
    cards = []
    if args.source in ("all", "email"):
        for e in load_emails(args):
            parsed = parse_alert_html(e["html"])
            n_see = parsed["see_all"]
            truncated = n_see is not None and n_see > len(parsed["cards"])
            report["truncated"] += int(truncated)
            report["emails"].append({**e, "cards": parsed["cards"], "see_all": n_see,
                                     "truncated": truncated})
            for c in parsed["cards"]:
                cards.append({**c, "source": "email"})
    if args.source in ("all", "search"):
        found, report["searches"] = run_searches(client, load_searches(), args.hours, args.max_pages,
                                                 search_delay=args.search_delay)
        cards.extend(found)
    return cards, report


def make_client(args):
    return GuestClient(delay=args.page_delay, max_seconds=args.max_minutes * 60,
                       max_429=args.max_429)


def make_llm(usage_conn):
    """(client, cfg), or (None, cfg) when no API key is configured."""
    cfg = load_config()
    key = os.environ.get("LLM_API_KEY")
    if not key:
        return None, cfg
    client = LLMClient(cfg, key, used_today=db.llm_usage_today(usage_conn),
                       record_usage=lambda r, t: db.add_llm_usage(usage_conn, r, t))
    return client, cfg


def scratch_db():
    """In-memory copy of the real DB (or an empty one), so debug sees real dedup state and
    queues but keeps nothing."""
    mem = sqlite3.connect(":memory:")
    real = db.connect(readonly=True)
    if real is not None:
        real.backup(mem)
        real.close()
    mem.row_factory = sqlite3.Row
    mem.executescript(db.SCHEMA)
    return mem


def execute(args, conn, li_client, llm_client, llm_cfg, run_id, log):
    log(f"run {run_id or 'debug'}: collecting (source {args.source}, last {args.hours}h)")
    raw, report = collect(args, li_client)
    log(f"collected {len(raw)} cards from {len(report['emails'])} email(s) and "
        f"{len(report['searches'])} search(es)")
    cards = prepare(merge_cards(raw), conn)
    store(conn, cards, run_id)
    stages = run_stages(conn, li_client, llm_client, llm_cfg, args, log)
    log(f"linkedin: {li_client.summary()}")

    stats = dict(emails=len(report["emails"]), raw_cards=len(raw), cards=len(cards),
                 new=sum(not c["already_sent"] for c in cards),
                 search_requests=li_client.requests,
                 searches_capped=sum(s["capped"] for s in report["searches"]),
                 truncated_alerts=report["truncated"],
                 llm_requests=llm_client.requests if llm_client else 0,
                 llm_tokens=llm_client.tokens if llm_client else 0,
                 pending=db.count_pending(conn, args.backlog_days), **stages)
    stats["stopped"] = "; ".join(x for x in (
        li_client.stop_reason(), llm_client.stopped if llm_client else None) if x) or None

    email_only = [c for c in cards if "email" in c["sources"]
                  and not any(s.startswith("search:") for s in c["sources"])]
    stats["email_cards"] = sum("email" in c["sources"] for c in cards)
    stats["email_only"] = len(email_only)
    stats["email_only_pass"] = 0
    for c in email_only:
        j = db.get_job(conn, c["job_id"])
        stats["email_only_pass"] += bool(j and j["verdict"] and j["verdict"].get("verdict") == "send")
    return stats, report, cards


def _money(lo, hi):
    if hi is None:
        return None
    return f"${lo:,}-${hi:,}" if lo != hi else f"${hi:,}"


def candidate(j):
    """What n8n gets for one job: the fields, plus the ready-to-send Telegram text (HTML mode)."""
    v = j["verdict"] or {}
    salary = _money(j.get("salary_min"), j.get("salary_max")) or j.get("salary_text")
    e = lambda x: html.escape(str(x or "?"), quote=False)
    yoe = v.get("yoe_required")
    if yoe is None:
        yoe = j.get("yoe_min")
    lines = [f"<b>{e(j['title'])}</b>", f"{e(j['company'])} - {e(j['location'])}",
             f"YoE: {yoe}+ years" if yoe is not None else "YoE: not stated"]
    if salary:
        lines.append(f"Posted salary: {e(salary)}")
    if v.get("comp_note"):
        lines.append(f"Pay: {e(v['comp_note'])}")
    if v.get("summary"):
        lines += ["", e(v["summary"])]
    lines += ["", e(j["url"])]
    return {
        "job_key": j["job_id"], "url": j["url"], "title": j["title"], "company": j["company"],
        "location": j["location"], "salary": salary, "yoe": yoe, "yoe_min": j.get("yoe_min"),
        "seniority_level": j.get("seniority_level"), "posted": j.get("posted"),
        "discipline": v.get("discipline"), "seniority_fit": v.get("seniority_fit"),
        "comp_fit": v.get("comp_fit"), "comp_note": v.get("comp_note"), "reason": v.get("reason"),
        "confidence": v.get("confidence"), "summary": v.get("summary"),
        "judged_on": v.get("judged_on"), "message": "\n".join(lines),
    }


# --- debug -------------------------------------------------------------------

def cmd_debug(args):
    if args.file:
        args.source = "email"
    conn = scratch_db()
    real = db.connect(readonly=True)
    li = make_client(args)
    llm_client, llm_cfg = (None, None)
    if args.llm:
        usage_conn = db.connect()                 # the only write: LLM usage, so budgets hold
        llm_client, llm_cfg = make_llm(usage_conn)
        if llm_client is None:
            raise SystemExit("--llm needs LLM_API_KEY in .env")

    print(f"DB: {db.db_path()} ({'copied to scratch' if real else 'absent - starting empty'}); nothing is kept")
    print(f"source: {args.source} | hours: {args.hours} | max-pages {args.max_pages} | "
          f"page-delay {args.page_delay[0]:g}-{args.page_delay[1]:g}s | search-delay "
          f"{args.search_delay[0]:g}-{args.search_delay[1]:g}s | LinkedIn budget {args.max_minutes:g} min | "
          f"enrich: {'on, cap %d' % args.enrich_cap if args.enrich else 'off'} | LLM: "
          + (f"{llm_cfg['model']} ({llm_client.requests_left_today()} req / "
             f"{llm_client.tokens_left_today():,} tokens left today)" if llm_client else "off (use --llm)")
          + "\n")

    stats, report, cards = execute(args, conn, li, llm_client, llm_cfg, None, print)

    for e in report["emails"]:
        date = e["date"].isoformat(timespec="minutes") if e["date"] else "?"
        print(f"EMAIL {date}  {e['subject']}")
        print(f"  cards parsed: {len(e['cards'])}   alert total: {e['see_all'] if e['see_all'] is not None else '-'}"
              + (f"   ** TRUNCATED: {e['see_all'] - len(e['cards'])} not in the email **" if e["truncated"] else ""))
        if not e["html"]:
            print("  !! no text/html part")
        for c in e["cards"]:
            print(f"    - {c['title']}  |  {c['company']}  |  {c['location']}")
    for s in report["searches"]:
        print(f"SEARCH {s['keywords']!r}: {s['cards']} cards in {s['pages']} page(s)"
              + ("   (page limit reached)" if s["capped"] else "   (ran dry)" if not s["stopped"] else "")
              + (f"   [stopped: {s['stopped']}]" if s["stopped"] else ""))

    print("\n" + "=" * 100)
    sent = [c for c in cards if c["already_sent"]]
    if sent:
        print(f"already sent ({len(sent)}):")
        for c in sent:
            print(f"  - {c['title']}  |  {c['company']}")
    groups = {"yes": [], "maybe": [], "no": [], None: []}
    for c in cards:
        if not c["already_sent"]:
            j = db.get_job(conn, c["job_id"])
            c.update(j or {})
            groups.setdefault(c.get("triage"), []).append(c)
    for label, title in (("yes", "TRIAGE yes"), ("maybe", "TRIAGE maybe"), ("no", "TRIAGE no"),
                         (None, "not triaged" + ("" if llm_client else " (LLM off)"))):
        if not groups.get(label):
            continue
        print(f"\n{title} ({len(groups[label])}):")
        for c in groups[label]:
            v = c.get("verdict")
            if v:
                print(f"  [{v['verdict'].upper()}] {c['title']}  |  {c['company']}  |  {c['location']}")
                print(f"         discipline {v.get('discipline')} | seniority {v.get('seniority_fit')}"
                      f" (yoe {v.get('yoe_required')}) | pay {v.get('comp_fit')}: {v.get('comp_note')}"
                      f" | on {v.get('judged_on')}")
                print(f"         {v.get('reason')}")
            elif label in ("yes", "maybe"):
                extra = (f"enriched, yoe {c.get('yoe_min')}, salary "
                         f"{_money(c.get('salary_min'), c.get('salary_max')) or '-'}"
                         if c.get("enriched_at") else "not enriched")
                print(f"  [ -- ] {c['title']}  |  {c['company']}  ({extra}, not judged)")
            else:
                print(f"  - {c['title']}  |  {c['company']}")

    print("\n" + "=" * 100)
    print("funnel: emails {emails} | raw cards {raw_cards} -> unique {cards} -> new {new} -> "
          "triaged {triaged} (pass {triage_pass}) -> enriched {enriched} -> judged {judged} -> "
          "send {send}".format(**stats))
    print(f"LinkedIn requests {stats['search_requests']} | LLM {stats['llm_requests']} requests, "
          f"{stats['llm_tokens']:,} tokens | left for next run {stats['pending']} | "
          f"email jobs {stats['email_cards']} (not found by any search {stats['email_only']}) | "
          f"truncated alerts {stats['truncated_alerts']}"
          + (f" | stopped: {stats['stopped']}" if stats["stopped"] else ""))
    alerts = report["emails"]
    if alerts and not any(e["cards"] for e in alerts):
        print("!! job-alert emails arrived but 0 cards parsed: the parser may not match this template")
    if stats["triaged"] and not stats["triage_pass"]:
        print("!! triage passed nothing: check searches.yaml or the triage prompt")


# --- fetch / mark-notified / stats --------------------------------------------

def cmd_fetch(args):
    if args.file:
        args.source = "email"
    conn = db.connect()
    run_id = db.start_run(conn, args.hours)
    li = make_client(args)
    llm_client, llm_cfg = (None, None) if args.no_llm else make_llm(conn)
    if llm_client is None and not args.no_llm:
        log("!! LLM_API_KEY not set: collecting only, nothing will be judged")
    try:
        stats, report, cards = execute(args, conn, li, llm_client, llm_cfg, run_id, log)
        db.finish_run(conn, run_id, stats)
    except Exception as e:
        db.finish_run(conn, run_id, {}, error=f"{type(e).__name__}: {e}")
        log(f"run {run_id} FAILED: {type(e).__name__}: {e}")
        traceback.print_exc(file=sys.stderr)
        json.dump({"stats": {"run_id": run_id, "error": str(e)}, "candidates": []}, sys.stdout)
        sys.exit(1)

    if args.mark_read and report["emails"]:
        with Mailbox(readonly=False) as mb:
            mb.mark_seen([e["uid"] for e in report["emails"] if e.get("uid")])

    stats["run_id"] = run_id
    stats["searches"] = report["searches"]
    json.dump({"stats": stats, "candidates": [candidate(j) for j in db.to_send(conn)]},
              sys.stdout, indent=1)
    print()


def cmd_mark_notified(args):
    keys = [line.strip() for line in sys.stdin if line.strip()]
    conn = db.connect()
    n = db.mark_notified(conn, keys)
    if n:
        db.add_to_latest_run(conn, "notified", n)
    print(json.dumps({"received": len(keys), "marked": n}))


def cmd_stats(args):
    conn = db.connect(readonly=True)
    rows = list(reversed(db.run_history(conn, args.limit)))
    if not rows:
        print("no runs recorded")
        return
    cols = [("started_at", "started (UTC)", 19), ("emails", "email", 5), ("raw_cards", "raw", 4),
            ("cards", "uniq", 4), ("new", "new", 4), ("triaged", "tri", 4), ("triage_pass", "t-ok", 4),
            ("enriched", "enr", 4), ("judged", "jdg", 4), ("send", "send", 4), ("notified", "sent", 4),
            ("pending", "left", 4), ("llm_requests", "llm-r", 5), ("llm_tokens", "llm-tok", 7),
            ("search_requests", "li-r", 4), ("email_only", "e-only", 6),
            ("email_only_pass", "e-send", 6), ("truncated_alerts", "trunc", 5)]
    print("  ".join(h.rjust(w) if k != "started_at" else h.ljust(w) for k, h, w in cols) + "  notes")
    for r in rows:
        cells = [str(r[k] if r[k] is not None else "-")[:w] for k, _, w in cols]
        line = "  ".join(c.rjust(w) if k != "started_at" else c.ljust(w)
                         for c, (k, _, w) in zip(cells, cols))
        notes = "; ".join(x for x in (r.get("stopped"), r.get("error") and f"ERROR {r['error']}",
                                       None if r.get("finished_at") else "unfinished") if x)
        print(line + "  " + notes)
    v = db.email_value(conn, args.limit)
    print(f"\nalert emails, last {len(rows)} run(s): {v['email_jobs']} unique jobs, "
          f"{v['email_only']} never returned by a search, {v['email_only_sent']} of those sent to Telegram")
    print("  e-only / sent near 0 for a few weeks -> the guest searches cover the alerts; "
          "consider --source search")
    req, tok = db.llm_usage_today(conn)
    lim = load_config()["limits"]
    print(f"LLM today (UTC): {req} / {lim['rpd']} requests, {tok:,} / {lim['tpd']:,} tokens")


def cmd_llm_check(args):
    import requests
    cfg = load_config()
    key = os.environ.get("LLM_API_KEY")
    if not key:
        raise SystemExit("LLM_API_KEY not set in .env")
    r = requests.post(cfg["base_url"].rstrip("/") + "/chat/completions", timeout=60,
                      headers={"Authorization": f"Bearer {key}"},
                      json={"model": cfg["model"], "max_tokens": 5 + int(cfg.get("reasoning_tokens", 0)),
                            "messages": [{"role": "user", "content": "Reply with: ok"}],
                            **(cfg.get("extra_body") or {})})
    print(f"HTTP {r.status_code}  model {cfg['model']}  at {cfg['base_url']}")
    for h, v in sorted(r.headers.items()):
        if "ratelimit" in h.lower() or h.lower() == "retry-after":
            print(f"  {h}: {v}")
    if r.status_code == 200:
        print("reply:", r.json()["choices"][0]["message"]["content"].strip())
        conn = db.connect()
        db.add_llm_usage(conn, 1, (r.json().get("usage") or {}).get("total_tokens") or 0)
    else:
        print(r.text[:500])
    print("Compare x-ratelimit-limit-requests (per day) and x-ratelimit-limit-tokens (per minute)"
          " with llm.yaml limits.")


def main(argv=None):
    load_env()
    ap = argparse.ArgumentParser(prog="linkedin_source.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("debug", help="full run on a scratch DB copy, print every step, keep nothing")
    add_run_args(p)
    p.add_argument("--file", action="append",
                   help="parse a saved .eml or .html instead of IMAP (repeatable)")
    p.add_argument("--llm", action="store_true",
                   help="also run the LLM stages (uses today's LLM budget)")
    p.set_defaults(func=cmd_debug)

    p = sub.add_parser("fetch", help="full run, store everything, print JSON for n8n")
    add_run_args(p)
    p.add_argument("--no-llm", action="store_true", help="collect and enrich only")
    p.add_argument("--mark-read", action="store_true", help="mark processed alert emails as read")
    p.add_argument("--file", action="append",
                   help="use a saved .eml or .html instead of IMAP (repeatable; for testing)")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("mark-notified", help="read job_keys from stdin, set notified_at")
    p.set_defaults(func=cmd_mark_notified)

    p = sub.add_parser("stats", help="funnel history and today's LLM budget")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("llm-check", help="verify LLM key/model, print provider rate limits")
    p.set_defaults(func=cmd_llm_check)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
