import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit


MODULE_PATH = Path(__file__).resolve().parents[1] / "monitor.py"
spec = importlib.util.spec_from_file_location("repo_monitor", MODULE_PATH)
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)
NOW = datetime(2026, 10, 15, 10, tzinfo=timezone.utc)
EARLIER = NOW - timedelta(hours=1)


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)


def issue(number, created=None):
    return {"number": number, "created_at": (created or NOW).isoformat(),
            "title": f"Issue {number}", "labels": [],
            "html_url": f"https://github.com/a/b/issues/{number}"}


def pr(number=1, state="open", merged=None):
    return {"number": number, "repository_url": "https://api.github.com/repos/a/b",
            "state": state, "comments": 2, "merged_at": merged,
            "title": f"PR {number}", "html_url": f"https://github.com/a/b/pull/{number}"}


def search(items):
    return {"items": items, "total_count": len(items), "incomplete_results": False}


def page_of(path):
    return int(parse_qs(urlsplit(path).query).get("page", ["1"])[0])


class PollTests(unittest.TestCase):
    def setUp(self):
        self.no_network = patch.object(monitor, "http", side_effect=AssertionError("real HTTP prohibited"))
        self.no_network.start()
        self.addCleanup(self.no_network.stop)
        self.old = {"a/b#1": {**monitor.pr_snapshot(pr()), "reviews": 4}}

    def test_disappearing_pr_is_confirmed_merged_or_closed(self):
        for merged, kind in ((NOW.isoformat(), "merged"), (None, "closed")):
            with self.subTest(kind=kind):
                def fake(path, token):
                    if path.startswith("/search/"):
                        return search([])
                    if path.endswith("/pulls/1"):
                        return pr(state="closed", merged=merged)
                    return [None] * 4
                with patch.object(monitor, "gh_get", side_effect=fake):
                    events, current = monitor.poll_prs("token", self.old)
                self.assertEqual([e["kind"] for e in events], [kind])
                self.assertEqual(current["a/b#1"]["state"], "closed")

    def test_failed_missing_pr_lookup_retains_snapshot(self):
        with patch.object(monitor, "gh_get", side_effect=[search([]), OSError("offline")]):
            events, current = monitor.poll_prs("token", self.old)
        self.assertEqual(events, [])
        self.assertEqual(current, self.old)

    def test_search_paginates_all_open_prs(self):
        items = [pr(n) for n in range(1, 102)]
        pages = []
        def fake(path, token):
            if path.startswith("/search/"):
                page = page_of(path)
                pages.append(page)
                return {**search(items), "items": items[(page - 1) * 100:page * 100]}
            return []
        with patch.object(monitor, "gh_get", side_effect=fake):
            events, current = monitor.poll_prs("token", {})
        self.assertEqual(pages, [1, 2])
        self.assertEqual(len(current), 101)
        self.assertEqual(events, [])

    def test_search_failure_or_incomplete_response_retains_entire_cache(self):
        first = {**search([pr(n) for n in range(100)]), "total_count": 101}
        for replies in ([first, OSError("page 2")],
                        [{"items": [], "incomplete_results": True}],
                        [{"items": [], "total_count": 1001}]):
            with self.subTest(replies=replies):
                with patch.object(monitor, "gh_get", side_effect=replies):
                    events, current = monitor.poll_prs("token", self.old)
                self.assertEqual(events, [])
                self.assertEqual(current, self.old)

    def test_reviews_paginate_and_notify_only_new_count(self):
        self.old["a/b#1"]["reviews"] = 100
        with patch.object(monitor, "gh_get", side_effect=[search([pr()]), [None] * 100, [None]]):
            events, current = monitor.poll_prs("token", self.old)
        self.assertEqual(current["a/b#1"]["reviews"], 101)
        self.assertEqual([(e["kind"], e["delta"]) for e in events], [("review", 1)])

    def test_failed_review_page_retains_count_and_retry_has_no_false_delta(self):
        for response in (OSError("page 2"), {"message": "bad response"}):
            with self.subTest(response=response):
                with patch.object(monitor, "gh_get", side_effect=[search([pr()]), [None] * 100, response]):
                    events, current = monitor.poll_prs("token", self.old)
                self.assertEqual(events, [])
                self.assertEqual(current["a/b#1"]["reviews"], 4)
                with patch.object(monitor, "gh_get", side_effect=[search([pr()]), [None] * 4]):
                    events, current = monitor.poll_prs("token", current)
                self.assertEqual(events, [])

    def test_unknown_review_count_gets_silent_baseline(self):
        self.old["a/b#1"].pop("reviews")
        with patch.object(monitor, "gh_get", side_effect=[search([pr()]), [None] * 7]):
            events, current = monitor.poll_prs("token", self.old)
        self.assertEqual(events, [])
        self.assertEqual(current["a/b#1"]["reviews"], 7)

    def test_each_repo_uses_its_own_watermark(self):
        checks = {"a/b": EARLIER.isoformat(), "c/d": (NOW - timedelta(minutes=10)).isoformat()}
        middle = NOW - timedelta(minutes=30)
        with patch.object(monitor, "gh_get", return_value=[issue(1, middle)]):
            fresh, successful = monitor.poll_issues(
                ["a/b", "c/d"], "token", checks, EARLIER.isoformat(), {}, NOW)
        self.assertEqual([i["key"] for i in fresh], ["a/b#1"])
        self.assertEqual(successful, {r: NOW.isoformat() for r in checks})
        self.assertEqual(checks["a/b"], EARLIER.isoformat())

    def test_issue_pages_continue_until_checkpoint(self):
        recent = [issue(n) for n in range(100)]
        with patch.object(monitor, "gh_get", side_effect=[recent, [issue(100), issue(101, EARLIER - timedelta(seconds=1))]]) as get:
            fresh, successful = monitor.poll_issues(
                ["a/b"], "token", {}, EARLIER.isoformat(), {"a/b#0": NOW.isoformat()}, NOW)
        self.assertEqual(len(fresh), 100)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(successful, {"a/b": NOW.isoformat()})

    def test_same_second_boundary_rechecks_unseen_issues_and_truncates_legacy_watermark(self):
        legacy_checkpoint = NOW.replace(microsecond=123456)
        checks = {"a/b": legacy_checkpoint.isoformat()}
        records = [issue(1, NOW), issue(2, NOW), issue(3, NOW - timedelta(seconds=1))]
        with patch.object(monitor, "gh_get", return_value=records):
            fresh, successful = monitor.poll_issues(
                ["a/b"], "token", checks, EARLIER.isoformat(),
                {"a/b#1": NOW.isoformat()}, NOW.replace(microsecond=900000))
        self.assertEqual([i["key"] for i in fresh], ["a/b#2"])
        self.assertEqual(successful["a/b"], NOW.isoformat())

    def test_same_second_boundary_does_not_stop_issue_pagination(self):
        records = [issue(n, NOW) for n in range(101)]
        with patch.object(monitor, "gh_get", side_effect=[records[:100], records[100:]]) as get:
            fresh, successful = monitor.poll_issues(
                ["a/b"], "token", {"a/b": NOW.isoformat()}, EARLIER.isoformat(), {}, NOW)
        self.assertEqual(len(fresh), 101)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(successful["a/b"], NOW.isoformat())

    def test_failed_issue_page_discards_only_that_repo_and_retains_watermark(self):
        checks = {"a/b": EARLIER.isoformat()}
        with patch.object(monitor, "gh_get", side_effect=[
                [issue(n) for n in range(100)], OSError("page 2"), [issue(999)]]):
            fresh, successful = monitor.poll_issues(
                ["a/b", "c/d"], "token", checks, EARLIER.isoformat(), {}, NOW)
        self.assertEqual([i["key"] for i in fresh], ["c/d#999"])
        self.assertEqual(successful["a/b"], EARLIER.isoformat())
        self.assertEqual(successful["c/d"], NOW.isoformat())


class MainTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state_file = Path(directory.name) / "state.json"
        self.initial = {"last_check": EARLIER.isoformat(), "seen": {}, "prs": {}, "rot_index": 0}
        self.state_file.write_text(json.dumps(self.initial), encoding="utf-8")
        for mocked in (
            patch.object(monitor, "STATE_FILE", self.state_file),
            patch.object(monitor, "datetime", FixedDatetime),
            patch.object(monitor, "http", side_effect=AssertionError("real HTTP prohibited")),
            patch.object(monitor, "load_tiers", return_value=(["a/b"], [])),
            patch.object(monitor, "feishu_token", return_value="fake"),
            patch.dict(os.environ, GITHUB_TOKEN="fake", FEISHU_APP_ID="fake",
                       FEISHU_APP_SECRET="fake", FEISHU_CHAT_ID="fake"),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)

    def api(self, count):
        def fake(path, token):
            if path.startswith("/search/"):
                return search([])
            return [issue(n) for n in range(count)]
        return fake

    def read_state(self):
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def test_issue_notifications_include_every_item_in_batches(self):
        with patch.object(monitor, "gh_get", side_effect=self.api(25)), patch.object(monitor, "send_card") as send:
            monitor.main()
        self.assertEqual(send.call_count, 3)
        bodies = [call.args[3] for call in send.call_args_list]
        self.assertEqual([len(body.splitlines()) for body in bodies], [12, 12, 1])
        self.assertEqual(len(self.read_state()["seen"]), 25)

    def test_first_install_sets_baseline_for_all_rotation_slices(self):
        self.state_file.unlink()
        def old_issue(path, token):
            return search([]) if path.startswith("/search/") else [issue(1, NOW - timedelta(seconds=1))]
        with patch.object(monitor, "gh_get", side_effect=old_issue), patch.object(monitor, "send_card") as send:
            monitor.main()
        first = self.read_state()
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[2], "GitHub 监测已上线")
        self.assertEqual(first["issue_backfill_since"], NOW.isoformat())
        with patch.object(monitor, "gh_get", side_effect=old_issue), patch.object(monitor, "send_card") as send, patch.object(
                monitor, "load_tiers", return_value=(["c/d"], [])):
            monitor.main()
        send.assert_not_called()

    def test_notification_failure_keeps_disk_state_for_retry(self):
        with patch.object(monitor, "gh_get", side_effect=self.api(25)), patch.object(
                monitor, "send_card", side_effect=["sent", RuntimeError("send failed")]):
            with self.assertRaisesRegex(RuntimeError, "send failed"):
                monitor.main()
        self.assertEqual(self.read_state(), self.initial)

    def test_legacy_state_gets_fixed_lookback_for_unscanned_repos(self):
        with patch.object(monitor, "gh_get", side_effect=self.api(0)), patch.object(monitor, "send_card"):
            monitor.main()
        first = self.read_state()
        self.assertIn("a/b", first["issue_checked_at"])
        self.assertLessEqual(monitor.parse_time(first["issue_backfill_since"]), EARLIER)
        with patch.object(monitor, "gh_get", side_effect=self.api(0)), patch.object(monitor, "send_card"), patch.object(
                monitor, "load_tiers", return_value=(["c/d"], [])):
            monitor.main()
        second = self.read_state()
        self.assertEqual(first["issue_backfill_since"], second["issue_backfill_since"])
        self.assertEqual(first["issue_checked_at"]["a/b"], second["issue_checked_at"]["a/b"])
        self.assertIn("c/d", second["issue_checked_at"])

    def test_merged_notification_does_not_require_delta(self):
        event = {"key": "a/b#1", "kind": "merged", "title": "fixed", "url": "https://example.com"}
        with patch.object(monitor, "gh_get", side_effect=self.api(0)), patch.object(
                monitor, "poll_prs", return_value=([event], {})), patch.object(monitor, "send_card") as send:
            monitor.main()
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[2], "🎉 PR 已合并")

    def test_atomic_replace_failure_preserves_previous_state(self):
        with patch.object(monitor.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                monitor.save_state({"new": True})
        self.assertEqual(self.read_state(), self.initial)
        self.assertEqual(list(self.state_file.parent.glob(".state-*.tmp")), [])

    def test_state_lock_excludes_other_process_and_releases(self):
        code = (
            "import importlib.util\nfrom pathlib import Path\n"
            f"spec = importlib.util.spec_from_file_location('monitor', {str(MODULE_PATH)!r})\n"
            "module = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(module)\n"
            f"module.STATE_FILE = Path({str(self.state_file)!r})\n"
            "with module.state_lock():\n print('acquired')\n"
        )
        command = [sys.executable, "-S", "-B", "-c", code]
        with monitor.state_lock():
            blocked = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(blocked.returncode, 0)
        acquired = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(acquired.returncode, 0, acquired.stderr)
        self.assertEqual(acquired.stdout.strip(), "acquired")


if __name__ == "__main__":
    unittest.main()
