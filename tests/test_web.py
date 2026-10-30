import json
import time
import unittest

try:
    from fastapi.testclient import TestClient
    from devos.web import create_app
    WEB_AVAILABLE = True
except ImportError:
    WEB_AVAILABLE = False

from tests.helpers import FakeTransport, VaultCase
from tests.test_providers import provider_routes

HEADERS = {"X-Devos-Request": "1", "Content-Type": "application/json"}


@unittest.skipUnless(WEB_AVAILABLE, "Install the web/dev extras for API tests")
class WebTests(VaultCase):
    def client(self, worker=False):
        return TestClient(create_app(self.path, transport=FakeTransport(provider_routes()), run_worker=worker), base_url="http://localhost")

    def test_api_crud_search_graph_and_export_use_shared_vault(self):
        with self.client() as client:
            self.assertEqual(client.get("/").status_code, 200)
            created = client.post("/api/entities", json={"type": "note", "name": "Web note", "body": "browser evidence"}, headers=HEADERS)
            self.assertEqual(created.status_code, 201, created.text)
            entity_id = created.json()["id"]
            self.assertEqual(self.vault.search("browser")[0]["id"], entity_id)
            edit = client.patch(f"/api/entities/{entity_id}", json={"description": "Manual override"}, headers=HEADERS)
            self.assertEqual(edit.status_code, 200, edit.text)
            self.assertEqual(client.get("/api/entities?q=browser").json()["total"], 1)
            detail = client.get(f"/api/entities/{entity_id}").json()
            self.assertTrue(detail["claims"])
            self.assertEqual(client.get("/api/graph").json()["nodes"][0]["id"], entity_id)
            self.assertEqual(client.get(f"/api/impact/{entity_id}").json()["nodes"][0]["id"], entity_id)
            self.assertEqual(client.get(f"/api/impact/{entity_id}?depth=7").status_code, 422)
            repository = client.post("/api/entities", json={"type": "repository", "name": "Web repository"}, headers=HEADERS).json()
            package = client.post("/api/entities", json={"type": "package", "name": "Web package"}, headers=HEADERS).json()
            linked = client.post("/api/links", json={"subject": repository["id"], "relation": "depends_on",
                                                       "object": package["id"], "qualifiers": {"scope": "runtime"}}, headers=HEADERS)
            self.assertEqual(json.loads(linked.json()["qualifiers_json"])["scope"], "runtime")
            self.assertEqual(client.get(f"/api/impact/{package['id']}").json()["nodes"][1]["id"], repository["id"])
            export = client.get("/api/export")
            self.assertIn("attachment", export.headers["content-disposition"])
            self.assertEqual(export.json()["schema_version"], 2)

    def test_cross_origin_requests_and_dns_rebinding_are_rejected(self):
        with self.client() as client:
            body = {"type": "note", "name": "Blocked"}
            self.assertEqual(client.post("/api/entities", json=body).status_code, 403)
            self.assertEqual(client.post("/api/entities", json=body, headers={**HEADERS, "Origin": "https://evil.test"}).status_code, 403)
            self.assertEqual(client.get("/api/overview", headers={"Host": "evil.test"}).status_code, 400)
            self.assertEqual(client.get("/api/overview", headers={"Sec-Fetch-Site": "cross-site"}).status_code, 403)
            self.assertEqual(client.post("/api/entities", content='x' * 1_000_001, headers=HEADERS).status_code, 413)
        self.assertEqual(self.count("entities"), 0)

    def test_background_ingestion_and_sse_progress_are_durable(self):
        with self.client(worker=True) as client:
            created = client.post("/api/jobs", json={"inputs": ["hf:example/research-model"]}, headers=HEADERS)
            self.assertEqual(created.status_code, 202, created.text)
            job_id = created.json()["id"]
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                job = client.get(f"/api/jobs/{job_id}").json()
                if job["state"] in {"complete", "failed"}:
                    break
                time.sleep(0.05)
            self.assertEqual(job["state"], "complete", job)
            self.assertGreater(client.get("/api/overview").json()["relations"], 0)
            events = client.get(f"/api/jobs/{job_id}/events").text
            self.assertIn("event: progress", events)
            self.assertIn("event: done", events)
            cursor = job["events"][-1]["sequence"]
            resumed = client.get(f"/api/jobs/{job_id}/events", headers={"Last-Event-ID": str(cursor)}).text
            self.assertNotIn("event: progress", resumed)
            self.assertIn("event: done", resumed)

    def test_cancel_pending_job_and_api_validation(self):
        with self.client() as client:
            job_id = client.post("/api/jobs", json={"inputs": ["arxiv:1706.03762"]}, headers=HEADERS).json()["id"]
            canceled = client.post(f"/api/jobs/{job_id}/cancel", json={}, headers=HEADERS)
            self.assertEqual(canceled.json()["state"], "canceled")
            self.assertEqual(client.post("/api/jobs", json={"inputs": []}, headers=HEADERS).status_code, 422)
            self.assertEqual(client.get("/api/entities/not-found").status_code, 404)
            schema = client.get("/openapi.json").json()
            checked_in = json.loads((__import__('pathlib').Path(__file__).parents[1] / "web/openapi.json").read_text())
            self.assertEqual(schema, checked_in)
