import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
from filters import fingerprint


def job(job_id, title, company="Acme", location="San Francisco, CA"):
    return {"job_id": job_id, "title": title, "company": company, "location": location,
            "url": f"https://www.linkedin.com/jobs/view/{job_id}/",
            "fingerprint": fingerprint(company, title, location)}


class DbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_wal_enabled(self):
        self.assertEqual(self.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_schema_is_idempotent(self):
        path = os.path.join(self.tmp.name, "t.db")
        db.connect(path).close()
        db.connect(path).close()

    def test_repost_with_new_job_id_collides(self):
        original = job("4000000001", "Application Security Engineer")
        db.upsert_seen(self.conn, original)
        db.mark_notified(self.conn, ["4000000001"])

        repost = job("4000000999", "Application Security Engineer II (Remote)")
        db.upsert_seen(self.conn, repost)
        self.assertTrue(db.is_notified(self.conn, repost["job_id"], repost["fingerprint"]))

    def test_different_role_does_not_collide(self):
        db.upsert_seen(self.conn, job("4000000001", "Application Security Engineer"))
        db.mark_notified(self.conn, ["4000000001"])
        other = job("4000000002", "Product Security Engineer")
        self.assertFalse(db.is_notified(self.conn, other["job_id"], other["fingerprint"]))

    def test_seen_but_not_notified_is_not_deduped(self):
        j = job("4000000001", "Application Security Engineer")
        db.upsert_seen(self.conn, j)
        self.assertFalse(db.is_notified(self.conn, j["job_id"], j["fingerprint"]))

    def test_exact_job_id_repeat_caught_even_if_fingerprint_changes(self):
        db.upsert_seen(self.conn, job("4000000001", "Application Security Engineer"))
        db.mark_notified(self.conn, ["4000000001"])
        edited = job("4000000001", "AppSec Engineer", location="Remote")
        self.assertTrue(db.is_notified(self.conn, edited["job_id"], edited["fingerprint"]))

    def test_mark_notified_is_idempotent(self):
        db.upsert_seen(self.conn, job("4000000001", "Application Security Engineer"))
        self.assertEqual(db.mark_notified(self.conn, ["4000000001"]), 1)
        self.assertEqual(db.mark_notified(self.conn, ["4000000001"]), 0)

    def test_verdict_cache_shared_across_reposts(self):
        a = job("4000000001", "Application Security Engineer")
        b = job("4000000999", "Application Security Engineer III")
        db.upsert_seen(self.conn, a)
        db.upsert_seen(self.conn, b)
        db.save_verdict(self.conn, "4000000001", {"verdict": "reject", "reason": "SOC tooling"})
        self.assertEqual(db.get_cached_verdict(self.conn, b["fingerprint"])["verdict"], "reject")

    def test_run_bookkeeping(self):
        rid = db.start_run(self.conn, 26)
        db.finish_run(self.conn, rid, {"emails": 3, "cards": 20, "title_pass": 4})
        db.add_to_latest_run(self.conn, "notified", 2)
        r = db.run_history(self.conn)[0]
        self.assertEqual((r["cards"], r["title_pass"], r["notified"]), (20, 4, 2))


if __name__ == "__main__":
    unittest.main()
