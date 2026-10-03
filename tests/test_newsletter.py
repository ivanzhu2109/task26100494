import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from newsletter import NewsletterService, RosterError
from newsletter.issues import IssueError


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        os.unlink(self.db_path)
        self.svc = NewsletterService(self.db_path)

    def tearDown(self):
        self.svc.close()
        os.unlink(self.db_path)

    def ok_generator(self, content, target_id):
        return f"{content} -> {target_id}"


class RosterTest(ServiceTestCase):
    def test_versions_strictly_increase(self):
        v1 = self.svc.roster.subscribe("a", "free")
        v2 = self.svc.roster.subscribe("b", "paid")
        v3 = self.svc.roster.set_tier("a", "paid")
        v4 = self.svc.roster.unsubscribe("b")
        self.assertEqual([v1, v2, v3, v4], [1, 2, 3, 4])

    def test_noop_change_does_not_bump_version(self):
        self.svc.roster.subscribe("a", "free")
        v = self.svc.roster.subscribe("a", "free")
        self.assertEqual(v, 1)
        self.assertEqual(self.svc.roster.current_version(), 1)

    def test_resubscribe_reuses_id(self):
        self.svc.roster.subscribe("a", "paid")
        self.svc.roster.unsubscribe("a")
        self.svc.roster.subscribe("a", "free")
        issue_id = self.svc.issues.seal("hello", "free")
        result = self.svc.issues.result(issue_id)
        self.assertEqual([t.subscriber_id for t in result.targets], ["a"])

    def test_unknown_subscriber_raises(self):
        with self.assertRaises(RosterError):
            self.svc.roster.unsubscribe("ghost")
        with self.assertRaises(RosterError):
            self.svc.roster.set_tier("ghost", "paid")


class SealTest(ServiceTestCase):
    def test_targets_match_sealed_version_exactly(self):
        self.svc.roster.subscribe("free1", "free")
        self.svc.roster.subscribe("paid1", "paid")
        self.svc.roster.subscribe("gone", "paid")
        self.svc.roster.unsubscribe("gone")
        paid_issue = self.svc.issues.seal("paid content", "paid")
        free_issue = self.svc.issues.seal("free content", "free")
        paid_targets = {
            t.subscriber_id for t in self.svc.issues.result(paid_issue).targets
        }
        free_targets = {
            t.subscriber_id for t in self.svc.issues.result(free_issue).targets
        }
        self.assertEqual(paid_targets, {"paid1"})
        self.assertEqual(free_targets, {"free1", "paid1"})

    def test_post_seal_changes_do_not_drift_targets(self):
        self.svc.roster.subscribe("a", "free")
        issue_id = self.svc.issues.seal("c", "paid")
        self.svc.roster.subscribe("b", "paid")
        self.svc.roster.set_tier("a", "paid")
        result = self.svc.issues.result(issue_id)
        self.assertEqual(result.targets, [])
        self.assertEqual(result.status, "open")

    def test_sealed_version_returned_boundary(self):
        self.svc.roster.subscribe("a", "free")
        issue_id = self.svc.issues.seal("c", "free")
        result = self.svc.issues.result(issue_id)
        self.assertEqual(result.sealed_version, 1)
        self.svc.roster.subscribe("b", "free")
        issue_id2 = self.svc.issues.seal("c2", "free")
        result2 = self.svc.issues.result(issue_id2)
        self.assertEqual(result2.sealed_version, 2)
        self.assertEqual(len(result2.targets), 2)

    def test_invalid_tier_raises(self):
        with self.assertRaises(IssueError):
            self.svc.issues.seal("c", "vip")


class UnsubDuringGenerationTest(ServiceTestCase):
    def test_unsub_resubscribe_during_generation(self):
        self.svc.roster.subscribe("x", "paid")
        old_issue = self.svc.issues.seal("old", "paid")

        def flaky_generator(content, target_id):
            self.svc.roster.unsubscribe(target_id)
            self.svc.roster.subscribe(target_id, "paid")
            return "late body"

        self.svc.issues.advance(old_issue, flaky_generator)
        old_result = self.svc.issues.result(old_issue)
        self.assertEqual(old_result.status, "complete")
        self.assertEqual(old_result.targets[0].status, "suppressed")
        self.assertIsNone(old_result.targets[0].body)

        new_issue = self.svc.issues.seal("new", "paid")
        self.svc.issues.advance(new_issue, self.ok_generator)
        new_result = self.svc.issues.result(new_issue)
        self.assertEqual(new_result.status, "complete")
        self.assertEqual(new_result.targets[0].status, "confirmed")
        self.assertEqual(new_result.targets[0].body, "new -> x")

    def test_unsub_after_seal_suppresses_even_without_resubscribe(self):
        self.svc.roster.subscribe("x", "free")
        self.svc.roster.subscribe("y", "free")
        issue_id = self.svc.issues.seal("c", "free")
        self.svc.roster.unsubscribe("x")
        self.svc.issues.advance(issue_id, self.ok_generator)
        result = self.svc.issues.result(issue_id)
        by_id = {t.subscriber_id: t for t in result.targets}
        self.assertEqual(by_id["x"].status, "suppressed")
        self.assertEqual(by_id["y"].status, "confirmed")

    def test_confirmation_before_unsub_is_kept(self):
        self.svc.roster.subscribe("x", "free")
        issue_id = self.svc.issues.seal("c", "free")
        self.svc.issues.advance(issue_id, self.ok_generator)
        self.svc.roster.unsubscribe("x")
        result = self.svc.issues.result(issue_id)
        self.assertEqual(result.targets[0].status, "confirmed")
        self.assertEqual(result.targets[0].body, "c -> x")


class AdvanceRetryTest(ServiceTestCase):
    def test_exceptions_retry_and_single_terminal_state(self):
        self.svc.roster.subscribe("a", "free")
        self.svc.roster.subscribe("b", "free")
        issue_id = self.svc.issues.seal("c", "free")

        calls = {"a": 0, "b": 0}

        def unstable(content, target_id):
            calls[target_id] += 1
            if calls[target_id] == 1:
                raise RuntimeError("boom")
            return f"body-{target_id}"

        self.svc.issues.advance(issue_id, unstable)
        mid = self.svc.issues.result(issue_id)
        self.assertEqual(mid.status, "open")
        self.assertTrue(all(t.status == "pending" for t in mid.targets))
        self.assertTrue(all(t.body is None for t in mid.targets))

        self.svc.issues.advance(issue_id, unstable)
        self.svc.issues.advance(issue_id, unstable)
        result = self.svc.issues.result(issue_id)
        self.assertEqual(result.status, "complete")
        self.assertEqual(
            {t.subscriber_id: t.status for t in result.targets},
            {"a": "confirmed", "b": "confirmed"},
        )
        bodies = [t.body for t in result.targets]
        self.assertEqual(sorted(bodies), ["body-a", "body-b"])
        self.assertEqual(calls, {"a": 2, "b": 2})

    def test_advance_after_complete_is_noop(self):
        self.svc.roster.subscribe("a", "free")
        issue_id = self.svc.issues.seal("c", "free")
        self.svc.issues.advance(issue_id, self.ok_generator)
        self.assertEqual(self.svc.issues.result(issue_id).status, "complete")

        def forbidden(content, target_id):
            raise AssertionError("generator must not run after completion")

        self.svc.issues.advance(issue_id, forbidden)
        result = self.svc.issues.result(issue_id)
        self.assertEqual(result.targets[0].status, "confirmed")
        self.assertEqual(result.targets[0].body, "c -> a")

    def test_mixed_suppress_and_confirm_completion(self):
        self.svc.roster.subscribe("keep", "paid")
        self.svc.roster.subscribe("drop", "paid")
        issue_id = self.svc.issues.seal("c", "paid")
        self.svc.roster.unsubscribe("drop")
        self.svc.roster.subscribe("drop", "paid")

        attempts = {"n": 0}

        def sometimes(content, target_id):
            if target_id == "keep":
                attempts["n"] += 1
                if attempts["n"] == 1:
                    raise RuntimeError("transient")
            return f"ok-{target_id}"

        self.svc.issues.advance(issue_id, sometimes)
        self.svc.issues.advance(issue_id, sometimes)
        result = self.svc.issues.result(issue_id)
        self.assertEqual(result.status, "complete")
        by_id = {t.subscriber_id: t for t in result.targets}
        self.assertEqual(by_id["keep"].status, "confirmed")
        self.assertEqual(by_id["keep"].body, "ok-keep")
        self.assertEqual(by_id["drop"].status, "suppressed")
        self.assertIsNone(by_id["drop"].body)


class QueryTest(ServiceTestCase):
    def test_unknown_issue_raises(self):
        with self.assertRaises(IssueError):
            self.svc.issues.result(999)
        with self.assertRaises(IssueError):
            self.svc.issues.advance(999, self.ok_generator)

    def test_pending_targets_expose_no_body(self):
        self.svc.roster.subscribe("a", "free")
        issue_id = self.svc.issues.seal("c", "free")
        result = self.svc.issues.result(issue_id)
        self.assertEqual(result.targets[0].status, "pending")
        self.assertIsNone(result.targets[0].body)

    def test_persistence_across_reopen(self):
        self.svc.roster.subscribe("a", "free")
        issue_id = self.svc.issues.seal("c", "free")
        self.svc.issues.advance(issue_id, self.ok_generator)
        self.svc.close()
        self.svc = NewsletterService(self.db_path)
        result = self.svc.issues.result(issue_id)
        self.assertEqual(result.targets[0].body, "c -> a")
        self.assertEqual(self.svc.roster.current_version(), 1)


if __name__ == "__main__":
    unittest.main()
