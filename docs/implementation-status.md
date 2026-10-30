# Implementation status

Version 0.3 adds direct dependency extraction and downstream traversal to the local web app and public GitHub, arXiv, and Hugging Face ingestion. The source remains a local package, not a published service.

## Available now

- Schema 2 adds a provider request lease for pacing across processes. Opening a schema 1 vault creates a consistent backup before an atomic migration. The original schema migration remains unchanged.
- GitHub repository metadata and README, arXiv Atom metadata and abstracts, and Hugging Face public model/dataset metadata and cards are retained as snapshots. Fetches are bounded, HTTPS provider hosts are restricted, redirects checked, and tokens isolated by provider.
- Explicit provider references become typed, evidenced edges. arXiv IDs and Hugging Face slugs can point to referenced items, which gain their own source claims when imported. Manual overrides and manual links survive source refresh.
- GitHub root `requirements.txt` and `package.json` declarations create direct package dependencies with source locators, ecosystem, scope, and version specifier. Complete successful manifest refreshes supersede removed links; a failed optional manifest retains its prior scope.
- A bounded reverse traversal follows active `depends_on`, `fine_tuned_from`, and `derived_from` edges, with cycle protection, depth and item limits, and a downstream panel in entity details. The same result is available by CLI and API.
- Library search, entity details, evidence, graph neighborhoods, manual notes and links, imports, and job activity are available in a loopback-only web app. The app is built into the Python wheel and uses the same vault services as the CLI.
- Browser imports enqueue durable jobs. A local worker resumes pending and due retries, with ownership checks and cancel support. CLI imports still run in the foreground. Offline replay, provenance, reviews, export, and backup continue to work.
- Fixture tests cover provider parsing, reference identity, refresh behavior, credential isolation, migration, API security, background completion, and the built web surface. No live network is used by the default suite.

## Boundaries and next work

- Text links are conservative: known GitHub and Hugging Face URLs become `mentions`, arXiv IDs become `cites`. Hugging Face `base_model` yields `fine_tuned_from` only when its relation is explicitly `finetune`; otherwise it yields `derived_from`. No fuzzy identity merging or inferred dependency graph is performed.
- Only public repositories, papers, models, and datasets are imported. PDFs, model weights, and dataset files are not downloaded. The arXiv adapter reads an Atom entry, not the full paper bibliography. An arbitrary web URL is not crawled.
- Graph exploration and downstream impact are bounded and local to SQLite; PageRank, community detection, and vulnerability propagation semantics are not implemented. A queued single-process worker is sufficient for this deployment; Kafka, MongoDB, Neo4j, and Redis remain optional future choices at demonstrated scale.
- Dependency extraction covers only direct root requirements and npm manifest fields. It skips includes, VCS/URL/local references, lockfiles, workspace packages, and nested manifests. It does not resolve transitive package versions or assert that a vulnerability affects a downstream item. GitLab and scheduled refresh remain future stages.
- The storage port still exposes SQL to services. A different storage backend would need semantic repository operations and query-equivalence tests. JSON import, conflict resolution, retention controls, dynamic entity-type plugins, and multi-user hosting remain future work.

Raw snapshots and exports may include sensitive source text. Cache partitions change when token values change; permission changes to an unchanged token cannot be discovered offline. Browser mutation requests require same-origin context and a custom header, and the web server binds only to loopback.
