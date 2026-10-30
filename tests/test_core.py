import json
import unittest
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import patch

from devos.adapters import Registry
from devos.adapters.github import GitHubAdapter
from devos.core import DevosError, github_slug, safe_url
from devos.ports import Document
from tests.helpers import FIXTURES, VaultCase


class CoreTests(unittest.TestCase):
    def test_equivalent_github_inputs(self):
        for value in ("Example/VectorKit", "https://github.com/Example/VectorKit.git/",
                      "https://www.github.com/example/vectorkit?utm_source=test#readme"):
            self.assertEqual(github_slug(value), "example/vectorkit")

    def test_reject_non_repository_inputs(self):
        for value in ("https://example.com/a/b", "https://github.com/a/b/issues", "../x", "file:///a/b"):
            with self.assertRaises(DevosError):
                github_slug(value)

    def test_reject_credential_urls(self):
        for value in ("https://user:secret@github.com/a/b", "https://github.com/a/b?token=secret"):
            with self.assertRaises(DevosError):
                safe_url(value)

    def test_plugin_initialization_failure_is_isolated(self):
        registry = Registry()

        class BrokenEntry:
            def load(self):
                raise RuntimeError("secret exception")

        registry._entries["broken"] = BrokenEntry()
        with self.assertRaisesRegex(DevosError, "failed to load"):
            registry.load("broken")
        self.assertEqual(registry.select("a/b").descriptor.key, "github")

    def test_duplicate_plugins_cannot_disable_other_adapters(self):
        entries = [SimpleNamespace(name=key) for key in ("duplicate", "duplicate", "github")]
        with patch("devos.adapters.entry_points", return_value=entries):
            registry = Registry()
        self.assertEqual(registry.select("a/b").descriptor.key, "github")
        with self.assertRaises(DevosError):
            registry.load("duplicate")

    def test_parser_fixture(self):
        adapter = GitHubAdapter()

        class FixtureFetcher:
            def fetch(self, resource, url):
                if "/contents/" in url:
                    raise DevosError("not_found", "Optional manifest is absent.")
                filename = "readme.json" if resource.endswith("/readme") else "repository.json"
                return Document(filename, (FIXTURES / filename).read_bytes(), "fresh", 200)

        batch = adapter.extract(adapter.discover("example/vectorkit"), FixtureFetcher())
        self.assertEqual(batch, adapter.extract(adapter.discover("example/vectorkit"), FixtureFetcher()))
        golden = json.loads((FIXTURES / "github-normalized.json").read_text())
        self.assertEqual(json.loads(json.dumps(asdict(batch))), golden)
        self.assertEqual(batch.identifiers[0], ("github.com:repository", "101", "authoritative"))
        self.assertEqual({f.predicate for f in batch.facts}, {"name", "canonical_url", "description", "metadata", "readme"})


class ManualTests(VaultCase):
    def test_manual_note_persists_with_provenance(self):
        entity = self.vault.add("note", "Research", body="offline notebooks")
        self.assertEqual(len(self.vault.search("notebooks")), 1)
        detailed = self.store.entity(entity["id"], True)
        self.assertTrue(all(c["origin"] == "manual" and c["actor"] for c in detailed["claims"]))
        self.store.close()
        from devos.storage import SQLiteStore
        self.store = SQLiteStore(self.path)
        self.assertEqual(self.store.entity(entity["id"])["values"]["name"], "Research")

    def test_edits_preserve_old_claims(self):
        entity = self.vault.add("note", "Research", description="old")
        self.vault.edit(entity["id"], {"description": "new"})
        self.assertEqual(self.store.entity(entity["id"])["values"]["description"], "new")
        self.assertEqual(self.count("claims", "predicate='description'"), 2)
        self.assertEqual(self.count("claims", "predicate='description' AND state='active'"), 1)
        self.vault.edit(entity["id"], {"description": "old"})
        self.assertEqual(self.store.entity(entity["id"])["values"]["description"], "old")

    def test_typed_links_are_idempotent_and_keep_cycles(self):
        a = self.vault.add("package", "A")["id"]
        b = self.vault.add("package", "B")["id"]
        first = self.vault.link(a, "depends_on", b, {"scope": "runtime"})
        second = self.vault.link(a, "depends_on", b, {"scope": "runtime"})
        self.assertEqual(first["id"], second["id"])
        self.vault.link(b, "depends_on", a)
        self.assertEqual(len(self.store.entity(a)["relations"]), 2)
        with self.assertRaises(DevosError):
            self.vault.link(a, "implements", b)

    def test_symmetric_relation_uses_one_edge(self):
        a, b = [self.vault.add("tool", name)["id"] for name in ("A", "B")]
        self.assertEqual(self.vault.link(a, "similar_to", b)["id"], self.vault.link(b, "similar_to", a)["id"])

    def test_search_filters_and_literal_punctuation(self):
        a = self.vault.add("note", "Vector guide")
        self.vault.add("tool", "Vector tool")
        self.assertEqual([r["id"] for r in self.vault.search("vector", entity_type="note")], [a["id"]])
        self.assertEqual(len(self.vault.search("vector", limit=1)), 1)
        self.assertNotEqual(self.vault.search("vector", limit=1), self.vault.search("vector", limit=1, offset=1))
        self.assertEqual(self.vault.search('" OR *'), [])

    def test_url_uniqueness_and_type_separation(self):
        package = self.vault.add("package", "VectorKit", url="https://pypi.org/project/vectorkit")
        paper = self.vault.add("paper", "VectorKit Paper")
        repo_id, _, _ = self.ingest()
        self.vault.link(package["id"], "source_repository", repo_id)
        self.vault.link(repo_id, "implements", paper["id"])
        self.ingest()
        self.assertEqual(self.count("entities"), 3)
        with self.assertRaises(DevosError):
            self.vault.add("tool", "duplicate", url="https://pypi.org/project/vectorkit")

    def test_export_and_raw_opt_in(self):
        entity_id, _, _ = self.ingest()
        export = self.vault.export()
        self.assertNotIn("artifacts", export["data"])
        self.assertNotIn("jobs", export["data"])
        self.assertNotIn("cache_scope", export["data"]["snapshots"][0])
        raw = self.vault.export(include_raw=True)
        self.assertEqual(len(raw["data"]["artifacts"]), 2)
        self.assertEqual(raw["data"]["entities"][0]["id"], entity_id)
        extraction_ids = {e["id"] for e in raw["data"]["extractions"]}
        self.assertTrue(all(c["attempt_id"] in extraction_ids for c in raw["data"]["claims"]))
        json.dumps(raw)
