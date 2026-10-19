import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from email_parser import job_id_from_href, parse_alert_html

TRACK = "https://www.linkedin.com/comm/jobs/view/{id}/?trackingId=abc%3D%3D&refId=xyz&lipi=urn%3Ali"
REDIR = ("https://click.linkedin.com/redir/redirect?url=https%3A%2F%2Fwww.linkedin.com%2Fjobs%2Fview%2F"
         "{id}%2F%3FtrackingId%3Dq&urlhash=AbCd&trk=eml-email_job_alert")

# Modern layout: logo cell + text cell, "Company · Location" on one line,
# separate shared-ID Apply button, and a "See all 47 jobs" footer.
MODERN = f"""
<html><head><style>.x{{color:red}}</style></head><body>
<table><tr><td>
  <h2>Your job alert for application security</h2>
  <table class="card"><tr>
    <td><a href="{TRACK.format(id='4012345601')}"><img src="logo.png" alt="Acme logo"></a></td>
    <td>
      <a href="{TRACK.format(id='4012345601')}"><span>Application Security Engineer II</span></a>
      <p>Acme Corp · San Francisco, CA (Hybrid)</p>
      <p>$180K-$220K / year</p>
      <p>Actively recruiting</p>
      <p><a href="{TRACK.format(id='4012345601')}">Apply</a></p>
    </td>
  </tr></table>
  <table class="card"><tr>
    <td><a href="{REDIR.format(id='4012345602')}"><img src="logo.png"></a></td>
    <td>
      <a href="{REDIR.format(id='4012345602')}">Product Security Engineer</a>
      <p>Globex &middot; Remote</p>
      <p>3 connections work here</p>
      <a href="{REDIR.format(id='4012345602')}">View job</a>
    </td>
  </tr></table>
  <table class="card"><tr><td>
      <a href="{TRACK.format(id='4012345603')}">SOC Analyst</a>
      <p>Initech · Austin, TX</p>
  </td></tr></table>
  <p><a href="https://www.linkedin.com/comm/jobs/search?keywords=appsec">See all 47 jobs</a></p>
</td></tr></table>
</body></html>
"""

# Older layout: company and location on separate lines, whole-card anchor.
SEPARATE_LINES = f"""
<html><body><table>
<tr><td>
  <a href="{TRACK.format(id='3900000001')}">
    <table><tr><td>
      <div>Offensive Security Engineer</div>
      <div>Hooli</div>
      <div>New York, NY</div>
    </td></tr></table>
  </a>
</td></tr>
<tr><td>
  <a href="{TRACK.format(id='3900000002')}">Red Team Operator</a><br>
  Umbrella<br>
  Seattle, WA<br>
  Posted 2 days ago
</td></tr>
</table>
<p>View all 2 jobs</p>
</body></html>
"""


class HrefTest(unittest.TestCase):
    def test_direct(self):
        self.assertEqual(job_id_from_href(TRACK.format(id="4012345601")), "4012345601")

    def test_url_encoded_redirect(self):
        self.assertEqual(job_id_from_href(REDIR.format(id="4012345602")), "4012345602")

    def test_slug_url(self):
        self.assertEqual(job_id_from_href(
            "https://www.linkedin.com/jobs/view/appsec-engineer-at-acme-4012345609?x=1"), "4012345609")

    def test_non_job_links(self):
        self.assertIsNone(job_id_from_href("https://www.linkedin.com/comm/jobs/search?keywords=x"))
        self.assertIsNone(job_id_from_href("https://www.linkedin.com/company/acme"))


class ModernLayoutTest(unittest.TestCase):
    def setUp(self):
        self.result = parse_alert_html(MODERN)
        self.cards = {c["job_id"]: c for c in self.result["cards"]}

    def test_one_card_per_job_id(self):
        self.assertEqual(list(self.cards), ["4012345601", "4012345602", "4012345603"])

    def test_title_not_replaced_by_apply_button(self):
        self.assertEqual(self.cards["4012345601"]["title"], "Application Security Engineer II")
        self.assertEqual(self.cards["4012345602"]["title"], "Product Security Engineer")

    def test_company_dot_location(self):
        c = self.cards["4012345601"]
        self.assertEqual((c["company"], c["location"]), ("Acme Corp", "San Francisco, CA (Hybrid)"))
        c = self.cards["4012345602"]
        self.assertEqual((c["company"], c["location"]), ("Globex", "Remote"))

    def test_salary_line_captured_not_used_as_location(self):
        self.assertEqual(self.cards["4012345601"]["salary_text"], "$180K-$220K / year")

    def test_cards_do_not_bleed_into_neighbours(self):
        c = self.cards["4012345603"]
        self.assertEqual((c["title"], c["company"], c["location"]), ("SOC Analyst", "Initech", "Austin, TX"))

    def test_canonical_url_strips_tracking(self):
        self.assertEqual(self.cards["4012345602"]["url"], "https://www.linkedin.com/jobs/view/4012345602/")

    def test_see_all_count(self):
        self.assertEqual(self.result["see_all"], 47)


class SeparateLinesTest(unittest.TestCase):
    def setUp(self):
        self.result = parse_alert_html(SEPARATE_LINES)
        self.cards = {c["job_id"]: c for c in self.result["cards"]}

    def test_whole_card_anchor(self):
        c = self.cards["3900000001"]
        self.assertEqual((c["title"], c["company"], c["location"]),
                         ("Offensive Security Engineer", "Hooli", "New York, NY"))

    def test_br_separated(self):
        c = self.cards["3900000002"]
        self.assertEqual((c["title"], c["company"], c["location"]),
                         ("Red Team Operator", "Umbrella", "Seattle, WA"))

    def test_view_all_count(self):
        self.assertEqual(self.result["see_all"], 2)


class EdgeTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(parse_alert_html(""), {"cards": [], "see_all": None})

    def test_no_footer(self):
        html = f'<table><tr><td><a href="{TRACK.format(id="4000000001")}">Pentester</a><p>A · B</p></td></tr></table>'
        r = parse_alert_html(html)
        self.assertIsNone(r["see_all"])
        self.assertEqual(r["cards"][0]["company"], "A")


if __name__ == "__main__":
    unittest.main()
