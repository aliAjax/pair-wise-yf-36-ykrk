from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, InvalidTransition, NotFoundError
from .repository import utcnow
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)
        # Simulated external-network switch for receipt submission.
        self.online = True

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    # ------------------------------------------------------------------ create

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        if kind == "dispatch":
            return self.issue_dispatch(actor, data, idempotency_key)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def issue_dispatch(self, actor, data, idempotency_key=None):
        """出库：登记接收机构、检测用途和样本，样本转为外借，留待回执。"""
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        checked = self.rules.validate_create(actor, "dispatch", payload, self._lookup)
        dispatch_id = str(checked.pop("id", "") or uuid4())
        sample_ids = checked["sample_ids"]
        with self.repository.transaction() as conn:
            if conn.execute(
                "SELECT 1 FROM entities WHERE id = ?", (dispatch_id,)
            ).fetchone():
                raise ConflictError("entity already exists: " + dispatch_id)
            for sample_id in sample_ids:
                row = self.repository._lock_entity(conn, sample_id)
                sample = self.repository._entity_from_row(row)
                if sample["status"] != "stored":
                    raise InvalidTransition(
                        "sample %s is not in storage (status=%s)"
                        % (sample_id, sample["status"])
                    )
                sample_data = dict(sample["data"])
                sample_data["loaned_to"] = checked["recipient_org"]
                sample_data["dispatch_id"] = dispatch_id
                self.repository.apply_update(
                    conn, sample_id, sample["version"], "on_loan", sample_data
                )
                self.repository.append_audit_conn(
                    conn, sample_id, actor.user_id, actor.role,
                    "loan", "stored", "on_loan", {"dispatch_id": dispatch_id},
                )
            self.repository._insert_entity(
                conn, dispatch_id, "dispatch", "pending_receipt", checked, actor.user_id
            )
            self.repository.append_audit_conn(
                conn, dispatch_id, actor.user_id, actor.role,
                "issue", None, "pending_receipt",
                {"recipient_org": checked["recipient_org"], "sample_ids": sample_ids},
            )
        dispatch = self.repository.get_entity(dispatch_id)
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, dispatch_id)
        return dispatch

    # -------------------------------------------------------------- transition

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        kind = self.rules.normalize_kind(entity["kind"])
        if kind == "withdrawal" and action == "approve":
            return self._approve_withdrawal(actor, entity, dict(data or {}), expected_version)
        if kind == "dispatch" and action in ("recall_complete", "return", "destroy"):
            return self._dispatch_cascade(
                actor, entity, action, dict(data or {}), expected_version
            )
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def _approve_withdrawal(self, actor, entity, data, expected_version):
        """批准撤回：冻结未回执的发放单并召回；已出结果的安排退回/销毁。"""
        checked = dict(data)
        checked["approved_by"] = actor.user_id
        # 先跑规则（字段、样本、状态机），快照批准前的校验结果。
        _, _ = self.rules.validate_transition(
            actor, entity, "approve", checked, self._lookup
        )
        expected = int(expected_version) if expected_version is not None else entity["version"]
        sample_ids = list(checked["sample_ids"])
        now = utcnow()
        with self.repository.transaction() as conn:
            withdrawal_row = self.repository._lock_entity(conn, entity["id"])
            withdrawal = self.repository._entity_from_row(withdrawal_row)
            if withdrawal["status"] != "requested":
                raise InvalidTransition(
                    "cannot approve from status %s" % withdrawal["status"]
                )
            frozen, awaiting_disposition = [], []
            for sample_id in sample_ids:
                row = self.repository._lock_entity(conn, sample_id)
                if not row:
                    raise NotFoundError("sample not found: " + sample_id)
                sample = self.repository._entity_from_row(row)
                dispatch_id = sample["data"].get("dispatch_id")
                if not dispatch_id or sample["status"] != "on_loan":
                    continue
                d_row = self.repository._lock_entity(conn, dispatch_id)
                dispatch = self.repository._entity_from_row(d_row)
                if dispatch["status"] in ("recalled", "returned", "destroyed", "frozen"):
                    continue
                d_data = dict(dispatch["data"])
                d_data.update({
                    "frozen_at": now,
                    "frozen_by": actor.user_id,
                    "frozen_from": dispatch["status"],
                    "freeze_reason": "participant consent withdrawn: " + entity["id"],
                })
                if dispatch["status"] == "pending_receipt":
                    self.repository.apply_update(
                        conn, dispatch_id, dispatch["version"], "frozen", d_data
                    )
                    self.repository.append_audit_conn(
                        conn, dispatch_id, actor.user_id, actor.role,
                        "freeze", dispatch["status"], "frozen",
                        {"withdrawal_id": entity["id"], "recall": True},
                    )
                    frozen.append(dispatch_id)
                elif dispatch["status"] == "acknowledged":
                    self.repository.apply_update(
                        conn, dispatch_id, dispatch["version"], "frozen", d_data
                    )
                    self.repository.append_audit_conn(
                        conn, dispatch_id, actor.user_id, actor.role,
                        "freeze", dispatch["status"], "frozen",
                        {"withdrawal_id": entity["id"], "disposition": "return_or_destroy"},
                    )
                    awaiting_disposition.append(dispatch_id)
            w_data = dict(withdrawal["data"])
            w_data.update(checked)
            w_data["frozen_dispatch_ids"] = frozen
            w_data["disposition_dispatch_ids"] = awaiting_disposition
            self.repository.apply_update(
                conn, entity["id"], expected, "approved", w_data
            )
            self.repository.append_audit_conn(
                conn, entity["id"], actor.user_id, actor.role,
                "approve", withdrawal["status"], "approved",
                {
                    "frozen_dispatch_ids": frozen,
                    "disposition_dispatch_ids": awaiting_disposition,
                },
            )
        return self.repository.get_entity(entity["id"])

    def _dispatch_cascade(self, actor, entity, action, data, expected_version):
        """召回完成 / 实物退回 / 销毁：联动样本回到在库或销毁。"""
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, data, self._lookup
        )
        expected = int(expected_version) if expected_version is not None else entity["version"]
        with self.repository.transaction() as conn:
            row = self.repository._lock_entity(conn, entity["id"])
            dispatch = self.repository._entity_from_row(row)
            merged = dict(dispatch["data"])
            merged.update(patch)
            self.repository.apply_update(conn, entity["id"], expected, next_status, merged)
            self.repository.append_audit_conn(
                conn, entity["id"], actor.user_id, actor.role,
                action, dispatch["status"], next_status, {"patch": patch},
            )
            sample_target = {
                "recall_complete": "stored",
                "return": "stored",
                "destroy": "destroyed",
            }[action]
            for sample_id in dispatch["data"].get("sample_ids", []):
                s_row = self.repository._lock_entity(conn, sample_id)
                if not s_row:
                    continue
                sample = self.repository._entity_from_row(s_row)
                s_data = dict(sample["data"])
                s_data.pop("loaned_to", None)
                s_data.pop("dispatch_id", None)
                self.repository.apply_update(
                    conn, sample_id, sample["version"], sample_target, s_data
                )
                self.repository.append_audit_conn(
                    conn, sample_id, actor.user_id, actor.role,
                    action, sample["status"], sample_target,
                    {"dispatch_id": entity["id"]},
                )
        return self.repository.get_entity(entity["id"])

    # ------------------------------------------------------------------ reads

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    # ----------------------------------------------------- receipts & network

    def network_status(self):
        return {"online": self.online}

    def set_network(self, actor, online):
        self.online = bool(online)
        self.audit.record(
            "network", actor, "set_network", None,
            "online" if self.online else "offline",
            {"online": self.online},
        )
        return self.network_status()

    def submit_receipt(self, actor, data, idempotency_key=None):
        """接收机构回执与本地发放单对账；断网先挂起，恢复后重试。"""
        payload = dict(data or {})
        for field in ("dispatch_id", "received_at", "result_status"):
            if not payload.get(field):
                from .domain import ValidationError
                raise ValidationError("missing required field: " + field)
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        receipt_id = str(payload.pop("id", "") or uuid4())
        if not self.online:
            entity = self._persist_receipt(
                actor, receipt_id, payload, "suspended",
                {"note": "network offline; queued for retry"},
            )
            if idempotency_key:
                self.repository.save_idempotency(actor.user_id, idempotency_key, receipt_id)
            return entity
        try:
            entity = self._apply_receipt(actor, receipt_id, payload)
            if idempotency_key:
                self.repository.save_idempotency(actor.user_id, idempotency_key, receipt_id)
            return entity
        except ConflictError:
            # 对账与本地状态变更同时落地，只有一边能成功：本地优先，回执挂起。
            dispatch = self.repository.get_entity(payload["dispatch_id"])
            state = dispatch["status"] if dispatch else "missing"
            entity = self._persist_receipt(
                actor, receipt_id, payload, "suspended",
                {"note": "dispatch already %s when receipt arrived; suspended for review" % state},
            )
            if idempotency_key:
                self.repository.save_idempotency(actor.user_id, idempotency_key, receipt_id)
            return entity

    def _persist_receipt(self, actor, receipt_id, payload, status, detail):
        entity = self.repository.create_entity(
            receipt_id, "receipt", status, dict(payload), actor.user_id
        )
        self.audit.record(
            receipt_id, actor, "receipt_" + status, None, status,
            {"dispatch_id": payload.get("dispatch_id"), **detail},
        )
        return entity

    def _apply_receipt(self, actor, receipt_id, payload):
        dispatch_id = payload["dispatch_id"]
        with self.repository.transaction() as conn:
            row = self.repository._lock_entity(conn, dispatch_id)
            if not row:
                raise NotFoundError("dispatch not found: " + dispatch_id)
            dispatch = self.repository._entity_from_row(row)
            if dispatch["status"] != "pending_receipt":
                # 已冻结/已确认等：唯一允许的一边成功冲突。
                raise ConflictError(
                    "dispatch %s is %s; receipt cannot be reconciled"
                    % (dispatch_id, dispatch["status"])
                )
            received_ids = payload.get("received_sample_ids")
            if received_ids is not None and \
                    set(received_ids) != set(dispatch["data"]["sample_ids"]):
                from .domain import ValidationError
                raise ValidationError(
                    "receipt sample list does not match dispatch %s" % dispatch_id
                )
            d_data = dict(dispatch["data"])
            d_data["receipt"] = {
                "received_at": payload["received_at"],
                "result_status": payload["result_status"],
                "result_summary": payload.get("result_summary", ""),
            }
            self.repository.apply_update(
                conn, dispatch_id, dispatch["version"], "acknowledged", d_data
            )
            self.repository.append_audit_conn(
                conn, dispatch_id, actor.user_id, actor.role,
                "acknowledge", "pending_receipt", "acknowledged",
                {"receipt_id": receipt_id, "result_status": payload["result_status"]},
            )
            existed = conn.execute(
                "SELECT 1 FROM entities WHERE id = ?", (receipt_id,)
            ).fetchone() is not None
            self.repository.save_entity_conn(
                conn, receipt_id, "receipt", "reconciled", dict(payload), actor.user_id
            )
            self.repository.append_audit_conn(
                conn, receipt_id, actor.user_id, actor.role,
                "receipt_reconciled", "suspended" if existed else None, "reconciled",
                {"dispatch_id": dispatch_id, "replayed": bool(existed)},
            )
        return self.repository.get_entity(receipt_id)

    def retry_suspended_receipts(self, actor):
        """网络恢复后重放挂起回执；发放单已冻结的回执维持挂起并记录原因。"""
        if not self.online:
            from .domain import ValidationError
            raise ValidationError("network is still offline")
        receipts = self.repository.list_entities(kind="receipt", status="suspended")
        retried, still_suspended = [], []
        for receipt in receipts:
            payload = dict(receipt["data"])
            try:
                self._apply_receipt(actor, receipt["id"], payload)
                retried.append(receipt["id"])
            except ConflictError:
                note = "dispatch frozen before retry; awaiting return or destroy"
                stuck = dict(payload)
                stuck["suspension_note"] = note
                self.repository.update_entity(
                    receipt["id"], receipt["version"], "suspended", stuck
                )
                self.audit.record(
                    receipt["id"], actor, "receipt_retry_blocked", "suspended",
                    "suspended", {"dispatch_id": payload.get("dispatch_id"), "note": note},
                )
                still_suspended.append(receipt["id"])
        return {
            "retried": retried,
            "still_suspended": still_suspended,
        }
