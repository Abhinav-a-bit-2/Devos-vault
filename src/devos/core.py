"""Framework-independent domain records and validation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

ENTITY_TYPES = (
    "repository", "package", "tool", "hf_model", "hf_dataset", "paper",
    "note", "topic", "collection",
)
RELATIONS: dict[str, tuple[set[str], set[str]]] = {
    "source_repository": ({"package", "tool", "hf_model"}, {"repository"}),
    "implements": ({"repository"}, {"paper"}),
    "depends_on": ({"repository", "package", "tool"}, {"repository", "package", "tool"}),
    "fine_tuned_from": ({"hf_model"}, {"hf_model"}),
    "derived_from": ({"hf_model"}, {"hf_model"}),
    "uses_dataset": ({"hf_model"}, {"hf_dataset"}),
    "cites": (set(ENTITY_TYPES), {"paper"}),
    "part_of": (set(ENTITY_TYPES), {"collection"}),
    "tagged_with": (set(ENTITY_TYPES), {"topic"}),
    "mentions": (set(ENTITY_TYPES), set(ENTITY_TYPES)),
    "similar_to": (set(ENTITY_TYPES), set(ENTITY_TYPES)),
}
PREDICATES = {"name", "description", "canonical_url", "readme", "body", "metadata"}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def uid() -> str:
    return str(uuid4())


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class DevosError(Exception):
    """An actionable, safe-to-present application error."""

    def __init__(self, code: str, message: str, *, retry_at: float | None = None):
        super().__init__(message)
        self.code = code
        self.retry_at = retry_at


def safe_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if parsed.port not in (None, 80, 443):
            raise ValueError
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password):
            raise ValueError
        if re.search(r"(?:token|secret|password|api[_-]?key|auth)=", parsed.query, re.I):
            raise ValueError
        return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path,
                           parsed.query, ""))
    except ValueError:
        raise DevosError("invalid_input", "Use an HTTP(S) URL without credentials.") from None


def github_slug(value: str) -> str:
    value = value.strip()
    if "://" in value:
        parsed = urlsplit(safe_url(value))
        if parsed.hostname not in {"github.com", "www.github.com"}:
            raise DevosError("unsupported_input", "Only github.com repository URLs are supported.")
        value = parsed.path.strip("/")
    value = value.removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise DevosError("unsupported_input", "Use a GitHub owner/repository or repository URL.")
    if any(part in {".", ".."} for part in value.split("/")):
        raise DevosError("invalid_input", "Invalid repository path.")
    return value.lower()


@dataclass(frozen=True)
class Descriptor:
    key: str
    version: str
    contract_major: int = 1
    input_kinds: tuple[str, ...] = ()
    output_types: tuple[str, ...] = ()
    fetch_strategies: tuple[str, ...] = ("official_api", "retained_snapshot")
    credential_env: str | None = None
    quota_resources: tuple[str, ...] = ("core",)
    allowed_hosts: tuple[str, ...] = ("api.github.com",)
    min_interval: float = 0


@dataclass(frozen=True)
class Target:
    resource_key: str
    url: str


@dataclass(frozen=True)
class Scope:
    name: str
    snapshot_id: str
    complete: bool
    freshness: str  # fresh, revalidated, cached, replay


@dataclass(frozen=True)
class Fact:
    predicate: str
    value: Any
    scope: str
    locator: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Reference:
    entity_type: str
    identifiers: tuple[tuple[str, str, str], ...]
    name: str
    url: str
    relation: str
    scope: str
    locator: dict[str, Any] = field(default_factory=dict)
    qualifiers: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Batch:
    entity_type: str
    identifiers: tuple[tuple[str, str, str], ...]  # namespace, value, authoritative|alias
    facts: tuple[Fact, ...]
    scopes: tuple[Scope, ...]
    warnings: tuple[str, ...] = ()
    references: tuple[Reference, ...] = ()


def validate_batch(batch: Batch) -> None:
    if batch.entity_type not in ENTITY_TYPES or not batch.identifiers:
        raise DevosError("invalid_payload", "Adapter returned an invalid entity or identity.")
    scopes = {s.name for s in batch.scopes}
    if len(scopes) != len(batch.scopes):
        raise DevosError("invalid_payload", "Adapter returned duplicate scopes.")
    for namespace, value, kind in batch.identifiers:
        if not namespace or not value or kind not in {"authoritative", "alias"}:
            raise DevosError("invalid_payload", "Adapter returned an invalid identifier.")
    for fact in batch.facts:
        if fact.predicate not in PREDICATES or fact.scope not in scopes:
            raise DevosError("invalid_payload", "Adapter returned an invalid claim.")
        canonical(fact.value)
    if len(batch.references) > 200:
        raise DevosError("invalid_payload", "Extraction exceeded the 200-reference limit.")
    for ref in batch.references:
        if ref.scope not in scopes or ref.relation not in RELATIONS or not ref.identifiers:
            raise DevosError("invalid_payload", "Adapter returned an invalid reference.")
        left, right = RELATIONS[ref.relation]
        if batch.entity_type not in left or ref.entity_type not in right:
            raise DevosError("invalid_relation", "Reference has incompatible entity types.")
        safe_url(ref.url)
