"""Manifest evidence and downstream traversal over real temporary vaults."""

from __future__ import annotations

import json

from devos.ports import Response
from devos.services import IngestService
from tests.helpers import REPO_URL, FakeTransport, VaultCase, readme, repo, response


def manifest(text: str) -> Response:
    return response(readme(text))


class DependencyTests(VaultCase):
    def import_repo(self, requirements: str, package_json: str) -> str:
        transport = FakeTransport({
            REPO_URL: response(repo()),
            REPO_URL + "/readme": response(readme()),
            REPO_URL + "/contents/requirements.txt": manifest(requirements),
            REPO_URL + "/contents/package.json": manifest(package_json),
        })
        job = IngestService(self.store, transport=transport).ingest(["example/vectorkit"])
        self.assertEqual(job["state"], "complete", job)
        return job["targets"][0]["result"]["entity_id"]

    def test_manifest_dependencies_are_typed_and_impact_traverses_in_reverse(self):
        repo_id = self.import_repo(
            "Requests[socks]>=2.30; python_version >= '3.11'\n-r other.txt\nhttps://bad.test/egg=x\n",
            json.dumps({"dependencies": {"@scope/widget": "^1.2", "local": "file:../local"},
                        "devDependencies": {"vite": "^7"}}),
        )
        py = self.store.one("SELECT entity_id FROM identifiers WHERE namespace='pypi' AND normalized_value='requests'")["entity_id"]
        npm = self.store.one("SELECT entity_id FROM identifiers WHERE namespace='npm' AND normalized_value='@scope/widget'")["entity_id"]
        self.assertIsNone(self.store.one("SELECT entity_id FROM identifiers WHERE namespace='npm' AND normalized_value='local'"))
        edges = self.store.all("SELECT e.relation_key,e.qualifiers_json FROM edges e JOIN claims c ON c.edge_id=e.id "
                               "WHERE e.subject_id=? AND c.state='active'", (repo_id,))
        self.assertEqual(len(edges), 3)
        self.assertEqual({json.loads(edge["qualifiers_json"])["scope"] for edge in edges}, {"runtime", "development"})
        tool = self.vault.add("tool", "Downstream tool")["id"]
        self.vault.link(tool, "depends_on", repo_id)
        self.vault.link(repo_id, "depends_on", tool)  # Cycles are data, not traversal loops.
        impact = self.vault.impact(py, depth=3)
        self.assertEqual({row["id"]: row["depth"] for row in impact["nodes"]},
                         {py: 0, repo_id: 1, tool: 2})
        self.assertTrue(all(edge["relation_key"] == "depends_on" for edge in impact["edges"]))
        self.assertTrue(self.vault.impact(py, depth=3, limit=2)["truncated"])
        self.assertEqual(self.vault.impact(npm, depth=1)["nodes"][1]["id"], repo_id)

    def test_refresh_retracts_complete_manifest_but_retains_failed_scope(self):
        repo_id = self.import_repo("requests==2.0", '{"dependencies":{"lodash":"^4"}}')
        self.assertEqual(len(self.vault.impact(repo_id)["nodes"]), 1)
        py = self.store.one("SELECT entity_id FROM identifiers WHERE namespace='pypi' AND normalized_value='requests'")["entity_id"]
        npm = self.store.one("SELECT entity_id FROM identifiers WHERE namespace='npm' AND normalized_value='lodash'")["entity_id"]
        self.assertEqual(len(self.vault.impact(py)["nodes"]), 2)
        self.import_repo("requests==2.0", "{bad json")
        self.assertEqual(len(self.vault.impact(npm)["nodes"]), 2)
        self.import_repo("# no direct requirements\n", "{}")
        self.assertEqual(len(self.vault.impact(py)["nodes"]), 1)
        self.assertEqual(len(self.vault.impact(npm)["nodes"]), 1)
        historical = self.store.one("SELECT count(*) AS n FROM claims c JOIN edges e ON e.id=c.edge_id "
                                    "WHERE e.subject_id=? AND e.object_id=? AND c.state='superseded'", (repo_id, py))
        self.assertGreater(historical["n"], 0)
