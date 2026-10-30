# Milestone 1: a useful CLI vault

## Outcome

A user can create a local vault, manually record knowledge, ingest a public GitHub repository, inspect the evidence, search it offline, connect entities, and export the result. This validates source replay and identity boundaries before a web interface or advanced analytics.

## Scope and implementation order

1. Build core records and type/relation validation, versioned SQLite migrations, repositories, and unit-of-work transactions.
2. Add manual entity/claim/relationship services and inspectable provenance; expose a small CLI with human and JSON output.
3. Add raw snapshots and durable foreground jobs, ownership leases, idempotent commit, interruption/resume, and typed errors.
4. Implement the GitHub API adapter for repository metadata and README, provider-ID identity, retained-cache fallback, and offline re-extraction.
5. Add FTS keyword search, bounded one-hop relation listing, and versioned JSON export.
6. Package a CLI-only wheel and verify install and workflows against fixtures in a clean environment.

Proposed command surface: `init`, `add`, `show`, `edit`, `extract`, `reextract`, `search`, `link`, `export`, and `jobs show|resume`. Names and flags can be refined while maintaining the service contract. `show --provenance` exposes claims and their selected values. Multi-hop traversal is the next milestone.

## Acceptance criteria

| Scenario | Observable result |
| --- | --- |
| Empty setup | A new vault can be initialized without tokens, a server, Node, or separate database processes. |
| Manual entry | A note or registered entity can be added with no URL; its claims record manual provenance and survive restarts. |
| Identity repeat | Ingesting canonical and accepted equivalent URLs for the same GitHub provider ID produces one repository entity. |
| Identity conflict | Reused owner/name with a different provider ID produces a review item without corrupting the existing identity. |
| Artifact distinction | Manually linked repository/package/paper entities stay distinct after extraction and resolution. |
| Search | Names, descriptions, README text, and notes are searchable offline, with deterministic pagination/tie-breaking. |
| Explicit linking | A valid typed relation is stored once; invalid endpoint types are rejected before commit. |
| Evidence | An extracted description or link can be traced to source URL, snapshot hash, locator, attempt, and extractor version. |
| User correction | A manual description override remains the displayed value after a later upstream refresh; both claims are inspectable. |
| Repeat/re-extract | Repeating ingestion or offline re-extraction preserves entity/edge/current-claim counts for unchanged output; new attempts can be audited. |
| Partial source | Missing/failed README output preserves earlier README claims while fresh metadata is updated. |
| Removed source fact | A successfully complete scope supersedes absent claims only from that source/producer/scope; other sources/manual claims survive. |
| Offline fallback | With networking disabled, retained data supports search/show/edit/export/re-extract; cache use exposes original observation time. |
| Rate limiting | A simulated rate-limit response schedules a bounded retry respecting provider headers, does not erase data, and does not abort unrelated targets. |
| Interrupted commit | Simulated interruption before commit leaves no partial graph update; after commit, resume recognizes the durable result. |
| Worker ownership | After lease reclamation, the old worker cannot commit or mark a target successful. |
| Concurrent refresh/replay | An older snapshot processed later cannot replace newer displayed source claims; concurrent promotions check source-scope generation as well as job ownership. |
| Batch isolation | One malformed/unavailable target yields a partial job while another target commits successfully. |
| Export | Versioned JSON contains stable IDs, relationships, claims, and provenance; optional retained raw content is included only when requested. |
| Credential hygiene | Logs, snapshots, fixtures, and export contain no authorization headers or credential values. |
| Installation | The built wheel installs and the documented manual/fixture workflow works in a clean Python environment with no live API requirement. |

## Verification strategy

Unit tests cover provider URL normalization, identifier compatibility, claim reconciliation, manual override rules, parsers, and typed relation validation. Real SQLite integration tests cover uniqueness, foreign keys, FTS projection consistency, transaction rollback, and worker ownership. Recorded HTTP fixtures cover fresh content, 304 revalidation, stale cache, quota cooldown, access failure, and malformed data. CLI smoke tests cover JSON output, meaningful nonzero failure exits, and the packaged workflow.

Document the supported Python/OS matrix and exact dependency versions during implementation. CI runs the applicable lint/type checks, tests, and wheel build for that matrix. Optional live-provider smoke tests are separate and never gate deterministic fixture tests.

## Deferred

No web UI, HTTP server, daemon/scheduler, automatic dependency/citation inference, automatic fuzzy merge, remote LLM, embeddings, PageRank, community detection, multi-machine workers, message broker, Redis, MongoDB, or native graph database is required for this milestone. JSON import and merge undo have separate later acceptance criteria. This scope decision preserves the extension ports needed to add those features when justified.
