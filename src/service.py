from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine


class DomainService:
    # 两站并发修改时，后到请求最多按新版本重算的次数
    CORRECTION_MAX_RETRY = 20

    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

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
        if kind == "event":
            # 修订链起点：新建事件同样可沿链继续修订
            self.repository.seed_event_revision(entity, actor.user_id)
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
        if self.rules.normalize_kind(entity["kind"]) == "event":
            revision = {
                "revision_status": "applied",
                "station": None,
                "material": False,
                "changed_fields": sorted(patch.keys()),
                "review_note": patch.get("review_note"),
                "reviewer": patch.get("reviewer"),
            }
            updated = self.repository.apply_event_revision(
                entity_id, expected, next_status, merged, revision,
                actor.user_id, actor.role, action, entity["status"], {"patch": patch},
                resolve_pending=(action == "review" and entity["status"] == "revision_pending"),
            )
        else:
            updated = self.repository.update_entity(entity_id, expected, next_status, merged)
            self.audit.record(
                entity_id, actor, action, entity["status"], updated["status"],
                {"patch": patch},
            )
        return updated

    def submit_correction(self, actor, event_id, correction, expected_version=None):
        """台站修正报文落地：归并台站数据，处理实质性变更与并发重算。"""
        correction = dict(correction or {})
        station = correction.get("station")
        message_id = correction.get("message_id")

        attempts = 0
        while True:
            entity = self.repository.get_entity(event_id)
            if not entity:
                raise NotFoundError("entity not found: " + event_id)
            expected = (
                int(expected_version)
                if expected_version is not None
                else entity["version"]
            )
            if station and message_id:
                duplicate = self.repository.find_revision(event_id, station, message_id)
                if duplicate:
                    # 同一报文编号重传：直接返回已落地结果，不占用新版本
                    return self.repository.get_entity(event_id)
            to_status, merged, revision = self.rules.plan_correction(
                actor, entity, correction
            )
            detail = {
                "station": station,
                "message_id": message_id,
                "material": revision["material"],
                "changed_fields": revision["changed_fields"],
            }
            try:
                return self.repository.apply_event_revision(
                    event_id, expected, to_status, merged, revision,
                    actor.user_id, actor.role, "submit_correction",
                    entity["status"], detail,
                )
            except ConflictError:
                # 先落地的修订已占用版本：后到请求按新版本重算，避免审校意见被覆盖
                attempts += 1
                if attempts > self.CORRECTION_MAX_RETRY or expected_version is not None:
                    raise

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def revisions(self, event_id=None, status=None):
        return self.repository.list_revisions(event_id=event_id, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
