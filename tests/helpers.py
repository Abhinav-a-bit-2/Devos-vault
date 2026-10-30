from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from devos.ports import Response
from devos.services import IngestService, VaultService
from devos.storage import SQLiteStore

FIXTURES = Path(__file__).parent / "fixtures"
REPO_URL = "https://api.github.com/repos/example/vectorkit"


def response(value=None, *, status=200, headers=None):
    return Response(status, headers or {"etag": '"fixture-v1"'},
                    json.dumps(value).encode() if value is not None else b"")


def repo(**changes):
    return {**json.loads((FIXTURES / "repository.json").read_text()), **changes}


def readme(text="Vector search for local vaults."):
    return {"path": "README.md", "encoding": "base64", "content": base64.b64encode(text.encode()).decode()}


class FakeTransport:
    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []

    def get(self, url, headers):
        self.calls.append((url, headers))
        result = self.routes.get(url, response(status=404))
        if isinstance(result, list):
            result = result.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class OfflineTransport:
    def get(self, url, headers):
        raise AssertionError("Offline operations attempted networking")


class VaultCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "vault.sqlite3"
        self.store = SQLiteStore(self.path, create=True)
        self.vault = VaultService(self.store)
        self.env = patch.dict("os.environ", {"GITHUB_TOKEN": ""})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.store.close()
        self.tmp.cleanup()

    def ingest(self, metadata=None, text="Vector search for local vaults.", **kwargs):
        transport = FakeTransport({
            REPO_URL: response(repo() if metadata is None else metadata),
            REPO_URL + "/readme": response(readme(text)),
        })
        job = IngestService(self.store, transport=transport).ingest(["example/vectorkit"], **kwargs)
        self.assertEqual(job["state"], "complete", job)
        return job["targets"][0]["result"]["entity_id"], transport, job

    def count(self, table, where="1=1"):
        return self.store.one(f"SELECT count(*) AS n FROM {table} WHERE {where}")["n"]
