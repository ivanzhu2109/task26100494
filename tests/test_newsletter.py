import os
import tempfile
import unittest

from newsletter import NewsletterLibrary, TargetStatus, Tier


def ok_generator(content, subscriber_id):
    return f"[{content}] -> {subscriber_id}"


class RosterTest(unittest.TestCase):
    def setUp(self):
        self.lib = NewsletterLibrary()

    def tearDown(self):
        self.lib.close()

    def test_versions_strictly_increasing(self):
        v1 = self.lib.subscribe("a", Tier.FREE)
        v2 = self.lib.subscribe("b", Tier.PAID)
        v3 = self.lib.set_tier("a", Tier.PAID)
        v4 = self.lib.unsubscribe("b")
        v5 = self.lib.subscribe("b", Tier.FREE)  # 重新订阅沿用原 ID
        versions = [v1, v2, v3, v4, v5]
        self.assertEqual(versions, sorted(versions))
        self.assertEqual(len(set(versions)), len(versions))

    def test_invalid_changes_raise(self):
        self.lib.subscribe("a", Tier.FREE)
        with self.assertRaises(ValueError):
            self.lib.subscribe("a", Tier.PAID)  # 已活跃
        with self.assertRaises(ValueError):
            self.lib.unsubscribe("ghost")
        with self.assertRaises(ValueError):
            self.lib.set_tier("ghost", Tier.PAID)
        with self.assertRaises(ValueError):
            self.lib.subscribe("c", Tier.ALL)


class SealTest(unittest.TestCase):
    def setUp(self):
        self.lib = NewsletterLibrary()

    def tearDown(self):
        self.lib.close()

    def test_seal_snapshots_exact_active_tier_members(self):
        self.lib.subscribe("f1", Tier.FREE)
        self.lib.subscribe("f2", Tier.FREE)
        self.lib.subscribe("p1", Tier.PAID)
        self.lib.unsubscribe("f2")
        sealed = self.lib.seal_issue("i1", "hello", Tier.FREE)
        info = self.lib.get_issue("i1")
        self.assertEqual([t.subscriber_id for t in info.targets], ["f1"])
        self.assertEqual(info.sealed_version, sealed)
        self.assertFalse(info.completed)

    def test_seal_freezes_targets_against_later_changes(self):
        self.lib.subscribe("f1", Tier.FREE)
        self.lib.seal_issue("i1", "hello", Tier.FREE)
        self.lib.subscribe("f2", Tier.FREE)      # 封版后新增
        self.lib.subscribe("p1", Tier.PAID)
        self.lib.set_tier("p1", Tier.FREE)       # 封版后等级调整
        info = self.lib.get_issue("i1")
        self.assertEqual([t.subscriber_id for t in info.targets], ["f1"])

    def test_seal_all_tiers(self):
        self.lib.subscribe("f1", Tier.FREE)
        self.lib.subscribe("p1", Tier.PAID)
        self.lib.seal_issue("i1", "hello", Tier.ALL)
        info = self.lib.get_issue("i1")
        self.assertEqual([t.subscriber_id for t in info.targets], ["f1", "p1"])

    def test_duplicate_issue_rejected(self):
        self.lib.seal_issue("i1", "a", Tier.ALL)
        with self.assertRaises(ValueError):
            self.lib.seal_issue("i1", "b", Tier.ALL)

    def test_unknown_issue_query_raises(self):
        with self.assertRaises(KeyError):
            self.lib.get_issue("nope")


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.lib = NewsletterLibrary()

    def tearDown(self):
        self.lib.close()

    def test_unsubscribe_after_seal_suppresses_even_if_resubscribed(self):
        self.lib.subscribe("a", Tier.FREE)
        self.lib.subscribe("b", Tier.FREE)
        self.lib.seal_issue("i1", "c", Tier.FREE)
        self.lib.unsubscribe("a")
        self.lib.subscribe("a", Tier.FREE)  # 重新订阅也不能恢复本期资格
        self.lib.advance("i1", ok_generator)
        info = self.lib.get_issue("i1")
        by_id = {t.subscriber_id: t for t in info.targets}
        self.assertEqual(by_id["a"].status, TargetStatus.SUPPRESSED)
        self.assertIsNone(by_id["a"].body)
        self.assertEqual(by_id["b"].status, TargetStatus.CONFIRMED)
        self.assertEqual(by_id["b"].body, "[c] -> b")
        self.assertTrue(info.completed)

    def test_unsubscribe_during_generation_suppresses_old_issue_new_issue_ok(self):
        # 正文生成期间退订又重新订阅：旧刊期抑制，新刊期可确认。
        self.lib.subscribe("a", Tier.FREE)
        self.lib.seal_issue("old", "c1", Tier.FREE)

        lib = self.lib

        def flaky_generator(content, subscriber_id):
            lib.unsubscribe(subscriber_id)
            lib.subscribe(subscriber_id, Tier.FREE)
            return "late-body"

        self.lib.advance("old", flaky_generator)
        old_info = self.lib.get_issue("old")
        self.assertEqual(old_info.targets[0].status, TargetStatus.SUPPRESSED)
        self.assertIsNone(old_info.targets[0].body)
        self.assertTrue(old_info.completed)

        self.lib.seal_issue("new", "c2", Tier.FREE)
        self.lib.advance("new", ok_generator)
        new_info = self.lib.get_issue("new")
        self.assertEqual(new_info.targets[0].status, TargetStatus.CONFIRMED)
        self.assertEqual(new_info.targets[0].body, "[c2] -> a")

    def test_confirmed_before_unsubscribe_is_kept(self):
        self.lib.subscribe("a", Tier.FREE)
        self.lib.subscribe("b", Tier.FREE)
        self.lib.seal_issue("i1", "c", Tier.FREE)

        lib = self.lib
        calls = []

        def confirm_then_unsubscribe(content, subscriber_id):
            calls.append(subscriber_id)
            if subscriber_id == "b":
                lib.unsubscribe("b")  # b 尚未生成，之后应被抑制
            return ok_generator(content, subscriber_id)

        self.lib.advance("i1", confirm_then_unsubscribe)
        info = self.lib.get_issue("i1")
        by_id = {t.subscriber_id: t for t in info.targets}
        self.assertEqual(by_id["a"].status, TargetStatus.CONFIRMED)
        self.assertEqual(by_id["b"].status, TargetStatus.SUPPRESSED)

        # 已确认的 a 之后退订，确认保留不追溯撤销。
        self.lib.unsubscribe("a")
        info = self.lib.get_issue("i1")
        by_id = {t.subscriber_id: t for t in info.targets}
        self.assertEqual(by_id["a"].status, TargetStatus.CONFIRMED)
        self.assertEqual(by_id["a"].body, "[c] -> a")

    def test_repeated_advance_with_exceptions_single_terminal_state(self):
        self.lib.subscribe("a", Tier.FREE)
        self.lib.subscribe("b", Tier.FREE)
        self.lib.subscribe("c", Tier.FREE)
        self.lib.seal_issue("i1", "c", Tier.FREE)

        attempts = {"a": 0, "b": 0, "c": 0}

        def flaky(content, subscriber_id):
            attempts[subscriber_id] += 1
            if attempts[subscriber_id] == 1:
                raise RuntimeError("boom")  # 首轮全部失败
            return ok_generator(content, subscriber_id)

        self.lib.advance("i1", flaky)  # 全部异常：无确认、不卡死
        info = self.lib.get_issue("i1")
        self.assertTrue(all(t.status == TargetStatus.PENDING for t in info.targets))
        self.assertFalse(info.completed)

        self.lib.advance("i1", flaky)  # 重试成功
        self.lib.advance("i1", ok_generator)  # 完成后推进不改变结果
        info = self.lib.get_issue("i1")
        self.assertTrue(info.completed)
        bodies = [t.body for t in info.targets]
        self.assertEqual(len(bodies), len(set(bodies)))  # 确认正文无重复
        for t in info.targets:
            self.assertEqual(t.status, TargetStatus.CONFIRMED)
            self.assertEqual(t.body, f"[c] -> {t.subscriber_id}")
        # 每个目标只被确认一次：成功路径各调用一次生成
        self.assertEqual(attempts, {"a": 2, "b": 2, "c": 2})

    def test_advance_unknown_issue_raises(self):
        with self.assertRaises(KeyError):
            self.lib.advance("nope", ok_generator)

    def test_query_never_exposes_uncommitted_body(self):
        self.lib.subscribe("a", Tier.FREE)
        self.lib.seal_issue("i1", "c", Tier.FREE)
        info = self.lib.get_issue("i1")
        self.assertEqual(info.targets[0].status, TargetStatus.PENDING)
        self.assertIsNone(info.targets[0].body)


class PersistenceTest(unittest.TestCase):
    def test_file_backed_db_roundtrip(self):
        fd, path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            lib = NewsletterLibrary(path)
            lib.subscribe("a", Tier.PAID)
            lib.seal_issue("i1", "c", Tier.PAID)
            lib.advance("i1", ok_generator)
            lib.close()

            lib2 = NewsletterLibrary(path)
            info = lib2.get_issue("i1")
            self.assertEqual(info.targets[0].status, TargetStatus.CONFIRMED)
            self.assertEqual(info.targets[0].body, "[c] -> a")
            lib2.close()
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
