import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import filters
from filters import fingerprint, parse_salary, parse_yoe, title_decision


class FingerprintTest(unittest.TestCase):
    def test_repost_variants_collide(self):
        a = fingerprint("Acme", "Application Security Engineer", "San Francisco, CA")
        b = fingerprint("Acme", "Application Security Engineer II (Remote)", "San Francisco, CA")
        self.assertEqual(a, b)

    def test_different_discipline_differs(self):
        a = fingerprint("Acme", "Application Security Engineer", "San Francisco, CA")
        c = fingerprint("Acme", "Product Security Engineer", "San Francisco, CA")
        self.assertNotEqual(a, c)

    def test_company_and_location_matter(self):
        a = fingerprint("Acme", "Application Security Engineer", "San Francisco, CA")
        self.assertNotEqual(a, fingerprint("Globex", "Application Security Engineer", "San Francisco, CA"))
        self.assertNotEqual(a, fingerprint("Acme", "Application Security Engineer", "New York, NY"))

    def test_punctuation_and_case_insensitive(self):
        self.assertEqual(fingerprint("Acme, Inc.", "Security Engineer - AppSec", "Remote"),
                         fingerprint("acme inc", "security engineer appsec", "REMOTE"))


class TitleTest(unittest.TestCase):
    PASS = [
        "Application Security Engineer", "AppSec Engineer", "Senior Product Security Engineer",
        "Offensive Security Engineer", "Penetration Tester", "Pentester", "Red Team Operator",
        "Vulnerability Researcher", "Exploit Developer", "Security Engineer II",
        "Security Researcher", "Secure Code Reviewer", "Software Security Engineer",
        "Senior Security Engineer, Product",
    ]
    DROP = [
        "SOC Analyst", "Security Operations Engineer", "GRC Analyst", "Compliance Engineer",
        "IAM Engineer", "Security Sales Engineer", "Solutions Engineer, Security",
        "Physical Security Specialist", "Security Engineering Intern", "New Grad Security Engineer",
        "Junior Penetration Tester", "Staff Security Engineer", "Principal Application Security Engineer",
        "Director, Product Security", "Head of Offensive Security", "Application Security Manager",
        "Lead Red Team Operator", "Software Engineer", "Data Scientist",
    ]

    def test_pass(self):
        for t in self.PASS:
            with self.subTest(t=t):
                self.assertTrue(title_decision(t)[0], title_decision(t))

    def test_drop(self):
        for t in self.DROP:
            with self.subTest(t=t):
                self.assertFalse(title_decision(t)[0], title_decision(t))

    def test_vulnerability_management_is_excluded_by_management_rule(self):
        # Documents a conflict in the spec: include has vulnerability[\s-]*manage, but
        # exclude's \bmanagement\b wins. See notes; change deliberately if wanted.
        self.assertFalse(title_decision("Vulnerability Management Engineer")[0])


class YoeTest(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(parse_yoe("You have 3+ years of experience"), 3)
        self.assertEqual(parse_yoe("5-8 years in security"), 5)
        self.assertEqual(parse_yoe("5 – 8 years in security"), 5)
        self.assertEqual(parse_yoe("at least 4 years of AppSec"), 4)
        self.assertEqual(parse_yoe("a minimum of 2 years"), 2)
        self.assertEqual(parse_yoe("minimum 6 yrs experience"), 6)

    def test_smallest_minimum_wins(self):
        self.assertEqual(parse_yoe("3+ years security, 8+ for the senior track"), 3)

    def test_absurd_ignored(self):
        self.assertIsNone(parse_yoe("Trusted for 50+ years by customers"))
        self.assertEqual(parse_yoe("Trusted for 50+ years. You have 4+ years."), 4)

    def test_window(self):
        text = "x" * 6100 + " 2+ years"
        self.assertIsNone(parse_yoe(text))

    def test_none(self):
        self.assertIsNone(parse_yoe("Deep expertise in exploitation required."))
        self.assertIsNone(parse_yoe(None))


class SalaryTest(unittest.TestCase):
    def test_ranges(self):
        self.assertEqual(parse_salary("$185,000 - $240,000 per year"), (185000, 240000))
        self.assertEqual(parse_salary("$185K–$240K"), (185000, 240000))
        self.assertEqual(parse_salary("USD 170,000 to 210,000"), (170000, 210000))
        self.assertEqual(parse_salary("$199,000.00/yr"), (199000, 199000))

    def test_multiple_ranges_take_overall_min_max(self):
        self.assertEqual(parse_salary("SF: $180k-$220k. Elsewhere: $150k-$190k"), (150000, 220000))

    def test_noise_rejected(self):
        self.assertEqual(parse_salary("Call 415-555-1234 or visit 1600 Amphitheatre Pkwy"), (None, None))
        self.assertEqual(parse_salary("$85/hour"), (None, None))
        self.assertEqual(parse_salary("$5,000,000 in grants"), (None, None))
        self.assertEqual(parse_salary("401k match"), (None, None))

    def test_salary_decision(self):
        self.assertTrue(filters.salary_decision(None, 150000)[0])
        self.assertTrue(filters.salary_decision(160000, 150000)[0])
        self.assertFalse(filters.salary_decision(140000, 150000)[0])


class TierTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        self.tmp.write("companies:\n  Google: 1\n  Microsoft: 2\n  NCC Group: 3\n")
        self.tmp.close()
        self.tiers = filters.load_tiers(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_normalized_lookup(self):
        self.assertEqual(filters.company_tier("Google LLC", self.tiers), 1)
        self.assertEqual(filters.company_tier("Microsoft Corporation", self.tiers), 2)
        self.assertEqual(filters.company_tier("NCC Group", self.tiers), 3)

    def test_decision(self):
        self.assertTrue(filters.tier_decision("Google", self.tiers, 2, 2)[0])
        self.assertFalse(filters.tier_decision("NCC Group", self.tiers, 2, 2)[0])
        ok, tier, _ = filters.tier_decision("Unheard Of Startup", self.tiers, 2, 2)
        self.assertTrue(ok)
        self.assertEqual(tier, 2)
        self.assertFalse(filters.tier_decision("Unheard Of Startup", self.tiers, 2, 3)[0])


if __name__ == "__main__":
    unittest.main()
