# Devos

Devos is a local knowledge vault for repositories, papers, models, datasets, and notes. It stores source snapshots and evidence in SQLite, connects explicit references across providers, and lets you browse the result in a local web app or CLI. Your vault stays on your machine.

## Run the web app

Requires Python 3.11+ with SQLite JSON and FTS5. The package is built locally and is not published to PyPI. Built web assets ship in the Python package, so Node is not needed to run it.

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install ".[web]"
.\.venv\Scripts\devos.exe --vault .devos/vault.sqlite3 serve
```

Open `http://127.0.0.1:8765/`. The server listens on loopback only. The first run creates a vault; an existing schema 1 vault is backed up and upgraded to schema 2. On macOS/Linux use `python3 -m venv .venv`, `.venv/bin/pip install '.[web]'`, and `.venv/bin/devos serve`. Set `DEVOS_VAULT` to choose a default path; otherwise Devos uses `~/.devos/vault.sqlite3`. `--vault` goes before `serve` or another command.

The web app provides a searchable library, evidence and claim inspection, a bounded graph explorer, downstream impact lists, manual notes and links, and background imports with durable progress and cancel controls. Import a source by pasting its URL or one of these short forms:

```text
https://github.com/owner/repository       (or owner/repository)
https://arxiv.org/abs/1706.03762          (or arxiv:1706.03762)
https://huggingface.co/owner/model         (or hf:owner/model)
https://huggingface.co/datasets/owner/data (or hf:datasets/owner/data)
```

Bare `owner/name` resolves to GitHub; use `hf:` or a Hugging Face URL for a model or dataset. Inputs and evidence remain provider-specific. A link discovered in a README, abstract, or model card creates a referenced item; import that item to fetch its full source metadata. Devos does not download arXiv PDFs, model weights, or dataset files. It does not interpret an arbitrary web page as a source. Links in text are `mentions` except paper citations, which are `cites`; a Hugging Face card's `base_model` and `datasets` fields create typed derivation and usage links.

GitHub also reads root `requirements.txt` and `package.json` when present. Direct registry package declarations create evidenced `depends_on` links with ecosystem, scope, and version specifier. Open a package, repository, tool, or model to see saved items downstream through dependency and model-lineage links. This is a bounded graph view, not a security vulnerability verdict. Manifests in subdirectories, lockfiles, and recursive dependency resolution are not yet supported.

GitHub accepts `GITHUB_TOKEN` and Hugging Face accepts `HF_TOKEN` from the process environment; public content works without tokens. Tokens and Authorization headers are not saved. Offline imports and replay use retained snapshots in the same credential context. arXiv requests are paced to at least three seconds apart across workers using the vault's provider state.

## CLI

The CLI works with no web extra installed:

```text
devos init
devos add note "Architecture" --body "Keep provenance for every fact"
devos extract arxiv:1706.03762
devos extract hf:owner/model
devos extract owner/repository
devos extract hf:datasets/owner/data --offline
devos reextract hf:owner/model
devos --json show ENTITY_ID --provenance
devos search "vector search" --type paper
devos impact PACKAGE_ID --depth 3
devos link MODEL_ID implements PAPER_ID
devos jobs list
devos jobs show JOB_ID
devos jobs resume JOB_ID
devos jobs cancel JOB_ID
devos reviews
devos export --output vault.json
devos export --include-raw --output vault-with-snapshots.json
devos backup vault-backup.sqlite3
```

`extract -` reads newline-separated inputs from stdin. CLI ingestion runs in the foreground; web imports run in a background worker. Both use the same storage and services. Manual values take precedence over extracted values, and every claim retains its source and locator. A complete refresh supersedes removed source facts and links; failed optional card/README fetches retain earlier claims for that scope. A raw snapshot can be reprocessed without another provider request. The web app exposes its API schema at `/openapi.json`.

Exports and backups never overwrite a destination. The JSON export is versioned, with raw bodies opt-in. Exported or manually entered data may contain sensitive source content; handle the vault and export accordingly. JSON import and interactive identity conflict resolution are still future work.

## Develop and verify

```powershell
python -m pip install -e ".[web,dev]"
python -m unittest discover -q
python -m ruff check src tests tools
python -m mypy src
cd web
npm ci
python ../tools/export_openapi.py
npm run api:generate
npm run build
cd ..
python -m pip wheel . --no-deps --wheel-dir dist
```

`npm run build` writes the packaged UI into `src/devos/web_dist`. The default test suite uses fixture transports and makes no live HTTP requests. CI tests Python 3.11–3.14 on Windows and Linux, builds the UI, and tests an installed wheel. The adapter contract is `devos.ports.Adapter`; external packages can register entry points under `devos.adapters` and are loaded only when selected explicitly.
