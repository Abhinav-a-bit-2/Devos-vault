import json
import time
from pathlib import Path
from unittest.mock import patch

from devos.core import DevosError
from devos.runtime import HTTPTransport, SnapshotFetcher
from devos.services import IngestService
from devos.storage import SQLiteStore
from tests.helpers import FakeTransport, OfflineTransport, REPO_URL, VaultCase, readme, repo, response


class RuntimeTests(VaultCase):
    def fetcher(self, transport, **kwargs):
        return SnapshotFetcher(self.store, transport, adapter="github", token="", **kwargs)

    def test_auth_does_not_fall_back_to_public_cache(self):
        self.ingest()
        for status, code in [(401, "auth_required"), (403, "access_denied"), (404, "not_found")]:
            transport = FakeTransport({REPO_URL: response(status=status)})
            with self.assertRaises(DevosError) as caught:
                self.fetcher(transport).fetch("example/vectorkit/metadata", REPO_URL)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(len(transport.calls), 1)

    def test_retry_after_date_and_reset_are_respected(self):
        now = 2_000_000_000.0
        from email.utils import formatdate
        transport = FakeTransport({REPO_URL: response(status=429, headers={
            "Retry-After": formatdate(now + 240, usegmt=True),
            "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(now + 300),
        })})
        with self.assertRaises(DevosError) as caught:
            self.fetcher(transport, clock=lambda: now).fetch("example/vectorkit/metadata", REPO_URL)
        self.assertEqual(caught.exception.retry_at, now + 301)

    def test_transient_failures_open_circuit_without_disabling_other_providers(self):
        transport = FakeTransport({REPO_URL: response(status=503)})
        for _ in range(3):
            with self.assertRaises(DevosError) as caught:
                self.fetcher(transport).fetch("example/vectorkit/metadata", REPO_URL)
            self.assertEqual(caught.exception.code, "transient_network")
        with self.assertRaises(DevosError) as caught:
            self.fetcher(transport).fetch("example/vectorkit/metadata", REPO_URL)
        self.assertEqual(caught.exception.code, "rate_limited")
        self.assertEqual(len(transport.calls), 3)
        other = SnapshotFetcher(self.store, FakeTransport({REPO_URL: response(repo())}), adapter="another", token="")
        self.assertEqual(other.fetch("example/vectorkit/metadata", REPO_URL).freshness, "fresh")

    def test_timeout_falls_back_to_labeled_cache(self):
        self.ingest()
        transport = FakeTransport({REPO_URL: DevosError("transient_network", "timeout")})
        doc = self.fetcher(transport).fetch("example/vectorkit/metadata", REPO_URL)
        self.assertEqual(doc.freshness, "cached")

    def test_credential_partitions_and_header_hygiene(self):
        token = "test-secret-token"
        transport = FakeTransport({REPO_URL: response(repo(description=f"echo {token}"), headers={"etag": '"v1"', "authorization": token})})
        fetcher = SnapshotFetcher(self.store, transport, adapter="github", token=token)
        doc = fetcher.fetch("example/vectorkit/metadata", REPO_URL)
        self.assertNotIn(token.encode(), doc.body)
        self.assertEqual(transport.calls[0][1]["Authorization"], f"Bearer {token}")
        anonymous = self.fetcher(OfflineTransport(), offline=True)
        with self.assertRaises(DevosError):
            anonymous.fetch("example/vectorkit/metadata", REPO_URL)
        exported = json.dumps(self.vault.export(include_raw=True))
        self.assertNotIn(token, exported)
        self.assertNotIn("authorization", exported.lower())

    def test_transport_rejects_other_hosts_before_network(self):
        transport = HTTPTransport()
        for url in ("http://api.github.com/repos/a/b", "https://example.org/a", "https://api.github.com@evil.test/a"):
            with self.assertRaises(DevosError):
                transport.get(url, {})

    def test_credential_in_input_is_rejected_before_job_persistence(self):
        with self.assertRaises(DevosError):
            IngestService(self.store, transport=OfflineTransport()).ingest(["https://github.com/a/b?token=secret"])
        self.assertEqual(self.count("jobs"), 0)

    def test_wrong_historical_snapshot_is_rejected(self):
        _, _, job = self.ingest()
        sid = job["targets"][0]["result"]["scopes"][0]["snapshot_id"]
        result = IngestService(self.store, transport=OfflineTransport()).ingest(
            ["example/vectorkit"], replay=True, snapshots={"unrelated/resource": sid})
        self.assertEqual(result["targets"][0]["error"]["code"], "invalid_input")

    def test_size_limit_and_safe_redirect(self):
        class FakeResponse:
            status = 200
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, size):
                return b"x" * size

        transport = HTTPTransport(max_bytes=10)
        with patch.object(transport.opener, "open", return_value=FakeResponse()):
            with self.assertRaisesRegex(DevosError, "size limit"):
                transport.get(REPO_URL, {})
        from devos.runtime import SafeRedirect
        from urllib.request import Request
        with self.assertRaises(DevosError):
            SafeRedirect().redirect_request(Request(REPO_URL), None, 302, "", {}, "https://other.test/secret")

    def test_interrupted_response_is_retryable(self):
        class InterruptedResponse:
            status = 200
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, size):
                raise TimeoutError()

        transport = HTTPTransport()
        with patch.object(transport.opener, "open", return_value=InterruptedResponse()):
            with self.assertRaises(DevosError) as caught:
                transport.get(REPO_URL, {})
        self.assertEqual(caught.exception.code, "transient_network")
        self.assertIsNotNone(caught.exception.retry_at)

    def test_pruned_snapshot_is_unavailable_for_replay(self):
        _, _, job = self.ingest()
        sid = job["targets"][0]["result"]["scopes"][0]["snapshot_id"]
        with self.store.transaction():
            self.store.execute("UPDATE snapshots SET availability='pruned' WHERE id=?", (sid,))
        with self.assertRaises(DevosError):
            self.store.snapshot(sid)

    def test_backup_and_newer_schema_guard(self):
        entity = self.vault.add("note", "Backup me")
        destination = Path(self.tmp.name) / "backup.sqlite3"
        self.store.backup(destination)
        backup = SQLiteStore(destination)
        self.assertEqual(backup.entity(entity["id"])["values"]["name"], "Backup me")
        backup.execute("PRAGMA user_version=999")
        backup.close()
        with self.assertRaisesRegex(DevosError, "newer"):
            SQLiteStore(destination)

    def test_abrupt_interruption_resumes_from_saved_snapshots_without_network(self):
        service = IngestService(self.store, transport=FakeTransport({REPO_URL: response(repo()), REPO_URL + "/readme": response(readme())}))
        service.before_commit = lambda: (_ for _ in ()).throw(SystemExit("simulated process exit"))
        with self.assertRaises(SystemExit):
            service.ingest(["example/vectorkit"])
        self.assertEqual(self.count("entities"), 0)
        self.assertEqual(self.count("attempt_snapshots"), 2)
        job_id = self.store.one("SELECT id FROM jobs")["id"]
        self.store.execute("UPDATE job_targets SET lease_expires=0")
        resumed = IngestService(self.store, transport=OfflineTransport()).resume(job_id)
        self.assertEqual(resumed["state"], "complete", resumed)
        self.assertEqual(self.count("attempts", "state='interrupted'"), 1)

    def test_cancellation_rolls_back_current_target_and_skips_pending_targets(self):
        service = IngestService(self.store, transport=FakeTransport({REPO_URL: response(repo()), REPO_URL + "/readme": response(readme())}))
        service.before_commit = lambda: (_ for _ in ()).throw(KeyboardInterrupt())
        job = service.ingest(["example/vectorkit", "another/repo"])
        self.assertEqual(job["state"], "canceled", job)
        self.assertEqual(self.count("entities"), 0)

    def test_expired_lease_cannot_commit_even_without_another_worker(self):
        service = IngestService(self.store, transport=FakeTransport({REPO_URL: response(repo()), REPO_URL + "/readme": response(readme())}))
        service.before_commit = lambda: setattr(self.store, "clock", lambda: time.time() + 400)
        job = service.ingest(["example/vectorkit"])
        self.assertEqual(self.count("entities"), 0)
        self.assertEqual(job["targets"][0]["state"], "running")
