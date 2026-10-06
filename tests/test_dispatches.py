import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, InvalidTransition, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class DispatchTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin-1", "admin")
        self.recipient = Actor("ext-lab", "recipient")

    def tearDown(self):
        self.tmp.cleanup()

    def _stored_sample(self, code="B-001"):
        participant = self.service.create(
            self.admin, "participant", {"name": "参与者甲"}
        )
        consent = self.service.create(
            self.admin,
            "consent",
            {"participant_id": participant["id"], "scope": ["research"]},
        )
        consent = self.service.transition(
            self.admin,
            consent["id"],
            "activate",
            {"scope": ["research"], "version": "v1", "expires_at": "2099-01-01"},
        )
        sample = self.service.create(
            self.admin,
            "sample",
            {
                "participant_id": participant["id"],
                "sample_code": code,
                "collected_at": "2026-01-01",
            },
        )
        sample = self.service.transition(
            self.admin,
            sample["id"],
            "store",
            {"freezer": "F1", "position": "A1", "consent_id": consent["id"]},
        )
        return participant, sample

    def _issue(self, sample, **overrides):
        data = {
            "recipient_org": "外检中心",
            "purpose": "肿瘤标志物检测",
            "issued_at": "2026-02-01",
            "sample_ids": [sample["id"]],
        }
        data.update(overrides)
        return self.service.create(self.admin, "dispatch", data)


class DispatchFlowTest(DispatchTestBase):
    def test_issue_leaves_pending_receipt_and_marks_sample_on_loan(self):
        _, sample = self._stored_sample()
        dispatch = self._issue(sample)
        self.assertEqual(dispatch["status"], "pending_receipt")
        self.assertEqual(dispatch["data"]["recipient_org"], "外检中心")
        self.assertEqual(dispatch["data"]["purpose"], "肿瘤标志物检测")
        self.assertEqual(self.service.get(sample["id"])["status"], "on_loan")

        listed = self.service.list("dispatches", status="pending_receipt")
        self.assertEqual([d["id"] for d in listed], [dispatch["id"]])

    def test_issue_requires_recipient_and_purpose(self):
        _, sample = self._stored_sample()
        with self.assertRaises(ValidationError):
            self.service.create(
                self.admin,
                "dispatch",
                {
                    "purpose": "检测",
                    "issued_at": "2026-02-01",
                    "sample_ids": [sample["id"]],
                },
            )
        with self.assertRaises(ValidationError):
            self.service.create(
                self.admin,
                "dispatch",
                {
                    "recipient_org": "外检中心",
                    "issued_at": "2026-02-01",
                    "sample_ids": [sample["id"]],
                },
            )

    def test_only_stored_samples_can_be_issued(self):
        _, sample = self._stored_sample("B-001")
        self._issue(sample)
        _, other = self._stored_sample("B-002")
        self._issue(other)
        # 同一样本不能重复出库。
        with self.assertRaises(InvalidTransition):
            self._issue(sample)

    def test_receipt_reconciles_pending_dispatch(self):
        _, sample = self._stored_sample()
        dispatch = self._issue(sample)
        receipt = self.service.submit_receipt(
            self.recipient,
            {
                "dispatch_id": dispatch["id"],
                "received_at": "2026-02-03",
                "result_status": "completed",
                "result_summary": "未见异常",
            },
        )
        self.assertEqual(receipt["status"], "reconciled")
        self.assertEqual(self.service.get(dispatch["id"])["status"], "acknowledged")

    def test_receipt_sample_list_must_match_dispatch(self):
        _, sample = self._stored_sample()
        dispatch = self._issue(sample)
        with self.assertRaises(ValidationError):
            self.service.submit_receipt(
                self.recipient,
                {
                    "dispatch_id": dispatch["id"],
                    "received_at": "2026-02-03",
                    "result_status": "completed",
                    "received_sample_ids": ["SOME-OTHER-SAMPLE"],
                },
            )

    def test_acknowledged_dispatch_can_be_returned_or_destroyed(self):
        _, sample = self._stored_sample()
        dispatch = self._issue(sample)
        self.service.submit_receipt(
            self.recipient,
            {"dispatch_id": dispatch["id"], "received_at": "2026-02-03",
             "result_status": "completed"},
        )
        returned = self.service.transition(
            self.admin, dispatch["id"], "return", {"returned_at": "2026-03-01"}
        )
        self.assertEqual(returned["status"], "returned")
        self.assertEqual(self.service.get(sample["id"])["status"], "stored")

        # 第二张发放单走销毁分支。
        _, sample2 = self._stored_sample("B-002")
        dispatch2 = self._issue(sample2)
        self.service.submit_receipt(
            self.recipient,
            {"dispatch_id": dispatch2["id"], "received_at": "2026-02-03",
             "result_status": "completed"},
        )
        destroyed = self.service.transition(
            self.admin, dispatch2["id"], "destroy", {"destroyed_at": "2026-03-02"}
        )
        self.assertEqual(destroyed["status"], "destroyed")
        self.assertEqual(self.service.get(sample2["id"])["status"], "destroyed")


class WithdrawalFreezeTest(DispatchTestBase):
    def _withdraw(self, participant, sample):
        withdrawal = self.service.create(
            self.admin,
            "withdrawal",
            {"participant_id": participant["id"], "requested_at": "2026-03-01"},
        )
        return self.service.transition(
            self.admin,
            withdrawal["id"],
            "approve",
            {"reason": "参与者撤回", "sample_ids": [sample["id"]]},
        )

    def test_withdrawal_freezes_pending_dispatch_and_recall_returns_samples(self):
        participant, sample = self._stored_sample()
        dispatch = self._issue(sample)

        withdrawal = self._withdraw(participant, sample)
        self.assertEqual(withdrawal["data"]["frozen_dispatch_ids"], [dispatch["id"]])
        frozen = self.service.get(dispatch["id"])
        self.assertEqual(frozen["status"], "frozen")
        self.assertEqual(frozen["data"]["frozen_from"], "pending_receipt")
        self.assertTrue(frozen["data"]["freeze_reason"])

        # 冻结后不能再补回执确认。
        with self.assertRaises(ConflictError):
            self.service._apply_receipt(
                self.recipient,
                "receipt-late",
                {"dispatch_id": dispatch["id"], "received_at": "2026-03-02",
                 "result_status": "completed"},
            )

        recalled = self.service.transition(
            self.admin, dispatch["id"], "recall_complete",
            {"recalled_at": "2026-03-05"},
        )
        self.assertEqual(recalled["status"], "recalled")
        self.assertEqual(self.service.get(sample["id"])["status"], "stored")

    def test_withdrawal_on_acknowledged_dispatch_requires_disposition(self):
        participant, sample = self._stored_sample()
        dispatch = self._issue(sample)
        self.service.submit_receipt(
            self.recipient,
            {"dispatch_id": dispatch["id"], "received_at": "2026-02-03",
             "result_status": "completed"},
        )
        withdrawal = self._withdraw(participant, sample)
        self.assertEqual(
            withdrawal["data"]["disposition_dispatch_ids"], [dispatch["id"]]
        )
        frozen = self.service.get(dispatch["id"])
        self.assertEqual(frozen["status"], "frozen")
        self.assertEqual(frozen["data"]["frozen_from"], "acknowledged")

        # 已出结果的实物：退回或销毁，二选一。
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.admin, dispatch["id"], "recall_complete",
                {"recalled_at": "2026-03-05"},
            )
        destroyed = self.service.transition(
            self.admin, dispatch["id"], "destroy", {"destroyed_at": "2026-03-06"}
        )
        self.assertEqual(destroyed["status"], "destroyed")
        self.assertEqual(self.service.get(sample["id"])["status"], "destroyed")

    def test_dispatch_statuses_visible_by_filter(self):
        participant, sample = self._stored_sample()
        dispatch = self._issue(sample)
        self._withdraw(participant, sample)
        frozen = self.service.list("dispatches", status="frozen")
        self.assertEqual([d["id"] for d in frozen], [dispatch["id"]])
        self.assertEqual(self.service.list("dispatches", status="pending_receipt"), [])


class OfflineReceiptTest(DispatchTestBase):
    def test_offline_receipt_suspends_and_retry_reconciles(self):
        _, sample = self._stored_sample()
        dispatch = self._issue(sample)

        self.service.set_network(self.admin, False)
        receipt = self.service.submit_receipt(
            self.recipient,
            {"dispatch_id": dispatch["id"], "received_at": "2026-02-03",
             "result_status": "completed"},
        )
        self.assertEqual(receipt["status"], "suspended")
        self.assertEqual(self.service.get(dispatch["id"])["status"], "pending_receipt")

        # 未恢复网络不能重放。
        with self.assertRaises(ValidationError):
            self.service.retry_suspended_receipts(self.admin)

        self.service.set_network(self.admin, True)
        result = self.service.retry_suspended_receipts(self.admin)
        self.assertEqual(result["retried"], [receipt["id"]])
        self.assertEqual(result["still_suspended"], [])
        self.assertEqual(self.service.get(receipt["id"])["status"], "reconciled")
        self.assertEqual(self.service.get(dispatch["id"])["status"], "acknowledged")

    def test_retry_stays_suspended_when_dispatch_was_frozen_offline(self):
        participant, sample = self._stored_sample()
        dispatch = self._issue(sample)

        self.service.set_network(self.admin, False)
        receipt = self.service.submit_receipt(
            self.recipient,
            {"dispatch_id": dispatch["id"], "received_at": "2026-02-03",
             "result_status": "completed"},
        )
        # 断网期间本地批准撤回，冻结了发放单。
        withdrawal = self.service.create(
            self.admin,
            "withdrawal",
            {"participant_id": participant["id"], "requested_at": "2026-03-01"},
        )
        self.service.transition(
            self.admin, withdrawal["id"], "approve",
            {"reason": "撤回", "sample_ids": [sample["id"]]},
        )

        self.service.set_network(self.admin, True)
        result = self.service.retry_suspended_receipts(self.admin)
        self.assertEqual(result["retried"], [])
        self.assertEqual(result["still_suspended"], [receipt["id"]])
        still = self.service.get(receipt["id"])
        self.assertEqual(still["status"], "suspended")
        self.assertIn("frozen", still["data"]["suspension_note"])
        # 发放单仍是冻结状态，只能走退回/销毁。
        self.assertEqual(self.service.get(dispatch["id"])["status"], "frozen")

    def test_submit_receipt_while_frozen_suspends(self):
        participant, sample = self._stored_sample()
        dispatch = self._issue(sample)
        withdrawal = self.service.create(
            self.admin,
            "withdrawal",
            {"participant_id": participant["id"], "requested_at": "2026-03-01"},
        )
        self.service.transition(
            self.admin, withdrawal["id"], "approve",
            {"reason": "撤回", "sample_ids": [sample["id"]]},
        )
        # 在线但本地已冻结：回执不报错而是挂起等待人工处理。
        receipt = self.service.submit_receipt(
            self.recipient,
            {"dispatch_id": dispatch["id"], "received_at": "2026-03-02",
             "result_status": "completed"},
        )
        self.assertEqual(receipt["status"], "suspended")
        self.assertEqual(self.service.get(dispatch["id"])["status"], "frozen")


class ReceiptFreezeRaceTest(DispatchTestBase):
    def test_only_one_side_wins_when_receipt_and_freeze_race(self):
        """回执对账与本地冻结并发提交：先到先得，后到整笔回滚报冲突。"""
        import threading

        participant, sample = self._stored_sample()
        dispatch = self._issue(sample)
        withdrawal = self.service.create(
            self.admin,
            "withdrawal",
            {"participant_id": participant["id"], "requested_at": "2026-03-01"},
        )
        lock_held = threading.Event()
        finish = threading.Event()
        holder_error = []

        def hold_receipt_tx():
            # 模拟回执事务：拿到发放单行写锁后，在提交前停留。
            try:
                with self.repo.transaction() as conn:
                    self.repo._lock_entity(conn, dispatch["id"])
                    lock_held.set()
                    finish.wait(10)
            except Exception as exc:  # pragma: no cover - holder must succeed
                holder_error.append(exc)

        holder = threading.Thread(target=hold_receipt_tx)
        holder.start()
        self.assertTrue(lock_held.wait(5))

        # 冻结事务此刻拿不到写锁，5 秒后整体失败，不写入任何部分结果。
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.admin, withdrawal["id"], "approve",
                {"reason": "撤回", "sample_ids": [sample["id"]]},
            )
        self.assertEqual(self.service.get(withdrawal["id"])["status"], "requested")

        finish.set()
        holder.join(10)
        self.assertEqual(holder_error, [])

        # 持锁方随后把回执对账落库成功——只有一边成功。
        receipt = self.service._apply_receipt(
            self.recipient,
            "receipt-race",
            {"dispatch_id": dispatch["id"], "received_at": "2026-03-02",
             "result_status": "completed"},
        )
        self.assertEqual(receipt["status"], "reconciled")
        self.assertEqual(self.service.get(dispatch["id"])["status"], "acknowledged")

    def test_freeze_first_then_receipt_loses_and_suspends(self):
        """冻结先提交时，回执对账被拒并挂起，两边不会同时成功。"""
        participant, sample = self._stored_sample()
        dispatch = self._issue(sample)
        withdrawal = self.service.create(
            self.admin,
            "withdrawal",
            {"participant_id": participant["id"], "requested_at": "2026-03-01"},
        )
        self.service.transition(
            self.admin, withdrawal["id"], "approve",
            {"reason": "撤回", "sample_ids": [sample["id"]]},
        )
        receipt = self.service.submit_receipt(
            self.recipient,
            {"dispatch_id": dispatch["id"], "received_at": "2026-03-02",
             "result_status": "completed"},
        )
        self.assertEqual(receipt["status"], "suspended")
        self.assertEqual(self.service.get(dispatch["id"])["status"], "frozen")
        audit_actions = [
            a["action"]
            for a in self.service.audit_log(dispatch["id"])
        ]
        self.assertIn("freeze", audit_actions)
        self.assertNotIn("acknowledge", audit_actions)


if __name__ == "__main__":
    unittest.main()
