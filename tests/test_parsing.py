import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parsing import fingerprint, parse_salary, parse_yoe


class FingerprintTest(unittest.TestCase):
    def test_repost_variants_collide(self):
        a = fingerprint("Acme", "Application Security Engineer", "San Francisco, CA")
        b = fingerprint("Acme", "Application Security Engineer II (Remote)", "San Francisco, CA")
        self.assertEqual(a, b)

    def test_different_discipline_differs(self):
        a = fingerprint("Acme", "Application Security Engineer", "San Francisco, CA")
        c = fingerprint("Acme", "Product Security Engineer", "San Francisco, CA")
        self.assertNotEqual(a, c)

    def test_company_matters_location_does_not(self):
        a = fingerprint("Acme", "Application Security Engineer", "San Francisco, CA")
        self.assertNotEqual(a, fingerprint("Globex", "Application Security Engineer", "San Francisco, CA"))
        # same role in another city is the same job (one message listing both locations)
        self.assertEqual(a, fingerprint("Acme", "Application Security Engineer", "New York, NY"))

    def test_punctuation_and_case_insensitive(self):
        self.assertEqual(fingerprint("Acme, Inc.", "Security Engineer - AppSec", "Remote"),
                         fingerprint("acme inc", "security engineer appsec", "REMOTE"))


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


if __name__ == "__main__":
    unittest.main()
