#!/usr/bin/env python3
"""Generate the n8n workflow JSON (daily + test variants) for jobagent.

    python3 n8n/build_workflow.py            # chat ID from TELEGRAM_CHAT_ID in /opt/jobagent/.env

writes n8n/jobagent.workflow.json and n8n/jobagent-test.workflow.json (gitignored: they contain
your Telegram chat ID); import with
    set -a; . /opt/n8n.env; set +a; n8n import:workflow --input=n8n/jobagent.workflow.json

Flow:
  Daily 10:00 / Manual run -> Fetch jobs -> Parse -> Any matches?
      yes -> Send job (one Telegram message each) -> Collect sent keys -> Mark notified
      no  -> No matches (one line)
  Fetch / Parse failure -> Fetch failed (Telegram alert)

Mark notified runs last and only with the keys Telegram accepted, so a crash or a failed
send means the job is offered again next run instead of being lost.
"""
import argparse
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(os.path.dirname(HERE), ".env")


def env_value(key):
    """KEY from the environment, else from the project's .env."""
    if os.environ.get(key):
        return os.environ[key]
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k.strip() == key:
                    return v.strip().strip('"').strip("'")
    return None
CLI = "cd /opt/jobagent && {env}venv/bin/python linkedin_source.py"

PARSE_JS = r"""const out = $input.first().json;
let data;
try {
  data = JSON.parse(out.stdout);
} catch (e) {
  throw new Error('fetch did not print JSON. stderr tail: ' + String(out.stderr || '').slice(-800));
}
const candidates = data.candidates || [];
if (!candidates.length) return [{ json: { none: true, stats: data.stats || {} } }];
return candidates.map(c => ({ json: { none: false, ...c } }));"""

COLLECT_JS = r"""// Keys of the messages Telegram accepted (failed sends go out the error branch and are
// offered again next run). Numeric-only: they are interpolated into a shell command.
const keys = $input.all()
  .map((_, i) => String($('Parse').itemMatching(i).json.job_key))
  .filter(k => /^\d+$/.test(k));
if (!keys.length) return [];
return [{ json: { keys: keys.join(' '), count: keys.length } }];"""

ESC = ".replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')"
NO_MATCH_TEXT = ("={{ 'No new matches today. (' + ($json.stats.cards ?? 0) + ' jobs checked, '"
                 " + ($json.stats.judged ?? 0) + ' judged'"
                 " + ($json.stats.pending ? ', ' + $json.stats.pending + ' left for tomorrow' : '')"
                 " + ($json.stats.stopped ? '; stopped early: ' + String($json.stats.stopped)" + ESC + " : '') + ')' }}")

# HTML parse mode: every interpolated string is escaped (ESC). The failure text keeps the END of
# the error: a traceback's last lines name the actual exception.
FAIL_TEXT = ("={{ '<b>jobagent: daily fetch FAILED</b>\\n' + String($json.error?.message ?? $json.error ?? "
             "$json.message ?? 'see the n8n execution log').slice(-600)" + ESC + " }}")


def node(name, type_, version, position, params, **extra):
    n = {"parameters": params, "id": name.lower().replace(" ", "-").replace("?", ""), "name": name,
         "type": type_, "typeVersion": version, "position": position}
    n.update(extra)
    return n


def telegram(name, position, chat_id, cred_id, text, **extra):
    return node(name, "n8n-nodes-base.telegram", 1.2, position,
                {"chatId": chat_id, "text": text,
                 "additionalFields": {"appendAttribution": False, "disable_web_page_preview": True,
                                      "parse_mode": "HTML"}},
                credentials={"telegramApi": {"id": cred_id, "name": "jobagent Telegram"}},
                retryOnFail=True, maxTries=3, waitBetweenTries=5000, **extra)


def build(chat_id, cred_id, test=False):
    env = ("JOBAGENT_DB=/opt/jobagent/test.db JOBAGENT_LOG=/opt/jobagent/logs/test.log " if test
           else "JOBAGENT_LOG=/opt/jobagent/logs/jobagent.log ")
    fetch_args = " --file samples/alert-2026-03-01.eml" if test else ""
    nodes = []
    # A manual trigger in both: "Run now" in the UI, and the start node `n8n execute` needs.
    triggers = [node("Manual run", "n8n-nodes-base.manualTrigger", 1, [0, 420], {})]
    if not test:
        triggers.append(node("Daily 10:00", "n8n-nodes-base.scheduleTrigger", 1.2, [0, 200],
                             {"rule": {"interval": [{"field": "cronExpression", "expression": "0 10 * * *"}]}}))
    nodes += triggers + [
        node("Fetch jobs", "n8n-nodes-base.executeCommand", 1, [240, 300],
             {"command": CLI.format(env=env) + " fetch" + fetch_args},
             onError="continueErrorOutput",
             notes="Collect, triage, enrich, judge (20-30 min). JSON on stdout, progress on stderr."),
        node("Parse", "n8n-nodes-base.code", 2, [480, 280], {"jsCode": PARSE_JS},
             onError="continueErrorOutput"),
        node("Any matches?", "n8n-nodes-base.if", 2.2, [720, 280], {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
                "conditions": [{"id": "none-check", "leftValue": "={{ $json.none }}", "rightValue": "",
                                "operator": {"type": "boolean", "operation": "false", "singleValue": True}}],
                "combinator": "and"},
            "options": {}}),
        telegram("Send job", [960, 180], chat_id, cred_id, "={{ $json.message }}",
                 onError="continueErrorOutput"),
        node("Collect sent keys", "n8n-nodes-base.code", 2, [1200, 160], {"jsCode": COLLECT_JS}),
        node("Mark notified", "n8n-nodes-base.executeCommand", 1, [1440, 160],
             {"command": "=" + CLI.format(env=env).replace(" && ", " && printf '%s\\n' {{ $json.keys }} | ", 1)
              + " mark-notified"}),
        telegram("No matches", [960, 400], chat_id, cred_id, NO_MATCH_TEXT),
        telegram("Fetch failed", [720, 560], chat_id, cred_id, FAIL_TEXT),
    ]
    c = lambda *targets: [[{"node": t, "type": "main", "index": 0} for t in targets]]
    connections = {
        **{t["name"]: {"main": c("Fetch jobs")} for t in triggers},
        "Fetch jobs": {"main": c("Parse") + c("Fetch failed")},
        "Parse": {"main": c("Any matches?") + c("Fetch failed")},
        "Any matches?": {"main": c("Send job") + c("No matches")},
        "Send job": {"main": c("Collect sent keys") + [[]]},
        "Collect sent keys": {"main": c("Mark notified")},
    }
    return {
        "id": "JobAgentTest0001" if test else "JobAgentDaily001",
        "name": "jobagent (test)" if test else "jobagent daily",
        "nodes": nodes, "connections": connections, "active": False,
        "settings": {"executionOrder": "v1", "timezone": "America/Los_Angeles",
                     "saveManualExecutions": True},
        "pinData": {},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat-id", default=env_value("TELEGRAM_CHAT_ID"),
                    help="Telegram chat ID (default: TELEGRAM_CHAT_ID from .env)")
    ap.add_argument("--cred-id", default="JobAgentTgCred01", help="n8n Telegram credential ID")
    a = ap.parse_args()
    if not a.chat_id:
        ap.error("no chat ID: set TELEGRAM_CHAT_ID in .env or pass --chat-id")
    for test, fname in ((False, "jobagent.workflow.json"), (True, "jobagent-test.workflow.json")):
        with open(os.path.join(HERE, fname), "w") as f:
            json.dump(build(a.chat_id, a.cred_id, test), f, indent=2)
        print("wrote", os.path.join(HERE, fname))


if __name__ == "__main__":
    main()
