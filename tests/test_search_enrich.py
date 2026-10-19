import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from email_parser import parse_alert_html
from enrich import parse_posting_html
from linkedin_http import Blocked, GuestClient, OutOfTime, parse_range
from pipeline import merge_cards
from search_source import parse_search_html, run_searches, search_params


def search_card(jid, title, company, location, salary=None):
    sal = f'<span class="job-search-card__salary-info">{salary}</span>' if salary else ""
    return f"""
<li><div class="base-card base-search-card job-search-card" data-entity-urn="urn:li:jobPosting:{jid}">
  <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/some-slug-{jid}?position=1&amp;refId=x">
    <span class="sr-only">{title}</span></a>
  <div class="base-search-card__info">
    <h3 class="base-search-card__title">
      {title}
    </h3>
    <h4 class="base-search-card__subtitle"><a href="https://www.linkedin.com/company/x">{company}</a></h4>
    <div class="base-search-card__metadata">
      <span class="job-search-card__location">{location}</span>
      {sal}
      <span class="job-posting-benefits__text">Actively Hiring +4 benefits</span>
      <time class="job-search-card__listdate--new" datetime="2026-10-18">14 hours ago</time>
    </div>
  </div>
</div></li>"""


def page(n, base=4400000000):
    return "".join(search_card(base + i, f"Product Security Engineer {i}", "Acme", "Remote")
                   for i in range(n))


POSTING = """
<section class="top-card-layout"><h2 class="top-card-layout__title">AppSec Engineer</h2></section>
<div class="description__text"><div class="show-more-less-html__markup">
  <p>You will own threat modeling and secure code review.</p>
  <ul><li>5+ years of software engineering</li><li>3+ years in application security</li></ul>
  <p>Our office is at 1000 West Maude Avenue. The base pay range is $170,000 - $210,000.</p>
</div></div>
<ul class="description__job-criteria-list">
  <li class="description__job-criteria-item">
    <h3 class="description__job-criteria-subheader">Seniority level</h3>
    <span class="description__job-criteria-text">Mid-Senior level</span></li>
  <li class="description__job-criteria-item">
    <h3 class="description__job-criteria-subheader">Employment type</h3>
    <span class="description__job-criteria-text">Full-time</span></li>
</ul>
<section class="similar-jobs"><p>Other job: $95,000 - $120,000</p></section>
"""


class FakeClient:
    """Serves canned responses in order; a response that is an int is an HTTP block."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.blocked = None
        self.requests = 0

    throttled = False

    def stop_reason(self):
        return f"LinkedIn returned {self.blocked}" if self.blocked else None

    def summary(self):
        return f"{self.requests} requests"

    @property
    def stopped(self):
        return bool(self.blocked)

    def get(self, url, params=None, delay=None):
        if self.blocked:
            raise Blocked(self.blocked)
        self.calls.append((url, params, delay))
        self.requests += 1
        r = self.responses.pop(0) if self.responses else None
        if isinstance(r, int):
            self.blocked = r
            raise Blocked(r)
        return r


class SearchParseTest(unittest.TestCase):
    def test_fields(self):
        cards = parse_search_html(search_card("4441896001", "Senior Application Security Engineer",
                                              "East West Bank", "San Marino, CA", "$150K - $190K"))
        self.assertEqual(len(cards), 1)
        c = cards[0]
        self.assertEqual(c["job_id"], "4441896001")
        self.assertEqual(c["title"], "Senior Application Security Engineer")
        self.assertEqual(c["company"], "East West Bank")
        self.assertEqual(c["location"], "San Marino, CA")
        self.assertEqual(c["salary_text"], "$150K - $190K")
        self.assertEqual(c["posted"], "2026-10-18")
        self.assertEqual(c["url"], "https://www.linkedin.com/jobs/view/4441896001/")

    def test_page_of_ten(self):
        self.assertEqual(len(parse_search_html(page(10))), 10)

    def test_empty(self):
        self.assertEqual(parse_search_html(""), [])

    def test_params(self):
        p = search_params({"keywords": "red team", "geo_id": 103644278}, 26, 20)
        self.assertEqual(p, {"keywords": "red team", "f_TPR": "r93600", "start": 20, "geoId": "103644278"})


class RunSearchesTest(unittest.TestCase):
    def test_pages_until_short_page(self):
        client = FakeClient([page(10), page(10, 4500000000), page(3, 4600000000)])
        cards, info = run_searches(client, [{"keywords": "appsec"}], 26, max_pages=5)
        self.assertEqual(len(cards), 23)
        self.assertEqual(info[0]["pages"], 3)
        self.assertFalse(info[0]["capped"])
        self.assertEqual([p["start"] for _, p, _ in client.calls], [0, 10, 20])

    def test_capped(self):
        client = FakeClient([page(10), page(10, 4500000000)])
        _, info = run_searches(client, [{"keywords": "appsec"}], 26, max_pages=2)
        self.assertTrue(info[0]["capped"])

    def test_block_stops_every_search(self):
        client = FakeClient([page(10), 999])
        cards, info = run_searches(client, [{"keywords": "a"}, {"keywords": "b"}], 26, max_pages=5)
        self.assertEqual(len(cards), 10)
        self.assertEqual(len(client.calls), 2)       # never tried search "b"
        self.assertEqual(client.blocked, 999)
        self.assertEqual([i["stopped"] for i in info], ["LinkedIn returned 999", "not run"])

    def test_per_search_max_pages(self):
        client = FakeClient([page(10), page(10, 4500000000), page(10, 4600000000)])
        _, info = run_searches(client, [{"keywords": "a", "max_pages": 1}, {"keywords": "b"}], 26,
                               max_pages=2)
        self.assertEqual([(i["pages"], i["capped"]) for i in info], [(1, True), (2, True)])

    def test_repeated_page_ends_search(self):
        client = FakeClient([page(10), page(10)])     # LinkedIn served page 1 again
        cards, info = run_searches(client, [{"keywords": "a"}], 26, max_pages=5)
        self.assertEqual(len(cards), 10)
        self.assertEqual(info[0]["pages"], 2)

    def test_search_delay_only_before_later_searches(self):
        client = FakeClient([page(10), page(2, 4500000000), page(1, 4600000000)])
        run_searches(client, [{"keywords": "a"}, {"keywords": "b"}], 26, max_pages=5,
                     search_delay=(30, 60))
        self.assertEqual([d for _, _, d in client.calls], [None, None, (30, 60)])


class GuestClientTest(unittest.TestCase):
    def _client(self, statuses, **kw):
        resp = iter(statuses)
        self.urls = []
        self.sessions = 0

        def factory():
            self.sessions += 1
            jar = SimpleNamespace(jar=[SimpleNamespace(name="bcookie")])

            def get(url, params=None, timeout=None):
                self.urls.append(url)
                return SimpleNamespace(status_code=next(resp), text="ok", headers={})
            return SimpleNamespace(get=get, cookies=jar)
        self.t = [0.0]
        slept = []

        def sleep(s):
            slept.append(s)
            self.t[0] += s
        kw.setdefault("delay", (4.0, 9.0))
        kw.setdefault("bootstrap", False)
        return GuestClient(session_factory=factory, sleep=sleep, clock=lambda: self.t[0], **kw), slept

    def test_bootstraps_visitor_session_once(self):
        c, _ = self._client([200, 200, 200], bootstrap=True)
        c.get("https://api/a")
        c.get("https://api/b")
        self.assertIn("/jobs/search", self.urls[0])
        self.assertEqual(self.urls[1:], ["https://api/a", "https://api/b"])

    def test_stops_permanently_on_403(self):
        c, _ = self._client([200, 403, 200])
        self.assertEqual(c.get("u"), "ok")
        with self.assertRaises(Blocked):
            c.get("u")
        with self.assertRaises(Blocked):              # no further request is made
            c.get("u")
        self.assertEqual(c.requests, 2)

    def test_429_retried_quickly(self):
        c, slept = self._client([429, 200])
        self.assertEqual(c.get("u"), "ok")
        self.assertTrue(5 <= slept[-1] <= 15)
        self.assertEqual((c.requests, c.count_429, c.ok), (2, 1, 1))

    def test_429_after_retries_skips_request_and_continues(self):
        c, _ = self._client([429, 429, 429, 429, 200], rebootstrap_after=99)
        self.assertIsNone(c.get("u"))
        self.assertTrue(c.throttled)
        self.assertEqual(c.get("u"), "ok")             # next request goes on normally
        self.assertFalse(c.throttled)
        self.assertEqual((c.skipped, c.blocked), (1, None))

    def test_fresh_visitor_session_after_429_streak(self):
        # 3 x 429 -> new session + bootstrap (200) -> retry ok
        c, _ = self._client([429, 429, 429, 200, 200], bootstrap=False)
        self.assertEqual(c.get("u"), "ok")
        self.assertEqual(self.sessions, 2)
        self.assertIn("/jobs/search", self.urls[3])

    def test_several_requests_failing_in_a_row_stops_run(self):
        c, _ = self._client([429] * 12, rebootstrap_after=99, max_failed_in_a_row=3)
        self.assertIsNone(c.get("a"))
        self.assertIsNone(c.get("b"))
        with self.assertRaises(Blocked):
            c.get("c")
        self.assertEqual((c.blocked, c.skipped), (429, 3))

    def test_429_cap_per_run(self):
        c, _ = self._client([429, 200, 429, 200, 429], max_429=3)
        c.get("a")
        c.get("b")
        with self.assertRaises(Blocked):
            c.get("c")

    def test_delay_between_requests_only(self):
        c, slept = self._client([200, 200])
        c.get("u")
        self.assertEqual(slept, [])
        c.get("u")
        self.assertEqual(len(slept), 1)
        self.assertTrue(3.9 <= slept[0] <= 9.0)

    def test_per_call_delay_override(self):
        c, slept = self._client([200, 200])
        c.get("u")
        c.get("u", delay=(30, 30))
        self.assertEqual(slept, [30])

    def test_time_budget(self):
        c, slept = self._client([200, 200, 200], delay=(10, 10), max_seconds=25)
        c.get("u")
        c.get("u")                                    # t=10
        with self.assertRaises(OutOfTime):            # would wait until t=30 > 25
            c.get("u", delay=(20, 20))
        self.assertTrue(c.out_of_time)
        self.assertEqual(c.requests, 2)
        self.assertEqual(c.stop_reason(), "time budget used up")

    def test_parse_range(self):
        self.assertEqual(parse_range("10-20"), (10.0, 20.0))
        self.assertEqual(parse_range("15"), (15.0, 15.0))

    def test_404_is_not_a_block(self):
        c, _ = self._client([404, 200])
        self.assertIsNone(c.get("u"))
        self.assertFalse(c.throttled)
        self.assertEqual(c.get("u"), "ok")


class PostingParseTest(unittest.TestCase):
    def setUp(self):
        self.info = parse_posting_html(POSTING)

    def test_yoe_smallest_minimum(self):
        self.assertEqual(self.info["yoe_min"], 3)

    def test_salary_from_description_not_similar_jobs(self):
        self.assertEqual((self.info["salary_min"], self.info["salary_max"]), (170000, 210000))

    def test_seniority_and_description(self):
        self.assertEqual(self.info["seniority_level"], "Mid-Senior level")
        self.assertIn("threat modeling", self.info["description"])
        self.assertNotIn("Other job", self.info["description"])


class NewJobsTruncationTest(unittest.TestCase):
    def test_header_count_without_see_all_number(self):
        html = ("<p>30+ new jobs in United States match your preferences.</p>"
                '<a href="https://www.linkedin.com/comm/jobs/view/4363684466/">Security Engineer</a>'
                "<p>Microsoft · Redmond, WA</p><a href='https://www.linkedin.com/comm/jobs/search'>See all jobs</a>")
        self.assertEqual(parse_alert_html(html)["see_all"], 30)


class MergeTest(unittest.TestCase):
    def card(self, jid, source="search:x"):
        return {"job_id": jid, "title": "Application Security Engineer", "company": "Acme",
                "location": "Remote", "salary_text": None, "source": source,
                "url": f"https://www.linkedin.com/jobs/view/{jid}/"}

    def test_merge_keeps_email_salary_and_both_sources(self):
        email = {**self.card("1", source="email"), "salary_text": "$200K"}
        merged = merge_cards([email, self.card("1"), self.card("2")])
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]["sources"], ["email", "search:x"])
        self.assertEqual(merged[0]["salary_text"], "$200K")


if __name__ == "__main__":
    unittest.main()
