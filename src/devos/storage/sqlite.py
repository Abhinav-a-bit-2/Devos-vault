"""SQLite unit of work, snapshots, durable jobs, and rebuildable projections."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import closing, contextmanager
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterator

from devos.core import ENTITY_TYPES, RELATIONS, DevosError, canonical, uid, utcnow

APPLICATION_ID = 0x44564F53
SCHEMA_VERSION = 2


class SQLiteStore:
    def __init__(self, path: str | Path, *, create: bool = False, clock: Any = time.time):
        self.path = Path(path).expanduser().resolve()
        self.clock = clock
        if not self.path.is_file() and not create:
            raise DevosError("vault_missing", "Vault does not exist; run devos init first.")
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA foreign_keys=ON")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            app = self.db.execute("PRAGMA application_id").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise DevosError("newer_schema", "Vault schema is newer than this Devos version.")
            tables = self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if version == 0 and not tables and create:
                self._initialize()
            elif app != APPLICATION_ID or version < 1:
                raise DevosError("invalid_vault", "This file is not a supported Devos vault.")
            elif version < SCHEMA_VERSION:
                self._upgrade(version)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("SELECT rowid FROM entity_search LIMIT 0")
        except Exception:
            self.db.close()
            raise

    def _initialize(self) -> None:
        migration = files("devos.storage").joinpath("migrations/001_initial.sql").read_text("utf-8")
        try:
            second = files("devos.storage").joinpath("migrations/002_provider_leases.sql").read_text("utf-8")
            self.db.executescript("BEGIN IMMEDIATE;\n" + migration + "\n" + second)
            self.db.executemany("INSERT INTO entity_types VALUES (?,1)", [(t,) for t in ENTITY_TYPES])
            self.db.executemany(
                "INSERT INTO relation_types VALUES (?,?)",
                [(key, canonical([sorted(left), sorted(right)])) for key, (left, right) in RELATIONS.items()],
            )
            self.db.execute(f"PRAGMA application_id={APPLICATION_ID}")
            self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self.db.execute("COMMIT")
        except sqlite3.Error as exc:
            if self.db.in_transaction:
                self.db.rollback()
            raise DevosError("storage_unavailable", "Cannot initialize SQLite with JSON and FTS5 support.") from exc

    def _upgrade(self, version: int) -> None:
        backup = self.path.with_name(self.path.name + f".v{version}-{uid()[:8]}.bak")
        self.backup(backup)
        try:
            self.db.execute("BEGIN IMMEDIATE")
            current = self.db.execute("PRAGMA user_version").fetchone()[0]
            if current == 1:
                # One statement in this migration; execute without executescript's implicit commit.
                migration = files("devos.storage").joinpath("migrations/002_provider_leases.sql").read_text("utf-8")
                self.db.execute(migration)
                self.db.executemany("INSERT OR IGNORE INTO relation_types VALUES (?,?)",
                    [(key, canonical([sorted(left), sorted(right)])) for key, (left, right) in RELATIONS.items()])
                self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif current != SCHEMA_VERSION:
                raise DevosError("newer_schema", "Vault schema changed during migration; reopen with a compatible version.")
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self) -> Iterator[SQLiteStore]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def execute(self, sql: str, args: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        return self.db.execute(sql, args)

    def one(self, sql: str, args: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        row = self.execute(sql, args).fetchone()
        return dict(row) if row is not None else None

    def all(self, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in self.execute(sql, args).fetchall()]

    def put_snapshot(
        self, adapter: str, resource: str, url: str, body: bytes, status: int,
        headers: dict[str, str], cache_scope: str, *, observed_at: str | None = None,
    ) -> str:
        now = utcnow()
        artifact_hash = hashlib.sha256(body).hexdigest()
        with self.transaction():
            self.execute("INSERT OR IGNORE INTO sources VALUES (?,?,?,?,?)",
                         (uid(), adapter, resource, url, now))
            source = self.one("SELECT id FROM sources WHERE adapter_key=? AND resource_key=?", (adapter, resource))
            assert source
            self.execute("INSERT OR IGNORE INTO artifacts VALUES (?,?,?)", (artifact_hash, body, len(body)))
            snapshot_id = uid()
            self.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                snapshot_id, source["id"], artifact_hash, observed_at or now, now, status,
                headers.get("content-type", "application/json"), headers.get("etag"),
                headers.get("last-modified"), cache_scope, "available",
            ))
        return snapshot_id

    def latest_snapshot(self, adapter: str, resource: str, cache_scope: str) -> dict[str, Any] | None:
        return self.one(
            "SELECT s.*, a.body FROM snapshots s JOIN sources r ON r.id=s.source_id "
            "JOIN artifacts a ON a.sha256=s.artifact_hash "
            "WHERE r.adapter_key=? AND r.resource_key=? AND s.cache_scope=? "
            "AND s.availability='available' ORDER BY s.observed_at DESC,s.retrieved_at DESC,s.id LIMIT 1",
            (adapter, resource, cache_scope),
        )

    def snapshot(self, snapshot_id: str) -> dict[str, Any]:
        row = self.one(
            "SELECT s.*,a.body,r.adapter_key,r.resource_key,r.source_url FROM snapshots s "
            "JOIN sources r ON r.id=s.source_id LEFT JOIN artifacts a ON a.sha256=s.artifact_hash WHERE s.id=?",
            (snapshot_id,),
        )
        if not row or row["body"] is None or row["availability"] != "available":
            raise DevosError("replay_unavailable", "The requested raw snapshot is unavailable.")
        return row

    def create_job(self, inputs: list[str], *, mode: str, config: dict[str, Any]) -> str:
        job_id, now = uid(), utcnow()
        with self.transaction():
            self.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?)",
                         (job_id, mode, canonical(config), "pending", now, now))
            for value in dict.fromkeys(inputs):
                self.execute("INSERT INTO job_targets(id,job_id,input,state) VALUES (?,?,?,'pending')",
                             (uid(), job_id, value))
            self.event(job_id, None, "pending")
        return job_id

    def event(self, job_id: str, target_id: str | None, status: str, **payload: Any) -> None:
        self.execute(
            "INSERT INTO job_events SELECT ?,COALESCE(MAX(sequence),0)+1,?,?,?,? "
            "FROM job_events WHERE job_id=?",
            (job_id, target_id, utcnow(), status, canonical(payload), job_id),
        )

    def claim_target(self, target_id: str, *, producer: str, version: str, config: str,
                     ttl: float = 300, max_attempts: int = 3) -> dict[str, Any] | None:
        with self.transaction():
            target = self.one("SELECT * FROM job_targets WHERE id=?", (target_id,))
            if not target:
                raise DevosError("not_found", "Job target not found.")
            if target["state"] in {"committed", "failed", "canceled"}:
                return None
            now = self.clock()
            if target["state"] == "running" and target["lease_expires"] > now:
                return None
            if target["next_attempt_at"] and target["next_attempt_at"] > now:
                return None
            if target["state"] == "running":
                self.execute("UPDATE attempts SET state='interrupted',ended_at=? "
                             "WHERE job_target_id=? AND state='running'", (utcnow(), target_id))
            if target["attempt_count"] >= max_attempts:
                self.execute("UPDATE job_targets SET state='failed',lease_token=NULL,lease_expires=NULL "
                             "WHERE id=?", (target_id,))
                self.event(target["job_id"], target_id, "failed", code="attempts_exhausted")
                return None
            token, attempt_id = uid(), uid()
            number = target["attempt_count"] + 1
            self.execute(
                "UPDATE job_targets SET state='running',attempt_count=?,lease_token=?,lease_expires=?,"
                "next_attempt_at=NULL,error_json=NULL WHERE id=?", (number, token, now + ttl, target_id),
            )
            self.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                attempt_id, target_id, number, token, producer, version, config,
                utcnow(), None, "running", None,
            ))
            self.execute("UPDATE jobs SET state='running',updated_at=? WHERE id=?", (utcnow(), target["job_id"]))
            self.event(target["job_id"], target_id, "running", attempt=number)
            return {**target, "lease_token": token, "attempt_id": attempt_id, "attempt_count": number}

    def check_lease(self, target_id: str, token: str) -> dict[str, Any]:
        row = self.one("SELECT * FROM job_targets WHERE id=? AND lease_token=? AND state='running'",
                       (target_id, token))
        if not row or row["lease_expires"] <= self.clock():
            raise DevosError("lease_lost", "The job lease expired or another worker owns it; resume the job.")
        return row

    def finish_target(self, target_id: str, token: str, *, state: str, result: Any = None,
                      error: Any = None, retry_at: float | None = None, coverage: Any = None) -> None:
        target = self.check_lease(target_id, token)
        self.execute(
            "UPDATE job_targets SET state=?,result_json=?,error_json=?,next_attempt_at=?,"
            "lease_token=NULL,lease_expires=NULL WHERE id=?",
            (state, canonical(result) if result is not None else None,
             canonical(error) if error is not None else None, retry_at, target_id),
        )
        self.execute("UPDATE attempts SET state=?,ended_at=?,coverage_json=? "
                     "WHERE job_target_id=? AND lease_token=? AND state='running'",
                     (state, utcnow(), canonical(coverage) if coverage else None, target_id, token))
        self.event(target["job_id"], target_id, state, result=result, error=error, retry_at=retry_at)

    def summarize(self, job_id: str) -> dict[str, Any]:
        with self.transaction():
            job = self.one("SELECT * FROM jobs WHERE id=?", (job_id,))
            if not job:
                raise DevosError("not_found", "Job not found.")
            targets = self.all("SELECT * FROM job_targets WHERE job_id=? ORDER BY rowid", (job_id,))
            states = {t["state"] for t in targets}
            if "running" in states or "pending" in states:
                state = "running" if "running" in states else "pending"
            elif "retry_wait" in states:
                state = "retry_wait"
            elif states == {"committed"}:
                state = "complete"
            elif "committed" in states:
                state = "partial"
            elif states == {"canceled"}:
                state = "canceled"
            else:
                state = "failed"
            if job["state"] != state:
                self.event(job_id, None, state)
            self.execute("UPDATE jobs SET state=?,updated_at=? WHERE id=?", (state, utcnow(), job_id))
        for target in targets:
            for key in ("result_json", "error_json"):
                target[key.removesuffix("_json")] = json.loads(target.pop(key) or "null")
            target.pop("lease_token", None)
        return {"id": job_id, "state": state, "mode": job["mode"], "targets": targets}

    def rebuild_view(self, entity_id: str) -> None:
        claims = self.all(
            "SELECT c.* FROM claims c LEFT JOIN snapshots s ON s.id=c.snapshot_id "
            "WHERE c.entity_id=? AND c.state='active' "
            "ORDER BY (c.origin='manual') DESC,(c.origin='extracted') DESC,COALESCE(s.observed_at,c.last_seen_at) DESC,c.last_seen_at DESC,c.id",
            (entity_id,),
        )
        values: dict[str, Any] = {}
        chosen: dict[str, str] = {}
        for claim in claims:
            if claim["predicate"] not in values:
                values[claim["predicate"]] = json.loads(claim["value_json"])
                chosen[claim["predicate"]] = claim["id"]
        name = str(values.get("name", entity_id))
        description = str(values.get("description") or "")
        self.execute("INSERT OR REPLACE INTO entity_views VALUES (?,?,?,?,?,?,?,1)", (
            entity_id, name, description, values.get("canonical_url"), canonical(values.get("metadata", {})),
            canonical(values), canonical(chosen),
        ))
        self.execute("DELETE FROM entity_search WHERE entity_id=?", (entity_id,))
        self.execute("INSERT INTO entity_search VALUES (?,?,?,?,?)", (
            entity_id, name, description, str(values.get("readme") or ""), str(values.get("body") or ""),
        ))
        self.execute("UPDATE entities SET updated_at=? WHERE id=?", (utcnow(), entity_id))

    def entity(self, entity_id: str, provenance: bool = False) -> dict[str, Any]:
        row = self.one("SELECT e.*,v.values_json FROM entities e JOIN entity_views v ON v.entity_id=e.id WHERE e.id=?",
                       (entity_id,))
        if not row:
            raise DevosError("not_found", "Entity not found.")
        row["values"] = json.loads(row.pop("values_json"))
        row["identifiers"] = self.all("SELECT namespace,normalized_value,kind FROM identifiers WHERE entity_id=? "
                                      "ORDER BY namespace,normalized_value", (entity_id,))
        row["relations"] = self.all(
            "SELECT DISTINCT e.* FROM edges e JOIN claims c ON c.edge_id=e.id AND c.state='active' "
            "WHERE e.subject_id=? OR e.object_id=? ORDER BY e.relation_key,e.id LIMIT 101", (entity_id, entity_id),
        )
        row["relations_truncated"] = len(row["relations"]) > 100
        row["relations"] = row["relations"][:100]
        if provenance:
            row["claims"] = self.all(
                "SELECT c.*,s.observed_at,s.artifact_hash,r.source_url FROM claims c "
                "LEFT JOIN snapshots s ON s.id=c.snapshot_id LEFT JOIN sources r ON r.id=c.source_id "
                "WHERE c.entity_id=? OR c.edge_id IN (SELECT id FROM edges WHERE subject_id=? OR object_id=?) "
                "ORDER BY c.first_seen_at,c.id", (entity_id, entity_id, entity_id),
            )
            for claim in row["claims"]:
                claim["value"] = json.loads(claim.pop("value_json"))
                claim["observations"] = self.all(
                    "SELECT o.*,a.producer_version,a.config_fingerprint FROM claim_observations o "
                    "JOIN attempts a ON a.id=o.attempt_id WHERE claim_id=? ORDER BY o.observed_at,o.attempt_id",
                    (claim["id"],),
                )
        return row

    def backup(self, destination: Path) -> None:
        if destination.resolve() == self.path:
            raise DevosError("invalid_input", "Backup destination must differ from the vault.")
        if destination.exists():
            raise DevosError("already_exists", "Backup destination already exists.")
        with closing(sqlite3.connect(destination)) as backup_db:
            self.db.backup(backup_db)
