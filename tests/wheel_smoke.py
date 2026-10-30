"""Run from an installed wheel, without source imports or live HTTP."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def run() -> None:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    with tempfile.TemporaryDirectory() as tmp:
        vault = Path(tmp) / "vault.sqlite3"

        def cli(*args):
            completed = subprocess.run([sys.executable, "-I", "-m", "devos", "--vault", str(vault), "--json", *args],
                                       check=True, capture_output=True, text=True, cwd=tmp, env=env)
            return json.loads(completed.stdout)

        assert cli("init")["status"] == "ready"
        entity = cli("add", "note", "Installed wheel", "--body", "offline package test")
        assert cli("search", "offline")[0]["id"] == entity["id"]
        assert cli("impact", entity["id"])["nodes"][0]["id"] == entity["id"]
        assert cli("show", entity["id"], "--provenance")["claims"]
        assert cli("export")["format_version"] == 1
        assert cli("init")["schema_version"] == 2
        # Isolated Python imports only the installed package. Migrations must be in the wheel.
        probe = subprocess.run([sys.executable, "-I", "-c", (
            "import tempfile,pathlib; from devos.storage import SQLiteStore; "
            "from devos.services import IngestService; from devos.ports import Response; "
            "from devos.adapters.dependencies import requirements; "
            "assert len(requirements(b'requests>=2'))==1; "
            "import json; p=pathlib.Path('fixture.sqlite3'); s=SQLiteStore(p,create=True); "
            "T=type('T',(),{'get':lambda self,url,headers: Response(200,{},json.dumps("
            "{'id':101,'full_name':'example/vectorkit','private':False} if not url.endswith('/readme') "
            "else {'encoding':'base64','content':'b2ZmbGluZSBmaXh0dXJl','path':'README.md'}).encode())}); "
            "j=IngestService(s,transport=T()).ingest(['example/vectorkit']); "
            "assert j['state']=='complete',j; "
            "j=IngestService(s,transport=T()).ingest(['example/vectorkit'],replay=True); "
            "assert j['state']=='complete',j; s.close()"
        )], cwd=tmp, env=env, capture_output=True, text=True)
        assert probe.returncode == 0, probe.stderr + probe.stdout
        web_probe = subprocess.run([sys.executable, "-I", "-c", (
            "from importlib.resources import files; from devos.web import create_app; "
            "from fastapi.testclient import TestClient; "
            "assert files('devos').joinpath('web_dist/index.html').is_file(); "
            "assert files('devos.storage').joinpath('migrations/002_provider_leases.sql').is_file(); "
            "app=create_app('web.sqlite3',run_worker=False); "
            "c=TestClient(app,base_url='http://localhost'); c.__enter__(); "
            "assert c.get('/').status_code==200; "
            "assert c.get('/api/overview').json()['entities']==0; c.__exit__(None,None,None)"
        )], cwd=tmp, env=env, capture_output=True, text=True)
        assert web_probe.returncode == 0, web_probe.stderr + web_probe.stdout
    print("Installed wheel: CLI, provider fixture, replay, migrations, and web API/assets passed.")


if __name__ == "__main__":
    run()
