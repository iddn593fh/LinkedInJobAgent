import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
from parsing import fingerprint


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

    def test_repost_inherits_triage_and_verdict(self):
        a = job("4000000001", "Application Security Engineer")
        db.upsert_seen(self.conn, a)
        db.save_triage(self.conn, a["fingerprint"], "yes")
        db.save_verdict(self.conn, a["fingerprint"], {"verdict": "reject", "reason": "SOC tooling"})
        b = job("4000000999", "Application Security Engineer III")       # reposted later
        db.upsert_seen(self.conn, b)
        got = db.get_job(self.conn, "4000000999")
        self.assertEqual((got["triage"], got["verdict"]["verdict"]), ("yes", "reject"))
        self.assertEqual(db.pending_triage(self.conn, 3), [])
        self.assertEqual(db.pending_judge(self.conn, 3, 30), [])

    def test_queues(self):
        a, b, c = (job("1", "AppSec Engineer", company="A"), job("2", "Red Team", company="B"),
                   job("3", "Pentester", company="C"))
        for j in (a, b, c):
            db.upsert_seen(self.conn, j)
        self.assertEqual(len(db.pending_triage(self.conn, 3)), 3)
        db.save_triage(self.conn, a["fingerprint"], "maybe")
        db.save_triage(self.conn, b["fingerprint"], "yes")
        db.save_triage(self.conn, c["fingerprint"], "no")
        self.assertEqual([j["job_id"] for j in db.pending_enrich(self.conn, 3)], ["2", "1"])  # yes first
        self.assertEqual(db.pending_judge(self.conn, 3, 30), [])     # waiting for enrichment
        db.save_enrichment(self.conn, "2", {"description": "d", "seniority_level": None, "yoe_min": 3,
                                            "salary_min": None, "salary_max": None})
        db.save_enrichment(self.conn, "1", None)                      # posting gone: judge on card
        self.assertEqual([j["job_id"] for j in db.pending_judge(self.conn, 3, 30)], ["2", "1"])
        self.assertEqual(db.count_pending(self.conn, 3), 2)
        db.save_verdict(self.conn, b["fingerprint"], {"verdict": "send"})
        db.save_verdict(self.conn, a["fingerprint"], {"verdict": "reject"})
        self.assertEqual([j["job_id"] for j in db.to_send(self.conn)], ["2"])
        db.mark_notified(self.conn, ["2"])
        self.assertEqual(db.to_send(self.conn), [])
        self.assertEqual(db.count_pending(self.conn, 3), 0)

    def test_same_role_in_two_cities_is_one_job(self):
        sf, ny = job("1", "Security Engineer, Offensive Security"), job("2", "Security Engineer, Offensive Security", location="New York, NY")
        self.assertEqual(sf["fingerprint"], ny["fingerprint"])
        for j in (sf, ny):
            db.upsert_seen(self.conn, j)
        self.assertEqual(len(db.pending_triage(self.conn, 3)), 1)          # judged once
        db.save_triage(self.conn, sf["fingerprint"], "yes")
        db.save_verdict(self.conn, sf["fingerprint"], {"verdict": "send"})
        sends = db.to_send(self.conn)
        self.assertEqual(len(sends), 1)                                    # one message
        self.assertEqual(db.locations(self.conn, sends[0]["fingerprint"]), ["San Francisco, CA", "New York, NY"])
        db.mark_notified(self.conn, [sends[0]["job_id"]])
        self.assertTrue(db.is_notified(self.conn, "3", fingerprint("Acme", "Security Engineer, Offensive Security", "Austin, TX")))

    def test_migration_recomputes_old_location_fingerprints(self):
        path = os.path.join(self.tmp.name, "old.db")
        conn = db.connect(path)
        conn.execute("PRAGMA user_version = 0")
        db.upsert_seen(conn, {**job("1", "AppSec Engineer"), "fingerprint": "old-with-location"})
        conn.close()
        conn = db.connect(path)                                            # migrates
        self.assertEqual(db.get_job(conn, "1")["fingerprint"], fingerprint("Acme", "AppSec Engineer"))
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        conn.close()

    def test_llm_usage_accumulates(self):
        db.add_llm_usage(self.conn, 1, 500)
        db.add_llm_usage(self.conn, 2, 700)
        self.assertEqual(db.llm_usage_today(self.conn), (3, 1200))

    def test_run_bookkeeping(self):
        rid = db.start_run(self.conn, 26)
        db.finish_run(self.conn, rid, {"emails": 3, "cards": 20, "triage_pass": 4})
        db.add_to_latest_run(self.conn, "notified", 2)
        r = db.run_history(self.conn)[0]
        self.assertEqual((r["cards"], r["triage_pass"], r["notified"]), (20, 4, 2))


if __name__ == "__main__":
    unittest.main()
