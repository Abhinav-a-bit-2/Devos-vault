import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from devos.cli import main
from tests.helpers import VaultCase


class CLITests(VaultCase):
    def cli(self, *args, stdin=""):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors), patch("sys.stdin", io.StringIO(stdin)):
            code = main(["--vault", str(self.path), "--json", *args])
        return code, output.getvalue(), errors.getvalue()

    def test_manual_cli_workflow_and_export(self):
        code, text, errors = self.cli("add", "note", "CLI note", "--body", "searchable offline")
        self.assertEqual(code, 0, errors)
        entity = json.loads(text)
        code, text, errors = self.cli("edit", entity["id"], "--description", "Manual correction")
        self.assertEqual(code, 0, errors)
        code, text, errors = self.cli("search", "offline")
        self.assertEqual(json.loads(text)[0]["id"], entity["id"])
        code, text, errors = self.cli("show", entity["id"], "--provenance")
        self.assertTrue(json.loads(text)["claims"])
        destination = Path(self.tmp.name) / "export.json"
        code, _, errors = self.cli("export", "--output", str(destination))
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(destination.read_text())["format_version"], 1)
        code, _, errors = self.cli("export", "--output", str(destination))
        self.assertEqual(code, 1)
        self.assertIn("already exists", errors)

    def test_json_failures_have_nonzero_exit(self):
        for args in (("show", "missing"), ("edit", "missing"), ("search", "x", "--limit", "0")):
            code, output, error = self.cli(*args)
            self.assertEqual(code, 1)
            self.assertEqual(output, "")
            self.assertIn("code", json.loads(error)["error"])

    def test_pipeline_stdin_offline_jobs_and_reviews(self):
        entity_id, _, job = self.ingest()
        code, output, errors = self.cli("reextract", "-", stdin="example/vectorkit\n\nexample/vectorkit\n")
        self.assertEqual(code, 0, errors)
        replay = json.loads(output)
        self.assertEqual(len(replay["targets"]), 1)
        self.assertEqual(replay["targets"][0]["result"]["entity_id"], entity_id)
        code, output, errors = self.cli("jobs", "show", job["id"])
        self.assertEqual(code, 0, errors)
        self.assertTrue(json.loads(output)["events"])
        self.assertEqual(self.cli("reviews")[0], 0)

    def test_partial_job_returns_exit_two(self):
        code, output, errors = self.cli("extract", "not-a-repository", "--offline")
        self.assertEqual(code, 2, errors)
        self.assertEqual(json.loads(output)["state"], "failed")

    def test_link_qualifiers_require_object(self):
        code, _, errors = self.cli("link", "a", "depends_on", "b", "--qualifiers", "[]")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(errors)["error"]["code"], "invalid_input")

    def test_subprocess_module_entrypoint(self):
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
        run = subprocess.run([sys.executable, "-m", "devos", "--vault", str(self.path), "--json", "add", "note", "Subprocess"],
                             capture_output=True, text=True, env=env, check=True)
        self.assertEqual(json.loads(run.stdout)["values"]["name"], "Subprocess")
