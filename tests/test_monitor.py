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
                    events, current, _ = monitor.poll_prs("token", self.old)
                self.assertEqual([e["kind"] for e in events], [kind])
                self.assertEqual(current["a/b#1"]["state"], "closed")

    def test_failed_missing_pr_lookup_retains_snapshot(self):
        with patch.object(monitor, "gh_get", side_effect=[search([]), OSError("offline")]):
            events, current, _ = monitor.poll_prs("token", self.old)
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
            events, current, search_ok = monitor.poll_prs("token", {})
        self.assertTrue(search_ok)
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
                    events, current, search_ok = monitor.poll_prs("token", self.old)
                self.assertFalse(search_ok)
                self.assertEqual(events, [])
                self.assertEqual(current, self.old)

    def test_reviews_paginate_and_notify_only_new_count(self):
        self.old["a/b#1"]["reviews"] = 100
        with patch.object(monitor, "gh_get", side_effect=[search([pr()]), [None] * 100, [None]]):
            events, current, _ = monitor.poll_prs("token", self.old)
        self.assertEqual(current["a/b#1"]["reviews"], 101)
        self.assertEqual([(e["kind"], e["delta"]) for e in events], [("review", 1)])

    def test_failed_review_page_retains_count_and_retry_has_no_false_delta(self):
        for response in (OSError("page 2"), {"message": "bad response"}):
            with self.subTest(response=response):
                with patch.object(monitor, "gh_get", side_effect=[search([pr()]), [None] * 100, response]):
                    events, current, _ = monitor.poll_prs("token", self.old)
                self.assertEqual(events, [])
                self.assertEqual(current["a/b#1"]["reviews"], 4)
                with patch.object(monitor, "gh_get", side_effect=[search([pr()]), [None] * 4]):
                    events, current, _ = monitor.poll_prs("token", current)
                self.assertEqual(events, [])

    def test_unknown_review_count_gets_silent_baseline(self):
        self.old["a/b#1"].pop("reviews")
        with patch.object(monitor, "gh_get", side_effect=[search([pr()]), [None] * 7]):
            events, current, _ = monitor.poll_prs("token", self.old)
        self.assertEqual(events, [])
        self.assertEqual(current["a/b#1"]["reviews"], 7)

    def test_each_repo_uses_its_own_watermark(self):
        checks = {"a/b": EARLIER.isoformat(), "c/d": (NOW - timedelta(minutes=10)).isoformat()}
        middle = NOW - timedelta(minutes=30)
        with patch.object(monitor, "gh_get", return_value=[issue(1, middle)]):
            fresh, successful, _ = monitor.poll_issues(
                ["a/b", "c/d"], "token", checks, EARLIER.isoformat(), {}, NOW)
        self.assertEqual([i["key"] for i in fresh], ["a/b#1"])
        self.assertEqual(successful, {r: NOW.isoformat() for r in checks})
        self.assertEqual(checks["a/b"], EARLIER.isoformat())

    def test_issue_pages_continue_until_checkpoint(self):
        recent = [issue(n) for n in range(100)]
        with patch.object(monitor, "gh_get", side_effect=[recent, [issue(100), issue(101, EARLIER - timedelta(seconds=1))]]) as get:
            fresh, successful, _ = monitor.poll_issues(
                ["a/b"], "token", {}, EARLIER.isoformat(), {"a/b#0": NOW.isoformat()}, NOW)
        self.assertEqual(len(fresh), 100)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(successful, {"a/b": NOW.isoformat()})

    def test_same_second_boundary_rechecks_unseen_issues_and_truncates_legacy_watermark(self):
        legacy_checkpoint = NOW.replace(microsecond=123456)
        checks = {"a/b": legacy_checkpoint.isoformat()}
        records = [issue(1, NOW), issue(2, NOW), issue(3, NOW - timedelta(seconds=1))]
        with patch.object(monitor, "gh_get", return_value=records):
            fresh, successful, _ = monitor.poll_issues(
                ["a/b"], "token", checks, EARLIER.isoformat(),
                {"a/b#1": NOW.isoformat()}, NOW.replace(microsecond=900000))
        self.assertEqual([i["key"] for i in fresh], ["a/b#2"])
        self.assertEqual(successful["a/b"], NOW.isoformat())

    def test_same_second_boundary_does_not_stop_issue_pagination(self):
        records = [issue(n, NOW) for n in range(101)]
        with patch.object(monitor, "gh_get", side_effect=[records[:100], records[100:]]) as get:
            fresh, successful, _ = monitor.poll_issues(
                ["a/b"], "token", {"a/b": NOW.isoformat()}, EARLIER.isoformat(), {}, NOW)
        self.assertEqual(len(fresh), 101)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(successful["a/b"], NOW.isoformat())

    def test_failed_issue_page_discards_only_that_repo_and_retains_watermark(self):
        checks = {"a/b": EARLIER.isoformat()}
        with patch.object(monitor, "gh_get", side_effect=[
                [issue(n) for n in range(100)], OSError("page 2"), [issue(999)]]):
            fresh, successful, issues_ok = monitor.poll_issues(
                ["a/b", "c/d"], "token", checks, EARLIER.isoformat(), {}, NOW)
        self.assertTrue(issues_ok)
        self.assertEqual([i["key"] for i in fresh], ["c/d#999"])
        self.assertEqual(successful["a/b"], EARLIER.isoformat())
        self.assertEqual(successful["c/d"], NOW.isoformat())


class MainTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state_file = Path(directory.name) / "state.json"
        self.undelivered = Path(directory.name) / "undelivered.jsonl"
        self.initial = {"last_check": EARLIER.isoformat(), "seen": {}, "prs": {}, "rot_index": 0}
        self.state_file.write_text(json.dumps(self.initial), encoding="utf-8")
        for mocked in (
            patch.object(monitor, "STATE_FILE", self.state_file),
            patch.object(monitor, "UNDELIVERED_FILE", self.undelivered),
            patch.object(monitor, "datetime", FixedDatetime),
            patch.object(monitor, "http", side_effect=AssertionError("real HTTP prohibited")),
            patch.object(monitor, "load_tiers", return_value=(["a/b"], [])),
            patch.object(monitor, "feishu_token", return_value="fake"),
            patch.dict(os.environ, GITHUB_TOKEN="fake", FEISHU_APP_ID="fake",
                       FEISHU_APP_SECRET="fake", FEISHU_CHAT_ID="fake",
                       GITHUB_REPOSITORY=""),
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

    def test_all_github_polls_failed_preserves_state_without_notifying(self):
        self.initial.update(issue_checked_at={"a/b": EARLIER.isoformat()},
                            prs={"a/b#1": {**monitor.pr_snapshot(pr()), "reviews": 4}})
        self.state_file.write_text(json.dumps(self.initial), encoding="utf-8")
        original = self.state_file.read_bytes()
        with patch.object(monitor, "gh_get", side_effect=OSError("offline")), patch.object(
                monitor, "feishu_token") as token, patch.object(monitor, "send_card") as send:
            with self.assertRaisesRegex(RuntimeError, "All GitHub polls failed"):
                monitor.main()
        token.assert_not_called()
        send.assert_not_called()
        self.assertEqual(self.state_file.read_bytes(), original)

    def test_first_install_does_not_announce_or_save_a_failed_baseline(self):
        self.state_file.unlink()
        with patch.object(monitor, "gh_get", side_effect=OSError("offline")), patch.object(
                monitor, "feishu_token") as token, patch.object(monitor, "send_card") as send:
            with self.assertRaisesRegex(RuntimeError, "All GitHub polls failed"):
                monitor.main()
        token.assert_not_called()
        send.assert_not_called()
        self.assertFalse(self.state_file.exists())

    def test_successful_empty_poll_is_not_failure_even_with_unchanged_watermark(self):
        for issues_ok, prs_ok in ((True, True), (True, False), (False, True)):
            with self.subTest(issues_ok=issues_ok, prs_ok=prs_ok):
                self.initial["issue_checked_at"] = {"a/b": NOW.isoformat()}
                self.state_file.write_text(json.dumps(self.initial), encoding="utf-8")
                def fake(path, token):
                    if path.startswith("/search/"):
                        if prs_ok:
                            return search([])
                    elif issues_ok:
                        return []
                    raise OSError("offline")
                with patch.object(monitor, "gh_get", side_effect=fake), patch.object(
                        monitor, "send_card") as send:
                    monitor.main()
                send.assert_not_called()
                self.assertEqual(self.read_state()["last_check"], NOW.isoformat())
                self.assertEqual(self.read_state()["issue_checked_at"], {"a/b": NOW.isoformat()})

    def test_partial_failure_still_notifies_and_saves_successful_repository(self):
        self.initial.update(issue_checked_at={"a/b": EARLIER.isoformat()},
                            prs={"a/b#1": {**monitor.pr_snapshot(pr()), "reviews": 4}})
        self.state_file.write_text(json.dumps(self.initial), encoding="utf-8")
        def fake(path, token):
            if path.startswith("/repos/c/d/issues?"):
                return [issue(2)]
            raise OSError("offline")
        with patch.object(monitor, "load_tiers", return_value=(["a/b", "c/d"], [])), patch.object(
                monitor, "gh_get", side_effect=fake), patch.object(monitor, "send_card") as send:
            monitor.main()
        send.assert_called_once()
        self.assertIn("c/d#2", send.call_args.args[3])
        state = self.read_state()
        self.assertEqual(state["issue_checked_at"],
                         {"a/b": EARLIER.isoformat(), "c/d": NOW.isoformat()})
        self.assertEqual(state["prs"], self.initial["prs"])
        self.assertIn("c/d#2", state["seen"])

    def test_quiet_round_saves_progress_without_feishu_credentials_or_service(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "fake"}, clear=True), \
             patch.object(monitor, "gh_get", side_effect=self.api(0)), \
             patch.object(monitor, "feishu_token", side_effect=RuntimeError("offline")) as token, \
             patch.object(monitor, "send_card") as send:
            monitor.main()
        token.assert_not_called()
        send.assert_not_called()
        self.assertEqual(self.read_state()["issue_checked_at"]["a/b"], NOW.isoformat())
        self.assertEqual(self.read_state()["rot_index"], 1)

    def test_notification_auth_failure_degrades_to_digest_and_saves_state(self):
        for first_install in (False, True):
            with self.subTest(first_install=first_install):
                if first_install:
                    self.state_file.unlink()
                    self.undelivered.unlink(missing_ok=True)
                with patch.object(monitor, "gh_get", side_effect=self.api(1)), \
                     patch.object(monitor, "feishu_token", side_effect=RuntimeError("offline")), \
                     patch.object(monitor, "send_card") as send:
                    monitor.main()
                send.assert_not_called()
                state = self.read_state()
                self.assertEqual(state["last_check"], NOW.isoformat())
                self.assertIn("a/b#0", state["seen"])
                if first_install:
                    self.assertFalse(self.undelivered.exists())
                else:
                    lines = self.undelivered.read_text(encoding="utf-8").splitlines()
                    self.assertEqual([json.loads(l)["key"] for l in lines], ["a/b#0"])

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

    def test_send_failure_degrades_to_digest_and_saves_state(self):
        with patch.object(monitor, "gh_get", side_effect=self.api(25)), patch.object(
                monitor, "send_card", side_effect=["sent", RuntimeError("send failed")]):
            monitor.main()
        state = self.read_state()
        self.assertEqual(len(state["seen"]), 25)
        lines = self.undelivered.read_text(encoding="utf-8").splitlines()
        self.assertEqual({json.loads(l)["key"] for l in lines},
                         {f"a/b#{n}" for n in range(25)})

    def test_undelivered_digest_appends_across_rounds_and_caps_at_limit(self):
        self.undelivered.write_text(
            "\n".join(json.dumps({"ts": "x", "key": f"old{i}"})
                      for i in range(monitor.UNDELIVERED_LIMIT)) + "\n",
            encoding="utf-8")
        events = [{"key": "a/b#1", "kind": "merged", "title": "t", "url": "u"}]
        with patch.object(monitor, "gh_get", side_effect=self.api(0)), patch.object(
                monitor, "poll_prs", return_value=([events[0]], {}, True)), patch.object(
                monitor, "feishu_token", side_effect=RuntimeError("offline")):
            monitor.main()
        lines = self.undelivered.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), monitor.UNDELIVERED_LIMIT)
        self.assertEqual(json.loads(lines[0])["key"], "old1")
        self.assertEqual(json.loads(lines[-1])["key"], "a/b#1")

    def digest_http(self, open_issues, calls):
        def fake(method, url, payload=None, token=None, headers=None):
            calls.append((method, url, payload))
            if "/issues?" in url:  # digest issue search
                return open_issues
            if url.endswith("/issues"):
                return {"number": 9}
            return {}
        return fake

    def test_digest_issue_opened_when_channel_down(self):
        calls = []
        with patch.dict(os.environ, GITHUB_REPOSITORY="own/repo"), \
             patch.object(monitor, "http", side_effect=self.digest_http([], calls)), \
             patch.object(monitor, "gh_get", side_effect=self.api(1)), \
             patch.object(monitor, "feishu_token", side_effect=RuntimeError("offline")):
            monitor.main()
        posts = [(m, u, p) for m, u, p in calls if m == "POST" and u.endswith("/issues")]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2]["labels"], [monitor.DIGEST_LABEL])
        self.assertIn("a/b#0", posts[0][2]["body"])
        self.assertIn("a/b#0", self.read_state()["seen"])

    def test_digest_issue_commented_when_already_open(self):
        calls = []
        with patch.dict(os.environ, GITHUB_REPOSITORY="own/repo"), \
             patch.object(monitor, "http", side_effect=self.digest_http(
                 [{"number": 7, "title": monitor.DIGEST_TITLE}], calls)), \
             patch.object(monitor, "gh_get", side_effect=self.api(1)), \
             patch.object(monitor, "feishu_token", side_effect=RuntimeError("offline")):
            monitor.main()
        comments = [(m, u, p) for m, u, p in calls if m == "POST" and u.endswith("/comments")]
        self.assertEqual(len(comments), 1)
        self.assertIn("/issues/7/comments", comments[0][1])
        self.assertFalse(any(m == "POST" and u.endswith("/issues") for m, u, _ in calls))

    def test_digest_issue_closed_when_channel_recovers(self):
        calls = []
        with patch.dict(os.environ, GITHUB_REPOSITORY="own/repo"), \
             patch.object(monitor, "http", side_effect=self.digest_http(
                 [{"number": 7, "title": monitor.DIGEST_TITLE}], calls)), \
             patch.object(monitor, "gh_get", side_effect=self.api(1)), \
             patch.object(monitor, "send_card") as send:
            monitor.main()
        send.assert_called_once()
        patches = [(m, u, p) for m, u, p in calls if m == "PATCH"]
        self.assertEqual(patches, [("PATCH", "https://api.github.com/repos/own/repo/issues/7",
                                    {"state": "closed"})])

    def test_quiet_round_keeps_outage_digest_open_without_testing_channel(self):
        calls = []
        with patch.dict(os.environ, GITHUB_REPOSITORY="own/repo"), \
             patch.object(monitor, "http", side_effect=self.digest_http([], calls)), \
             patch.object(monitor, "gh_get", side_effect=self.api(1)), \
             patch.object(monitor, "feishu_token", side_effect=RuntimeError("offline")):
            monitor.main()
        self.assertTrue(any(method == "POST" and url.endswith("/issues")
                            for method, url, _ in calls))
        undelivered = self.undelivered.read_bytes()
        calls.clear()
        with patch.dict(os.environ, GITHUB_REPOSITORY="own/repo"), \
             patch.object(monitor, "http", side_effect=self.digest_http(
                 [{"number": 9, "title": monitor.DIGEST_TITLE}], calls)), \
             patch.object(monitor, "gh_get", side_effect=self.api(0)), \
             patch.object(monitor, "feishu_token", side_effect=RuntimeError("offline")) as token, \
             patch.object(monitor, "send_card") as send:
            monitor.main()
        token.assert_not_called()
        send.assert_not_called()
        self.assertEqual(calls, [])
        self.assertEqual(self.undelivered.read_bytes(), undelivered)
        self.assertEqual(self.read_state()["rot_index"], 2)

    def test_digest_issue_api_failure_does_not_fail_round(self):
        with patch.dict(os.environ, GITHUB_REPOSITORY="own/repo"), \
             patch.object(monitor, "http", side_effect=OSError("issues api down")), \
             patch.object(monitor, "gh_get", side_effect=self.api(1)), \
             patch.object(monitor, "feishu_token", side_effect=RuntimeError("offline")):
            monitor.main()
        self.assertEqual(self.read_state()["last_check"], NOW.isoformat())
        self.assertIn("a/b#0", self.read_state()["seen"])

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
                monitor, "poll_prs", return_value=([event], {}, True)), patch.object(monitor, "send_card") as send:
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
