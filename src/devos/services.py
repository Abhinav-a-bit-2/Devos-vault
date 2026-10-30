"""Shared application services for CLI and future HTTP clients."""

from __future__ import annotations

import base64
import json
import os
from dataclasses import asdict
from typing import Any, Callable

from devos import __version__
from devos.adapters import Registry
from devos.core import (
    ENTITY_TYPES, PREDICATES, RELATIONS, Batch, DevosError, canonical, digest,
    safe_url, uid, utcnow, validate_batch,
)
from devos.ports import Storage, Transport
from devos.runtime import HTTPTransport, SnapshotFetcher


class VaultService:
    def __init__(self, store: Storage):
        self.store = store

    def show(self, entity_id: str, *, provenance: bool = False) -> dict[str, Any]:
        return self.store.entity(entity_id, provenance)

    def jobs(self) -> list[dict[str, Any]]:
        return self.store.all("SELECT id,mode,state,created_at,updated_at FROM jobs ORDER BY created_at DESC,id LIMIT 100")

    def job(self, job_id: str) -> dict[str, Any]:
        result = self.store.summarize(job_id)
        result["events"] = self.store.all(
            "SELECT sequence,time,status,payload_json FROM job_events WHERE job_id=? ORDER BY sequence", (job_id,))
        return result

    def reviews(self) -> list[dict[str, Any]]:
        return self.store.all("SELECT * FROM review_items WHERE state='open' ORDER BY created_at,id")

    def snapshot_selection(self, snapshot_id: str) -> dict[str, str]:
        snapshot = self.store.snapshot(snapshot_id)
        return {snapshot["resource_key"]: snapshot_id}

    def overview(self) -> dict[str, Any]:
        counts = self.store.all("SELECT type_key,count(*) AS count FROM entities WHERE merged_into_id IS NULL GROUP BY type_key")
        return {"entities": sum(row["count"] for row in counts), "by_type": {row["type_key"]: row["count"] for row in counts},
                "relations": self.store.one("SELECT count(DISTINCT edge_id) AS n FROM claims WHERE edge_id IS NOT NULL AND state='active'")["n"],
                "sources": self.store.one("SELECT count(*) AS n FROM sources")["n"],
                "active_jobs": self.store.one("SELECT count(*) AS n FROM jobs WHERE state IN ('pending','running','retry_wait')")["n"],
                "reviews": self.store.one("SELECT count(*) AS n FROM review_items WHERE state='open'")["n"]}

    def browse(self, *, query: str = "", entity_type: str | None = None, limit: int = 30, offset: int = 0) -> dict[str, Any]:
        if not 1 <= limit <= 100 or offset < 0 or (entity_type and entity_type not in ENTITY_TYPES):
            raise DevosError("invalid_input", "Invalid entity filter or page size.")
        if query.strip():
            rows = self.search(query, limit=limit, offset=offset, entity_type=entity_type)
            terms = " AND ".join('"' + term.replace('"', '""') + '"' for term in query.split())
            total = self.store.one("SELECT count(*) AS n FROM entity_search JOIN entities e ON e.id=entity_search.entity_id WHERE entity_search MATCH ? AND (? IS NULL OR e.type_key=?)", (terms, entity_type, entity_type))["n"]
        else:
            rows = self.store.all("SELECT e.id,e.type_key,v.name,v.description,v.canonical_url FROM entities e JOIN entity_views v ON v.entity_id=e.id WHERE e.merged_into_id IS NULL AND (? IS NULL OR e.type_key=?) ORDER BY e.updated_at DESC,e.id LIMIT ? OFFSET ?", (entity_type, entity_type, limit, offset))
            total = self.store.one("SELECT count(*) AS n FROM entities WHERE merged_into_id IS NULL AND (? IS NULL OR type_key=?)", (entity_type, entity_type))["n"]
        for row in rows:
            row["reference_only"] = not bool(self.store.one("SELECT id FROM claims WHERE entity_id=? AND origin IN ('manual','extracted') AND state='active' LIMIT 1", (row["id"],)))
            row["connections"] = self.store.one("SELECT count(DISTINCT e.id) AS n FROM edges e JOIN claims c ON c.edge_id=e.id AND c.state='active' WHERE e.subject_id=? OR e.object_id=?", (row["id"], row["id"]))["n"]
        return {"items": rows, "total": total, "limit": limit, "offset": offset}

    def graph(self, *, root: str | None = None, limit: int = 80) -> dict[str, Any]:
        if not 1 <= limit <= 200:
            raise DevosError("invalid_input", "Graph limit must be from 1 to 200.")
        if root:
            self.store.entity(root)
            ids, frontier = {root}, [root]
            truncated = False
            while frontier:
                current = frontier.pop(0)
                neighbors = self.store.all("SELECT DISTINCT CASE WHEN e.subject_id=? THEN e.object_id ELSE e.subject_id END AS id FROM edges e JOIN claims c ON c.edge_id=e.id AND c.state='active' WHERE e.subject_id=? OR e.object_id=? ORDER BY id LIMIT 201", (current, current, current))
                for neighbor in neighbors:
                    if neighbor["id"] not in ids:
                        if len(ids) >= limit:
                            truncated = True
                            continue
                        ids.add(neighbor["id"])
                        frontier.append(neighbor["id"])
            node_ids = sorted(ids)
        else:
            node_ids = [row["id"] for row in self.store.all("SELECT id FROM entities WHERE merged_into_id IS NULL ORDER BY updated_at DESC,id LIMIT ?", (limit,))]
            truncated = self.overview()["entities"] > limit
        if not node_ids:
            return {"nodes": [], "edges": [], "truncated": False}
        marks = ",".join("?" for _ in node_ids)
        nodes = self.store.all(f"SELECT e.id,e.type_key,v.name,v.canonical_url FROM entities e JOIN entity_views v ON v.entity_id=e.id WHERE e.id IN ({marks}) ORDER BY e.type_key,v.name", tuple(node_ids))
        edges = self.store.all(f"SELECT DISTINCT e.id,e.subject_id,e.object_id,e.relation_key FROM edges e JOIN claims c ON c.edge_id=e.id AND c.state='active' WHERE e.subject_id IN ({marks}) AND e.object_id IN ({marks}) ORDER BY e.id", tuple(node_ids + node_ids))
        return {"nodes": nodes, "edges": edges, "truncated": truncated}

    def impact(self, root: str, *, depth: int = 3, limit: int = 100) -> dict[str, Any]:
        """Bounded reverse traversal over declared dependencies and model lineage."""
        if not 1 <= depth <= 6 or not 1 <= limit <= 200:
            raise DevosError("invalid_input", "Impact depth must be 1–6 and item limit must be 1–200.")
        self.store.entity(root)
        distances = {root: 0}
        frontier = [root]
        edges: dict[str, dict[str, Any]] = {}
        truncated = False
        for level in range(1, depth + 1):
            following: list[str] = []
            for current in frontier:
                rows = self.store.all(
                    "SELECT DISTINCT e.id,e.subject_id,e.object_id,e.relation_key FROM edges e "
                    "JOIN claims c ON c.edge_id=e.id AND c.state='active' "
                    "WHERE e.object_id=? AND e.relation_key IN ('depends_on','fine_tuned_from','derived_from') "
                    "ORDER BY e.subject_id,e.id LIMIT 201", (current,))
                if len(rows) > 200:
                    truncated = True
                for edge in rows:
                    subject = edge["subject_id"]
                    if subject not in distances:
                        if len(distances) >= limit:
                            truncated = True
                            continue
                        distances[subject] = level
                        following.append(subject)
                    if subject in distances:
                        if len(edges) >= 1000 and edge["id"] not in edges:
                            truncated = True
                        else:
                            edges[edge["id"]] = edge
            frontier = following
            if not frontier:
                break
        ids = list(distances)
        marks = ",".join("?" for _ in ids)
        nodes = self.store.all(
            f"SELECT e.id,e.type_key,v.name,v.canonical_url FROM entities e JOIN entity_views v "
            f"ON v.entity_id=e.id WHERE e.id IN ({marks})", tuple(ids))
        for node in nodes:
            node["depth"] = distances[node["id"]]
        nodes.sort(key=lambda node: (node["depth"], node["type_key"], node["name"]))
        return {"root": root, "nodes": nodes, "edges": list(edges.values()),
                "truncated": truncated, "max_depth": depth}

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        if not self.store.one("SELECT id FROM jobs WHERE id=?", (job_id,)):
            raise DevosError("not_found", "Job not found.")
        with self.store.transaction():
            targets = self.store.all("SELECT id FROM job_targets WHERE job_id=? AND state IN ('pending','running','retry_wait')", (job_id,))
            for target in targets:
                self.store.execute("UPDATE attempts SET state='canceled',ended_at=? WHERE job_target_id=? AND state='running'", (utcnow(), target["id"]))
                self.store.execute("UPDATE job_targets SET state='canceled',lease_token=NULL,lease_expires=NULL,next_attempt_at=NULL WHERE id=?", (target["id"],))
                self.store.event(job_id, target["id"], "canceled")
        return self.store.summarize(job_id)

    def _new_entity(self, entity_type: str) -> str:
        entity_id, now = uid(), utcnow()
        self.store.execute("INSERT INTO entities VALUES (?,?,?,?,NULL)", (entity_id, entity_type, now, now))
        return entity_id

    def _claim(self, *, entity_id: str | None = None, edge_id: str | None = None,
               predicate: str, value: Any, origin: str = "manual", source_id: str | None = None,
               snapshot_id: str | None = None, attempt_id: str | None = None,
               producer: str = "manual", version: str = __version__, config: str = "default",
               scope: str = "manual", state: str = "active", locator: Any = None) -> str:
        now = utcnow()
        key = digest([entity_id, edge_id, predicate, value, origin, source_id, producer, scope])
        old = self.store.one("SELECT id,state FROM claims WHERE claim_key=?", (key,))
        if old:
            claim_id = old["id"]
            if state == "active":
                self.store.execute("UPDATE claims SET state='active',last_seen_at=?,snapshot_id=?,attempt_id=?,"
                                   "producer_version=?,config_fingerprint=? WHERE id=?",
                                   (now, snapshot_id, attempt_id, version, config, claim_id))
        else:
            claim_id = uid()
            self.store.execute(
                "INSERT INTO claims VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (claim_id, key, entity_id, edge_id, predicate, canonical(value), origin,
                 source_id, snapshot_id, attempt_id, "local-user" if origin == "manual" else None,
                 producer, version, config, scope, None, state, now, now),
            )
        if attempt_id and snapshot_id:
            snapshot = self.store.snapshot(snapshot_id)
            self.store.execute("INSERT OR IGNORE INTO claim_observations VALUES (?,?,?,?,?)",
                               (claim_id, attempt_id, snapshot_id, canonical(locator or {}), snapshot["observed_at"]))
        return claim_id

    def add(self, entity_type: str, name: str, *, description: str | None = None,
            url: str | None = None, body: str | None = None) -> dict[str, Any]:
        if entity_type not in ENTITY_TYPES or not name.strip():
            raise DevosError("invalid_input", "A registered entity type and nonempty name are required.")
        url = safe_url(url) if url else None
        with self.store.transaction():
            if url and self.store.one("SELECT id FROM identifiers WHERE namespace='url' AND normalized_value=?", (url,)):
                raise DevosError("identity_conflict", "This URL is already assigned; edit its existing entity.")
            entity_id = self._new_entity(entity_type)
            values = {"name": name, "description": description, "canonical_url": url, "body": body}
            for predicate, value in values.items():
                if value is not None:
                    self._claim(entity_id=entity_id, predicate=predicate, value=value)
            if url:
                self.store.execute("INSERT INTO identifiers VALUES (?,?,?,?,?,?)",
                                   (uid(), entity_id, "url", url, "alias", utcnow()))
            self.store.rebuild_view(entity_id)
        return self.store.entity(entity_id)

    def edit(self, entity_id: str, values: dict[str, Any]) -> dict[str, Any]:
        if not values or set(values) - PREDICATES:
            raise DevosError("invalid_input", "Supply at least one supported field to edit.")
        if "name" in values and (not isinstance(values["name"], str) or not values["name"].strip()):
            raise DevosError("invalid_input", "Name cannot be empty.")
        if "canonical_url" in values and values["canonical_url"] is not None:
            values = {**values, "canonical_url": safe_url(values["canonical_url"])}
        with self.store.transaction():
            self.store.entity(entity_id)
            for predicate, value in values.items():
                if predicate == "canonical_url" and value:
                    existing = self.store.one("SELECT entity_id FROM identifiers WHERE namespace='url' AND normalized_value=?", (value,))
                    if existing and existing["entity_id"] != entity_id:
                        raise DevosError("identity_conflict", "This URL belongs to a different entity.")
                    self.store.execute("INSERT OR IGNORE INTO identifiers VALUES (?,?,?,?,?,?)",
                                       (uid(), entity_id, "url", value, "alias", utcnow()))
                self.store.execute("UPDATE claims SET state='superseded' WHERE entity_id=? AND predicate=? "
                                   "AND origin='manual' AND state='active'", (entity_id, predicate))
                self._claim(entity_id=entity_id, predicate=predicate, value=value)
            self.store.rebuild_view(entity_id)
        return self.store.entity(entity_id)

    def link(self, subject: str, relation: str, obj: str, qualifiers: dict[str, Any] | None = None) -> dict[str, Any]:
        if relation not in RELATIONS:
            raise DevosError("invalid_input", "Unknown relation type.")
        qualifiers = {} if qualifiers is None else qualifiers
        if not isinstance(qualifiers, dict):
            raise DevosError("invalid_input", "Relation qualifiers must be a JSON object.")
        with self.store.transaction():
            left, right = self.store.entity(subject), self.store.entity(obj)
            allowed_left, allowed_right = RELATIONS[relation]
            if left["type_key"] not in allowed_left or right["type_key"] not in allowed_right:
                raise DevosError("invalid_relation", "These entity types do not support that directed relation.")
            if relation == "similar_to":
                subject, obj = sorted([subject, obj])
            qualifier_hash = digest(qualifiers)
            self.store.execute("INSERT OR IGNORE INTO edges VALUES (?,?,?,?,?,?,?)",
                               (uid(), subject, relation, obj, canonical(qualifiers), qualifier_hash, utcnow()))
            edge = self.store.one("SELECT * FROM edges WHERE subject_id=? AND relation_key=? AND object_id=? AND qualifier_hash=?",
                                  (subject, relation, obj, qualifier_hash))
            assert edge
            self._claim(edge_id=edge["id"], predicate="exists", value=True)
        return edge

    def search(self, query: str, *, limit: int = 20, offset: int = 0, entity_type: str | None = None) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100 or offset < 0 or not query.strip():
            raise DevosError("invalid_input", "Search needs text, a limit from 1 to 100, and a nonnegative offset.")
        if entity_type and entity_type not in ENTITY_TYPES:
            raise DevosError("invalid_input", "Unknown entity type.")
        # Plain keyword input is quoted as literals; user punctuation is not SQL or FTS syntax.
        terms = [f'"{term.replace(chr(34), chr(34) * 2)}"' for term in query.split()]
        return self.store.all(
            "SELECT e.id,e.type_key,v.name,v.description,v.canonical_url,bm25(entity_search) AS rank "
            "FROM entity_search JOIN entities e ON e.id=entity_search.entity_id "
            "JOIN entity_views v ON v.entity_id=e.id WHERE entity_search MATCH ? "
            "AND (? IS NULL OR e.type_key=?) ORDER BY rank,e.id LIMIT ? OFFSET ?",
            (" AND ".join(terms), entity_type, entity_type, limit, offset),
        )

    def export(self, *, include_raw: bool = False) -> dict[str, Any]:
        tables = ("entity_types", "relation_types", "entities", "identifiers", "edges", "claims", "claim_observations",
                  "sources", "snapshots", "review_items", "merge_events", "scope_heads")
        # A consistent read snapshot; no write lock or half-exported target commits.
        self.store.execute("BEGIN")
        try:
            data = {table: self.store.all(f"SELECT * FROM {table} ORDER BY rowid") for table in tables}
            # Attempts referenced by claims/observations are provenance, not operational jobs.
            data["extractions"] = self.store.all(
                "SELECT id,producer_key,producer_version,config_fingerprint,started_at,ended_at,state,coverage_json "
                "FROM attempts WHERE id IN (SELECT attempt_id FROM claim_observations) ORDER BY rowid"
            )
            for snapshot in data["snapshots"]:
                snapshot.pop("cache_scope", None)
                if not include_raw:
                    snapshot["raw_included"] = False
            if include_raw:
                data["artifacts"] = [
                    {"sha256": row["sha256"], "byte_length": row["byte_length"],
                     "body_base64": base64.b64encode(row["body"]).decode("ascii")}
                    for row in self.store.all("SELECT * FROM artifacts ORDER BY sha256")
                ]
        finally:
            self.store.execute("ROLLBACK")
        return {"format": "devos-vault", "format_version": 1, "schema_version": 2,
                "projection_version": 1, "exported_at": utcnow(), "raw_included": include_raw, "data": data}


class IngestService(VaultService):
    def __init__(self, store: Storage, *, transport: Transport | None = None, registry: Registry | None = None):
        super().__init__(store)
        self.transport = transport
        self.registry = registry or Registry()
        self.before_commit: Callable[[], None] | None = None  # fault-injection seam for transaction tests

    def ingest(self, inputs: list[str], *, offline: bool = False, replay: bool = False,
               adapter: str | None = None, snapshots: dict[str, str] | None = None) -> dict[str, Any]:
        return self.resume(self.enqueue(inputs, offline=offline, replay=replay, adapter=adapter, snapshots=snapshots))

    def enqueue(self, inputs: list[str], *, offline: bool = False, replay: bool = False,
                adapter: str | None = None, snapshots: dict[str, str] | None = None) -> str:
        if not inputs or any(not value.strip() for value in inputs):
            raise DevosError("invalid_input", "Supply at least one nonempty input.")
        credentials = [os.environ.get(key) for key in ("GITHUB_TOKEN", "HF_TOKEN")]
        for value in inputs:
            if "://" in value:
                safe_url(value)  # Reject credential-bearing input before writing a durable job.
            if any(credential and credential in value for credential in credentials):
                raise DevosError("invalid_input", "Input must not contain the configured credential.")
        config = {"offline": offline, "adapter": adapter, "snapshots": snapshots or {}}
        return self.store.create_job(inputs, mode="replay" if replay else "fetch", config=config)

    def resume(self, job_id: str) -> dict[str, Any]:
        job = self.store.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not job:
            raise DevosError("not_found", "Job not found.")
        config = json.loads(job["config_json"])
        for target in self.store.all("SELECT * FROM job_targets WHERE job_id=? ORDER BY rowid", (job_id,)):
            # Adapter selection errors also become durable per-target outcomes.
            try:
                adapter = self.registry.select(target["input"], config.get("adapter"))
                producer, version = adapter.descriptor.key, adapter.descriptor.version
            except DevosError as exc:
                self._selection_failure(target, exc)
                continue
            owned = self.store.claim_target(target["id"], producer=producer, version=version, config=digest(config))
            if not owned:
                continue
            try:
                resolved_target = adapter.discover(target["input"])
                retained = self.store.all(
                    "SELECT a.resource_key,a.snapshot_id FROM attempt_snapshots a JOIN attempts t ON t.id=a.attempt_id "
                    "WHERE t.job_target_id=? AND t.state='interrupted' ORDER BY t.number", (target["id"],))
                snapshots = {row["resource_key"]: row["snapshot_id"] for row in retained}
                snapshots.update(config.get("snapshots", {}))
                transport = self.transport or HTTPTransport(allowed_hosts=adapter.descriptor.allowed_hosts)
                fetcher = SnapshotFetcher(self.store, transport, adapter=producer,
                                          offline=config["offline"] or bool(retained), replay=job["mode"] == "replay",
                                          snapshots=snapshots, min_interval=adapter.descriptor.min_interval,
                                          token=os.environ.get(adapter.descriptor.credential_env or "", ""))
                recorder = _RecordingFetcher(self.store, owned, fetcher)
                batch = adapter.extract(resolved_target, recorder)
                if set(snapshots) - recorder.resources:
                    raise DevosError("invalid_input", "Selected snapshot does not match the requested adapter resources.")
                validate_batch(batch)
                self._persist(batch, owned, producer, version, digest(config))
            except KeyboardInterrupt:
                self._failure(owned, DevosError("canceled", "Canceled by user."), canceled=True)
                with self.store.transaction():
                    self.store.execute("UPDATE job_targets SET state='canceled' WHERE job_id=? AND state IN ('pending','retry_wait')", (job_id,))
                break
            except DevosError as exc:
                self._failure(owned, exc)
            except Exception:
                # Never persist arbitrary exception text from plugins or network libraries.
                self._failure(owned, DevosError("internal_error", "Target processing failed; retained snapshots can be replayed."))
        return self.store.summarize(job_id)

    def _selection_failure(self, target: dict[str, Any], error: DevosError) -> None:
        owned = self.store.claim_target(target["id"], producer="selection", version="1", config="none")
        if owned:
            self._failure(owned, error)

    def _failure(self, target: dict[str, Any], error: DevosError, *, canceled: bool = False) -> None:
        retryable = error.retry_at is not None and target["attempt_count"] < 3
        message = str(error)
        for name in ("GITHUB_TOKEN", "HF_TOKEN"):
            credential = os.environ.get(name)
            if credential:
                message = message.replace(credential, "[REDACTED]")
        try:
            with self.store.transaction():
                self.store.finish_target(target["id"], target["lease_token"],
                    state="canceled" if canceled else "retry_wait" if retryable else "failed",
                    error={"code": error.code, "message": message},
                    retry_at=error.retry_at if retryable else None)
        except DevosError as exc:
            if exc.code != "lease_lost":
                raise

    def _review(self, reason: str, evidence: Any) -> None:
        self.store.execute("INSERT OR IGNORE INTO review_items VALUES (?,?,?,?,?,?)",
                           (uid(), digest([reason, evidence]), reason, canonical(evidence), "open", utcnow()))

    def _resolve(self, batch: Batch) -> tuple[str, bool]:
        matches = []
        for namespace, value, kind in batch.identifiers:
            match = self.store.one("SELECT i.entity_id,e.type_key FROM identifiers i JOIN entities e ON e.id=i.entity_id "
                                   "WHERE i.namespace=? AND i.normalized_value=?", (namespace, value))
            if match:
                matches.append((namespace, value, kind, match))
        strong = {m[3]["entity_id"] for m in matches if m[2] == "authoritative" and m[3]["type_key"] == batch.entity_type}
        if len(strong) > 1:
            raise DevosError("identity_conflict", "Authoritative identifiers disagree; no entities were merged.")
        entity_id = next(iter(strong), None)
        conflict = False
        if not entity_id:
            compatible = set()
            incoming_strong = {(ns, value) for ns, value, kind in batch.identifiers if kind == "authoritative"}
            for _, _, _, match in matches:
                existing = self.store.all("SELECT namespace,normalized_value FROM identifiers WHERE entity_id=? AND kind='authoritative'",
                                          (match["entity_id"],))
                incompatible = any((e["namespace"], e["normalized_value"]) not in incoming_strong
                                   for e in existing if any(ns == e["namespace"] for ns, _ in incoming_strong))
                if match["type_key"] == batch.entity_type and not incompatible:
                    compatible.add(match["entity_id"])
            if len(compatible) == 1:
                entity_id = next(iter(compatible))
        if not entity_id:
            entity_id = self._new_entity(batch.entity_type)
        for namespace, value, kind in batch.identifiers:
            old = self.store.one("SELECT entity_id FROM identifiers WHERE namespace=? AND normalized_value=?", (namespace, value))
            if old and old["entity_id"] != entity_id:
                conflict = True
                self._review("identifier_conflict", {"namespace": namespace, "value": value,
                                                       "existing_entity": old["entity_id"], "incoming_entity": entity_id})
            else:
                self.store.execute("INSERT OR IGNORE INTO identifiers VALUES (?,?,?,?,?,?)",
                                   (uid(), entity_id, namespace, value, kind, utcnow()))
        return entity_id, conflict

    def _persist(self, batch: Batch, target: dict[str, Any], producer: str, version: str, config: str) -> None:
        with self.store.transaction():
            self.store.check_lease(target["id"], target["lease_token"])
            entity_id, review = self._resolve(batch)
            touched = {entity_id}
            linked_ids: set[str] = set()
            for scope in batch.scopes:
                snapshot = self.store.snapshot(scope.snapshot_id)
                source_id = snapshot["source_id"]
                if snapshot["adapter_key"] != producer:
                    raise DevosError("invalid_payload", "Adapter cited a snapshot from another producer.")
                head = self.store.one("SELECT h.*,s.observed_at FROM scope_heads h JOIN snapshots s ON s.id=h.snapshot_id "
                                      "WHERE h.source_id=? AND h.producer_key=? AND h.scope=?", (source_id, producer, scope.name))
                promote = scope.complete and (not head or snapshot["observed_at"] >= head["observed_at"])
                reconcile = promote and scope.freshness in {"fresh", "revalidated", "replay"}
                if reconcile:
                    previous = self.store.all("SELECT DISTINCT entity_id FROM claims WHERE source_id=? AND producer_key=? AND scope=? AND origin='inferred' AND entity_id IS NOT NULL", (source_id, producer, scope.name))
                    touched.update(row["entity_id"] for row in previous)
                    self.store.execute("UPDATE claims SET state='superseded' WHERE source_id=? AND producer_key=? "
                                       "AND scope=? AND state='active' AND (entity_id=? OR origin='inferred' OR edge_id IN (SELECT id FROM edges WHERE subject_id=?))",
                                       (source_id, producer, scope.name, entity_id, entity_id))
                for fact in batch.facts:
                    if fact.scope == scope.name:
                        self._claim(entity_id=entity_id, predicate=fact.predicate, value=fact.value,
                                    origin="extracted", source_id=source_id, snapshot_id=scope.snapshot_id,
                                    attempt_id=target["attempt_id"], producer=producer, version=version, config=config,
                                    scope=scope.name, state="active" if promote else "historical", locator=fact.locator)
                for ref in batch.references:
                    if ref.scope != scope.name:
                        continue
                    reference_batch = Batch(ref.entity_type, ref.identifiers, (), ())
                    other_id, other_review = self._resolve(reference_batch)
                    review = review or other_review
                    if other_id == entity_id:
                        continue
                    touched.add(other_id)
                    linked_ids.add(other_id)
                    for predicate, value in (("name", ref.name), ("canonical_url", ref.url)):
                        self._claim(entity_id=other_id, predicate=predicate, value=value, origin="inferred",
                                    source_id=source_id, snapshot_id=scope.snapshot_id, attempt_id=target["attempt_id"],
                                    producer=producer, version=version, config=config, scope=scope.name,
                                    state="active" if promote else "historical", locator=ref.locator)
                    self.store.execute("INSERT OR IGNORE INTO edges VALUES (?,?,?,?,?,?,?)",
                        (uid(), entity_id, ref.relation, other_id, canonical(ref.qualifiers), digest(ref.qualifiers), utcnow()))
                    edge = self.store.one("SELECT id FROM edges WHERE subject_id=? AND relation_key=? AND object_id=? AND qualifier_hash=?",
                                          (entity_id, ref.relation, other_id, digest(ref.qualifiers)))
                    assert edge
                    self._claim(edge_id=edge["id"], predicate="exists", value=True, origin="extracted",
                                source_id=source_id, snapshot_id=scope.snapshot_id, attempt_id=target["attempt_id"],
                                producer=producer, version=version, config=config, scope=scope.name,
                                state="active" if promote else "historical", locator=ref.locator)
                if promote:
                    # BEGIN IMMEDIATE serializes the head read and replacement. No network
                    # or parsing occurs between this generation check and the write.
                    generation = head["generation"] + 1 if head else 1
                    self.store.execute("INSERT INTO scope_heads VALUES (?,?,?,?,?,?) ON CONFLICT(source_id,producer_key,scope) "
                                       "DO UPDATE SET snapshot_id=excluded.snapshot_id,attempt_id=excluded.attempt_id,generation=excluded.generation",
                                       (source_id, producer, scope.name, scope.snapshot_id, target["attempt_id"], generation))
            for changed_id in touched:
                self.store.rebuild_view(changed_id)
            if self.before_commit:
                self.before_commit()
            self.store.check_lease(target["id"], target["lease_token"])
            self.store.finish_target(target["id"], target["lease_token"], state="committed",
                result={"entity_id": entity_id, "linked_entity_ids": sorted(linked_ids), "review_required": review, "warnings": list(batch.warnings),
                        "scopes": [asdict(scope) for scope in batch.scopes]}, coverage=[asdict(scope) for scope in batch.scopes])


class _RecordingFetcher:
    """Save fetched inputs before parsing so an interrupted attempt can resume offline."""

    def __init__(self, store: Storage, target: dict[str, Any], fetcher: SnapshotFetcher):
        self.store, self.target, self.fetcher = store, target, fetcher
        self.resources: set[str] = set()

    def fetch(self, resource_key: str, url: str) -> Any:
        document = self.fetcher.fetch(resource_key, url)
        self.resources.add(resource_key)
        with self.store.transaction():
            self.store.check_lease(self.target["id"], self.target["lease_token"])
            self.store.execute("INSERT OR REPLACE INTO attempt_snapshots VALUES (?,?,?,?)",
                (self.target["attempt_id"], resource_key, document.snapshot_id, document.freshness))
        return document
