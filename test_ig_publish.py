"""Tests for ig_publish.py against a fake Instagram API. Run: python3 -m unittest test_ig_publish"""
import json
import os
import tempfile
import unittest
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import ig_publish as ig

NOW = datetime(2026, 10, 6, 13, 40, tzinfo=timezone.utc)  # 19:10 IST


class FakeInstagram:
    def __init__(self, recent=None, fail_publish=False):
        self.calls = []
        self.recent = recent or []
        self.fail_publish = fail_publish
        self.n = 0

    def __call__(self, method, path, **params):
        self.calls.append((method, path, params))
        if path == "me":
            return {"user_id": "42", "username": "tester", "account_type": "MEDIA_CREATOR"}
        if path == "42/media" and method == "GET":
            return {"data": self.recent}
        if path == "42/media":
            self.n += 1
            return {"id": f"c{self.n}"}
        if params.get("fields") == "status_code":
            return {"status_code": "FINISHED"}
        if path == "42/media_publish":
            if self.fail_publish:
                raise ig.PublishError("Instagram API 400: boom")
            return {"id": "m1"}
        if params.get("fields") == "permalink":
            return {"permalink": "https://instagram.com/p/x"}
        raise AssertionError(f"unexpected call {method} {path}")


def post(slug, when, status="pending"):
    return {"slug": slug, "when": when.isoformat(), "caption": f"caption for {slug}",
            "images": [f"{slug}/slide-01.jpg", f"{slug}/slide-02.jpg"], "status": status}


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sched = Path(self.tmp.name) / "schedule.json"
        patches = [
            mock.patch.object(ig, "SCHEDULE", self.sched),
            mock.patch.object(ig, "check_image", lambda url: None),
            mock.patch.object(ig.time, "sleep", lambda s: None),
            mock.patch.dict(os.environ, {"IG_ACCESS_TOKEN": "t", "IMAGE_BASE_URL": "https://img.example/publish"}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def write(self, posts):
        self.sched.write_text(json.dumps({"posts": posts}))

    def read(self):
        return {p["slug"]: p for p in json.loads(self.sched.read_text())["posts"]}

    def run_at(self, now, fake, dry=False):
        with mock.patch.object(ig, "api", fake), mock.patch.object(ig, "datetime", wraps=datetime) as dt:
            dt.now.return_value = now
            return ig.cmd_run(Namespace(dry_run=dry))

    def test_nothing_due_publishes_nothing(self):
        self.write([post("a", NOW + timedelta(hours=1))])
        fake = FakeInstagram()
        self.run_at(NOW, fake)
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.read()["a"]["status"], "pending")

    def test_due_post_goes_through_full_carousel_flow(self):
        self.write([post("a", NOW - timedelta(minutes=10))])
        fake = FakeInstagram()
        self.run_at(NOW, fake)
        created = [c for c in fake.calls if c[0] == "POST" and c[1] == "42/media"]
        self.assertEqual(len(created), 3)  # 2 slides + 1 carousel
        self.assertEqual(created[0][2]["image_url"], "https://img.example/publish/a/slide-01.jpg")
        self.assertEqual(created[2][2]["media_type"], "CAROUSEL")
        self.assertEqual(created[2][2]["children"], "c1,c2")
        a = self.read()["a"]
        self.assertEqual((a["status"], a["media_id"]), ("published", "m1"))

    def test_only_one_post_per_run_oldest_first(self):
        self.write([post("b", NOW - timedelta(minutes=5)), post("a", NOW - timedelta(minutes=30))])
        self.run_at(NOW, FakeInstagram())
        s = self.read()
        self.assertEqual((s["a"]["status"], s["b"]["status"]), ("published", "pending"))

    def test_too_late_is_marked_missed_not_published(self):
        self.write([post("a", NOW - timedelta(hours=ig.MAX_LATE_HOURS + 1))])
        fake = FakeInstagram()
        with self.assertRaises(ig.PublishError):
            self.run_at(NOW, fake)
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.read()["a"]["status"], "missed")

    def test_already_live_post_is_not_posted_twice(self):
        self.write([post("a", NOW - timedelta(minutes=10))])
        fake = FakeInstagram(recent=[{"id": "old", "caption": "caption for a", "permalink": "p"}])
        self.run_at(NOW, fake)
        self.assertFalse(any(c[1] == "42/media_publish" for c in fake.calls))
        self.assertEqual(self.read()["a"]["media_id"], "old")

    def test_failures_retry_then_park_as_failed(self):
        self.write([post("a", NOW - timedelta(minutes=10))])
        for i in range(ig.MAX_ATTEMPTS):
            with self.assertRaises(ig.PublishError):
                self.run_at(NOW, FakeInstagram(fail_publish=True))
        a = self.read()["a"]
        self.assertEqual((a["status"], a["attempts"]), ("failed", ig.MAX_ATTEMPTS))
        self.assertIn("boom", a["last_error"])

    def test_dry_run_calls_nothing(self):
        self.write([post("a", NOW - timedelta(minutes=10))])
        fake = FakeInstagram()
        self.run_at(NOW, fake, dry=True)
        self.assertEqual(fake.calls, [])

    def test_shift_requeues_missed_and_keeps_published(self):
        self.write([post("a", NOW, "published"), post("b", NOW, "missed"), post("c", NOW, "failed")])
        ig.cmd_shift(Namespace(start="2026-10-10", time="19:00"))
        s = self.read()
        self.assertEqual(s["a"]["status"], "published")
        self.assertEqual(s["b"]["when"], "2026-10-10T19:00:00+05:30")
        self.assertEqual(s["c"]["when"], "2026-10-11T19:00:00+05:30")
        self.assertEqual(s["c"]["status"], "pending")

    def test_time_without_offset_is_rejected(self):
        with self.assertRaises(ig.PublishError):
            ig.parse_when("2026-10-06T19:00:00")


if __name__ == "__main__":
    unittest.main()
