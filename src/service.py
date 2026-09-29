from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine

REVISION_NOTES = {
    "create": "初始候选事件",
    "associate": "台站报告关联",
    "review": "初审完成",
    "publish": "发布",
    "revise": "人工修订",
    "withdraw": "撤回发布稿",
}
CORRECTION_NOTES = {
    "waveform_supplement": "台站补波形，保留审校结论",
    "significant": "关键参数变化，撤回发布稿，待复核",
}


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def _snapshot_revision(self, entity, note):
        if entity["kind"] == "event":
            self.repository.append_event_revision(entity, note=note)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

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
        self._snapshot_revision(entity, REVISION_NOTES["create"])
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
        self._snapshot_revision(updated, REVISION_NOTES.get(action, action))
        return updated

    def apply_correction(self, actor, entity_id, data, expected_version=None):
        """落地一份台站修正报文。

        先落地的修订占用版本；后到请求若基于旧版本，则按新版本重新计算
        （重取事件、重新归并），避免把先到的审校意见覆盖掉。
        """
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        requested = (
            int(expected_version) if expected_version is not None else entity["version"]
        )
        initial_version = entity["version"]
        if expected_version is not None and requested != initial_version:
            # 客户端按旧版本提交：不静默改写，返回冲突让其重取后重算
            raise ConflictError(
                "version conflict: expected %s, found %s"
                % (requested, initial_version)
            )
        attempts = 0
        while True:
            plan = self.rules.plan_correction(
                actor, entity, dict(data or {}), self._lookup
            )
            base_version = entity["version"]
            try:
                updated = self.repository.update_entity(
                    entity_id,
                    base_version,
                    plan["status"],
                    plan["data"],
                )
            except ConflictError:
                attempts += 1
                stale_submission = (
                    expected_version is not None and requested != initial_version
                )
                if attempts >= 5 or stale_submission:
                    # 客户端明确按旧版本提交：不静默改写，返回冲突让其重取
                    raise
                refreshed = self.repository.get_entity(entity_id)
                if not refreshed:
                    raise NotFoundError("entity not found: " + entity_id)
                entity = refreshed
                continue
            self.audit.record(
                entity_id,
                actor,
                "correct",
                entity["status"],
                updated["status"],
                {
                    "station": plan["station"],
                    "change": plan["change"],
                    "significant_patch": plan["significant_patch"],
                    "recomputed_from_version": base_version,
                },
            )
            self._snapshot_revision(updated, CORRECTION_NOTES[plan["change"]])
            return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def revisions(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return self.repository.list_event_revisions(entity_id)

    def list(self, kind=None, status=None, station=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        items = self.repository.list_entities(kind=kind, status=status)
        if station:
            items = [
                entity
                for entity in items
                if any(
                    report.get("station") == station
                    for report in entity["data"].get("reports") or []
                )
            ]
        return items

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
