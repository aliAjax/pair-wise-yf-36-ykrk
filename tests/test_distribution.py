import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, InvalidTransition, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class DistributionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _create_stored_sample(self):
        participant = self.service.create(
            self.actor, "participant", {"name": "Participant"}
        )
        consent = self.service.create(
            self.actor,
            "consent",
            {"participant_id": participant["id"], "scope": ["research"]},
        )
        self.service.transition(
            self.actor,
            consent["id"],
            "activate",
            {"scope": ["research"], "version": "v1", "expires_at": "2099-01-01"},
        )
        sample = self.service.create(
            self.actor,
            "sample",
            {
                "participant_id": participant["id"],
                "sample_code": "B-001",
                "collected_at": "2026-01-01",
            },
        )
        self.service.transition(
            self.actor,
            sample["id"],
            "store",
            {"freezer": "F1", "position": "A1", "consent_id": consent["id"]},
        )
        return participant, sample

    def _create_distribution(self, sample, recipient="外单位检测中心", purpose="基因检测"):
        return self.service.create(
            self.actor,
            "distribution",
            {
                "sample_id": sample["id"],
                "recipient": recipient,
                "purpose": purpose,
                "shipped_at": "2026-02-01",
            },
        )

    def test_create_distribution_pending(self):
        _, sample = self._create_stored_sample()
        dist = self._create_distribution(sample)
        self.assertEqual(dist["status"], "pending")
        self.assertEqual(dist["data"]["recipient"], "外单位检测中心")
        self.assertEqual(dist["data"]["purpose"], "基因检测")
        self.assertEqual(dist["data"]["participant_id"], sample["data"]["participant_id"])

    def test_distribution_requires_stored_sample(self):
        participant = self.service.create(self.actor, "participant", {"name": "Participant"})
        sample = self.service.create(
            self.actor,
            "sample",
            {"participant_id": participant["id"], "sample_code": "B-002", "collected_at": "2026-01-01"},
        )
        with self.assertRaises(ValidationError):
            self.service.create(
                self.actor,
                "distribution",
                {"sample_id": sample["id"], "recipient": "外单位", "purpose": "检测"},
            )

    def test_distribution_requires_recipient_and_purpose(self):
        _, sample = self._create_stored_sample()
        with self.assertRaises(ValidationError):
            self.service.create(
                self.actor, "distribution", {"sample_id": sample["id"], "purpose": "检测"}
            )
        with self.assertRaises(ValidationError):
            self.service.create(
                self.actor, "distribution", {"sample_id": sample["id"], "recipient": "外单位"}
            )

    def test_receipt_distribution(self):
        _, sample = self._create_stored_sample()
        dist = self._create_distribution(sample)
        dist = self.service.transition(
            self.actor, dist["id"], "receipt", {"received_at": "2026-02-10"}
        )
        self.assertEqual(dist["status"], "receipted")

    def test_freeze_on_withdrawal_approval(self):
        participant, sample = self._create_stored_sample()
        dist = self._create_distribution(sample)
        self.assertEqual(dist["status"], "pending")
        withdrawal = self.service.create(
            self.actor,
            "withdrawal",
            {"participant_id": participant["id"], "requested_at": "2026-03-01"},
        )
        self.service.transition(
            self.actor,
            withdrawal["id"],
            "approve",
            {"reason": "participant request", "sample_ids": [sample["id"]]},
        )
        dist = self.service.get(dist["id"])
        self.assertEqual(dist["status"], "frozen")

    def test_freeze_skips_receipted_distribution(self):
        participant, sample = self._create_stored_sample()
        dist = self._create_distribution(sample)
        self.service.submit_receipt(
            self.actor, {"distribution_id": dist["id"], "received_at": "2026-02-10"}
        )
        dist = self.service.get(dist["id"])
        self.assertEqual(dist["status"], "receipted")
        withdrawal = self.service.create(
            self.actor,
            "withdrawal",
            {"participant_id": participant["id"], "requested_at": "2026-03-01"},
        )
        self.service.transition(
            self.actor,
            withdrawal["id"],
            "approve",
            {"reason": "participant request", "sample_ids": [sample["id"]]},
        )
        dist = self.service.get(dist["id"])
        self.assertEqual(dist["status"], "receipted")

    def test_receipt_reconciled_when_network_up(self):
        _, sample = self._create_stored_sample()
        dist = self._create_distribution(sample)
        self.assertTrue(self.service.network_ok)
        receipt = self.service.submit_receipt(
            self.actor,
            {
                "distribution_id": dist["id"],
                "receipt_no": "R-001",
                "received_at": "2026-02-10",
                "result": "检测完成",
            },
        )
        self.assertEqual(receipt["status"], "reconciled")
        dist = self.service.get(dist["id"])
        self.assertEqual(dist["status"], "receipted")

    def test_receipt_suspended_when_network_down_then_retried(self):
        _, sample = self._create_stored_sample()
        dist = self._create_distribution(sample)
        self.service.set_network(False)
        receipt = self.service.submit_receipt(
            self.actor, {"distribution_id": dist["id"], "received_at": "2026-02-10"}
        )
        self.assertEqual(receipt["status"], "pending")  # 挂起
        dist = self.service.get(dist["id"])
        self.assertEqual(dist["status"], "pending")  # 未对账
        # 网络恢复后重试
        self.service.set_network(True)
        receipt = self.service.retry_receipt(self.actor, receipt["id"])
        self.assertEqual(receipt["status"], "reconciled")
        dist = self.service.get(dist["id"])
        self.assertEqual(dist["status"], "receipted")

    def test_retry_keeps_pending_when_network_still_down(self):
        _, sample = self._create_stored_sample()
        dist = self._create_distribution(sample)
        self.service.set_network(False)
        receipt = self.service.submit_receipt(
            self.actor, {"distribution_id": dist["id"], "received_at": "2026-02-10"}
        )
        self.assertEqual(receipt["status"], "pending")
        receipt = self.service.retry_receipt(self.actor, receipt["id"])
        self.assertEqual(receipt["status"], "pending")

    def test_freeze_wins_then_receipt_conflicts(self):
        _, sample = self._create_stored_sample()
        dist = self._create_distribution(sample)
        self.service.transition(
            self.actor, dist["id"], "freeze", {}, expected_version=dist["version"]
        )
        dist = self.service.get(dist["id"])
        self.assertEqual(dist["status"], "frozen")
        receipt = self.service.submit_receipt(
            self.actor, {"distribution_id": dist["id"], "received_at": "2026-02-10"}
        )
        self.assertEqual(receipt["status"], "conflict")
        dist = self.service.get(dist["id"])
        self.assertEqual(dist["status"], "frozen")

    def test_receipt_wins_then_freeze_fails(self):
        _, sample = self._create_stored_sample()
        dist = self._create_distribution(sample)
        receipt = self.service.submit_receipt(
            self.actor, {"distribution_id": dist["id"], "received_at": "2026-02-10"}
        )
        self.assertEqual(receipt["status"], "reconciled")
        # 回执已成功，冻结要么被规则引擎拦截（状态已不是 pending），
        # 要么在乐观锁版本检查处失败——总之不能成功
        with self.assertRaises((ConflictError, InvalidTransition)):
            self.service.transition(
                self.actor, dist["id"], "freeze", {}, expected_version=dist["version"]
            )
        dist = self.service.get(dist["id"])
        self.assertEqual(dist["status"], "receipted")

    def test_receipt_vs_freeze_concurrent_only_one_wins(self):
        _, sample = self._create_stored_sample()
        dist = self._create_distribution(sample)
        results = {}

        def do_receipt():
            try:
                r = self.service.submit_receipt(
                    self.actor,
                    {"distribution_id": dist["id"], "received_at": "2026-02-10"},
                )
                # 回执只有对账成功才算赢；conflict 表示冻结赢了
                results["receipt"] = ("ok", r["status"])
            except Exception as exc:  # noqa: BLE001
                results["receipt"] = ("fail", type(exc).__name__)

        def do_freeze():
            try:
                r = self.service.transition(
                    self.actor, dist["id"], "freeze", {}, expected_version=dist["version"]
                )
                results["freeze"] = ("ok", r["status"])
            except Exception as exc:  # noqa: BLE001
                results["freeze"] = ("fail", type(exc).__name__)

        t1 = threading.Thread(target=do_receipt)
        t2 = threading.Thread(target=do_freeze)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        def receipt_won():
            return results.get("receipt") == ("ok", "reconciled")

        def freeze_won():
            return results.get("freeze") == ("ok", "frozen")

        self.assertEqual(
            receipt_won() + freeze_won(),
            1,
            "exactly one side should win, got: %s" % results,
        )
        dist = self.service.get(dist["id"])
        self.assertIn(dist["status"], ("receipted", "frozen"))

    def test_list_distributions_by_status(self):
        _, sample = self._create_stored_sample()
        d1 = self._create_distribution(sample, recipient="机构A")
        d2 = self._create_distribution(sample, recipient="机构B")
        self.service.submit_receipt(
            self.actor, {"distribution_id": d2["id"], "received_at": "2026-02-10"}
        )
        pending = self.service.list("distribution", status="pending")
        receipted = self.service.list("distribution", status="receipted")
        self.assertEqual([d["id"] for d in pending], [d1["id"]])
        self.assertEqual([d["id"] for d in receipted], [d2["id"]])

    def test_recall_then_return_or_destroy(self):
        _, sample = self._create_stored_sample()
        dist = self._create_distribution(sample)
        self.service.transition(
            self.actor, dist["id"], "freeze", {}, expected_version=dist["version"]
        )
        dist = self.service.transition(self.actor, dist["id"], "recall", {})
        self.assertEqual(dist["status"], "recalled")
        dist = self.service.transition(
            self.actor, dist["id"], "return", {"returned_at": "2026-04-01"}
        )
        self.assertEqual(dist["status"], "returned")

        dist2 = self._create_distribution(sample, recipient="机构C")
        self.service.transition(
            self.actor, dist2["id"], "freeze", {}, expected_version=dist2["version"]
        )
        self.service.transition(self.actor, dist2["id"], "recall", {})
        dist2 = self.service.transition(
            self.actor,
            dist2["id"],
            "destroy",
            {"destroyed_at": "2026-04-01", "reason": "无法退回，销毁实物"},
        )
        self.assertEqual(dist2["status"], "destroyed")

    def test_receipted_sample_return_or_destroy(self):
        _, sample = self._create_stored_sample()
        dist = self._create_distribution(sample)
        self.service.submit_receipt(
            self.actor, {"distribution_id": dist["id"], "received_at": "2026-02-10"}
        )
        dist = self.service.transition(
            self.actor, dist["id"], "return", {"returned_at": "2026-04-01"}
        )
        self.assertEqual(dist["status"], "returned")

        dist2 = self._create_distribution(sample, recipient="机构D")
        self.service.submit_receipt(
            self.actor, {"distribution_id": dist2["id"], "received_at": "2026-02-10"}
        )
        dist2 = self.service.transition(
            self.actor,
            dist2["id"],
            "destroy",
            {"destroyed_at": "2026-04-01", "reason": "结果已出，销毁实物"},
        )
        self.assertEqual(dist2["status"], "destroyed")


if __name__ == "__main__":
    unittest.main()
