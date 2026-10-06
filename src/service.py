from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .repository import utcnow
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)
        # 模拟外单位网络：断网时回执先挂起，等网络恢复再重试
        self.network_ok = True

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def set_network(self, ok):
        self.network_ok = bool(ok)
        return {"network_ok": self.network_ok}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
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

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
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
        # 撤回审批通过后：冻结还没回执的发放单并发起召回
        if entity["kind"] == "withdrawal" and action == "approve":
            self._freeze_pending_distributions(actor, entity)
        return updated

    def _freeze_pending_distributions(self, actor, withdrawal):
        participant_id = withdrawal["data"].get("participant_id")
        if not participant_id:
            return
        distributions = self._lookup("distribution", "participant_id", participant_id)
        for dist in distributions:
            if dist["status"] != "pending":
                continue
            try:
                self.transition(
                    actor,
                    dist["id"],
                    "freeze",
                    {"reason": "withdrawal approved"},
                    expected_version=dist["version"],
                )
            except ConflictError:
                # 已被回执或被其他操作并发处理，跳过
                continue

    def submit_receipt(self, actor, data):
        kind = "receipt"
        payload = dict(data or {})
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        # 回执先以 pending（挂起）状态落库
        receipt = self.repository.create_entity(entity_id, kind, "pending", payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, "pending", {"kind": kind})
        if self.network_ok:
            receipt = self._reconcile_receipt(actor, receipt)
        return receipt

    def retry_receipt(self, actor, receipt_id):
        receipt = self.repository.get_entity(receipt_id)
        if not receipt:
            raise NotFoundError("receipt not found: " + receipt_id)
        if receipt["status"] != "pending":
            return receipt
        if not self.network_ok:
            return receipt
        return self._reconcile_receipt(actor, receipt)

    def _reconcile_receipt(self, actor, receipt):
        distribution_id = receipt["data"].get("distribution_id")
        distributions = self._lookup("distribution", "id", distribution_id)
        distribution = distributions[0] if distributions else None
        if not distribution:
            return self._set_receipt_status(
                actor, receipt, "conflict", {"reason": "distribution not found"}
            )
        if distribution["status"] == "receipted":
            return self._set_receipt_status(
                actor, receipt, "reconciled", {"idempotent": True}
            )
        if distribution["status"] != "pending":
            return self._set_receipt_status(
                actor,
                receipt,
                "conflict",
                {"reason": "distribution status is " + distribution["status"]},
            )
        # 发放单处于待回执：用乐观锁把它翻成已回执
        # 若撤回冻结并发提交，版本号会冲突，回执失败（只能有一边成功）
        try:
            self.transition(
                actor,
                distribution["id"],
                "receipt",
                {"received_at": receipt["data"].get("received_at") or utcnow()},
                expected_version=distribution["version"],
            )
        except ConflictError:
            return self._set_receipt_status(
                actor, receipt, "conflict", {"reason": "concurrent freeze won"}
            )
        return self._set_receipt_status(actor, receipt, "reconciled", {})

    def _set_receipt_status(self, actor, receipt, status, detail):
        updated = self.repository.update_entity(
            receipt["id"], receipt["version"], status, receipt["data"]
        )
        self.audit.record(
            receipt["id"], actor, "reconcile", receipt["status"], status, detail
        )
        return updated

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
