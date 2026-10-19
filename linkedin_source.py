#!/opt/jobagent/venv/bin/python
"""jobagent CLI. Subcommands: debug (built), fetch / mark-notified / stats (pending)."""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
import filters
from email_parser import parse_alert_html
from imap_source import Mailbox, load_env, read_local_file
from pipeline import evaluate_card


def add_filter_args(p):
    p.add_argument("--hours", type=int, default=26)
    p.add_argument("--max-tier", type=int, default=2)
    p.add_argument("--unknown-tier", type=int, default=2)
    p.add_argument("--max-yoe", type=int, default=6)
    p.add_argument("--base-floor", type=int, default=150000)


def load_emails(args):
    if args.file:
        return [read_local_file(f) for f in args.file]
    with Mailbox(readonly=True) as mb:
        return mb.alert_emails(args.hours)


def cmd_debug(args):
    emails = load_emails(args)
    conn = db.connect(readonly=True)          # never creates or writes the DB
    tiers = filters.load_tiers()
    seen = set()
    totals = dict(emails=len(emails), cards=0, title_pass=0, new=0, tier_pass=0, passed=0, truncated=0)

    print(f"DB: {db.db_path()} ({'present' if conn else 'absent - dedup skipped'})")
    print(f"companies.yaml: {len(tiers)} companies | max-tier {args.max_tier} | "
          f"unknown-tier {args.unknown_tier} | base-floor ${args.base_floor:,}")
    print(f"{len(emails)} email(s)" + ("" if args.file else f" in the last {args.hours}h") + "\n")

    for e in emails:
        parsed = parse_alert_html(e["html"])
        cards = parsed["cards"]
        n_see = parsed["see_all"]
        truncated = n_see is not None and n_see > len(cards)
        totals["cards"] += len(cards)
        totals["truncated"] += int(truncated)
        date = e["date"].isoformat(timespec="minutes") if e["date"] else "?"
        print("=" * 100)
        print(f"{date}  {e['subject']}")
        print(f"  cards parsed: {len(cards)}   see-all: {n_see if n_see is not None else '-'}"
              + (f"   ** TRUNCATED: {n_see - len(cards)} not shown, alert too broad **" if truncated else ""))
        if not e["html"]:
            print("  !! no text/html part")
        for c in cards:
            r = evaluate_card(c, conn, tiers, args, seen)
            stage_rank = ["title", "dedup", "tier", "prefilter"].index(r["stage"])
            passed_title = r["stage"] != "title"
            totals["title_pass"] += passed_title
            totals["new"] += passed_title and r["stage"] != "dedup"
            totals["tier_pass"] += stage_rank >= 3
            totals["passed"] += r["passed"]
            flag = "PASS" if r["passed"] else "drop"
            print(f"  [{flag}] {r['title']}")
            print(f"         company:  {r['company']!s:<40} location: {r['location']}")
            extra = f"         id: {r['job_id']}  tier: {r['tier'] if r['tier'] is not None else '-'}"
            if r.get("salary_text"):
                extra += f"  salary: {r['salary_text']}"
            print(extra)
            print(f"         -> {r['stage']}: {r['reason']}")
            for w in r["warnings"]:
                print(f"         !! {w}")
        print()

    print("=" * 100)
    print("funnel: emails {emails} -> cards {cards} -> title-pass {title_pass} -> new {new} "
          "-> tier-pass {tier_pass} -> to LLM {passed}   truncated alerts: {truncated}".format(**totals))
    if totals["emails"] and not totals["cards"]:
        print("!! emails arrived but 0 cards parsed: the parser likely does not match this template")
    elif totals["cards"] and not totals["title_pass"]:
        print("!! cards parsed but none passed the title filter: alerts may be misconfigured")


def main(argv=None):
    load_env()
    ap = argparse.ArgumentParser(prog="linkedin_source.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("debug", help="parse recent alert emails, print extraction, change nothing")
    add_filter_args(p)
    p.add_argument("--file", action="append",
                   help="parse a saved .eml or .html instead of IMAP (repeatable)")
    p.set_defaults(func=cmd_debug)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
