import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import linkedin_source
from test_llm import CFG, FakeProvider, make_client as make_llm_client
from test_search_enrich import POSTING, FakeClient, search_card

ALERT = """<p>30+ new jobs in United States match your preferences.</p>
<table><tr><td><a href="https://www.linkedin.com/comm/jobs/view/4400000001/?trk=x">Application Security Engineer</a>
<p>Acme · Remote</p></td></tr></table>"""

EMAIL = {"uid": "1", "subject": "Your job alert", "from": "jobalerts-noreply@linkedin.com",
         "date": None, "html": ALERT}

SEARCH_PAGE = (search_card("4400000001", "Application Security Engineer", "Acme", "Remote")
               + search_card("4400000002", "Product Security Engineer II (Remote)", "Globex", "Seattle, WA")
               + search_card("4400000003", "SOC Analyst", "Initech", "Austin, TX"))


def fake_llm(user):
    """Triage: SOC -> no, else yes. Judge: Globex pays below the bar, everything else sends."""
    lines = user.splitlines()
    if lines and lines[0].startswith("id | title"):
        out = [{"id": int(l.split(" | ")[0]), "t": "no" if "SOC" in l else "yes"} for l in lines[1:]]
        return json.dumps({"results": out})
    out, cur = [], None
    for l in lines:
        if l.startswith("### id"):
            cur = {"id": int(l.split()[-1]), "role_type": "appsec", "offensive_work": "significant",
                   "offensive_evidence": "manual pentests of web apps", "seniority_fit": "good",
                   "comp_fit": "likely_above", "comp_note": "pays well", "reason": "appsec",
                   "confidence": 0.8, "summary": "Threat modeling and code review."}
            out.append(cur)
        elif l.startswith("company:") and "Globex" in l:
            cur["comp_fit"] = "likely_below"
    return json.dumps({"results": out})


class CliCycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"JOBAGENT_DB": os.path.join(self.tmp.name, "t.db")})
        self.env.start()
        self.provider = FakeProvider(handler=fake_llm)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_cli(self, argv, stdin="", responses=(), emails=(), llm=True):
        client = FakeClient(responses)

        def make_llm(conn):
            if not llm:
                return None, CFG
            c, _, _ = make_llm_client(self.provider)
            c.record_usage = lambda r, t: db.add_llm_usage(conn, r, t)
            return c, CFG
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(linkedin_source, "make_client", return_value=client), \
             mock.patch.object(linkedin_source, "make_llm", side_effect=make_llm), \
             mock.patch.object(linkedin_source, "load_searches", return_value=[{"keywords": "appsec"}]), \
             mock.patch.object(linkedin_source, "load_emails", return_value=list(emails)), \
             mock.patch.object(linkedin_source, "load_env"), \
             mock.patch.object(linkedin_source, "load_config", return_value=CFG), \
             mock.patch("sys.stdin", io.StringIO(stdin)), redirect_stdout(out), redirect_stderr(err):
            linkedin_source.main(argv)
        return out.getvalue(), client

    def fetch(self, **kw):
        out, client = self.run_cli(["fetch"], **kw)
        return json.loads(out), client

    def test_full_cycle(self):
        data, li = self.fetch(responses=[SEARCH_PAGE, POSTING, POSTING], emails=[EMAIL])
        st = data["stats"]
        self.assertEqual((st["raw_cards"], st["cards"], st["new"]), (4, 3, 3))
        self.assertEqual((st["triaged"], st["triage_pass"], st["enriched"]), (3, 2, 2))
        self.assertEqual((st["judged"], st["send"], st["pending"]), (2, 1, 0))
        self.assertEqual(st["truncated_alerts"], 1)
        self.assertEqual((st["email_cards"], st["email_only"]), (1, 0))
        self.assertEqual(len(li.calls), 3)                   # 1 search page + 2 postings, no SOC fetch

        self.assertEqual([c["job_key"] for c in data["candidates"]], ["4400000001"])
        c = data["candidates"][0]
        self.assertEqual(c["salary"], "$170,000-$210,000")
        self.assertEqual(c["judged_on"], "posting")
        self.assertIn("Threat modeling", c["message"])
        self.assertIn("YoE: 3+ years", c["message"])           # parsed from the posting
        self.assertEqual(c["yoe"], 3)
        self.assertIn("https://www.linkedin.com/jobs/view/4400000001/", c["message"])

        conn = db.connect()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 3)
        self.assertEqual(db.llm_usage_today(conn)[0], 2)    # one triage + one judge request

        out, _ = self.run_cli(["mark-notified"], stdin="4400000001\n")
        self.assertEqual(json.loads(out), {"received": 1, "marked": 1})

        # Next day: job 1 reposted under a new ID with a level suffix; job 2 resurfaces.
        repost = (search_card("4499999999", "Application Security Engineer II", "Acme", "Remote")
                  + search_card("4400000002", "Product Security Engineer II (Remote)", "Globex", "Seattle, WA"))
        n_llm = len(self.provider.bodies)
        data, li = self.fetch(responses=[repost])
        self.assertEqual(data["candidates"], [])
        self.assertEqual(data["stats"]["new"], 1)            # repost counts as already sent
        self.assertEqual(len(li.calls), 1)                   # no enrichment: nothing new to judge
        self.assertEqual(len(self.provider.bodies), n_llm)   # no LLM calls either

        out, _ = self.run_cli(["stats"])
        self.assertIn("1 unique jobs, 0 never returned by a search", out)
        self.assertIn("LLM today (UTC): 2 / 1000 requests", out)

    def test_unsent_send_is_offered_again(self):
        """Crash between fetch and mark-notified: the job comes back on the next run."""
        self.fetch(responses=[SEARCH_PAGE, POSTING, POSTING], emails=[EMAIL])
        data, _ = self.fetch(responses=[], emails=[])
        self.assertEqual([c["job_key"] for c in data["candidates"]], ["4400000001"])

    def test_linkedin_block_keeps_email_jobs_for_next_run(self):
        data, _ = self.fetch(responses=[429], emails=[EMAIL])
        self.assertEqual(data["stats"]["stopped"], "LinkedIn returned 429")
        self.assertEqual(data["candidates"], [])             # triaged yes, waiting for enrichment
        self.assertEqual(data["stats"]["pending"], 1)
        self.assertEqual(data["stats"]["email_only"], 1)
        data, _ = self.fetch(responses=["", POSTING], emails=[])   # empty search page, then the posting
        self.assertEqual([c["job_key"] for c in data["candidates"]], ["4400000001"])
        self.assertEqual(data["candidates"][0]["judged_on"], "posting")

    def test_no_llm_key_collects_only(self):
        data, li = self.fetch(responses=[SEARCH_PAGE], emails=[EMAIL], llm=False)
        self.assertEqual((data["stats"]["cards"], data["stats"]["triaged"]), (3, 0))
        self.assertEqual(data["stats"]["pending"], 3)
        self.assertEqual(len(li.calls), 1)

    def test_debug_keeps_nothing(self):
        out, _ = self.run_cli(["debug", "--llm"], responses=[SEARCH_PAGE, POSTING, POSTING],
                              emails=[EMAIL])
        self.assertIn("[SEND] Application Security Engineer", out)
        self.assertIn("[REJECT] Product Security Engineer II (Remote)", out)
        conn = db.connect()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
        self.assertEqual(db.llm_usage_today(conn)[0], 2)     # but LLM usage is counted


if __name__ == "__main__":
    unittest.main()
