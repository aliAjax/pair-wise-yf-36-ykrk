import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    # Short wait is enough inside one process; a timeout means a real
    # cross-object write race that must be reported as a conflict.
    LOCK_TIMEOUT = 5

    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=self.LOCK_TIMEOUT)
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
            """)

    @contextmanager
    def transaction(self):
        """Serialize writers so cross-entity orchestration commits atomically."""
        connection = self._connect()
        try:
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                raise ConflictError(
                    "database is locked by a concurrent write"
                ) from exc
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

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

    @staticmethod
    def _dump(data):
        return json.dumps(data, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _lock_entity(connection, entity_id):
        return connection.execute(
            "SELECT * FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()

    @staticmethod
    def _insert_entity(connection, entity_id, kind, status, data, actor_id, now=None):
        now = now or utcnow()
        connection.execute(
            "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
            (entity_id, kind, status, SQLiteRepository._dump(data), actor_id, now, now),
        )

    @staticmethod
    def save_entity_conn(connection, entity_id, kind, status, data, actor_id):
        """Insert an entity or promote an existing one within an open transaction."""
        row = connection.execute(
            "SELECT version FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if row:
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ?",
                (status, SQLiteRepository._dump(data), utcnow(), entity_id),
            )
        else:
            SQLiteRepository._insert_entity(
                connection, entity_id, kind, status, data, actor_id
            )

    @staticmethod
    def apply_update(connection, entity_id, expected_version, status, data):
        """Conditional update on a locked row. Raises on missing/stale version."""
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
        cursor = connection.execute(
            "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
            "WHERE id = ? AND version = ?",
            (status, SQLiteRepository._dump(data), utcnow(), entity_id, current_version),
        )
        if cursor.rowcount != 1:
            raise ConflictError("entity changed concurrently: " + entity_id)
        return current_version + 1

    @staticmethod
    def append_audit_conn(connection, entity_id, actor_id, actor_role, action,
                          from_status, to_status, detail):
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
                json.dumps(detail or {}, ensure_ascii=False, sort_keys=True),
                utcnow(),
            ),
        )

    def create_entity(self, entity_id, kind, status, data, actor_id):
        with self.transaction() as connection:
            self._insert_entity(connection, entity_id, kind, status, data, actor_id)
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def _list_conn(self, connection, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = connection.execute(
            "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
        ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def list_entities(self, kind=None, status=None):
        with self._connect() as connection:
            return self._list_conn(connection, kind=kind, status=status)

    def find_entities(self, kind, field, value):
        with self._connect() as connection:
            rows = self._list_conn(connection, kind=kind)
        return [
            entity
            for entity in rows
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        with self.transaction() as connection:
            self.apply_update(connection, entity_id, expected_version, status, data)
        return self.get_entity(entity_id)

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self.transaction() as connection:
            self.append_audit_conn(
                connection, entity_id, actor_id, actor_role, action,
                from_status, to_status, detail,
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
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
