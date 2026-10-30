import json
import sqlite3
from importlib.resources import files
from unittest.mock import patch

from devos.adapters import Registry
from devos.adapters.arxiv import ArxivAdapter
from devos.adapters.references import arxiv_id, hf_resource
from devos.core import DevosError
from devos.ports import Response
from devos.runtime import SnapshotFetcher
from devos.services import IngestService
from devos.storage import SQLiteStore
from devos.storage.sqlite import APPLICATION_ID
from tests.helpers import FIXTURES, FakeTransport, OfflineTransport, VaultCase, response

ARXIV_URL = "https://export.arxiv.org/api/query?id_list=1706.03762&max_results=1"
MODEL_URL = "https://huggingface.co/api/models/example/research-model"
DATASET_URL = "https://huggingface.co/api/datasets/example/research-corpus"


def provider_routes():
    return {ARXIV_URL: Response(200, {}, (FIXTURES / "arxiv.xml").read_bytes()),
            MODEL_URL: response(json.loads((FIXTURES / "hf-model.json").read_text())),
            DATASET_URL: response(json.loads((FIXTURES / "hf-dataset.json").read_text())),
            "https://huggingface.co/example/research-model/raw/main/README.md": Response(200, {}, (FIXTURES / "hf-card.md").read_bytes()),
            "https://huggingface.co/datasets/example/research-corpus/raw/main/README.md": Response(200, {}, b"# Research Corpus\nFixture dataset card.")}


class ProviderTests(VaultCase):
    def setUp(self):
        super().setUp()
        self.hf_env = patch.dict("os.environ", {"HF_TOKEN": ""})
        self.hf_env.start()

    def tearDown(self):
        self.hf_env.stop()
        super().tearDown()

    def import_model(self):
        job = IngestService(self.store, transport=FakeTransport(provider_routes())).ingest(["hf:example/research-model"])
        self.assertEqual(job["state"], "complete", job)
        return job["targets"][0]["result"]["entity_id"]

    def test_provider_selection_and_identifier_normalization(self):
        registry = Registry()
        for value in ("arxiv:1706.03762", "https://arxiv.org/pdf/1706.03762v7.pdf", "hep-th/9901001"):
            self.assertEqual(registry.select(value).descriptor.key, "arxiv")
        for value in ("hf:example/model", "https://huggingface.co/datasets/example/data"):
            self.assertEqual(registry.select(value).descriptor.key, "huggingface")
        self.assertEqual(registry.select("example/repo").descriptor.key, "github")
        self.assertEqual(ArxivAdapter().discover("arxiv:1706.03762v2").resource_key, "1706.03762")
        self.assertEqual(hf_resource("hf:datasets/example/data"), ("dataset", "example/data"))
        for value in ("https://arxiv.org/abs/not-a-paper", "https://evil.test/abs/1706.03762"):
            with self.assertRaises(DevosError):
                arxiv_id(value)

    def test_cross_source_graph_merges_only_reference_identity(self):
        model_id = self.import_model()
        initial = self.count("entities")
        model = self.store.entity(model_id, True)
        kinds = {r["relation_key"] for r in model["relations"]}
        self.assertTrue({"derived_from", "uses_dataset", "cites", "mentions"} <= kinds)
        self.assertNotIn("fine_tuned_from", kinds)
        paper = self.store.one("SELECT entity_id FROM identifiers WHERE namespace='arxiv'")["entity_id"]
        job = IngestService(self.store, transport=FakeTransport(provider_routes())).ingest(["arxiv:1706.03762v1", "https://huggingface.co/datasets/example/research-corpus"])
        self.assertEqual(job["state"], "complete", job)
        self.assertEqual(job["targets"][0]["result"]["entity_id"], paper)
        self.assertEqual(self.count("entities"), initial)
        self.assertEqual(self.store.entity(paper)["values"]["name"], "Attention Is All You Need")
        self.assertNotEqual(paper, model_id)
        self.assertTrue(self.vault.search("attention", entity_type="paper"))
        edges = [claim for claim in model["claims"] if claim["edge_id"]]
        self.assertTrue(all(claim["observations"] and claim["artifact_hash"] for claim in edges))

    def test_placeholder_cannot_overwrite_directly_fetched_metadata(self):
        self.import_model()
        IngestService(self.store, transport=FakeTransport(provider_routes())).ingest(["arxiv:1706.03762"])
        paper = self.store.one("SELECT entity_id FROM identifiers WHERE namespace='arxiv'")["entity_id"]
        self.import_model()
        self.assertEqual(self.store.entity(paper)["values"]["name"], "Attention Is All You Need")

    def test_refresh_removes_only_source_backed_edges_and_retains_manual_link(self):
        model_id = self.import_model()
        base = self.store.one("SELECT entity_id FROM identifiers WHERE namespace='huggingface:model' AND normalized_value='example/base-model'")["entity_id"]
        self.vault.link(model_id, "derived_from", base)
        routes = provider_routes()
        raw = json.loads((FIXTURES / "hf-model.json").read_text())
        raw["cardData"].pop("base_model")
        raw["cardData"].pop("datasets")
        raw["tags"] = []
        routes[MODEL_URL] = response(raw)
        routes["https://huggingface.co/example/research-model/raw/main/README.md"] = Response(200, {}, b"# Updated card")
        job = IngestService(self.store, transport=FakeTransport(routes)).ingest(["hf:example/research-model"])
        self.assertEqual(job["state"], "complete", job)
        relations = self.store.entity(model_id)["relations"]
        self.assertEqual([r["relation_key"] for r in relations], ["derived_from"])

    def test_failed_card_retains_previous_relationships(self):
        model_id = self.import_model()
        routes = provider_routes()
        routes["https://huggingface.co/example/research-model/raw/main/README.md"] = response(status=404)
        job = IngestService(self.store, transport=FakeTransport(routes)).ingest(["hf:example/research-model"])
        self.assertEqual(job["state"], "complete", job)
        self.assertTrue(job["targets"][0]["result"]["warnings"])
        self.assertIn("mentions", {r["relation_key"] for r in self.store.entity(model_id)["relations"]})

    def test_all_provider_snapshots_replay_offline_without_duplicate_edges(self):
        service = IngestService(self.store, transport=FakeTransport(provider_routes()))
        inputs = ["hf:example/research-model", "arxiv:1706.03762", "hf:datasets/example/research-corpus"]
        self.assertEqual(service.ingest(inputs)["state"], "complete")
        before = [self.count(t) for t in ("entities", "edges", "claims")]
        replay = IngestService(self.store, transport=OfflineTransport()).ingest(inputs, replay=True)
        self.assertEqual(replay["state"], "complete", replay)
        self.assertEqual(before, [self.count(t) for t in ("entities", "edges", "claims")])

    def test_provider_credentials_never_cross_hosts(self):
        transport = FakeTransport(provider_routes())
        with patch.dict("os.environ", {"HF_TOKEN": "hf-private-token", "GITHUB_TOKEN": "gh-private-token"}):
            SnapshotFetcher(self.store, transport, adapter="arxiv").fetch("paper", ARXIV_URL)
            SnapshotFetcher(self.store, transport, adapter="huggingface").fetch("model", MODEL_URL)
        self.assertNotIn("Authorization", transport.calls[0][1])
        self.assertEqual(transport.calls[1][1]["Authorization"], "Bearer hf-private-token")
        self.assertNotIn("X-GitHub-Api-Version", transport.calls[1][1])

    def test_malformed_atom_entities_and_wrong_paper_are_rejected(self):
        for content in (b"<!DOCTYPE x [<!ENTITY bad 'bad'>]><feed/>", b"<not-xml", (FIXTURES / "arxiv.xml").read_bytes().replace(b"1706.03762", b"1706.00000")):
            job = IngestService(self.store, transport=FakeTransport({ARXIV_URL: Response(200, {}, content)})).ingest(["arxiv:1706.03762"])
            self.assertEqual(job["state"], "failed", job)
            self.store.execute("UPDATE provider_state SET next_request_at=0")
        self.assertEqual(self.count("entities"), 0)

    def test_graph_bounds_and_cycles(self):
        self.import_model()
        graph = self.vault.graph(limit=2)
        self.assertEqual(len(graph["nodes"]), 2)
        self.assertTrue(graph["truncated"])
        ids = {node["id"] for node in graph["nodes"]}
        self.assertTrue(all(edge["subject_id"] in ids and edge["object_id"] in ids for edge in graph["edges"]))

    def test_v1_upgrade_backs_up_and_preserves_claims(self):
        old_path = self.path.with_name("old.sqlite3")
        connection = sqlite3.connect(old_path)
        connection.executescript(files("devos.storage").joinpath("migrations/001_initial.sql").read_text())
        connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
        connection.execute("PRAGMA user_version=1")
        connection.execute("INSERT INTO entity_types VALUES ('note',1)")
        connection.execute("INSERT INTO entities VALUES ('old-note','note','then','then',NULL)")
        connection.commit()
        connection.close()
        upgraded = SQLiteStore(old_path)
        try:
            self.assertIsNotNone(upgraded.one("SELECT id FROM entities WHERE id='old-note'"))
            self.assertEqual(upgraded.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertIsNotNone(upgraded.one("SELECT relation_key FROM relation_types WHERE relation_key='uses_dataset'"))
            backups = list(old_path.parent.glob("old.sqlite3.v1-*.bak"))
            self.assertEqual(len(backups), 1)
            before = sqlite3.connect(backups[0])
            self.assertEqual(before.execute("PRAGMA user_version").fetchone()[0], 1)
            before.close()
        finally:
            upgraded.close()
