"""Deterministic public GitHub metadata and README extraction."""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from devos.adapters.dependencies import package_json, requirements
from devos.core import Batch, Descriptor, DevosError, Fact, Reference, Scope, Target, github_slug
from devos.ports import Document, Fetcher
from devos.adapters.references import text_references


class GitHubAdapter:
    descriptor = Descriptor("github", "1.1.0", input_kinds=("repository_url", "repository_slug"),
                            output_types=("repository",), credential_env="GITHUB_TOKEN")

    def match(self, value: str) -> int:
        try:
            github_slug(value)
            return 100
        except (DevosError, ValueError):
            return 0

    def discover(self, value: str) -> Target:
        slug = github_slug(value)
        return Target(slug, f"https://github.com/{slug}")

    @staticmethod
    def parse(document: Document) -> dict[str, Any]:
        try:
            value = json.loads(document.body)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, UnicodeError):
            raise DevosError("invalid_payload", "GitHub returned malformed JSON.") from None

    def extract(self, target: Target, fetcher: Fetcher) -> Batch:
        slug = target.resource_key
        metadata = fetcher.fetch(f"{slug}/metadata", f"https://api.github.com/repos/{slug}")
        raw = self.parse(metadata)
        readme = None
        warnings: list[str] = []
        manifests: dict[str, Document] = {}
        canonical_slug = github_slug(raw["full_name"])
        try:
            readme = fetcher.fetch(f"{slug}/readme", f"https://api.github.com/repos/{canonical_slug}/readme")
        except DevosError as exc:
            warnings.append(f"repository.readme: {exc.code}")
        for filename in ("requirements.txt", "package.json"):
            try:
                manifests[filename] = fetcher.fetch(
                    f"{slug}/{filename}", f"https://api.github.com/repos/{canonical_slug}/contents/{filename}")
            except DevosError as exc:
                if exc.code != "not_found":
                    warnings.append(f"repository.{filename}: {exc.code}")
        result = self.normalize(target, metadata, raw, readme, manifests)
        return Batch(result.entity_type, result.identifiers, result.facts, result.scopes,
                     tuple(warnings) + result.warnings, result.references)

    def normalize(self, target: Target, metadata: Document, raw: dict[str, Any],
                  readme: Document | None = None,
                  manifests: dict[str, Document] | None = None) -> Batch:
        """Map parsed source data to canonical candidates without network or vault access."""
        try:
            if raw.get("private") is True:
                raise DevosError("access_denied", "This adapter currently ingests public repositories only.")
            repo_id = raw["id"]
            if isinstance(repo_id, bool) or not isinstance(repo_id, int) or repo_id <= 0:
                raise ValueError
            canonical_slug = github_slug(raw["full_name"])
            if raw.get("description") is not None and not isinstance(raw["description"], str):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise DevosError("invalid_payload", "GitHub repository identity or metadata is invalid.") from None
        url = f"https://github.com/{canonical_slug}"
        scope = "repository.metadata"
        facts = [
            Fact("name", raw["full_name"], scope, {"json_pointer": "/full_name"}),
            Fact("canonical_url", url, scope, {"json_pointer": "/full_name"}),
            Fact("metadata", {key: raw.get(key) for key in (
                "language", "topics", "license", "stargazers_count", "forks_count", "archived", "default_branch",
            )}, scope, {"json_pointer": "/"}),
        ]
        if raw.get("description") is not None:
            facts.append(Fact("description", raw["description"], scope, {"json_pointer": "/description"}))
        scopes = [Scope(scope, metadata.snapshot_id, True, metadata.freshness)]
        warnings = []
        references: tuple[Reference, ...] = ()
        try:
            if readme is not None:
                data = self.parse(readme)
                if data.get("encoding") != "base64" or not isinstance(data.get("content"), str):
                    raise DevosError("unsupported_schema", "GitHub README encoding is unsupported.")
                try:
                    body = base64.b64decode("".join(data["content"].split()), validate=True).decode("utf-8")
                except (UnicodeError, binascii.Error):
                    raise DevosError("invalid_payload", "GitHub README content is invalid.") from None
                facts.append(Fact("readme", body, "repository.readme", {"path": data.get("path", "README"), "json_pointer": "/content"}))
                scopes.append(Scope("repository.readme", readme.snapshot_id, True, readme.freshness))
                references = text_references(body, "repository.readme", locator={"path": data.get("path", "README")})[:50]
        except DevosError as exc:
            # An unavailable README is an omitted scope, not evidence to erase old text.
            warnings.append(f"repository.readme: {exc.code}")
        manifest_refs: list[Reference] = []
        for filename, parser, manifest_scope in (
            ("requirements.txt", requirements, "repository.requirements"),
            ("package.json", package_json, "repository.package_json"),
        ):
            document = (manifests or {}).get(filename)
            if document is None:
                continue
            try:
                data = self.parse(document)
                if data.get("encoding") != "base64" or not isinstance(data.get("content"), str):
                    raise DevosError("unsupported_schema", "GitHub manifest encoding is unsupported.")
                decoded = base64.b64decode("".join(data["content"].split()), validate=True)
                manifest_refs.extend(parser(decoded))
                scopes.append(Scope(manifest_scope, document.snapshot_id, True, document.freshness))
            except (DevosError, binascii.Error) as exc:
                code = exc.code if isinstance(exc, DevosError) else "invalid_payload"
                warnings.append(f"{manifest_scope}: {code}")
        return Batch("repository", tuple(dict.fromkeys([
            ("github.com:repository", str(repo_id), "authoritative"),
            ("url", url, "alias"), ("url", target.url, "alias"),
        ])), tuple(facts), tuple(scopes), tuple(warnings), references + tuple(manifest_refs))
