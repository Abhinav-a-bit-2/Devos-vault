# Milestone 2: source dependencies and downstream impact

Status: initial scope implemented in 0.3.0, 2026-10-30. This narrows the architecture roadmap's dependency/citation traversal stage to explicit evidence available from current providers. Web search, entity details, graph views, arXiv, and Hugging Face cross-linking were delivered in 0.2.0.

## User outcome

Import a public GitHub repository and see its direct registry package declarations as typed, attributable links. Open a dependency in the web app or run `devos impact ID` to find saved repositories, tools, and packages that depend on it, including indirect paths. Model lineage uses the same reverse traversal but keeps its distinct relation types.

## Acceptance criteria

1. The GitHub adapter reads root `requirements.txt` and `package.json` through its bounded provider transport. It does not execute scripts, install packages, or fetch package artifacts.
2. A direct declaration creates a `package` reference and `depends_on` edge. The package identifier is scoped to PyPI or npm; the edge records manifest path/line or JSON pointer, dependency scope, and declared version text. URL, VCS, local, workspace and include forms that need different identity rules are skipped.
3. A complete successful manifest refresh supersedes declarations no longer present in that scope. A failed or unavailable optional manifest does not retract its prior evidence. Existing manual links remain active. Replay can rebuild from retained manifest snapshots.
4. Reverse traversal follows only active dependency and model-lineage edges. It has visited-set cycle protection, configurable depth 1–6, item limit 1–200, at most 1,000 returned edges, and an explicit truncation flag. It never rewrites graph claims or treats a downstream path as a vulnerability verdict.
5. CLI, local API, and the current entity detail UI expose the same bounded result. Unit/integration tests cover parsing, provenance, refresh, cycles, caps, and API behavior.

## Deferred extensions

Nested manifests, lockfile resolution, package registry adapters, exact version resolution, GitLab, scheduled refresh, SCC diagnostics, semantic search, and graph analytics require separate acceptance criteria. The arXiv adapter currently extracts explicit links from metadata and abstracts, not citations from PDF bibliographies.
