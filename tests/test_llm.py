import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from judge import decide, excerpt, judge, triage
from llm import LLMClient, LLMStop, pack, parse_json

CFG = {"base_url": "https://llm.test/v1", "model": "m", "safety": 1.0,
       "limits": {"rpm": 30, "rpd": 1000, "tpm": 6000, "tpd": 100000, "context": 131072},
       "triage_batch_max": 80, "judge_batch_max": 10, "judge_desc_chars": 3500}


class Resp:
    def __init__(self, status=200, content=None, tokens=100, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self._content = content
        self._tokens = tokens
        self.text = "err"

    def json(self):
        return {"choices": [{"message": {"content": self._content}}],
                "usage": {"total_tokens": self._tokens}}


class FakeProvider:
    """Answers each request with `handler(user_prompt)`, or a queued Resp."""

    def __init__(self, handler=None, queue=()):
        self.handler = handler
        self.queue = list(queue)
        self.bodies = []

    def post(self, url, json=None, timeout=None, headers=None):
        self.bodies.append(json)
        if self.queue:
            return self.queue.pop(0)
        return Resp(content=self.handler(json["messages"][1]["content"]))


def make_client(provider, cfg=CFG, used=(0, 0)):
    t = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        t[0] += s
    usage = []
    c = LLMClient(cfg, "k", used_today=used, record_usage=lambda r, n: usage.append((r, n)),
                  session=provider, sleep=sleep, clock=lambda: t[0])
    return c, slept, usage


def triage_handler(labels):
    def h(user):
        rows = [l for l in user.splitlines()[1:] if l.strip()]
        out = []
        for row in rows:
            i, title = row.split(" | ")[:2]
            out.append({"id": int(i), "t": labels(title)})
        return json.dumps({"results": out})
    return h


def jobs(n, desc=None):
    return [{"job_id": str(i), "fingerprint": f"fp{i}", "title": f"Security Engineer {i}",
             "company": "Acme", "location": "Remote", "description": desc} for i in range(n)]


class PackTest(unittest.TestCase):
    def test_respects_token_cap_and_item_cap(self):
        b = pack(list(range(10)), lambda i: 100, overhead=50, out_per_item=10, max_tokens=500, max_items=3)
        self.assertEqual([len(x) for x in b], [3, 3, 3, 1])
        b = pack(list(range(10)), lambda i: 100, overhead=50, out_per_item=10, max_tokens=300, max_items=9)
        self.assertEqual([len(x) for x in b], [2, 2, 2, 2, 2])

    def test_oversized_item_gets_own_batch(self):
        self.assertEqual(pack([1, 2], lambda i: 900, 50, 10, 500, 5), [[1], [2]])

    def test_parse_json_tolerates_fences(self):
        self.assertEqual(parse_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(parse_json('Sure! {"a": 2} hope that helps'), {"a": 2})


class ClientTest(unittest.TestCase):
    def test_usage_recorded_and_json_mode(self):
        p = FakeProvider(queue=[Resp(content='{"ok": true}', tokens=321)])
        c, _, usage = make_client(p)
        self.assertEqual(c.chat("sys", "user", 50), {"ok": True})
        self.assertEqual(usage, [(1, 321)])
        self.assertEqual(p.bodies[0]["temperature"], 0)
        self.assertEqual(p.bodies[0]["response_format"], {"type": "json_object"})

    def test_daily_token_budget_stops_before_sending(self):
        p = FakeProvider(queue=[Resp(content="{}")])
        c, _, _ = make_client(p, used=(0, 99990))
        with self.assertRaises(LLMStop):
            c.chat("sys", "x" * 300, 50)
        self.assertEqual(p.bodies, [])

    def test_daily_request_budget(self):
        c, _, _ = make_client(FakeProvider(queue=[Resp(content="{}")]), used=(1000, 0))
        with self.assertRaises(LLMStop):
            c.chat("s", "u", 10)

    def test_short_429_waits_and_retries(self):
        p = FakeProvider(queue=[Resp(429, headers={"retry-after": "7"}), Resp(content='{"a": 1}')])
        c, slept, _ = make_client(p)
        self.assertEqual(c.chat("s", "u", 10), {"a": 1})
        self.assertIn(8.0, slept)

    def test_long_429_means_quota_spent(self):
        p = FakeProvider(queue=[Resp(429, headers={"retry-after": "3600"})])
        c, _, _ = make_client(p)
        with self.assertRaises(LLMStop):
            c.chat("s", "u", 10)
        self.assertIn("quota", c.stopped)

    def test_per_minute_tokens_paced(self):
        p = FakeProvider(handler=lambda u: "{}")
        c, slept, _ = make_client(p)
        p.queue = [Resp(content="{}", tokens=5000), Resp(content="{}", tokens=10)]
        c.chat("s", "u", 10)
        c.chat("s", "u" * 3000, 10)       # ~1000 more tokens would exceed 6000/min
        self.assertTrue(slept and slept[0] >= 59)


    def test_lone_request_over_minute_budget_does_not_crash(self):
        # Regression, 2026-10-20 daily run: empty window + estimate above tpm -> IndexError.
        p = FakeProvider(queue=[Resp(content="{}", tokens=3000), Resp(content='{"ok": 1}')])
        c, slept, _ = make_client(p)
        c.chat("s", "u", 10)
        c.clock = lambda: 1000.0                       # previous request has left the window
        self.assertEqual(c.chat("s", "x" * 18500, 10), {"ok": 1})   # ~6170 + 10 tokens > 6000 tpm


class PackConsistencyTest(unittest.TestCase):
    def test_judge_batches_never_exceed_request_cap(self):
        cfg = {**CFG, "reasoning_tokens": 800, "limits": {**CFG["limits"], "tpm": 8000}, "safety": 0.8}
        p = FakeProvider(handler=judge_handler)
        c, _, _ = make_client(p, cfg=cfg)
        jobs_ = [{**j, "description": "Responsibilities\n" + ("word " * 900)} for j in jobs(9)]
        judge(c, jobs_, cfg, lambda j, v: None)
        for b in p.bodies:
            est = sum(len(m["content"]) // 3 + 1 for m in b["messages"]) + b["max_tokens"]
            self.assertLessEqual(est, c.request_max)


class DecideTest(unittest.TestCase):
    def test_only_definite_negatives_reject(self):
        base = {"discipline": "appsec", "seniority_fit": "good", "comp_fit": "likely_above"}
        self.assertEqual(decide(base), "send")
        self.assertEqual(decide({**base, "seniority_fit": "unclear", "comp_fit": "unclear"}), "send")
        self.assertEqual(decide({**base, "discipline": "other"}), "reject")
        self.assertEqual(decide({**base, "seniority_fit": "role_too_senior"}), "reject")
        self.assertEqual(decide({**base, "seniority_fit": "role_too_junior", "yoe_required": 0}), "reject")
        self.assertEqual(decide({**base, "seniority_fit": "role_too_senior", "yoe_required": 8}), "reject")
        self.assertEqual(decide({**base, "comp_fit": "likely_below"}), "reject")
        self.assertEqual(decide({**base, "discipline": "soc"}), "reject")


    def test_seniority_label_contradicting_yoe_is_ignored(self):
        v = {"discipline": "appsec", "seniority_fit": "too_junior", "yoe_required": 5,
             "comp_fit": "likely_above"}                 # real reply for a "5+ years" posting
        self.assertEqual(decide(v), "send")


class ExcerptTest(unittest.TestCase):
    def test_keeps_opening_and_requirement_lines(self):
        text = "\n".join(["We build payments."] + [f"Filler paragraph {i} " * 8 for i in range(60)]
                         + ["You have 3+ years of application security experience.",
                            "The base pay range is $180,000-$220,000."])
        out = excerpt(text, 1200)
        self.assertLessEqual(len(out), 1200)
        self.assertTrue(out.startswith("We build payments."))
        self.assertIn("3+ years", out)
        self.assertIn("$180,000", out)
        self.assertIn("[...]", out)

    def test_short_text_untouched(self):
        self.assertEqual(excerpt("short", 100), "short")


class TriageTest(unittest.TestCase):
    def test_batches_and_labels(self):
        p = FakeProvider(handler=triage_handler(lambda t: "no" if t.endswith("3") else "yes"))
        c, _, _ = make_client(p)
        got = {}
        triage(c, jobs(200), CFG, lambda j, l: got.__setitem__(j["job_id"], l))
        self.assertEqual(len(got), 200)
        self.assertEqual(got["3"], "no")
        self.assertEqual(got["4"], "yes")
        self.assertEqual(len(p.bodies), 3)                # 80 + 80 + 40

    def test_missing_ids_default_to_maybe(self):
        p = FakeProvider(handler=lambda u: json.dumps({"results": [{"id": 1, "t": "no"}]}))
        c, _, _ = make_client(p)
        got = {}
        triage(c, jobs(3), CFG, lambda j, l: got.__setitem__(j["job_id"], l))
        self.assertEqual(got, {"0": "no", "1": "maybe", "2": "maybe"})

    def test_budget_stop_leaves_rest_untriaged(self):
        p = FakeProvider(handler=triage_handler(lambda t: "yes"))
        c, _, _ = make_client(p, used=(999, 0))           # one request left today
        got = {}
        triage(c, jobs(200), CFG, lambda j, l: got.__setitem__(j["job_id"], l))
        self.assertEqual(len(got), 80)


def judge_handler(user):
    ids = [int(l.split()[-1]) for l in user.splitlines() if l.startswith("### id")]
    return json.dumps({"results": [
        {"id": i, "discipline": "appsec", "seniority_fit": "unclear", "comp_fit": "likely_above",
         "comp_note": "big tech", "reason": "appsec role", "confidence": 0.7, "summary": "s"}
        for i in ids]})


class JudgeTest(unittest.TestCase):
    def test_verdicts_and_batching_within_request_cap(self):
        p = FakeProvider(handler=judge_handler)
        c, _, _ = make_client(p)
        got = {}
        judge(c, jobs(6, desc="Responsibilities: threat modeling.\n" + "x" * 5000), CFG,
              lambda j, v: got.__setitem__(j["job_id"], v))
        self.assertEqual(len(got), 6)
        self.assertEqual(got["0"]["verdict"], "send")
        self.assertEqual(got["0"]["judged_on"], "posting")
        for b in p.bodies:                                 # every request fits 6000 tokens
            est = sum(len(m["content"]) for m in b["messages"]) // 3 + b["max_tokens"]
            self.assertLessEqual(est, 6000)

    def test_excerpt_shrinks_to_fit_daily_budget(self):
        p = FakeProvider(handler=judge_handler)
        c, _, _ = make_client(p, used=(0, 100000 - 12000))     # 12k tokens left today
        got = {}
        judge(c, jobs(6, desc="y" * 6000), CFG, lambda j, v: got.__setitem__(j["job_id"], v))
        self.assertEqual(len(got), 6)
        self.assertLessEqual(c.tokens, 12000)

    def test_title_only_marked(self):
        p = FakeProvider(handler=judge_handler)
        c, _, _ = make_client(p)
        got = {}
        judge(c, jobs(1), CFG, lambda j, v: got.__setitem__(j["job_id"], v))
        self.assertEqual(got["0"]["judged_on"], "title only")
        self.assertIn("(posting text unavailable)", p.bodies[0]["messages"][1]["content"])


if __name__ == "__main__":
    unittest.main()
