import json
import time

from devos.core import DevosError, digest
from devos.services import IngestService
from tests.helpers import FakeTransport, OfflineTransport, REPO_URL, VaultCase, readme, repo, response


class IngestionTests(VaultCase):
    def test_repeat_and_offline_replay_keep_current_counts(self):
        entity_id, _, _ = self.ingest()
        counts = [self.count(t) for t in ("entities", "claims", "edges", "artifacts")]
        other_id, _, _ = self.ingest()
        self.assertEqual(entity_id, other_id)
        job = IngestService(self.store, transport=OfflineTransport()).ingest(["example/vectorkit"], replay=True)
        self.assertEqual(job["state"], "complete", job)
        self.assertEqual(counts, [self.count(t) for t in ("entities", "claims", "edges", "artifacts")])
        self.assertGreater(self.count("claim_observations"), self.count("claims"))

    def test_manual_override_survives_upstream_refresh(self):
        entity_id, _, _ = self.ingest()
        self.vault.edit(entity_id, {"description": "My preferred description"})
        self.ingest(metadata=repo(description="Upstream changed"))
        detail = self.store.entity(entity_id, True)
        self.assertEqual(detail["values"]["description"], "My preferred description")
        self.assertTrue(any(c["value"] == "Upstream changed" for c in detail["claims"]))

    def test_missing_description_supersedes_only_its_scope(self):
        entity_id, _, _ = self.ingest()
        self.ingest(metadata=repo(description=None))
        detail = self.store.entity(entity_id, True)
        self.assertNotIn("description", detail["values"])
        self.assertIn("readme", detail["values"])
        self.assertEqual(self.count("claims", "predicate='description' AND state='active'"), 0)

    def test_partial_readme_preserves_old_text(self):
        entity_id, _, _ = self.ingest(text="Old README")
        transport = FakeTransport({REPO_URL: response(repo(description="new metadata")),
                                   REPO_URL + "/readme": response(status=404)})
        job = IngestService(self.store, transport=transport).ingest(["example/vectorkit"])
        self.assertEqual(job["state"], "complete", job)
        self.assertIn("not_found", job["targets"][0]["result"]["warnings"][0])
        detail = self.store.entity(entity_id)
        self.assertEqual(detail["values"]["readme"], "Old README")
        self.assertEqual(detail["values"]["description"], "new metadata")

    def test_reused_url_does_not_merge_provider_ids(self):
        old, _, _ = self.ingest()
        new, _, job = self.ingest(metadata=repo(id=202))
        self.assertNotEqual(old, new)
        self.assertTrue(job["targets"][0]["result"]["review_required"])
        self.assertEqual(self.count("entities"), 2)
        self.assertEqual(self.count("review_items"), 1)
        self.assertEqual(self.store.one("SELECT entity_id FROM identifiers WHERE namespace='url'")["entity_id"], old)
        self.ingest(metadata=repo(id=202))
        self.assertEqual(self.count("review_items"), 1)

    def test_existing_manual_repository_accepts_provider_identity(self):
        manual = self.vault.add("repository", "Custom name", url="https://github.com/example/vectorkit")
        entity_id, _, _ = self.ingest()
        self.assertEqual(manual["id"], entity_id)
        self.assertEqual(self.store.entity(entity_id)["values"]["name"], "Custom name")

    def test_conditional_304_reuses_body_and_revalidates(self):
        entity_id, _, _ = self.ingest()
        transport = FakeTransport({REPO_URL: response(status=304), REPO_URL + "/readme": response(status=304)})
        job = IngestService(self.store, transport=transport).ingest(["example/vectorkit"])
        self.assertEqual(job["state"], "complete", job)
        self.assertEqual(self.count("artifacts"), 2)
        self.assertEqual(transport.calls[0][1]["If-None-Match"], '"fixture-v1"')
        self.assertTrue(all(s["freshness"] == "revalidated" for s in job["targets"][0]["result"]["scopes"]))
        self.assertEqual(self.store.entity(entity_id)["values"]["name"], "Example/VectorKit")

    def test_stale_offline_cache_labels_original_observation(self):
        entity_id, _, _ = self.ingest()
        observed = self.store.one("SELECT min(observed_at) AS t FROM snapshots")["t"]
        job = IngestService(self.store, transport=OfflineTransport()).ingest(["example/vectorkit"], offline=True)
        self.assertEqual(job["state"], "complete", job)
        scopes = job["targets"][0]["result"]["scopes"]
        self.assertTrue(all(s["freshness"] == "cached" for s in scopes))
        self.assertEqual(self.store.snapshot(scopes[0]["snapshot_id"])["observed_at"], observed)
        self.assertTrue(self.vault.search("vector"))

    def test_historical_replay_never_overwrites_new_source_values(self):
        entity_id, _, job = self.ingest(metadata=repo(description="Old description"), text="Old README")
        ids = {self.store.snapshot(s["snapshot_id"])["resource_key"]: s["snapshot_id"]
               for s in job["targets"][0]["result"]["scopes"]}
        self.ingest(metadata=repo(description="Current description"), text="Current README")
        replay = IngestService(self.store, transport=OfflineTransport()).ingest(["example/vectorkit"], replay=True, snapshots=ids)
        self.assertEqual(replay["state"], "complete", replay)
        values = self.store.entity(entity_id)["values"]
        self.assertEqual(values["description"], "Current description")
        self.assertEqual(values["readme"], "Current README")

    def test_batch_isolates_bad_input_and_malformed_json(self):
        transport = FakeTransport({REPO_URL: response(repo()), REPO_URL + "/readme": response(readme()),
                                   "https://api.github.com/repos/broken/repo": response(["invalid"])})
        job = IngestService(self.store, transport=transport).ingest(["invalid", "example/vectorkit", "broken/repo"])
        self.assertEqual(job["state"], "partial", job)
        self.assertEqual([t["state"] for t in job["targets"]], ["failed", "committed", "failed"])
        self.assertEqual(self.count("entities"), 1)

    def test_private_repository_is_rejected(self):
        service = IngestService(self.store, transport=FakeTransport({REPO_URL: response(repo(private=True))}))
        job = service.ingest(["example/vectorkit"])
        self.assertEqual(job["targets"][0]["error"]["code"], "access_denied")
        self.assertEqual(self.count("entities"), 0)

    def test_rate_limit_is_durable_and_other_target_commits(self):
        transport = FakeTransport({REPO_URL: response(repo()), REPO_URL + "/readme": response(readme()),
            "https://api.github.com/repos/limited/repo": response(status=429, headers={"Retry-After": "120"})})
        service = IngestService(self.store, transport=transport)
        job = service.ingest(["example/vectorkit", "limited/repo"])
        self.assertEqual(job["state"], "retry_wait", job)
        self.assertEqual(job["targets"][0]["state"], "committed")
        target = job["targets"][1]
        self.assertGreaterEqual(target["next_attempt_at"], time.time() + 119)
        count = len(transport.calls)
        service.resume(job["id"])
        self.assertEqual(count, len(transport.calls))

    def test_graph_write_rolls_back_and_retained_snapshot_can_replay(self):
        service = IngestService(self.store, transport=FakeTransport({REPO_URL: response(repo()), REPO_URL + "/readme": response(readme())}))
        service.before_commit = lambda: (_ for _ in ()).throw(RuntimeError("simulated crash"))
        job = service.ingest(["example/vectorkit"])
        self.assertEqual(job["state"], "failed")
        self.assertEqual(self.count("entities"), 0)
        self.assertEqual(self.count("claims"), 0)
        self.assertEqual(self.count("snapshots"), 2)
        replay = IngestService(self.store, transport=OfflineTransport()).ingest(["example/vectorkit"], replay=True)
        self.assertEqual(replay["state"], "complete", replay)

    def test_resuming_committed_target_does_not_refetch(self):
        _, _, job = self.ingest()
        same = IngestService(self.store, transport=OfflineTransport()).resume(job["id"])
        self.assertEqual(same["state"], "complete")
        self.assertEqual(self.count("attempts"), 1)

    def test_provenance_has_locator_versions_and_snapshot_hash(self):
        entity_id, _, _ = self.ingest()
        claims = self.store.entity(entity_id, True)["claims"]
        self.assertTrue(all(c["artifact_hash"] and c["source_url"] and c["observations"] for c in claims))
        description = next(c for c in claims if c["predicate"] == "description")
        self.assertEqual(json.loads(description["observations"][0]["locator_json"]), {"json_pointer": "/description"})
        self.assertEqual(description["observations"][0]["producer_version"], "1.1.0")

    def test_lease_reclamation_fences_old_worker(self):
        now = time.time()
        self.store.clock = lambda: now
        job = self.store.create_job(["example/vectorkit"], mode="fetch", config={})
        tid = self.store.one("SELECT id FROM job_targets WHERE job_id=?", (job,))["id"]
        first = self.store.claim_target(tid, producer="github", version="1", config=digest({}), ttl=1)
        self.assertIsNone(self.store.claim_target(tid, producer="github", version="1", config=digest({})))
        self.store.clock = lambda: now + 2
        second = self.store.claim_target(tid, producer="github", version="1", config=digest({}))
        self.assertNotEqual(first["lease_token"], second["lease_token"])
        with self.assertRaises(DevosError):
            with self.store.transaction():
                self.store.finish_target(tid, first["lease_token"], state="committed")
        self.assertEqual(self.count("attempts", "state='interrupted'"), 1)
        with self.store.transaction():
            self.store.finish_target(tid, second["lease_token"], state="committed", result={})
        self.assertEqual(self.store.summarize(job)["state"], "complete")
