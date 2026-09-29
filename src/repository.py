import json
import sqlite3
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS event_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    revision_status TEXT NOT NULL,
                    station TEXT,
                    message_id TEXT,
                    material INTEGER NOT NULL,
                    changed_fields TEXT NOT NULL,
                    data TEXT NOT NULL,
                    review_note TEXT,
                    reviewer TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(event_id, version)
                );
                CREATE INDEX IF NOT EXISTS idx_revisions_event
                    ON event_revisions(event_id, id);
                CREATE INDEX IF NOT EXISTS idx_revisions_status
                    ON event_revisions(revision_status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
                -- 审计记录只能追加，不能改写或删除
                CREATE TRIGGER IF NOT EXISTS audit_no_update
                BEFORE UPDATE ON audit_log
                BEGIN
                    SELECT RAISE(ABORT, 'audit records are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS audit_no_delete
                BEFORE DELETE ON audit_log
                BEGIN
                    SELECT RAISE(ABORT, 'audit records are immutable');
                END;
            """)
            self._migrate(connection)

    def _migrate(self, connection):
        """历史事件没有稳定报文编号：升级后为每个事件补一条初始修订链节点。"""
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version >= 2:
            return
        connection.execute(
            "INSERT INTO event_revisions"
            "(event_id, version, revision_status, station, message_id, material, "
            "changed_fields, data, review_note, reviewer, created_by, created_at) "
            "SELECT e.id, 1, 'applied', NULL, NULL, 0, '[]', e.data, "
            "json_extract(e.data, '$.review_note'), json_extract(e.data, '$.reviewer'), "
            "e.created_by, e.created_at "
            "FROM entities e WHERE e.kind = 'event' "
            "AND NOT EXISTS (SELECT 1 FROM event_revisions r WHERE r.event_id = e.id)"
        )
        connection.execute("PRAGMA user_version = 2")

    @staticmethod
    def _revision_from_row(row):
        return {
            "id": row["id"],
            "event_id": row["event_id"],
            "version": int(row["version"]),
            "revision_status": row["revision_status"],
            "station": row["station"],
            "message_id": row["message_id"],
            "material": bool(row["material"]),
            "changed_fields": json.loads(row["changed_fields"]),
            "data": json.loads(row["data"]),
            "review_note": row["review_note"],
            "reviewer": row["reviewer"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    def seed_event_revision(self, entity, actor_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO event_revisions"
                "(event_id, version, revision_status, station, message_id, material, "
                "changed_fields, data, review_note, reviewer, created_by, created_at) "
                "VALUES (?, 1, 'applied', NULL, NULL, 0, '[]', ?, ?, ?, ?, ?)",
                (
                    entity["id"],
                    json.dumps(entity["data"], ensure_ascii=False, sort_keys=True),
                    entity["data"].get("review_note"),
                    entity["data"].get("reviewer"),
                    actor_id,
                    entity["created_at"],
                ),
            )

    def list_revisions(self, event_id=None, status=None):
        clauses = []
        params = []
        if event_id:
            clauses.append("event_id = ?")
            params.append(event_id)
        if status:
            clauses.append("revision_status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM event_revisions" + where + " ORDER BY event_id, id",
                params,
            ).fetchall()
        return [self._revision_from_row(row) for row in rows]

    def find_revision(self, event_id, station, message_id):
        if not message_id:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM event_revisions "
                "WHERE event_id = ? AND station = ? AND message_id = ? LIMIT 1",
                (event_id, station, message_id),
            ).fetchone()
        return self._revision_from_row(row) if row else None

    def apply_event_revision(self, entity_id, expected_version, status, data, revision,
                             actor_id, actor_role, action, from_status, audit_detail,
                             resolve_pending=False):
        """在一个事务里占用新版本、写修订链节点和审计记录。

        两站同时修改时，先提交的事务在此占用版本号；后到事务读到的版本号
        与 expected_version 不符时抛 ConflictError，由上层按新版本重算。
        """
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            next_version = current_version + 1
            connection.execute(
                "UPDATE entities SET status = ?, version = ?, data = ?, updated_at = ? "
                "WHERE id = ?",
                (status, next_version, payload, now, entity_id),
            )
            connection.execute(
                "INSERT INTO event_revisions"
                "(event_id, version, revision_status, station, message_id, material, "
                "changed_fields, data, review_note, reviewer, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    next_version,
                    revision.get("revision_status", "applied"),
                    revision.get("station"),
                    revision.get("message_id"),
                    1 if revision.get("material") else 0,
                    json.dumps(revision.get("changed_fields", []), ensure_ascii=False),
                    payload,
                    revision.get("review_note"),
                    revision.get("reviewer"),
                    actor_id,
                    now,
                ),
            )
            if resolve_pending:
                # 复核通过：此前被撤回的待复核修订全部关闭，不再出现在待复核列表
                connection.execute(
                    "UPDATE event_revisions SET revision_status = 'reviewed' "
                    "WHERE event_id = ? AND revision_status = 'pending_review'",
                    (entity_id,),
                )
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, "
                "from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    status,
                    json.dumps(audit_detail, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
