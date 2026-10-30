# Adapter and pipeline contract

Status: proposed version 1 contract for implementation. This document defines behavior and records; it does not imply an existing SDK.

## Ownership

Adapters own provider-specific input recognition, target discovery, fetch strategy selection, parsing, and mapping to canonical candidates. The shared pipeline owns identity decisions, general relation validation, persistence, retries, and job state. Shared transport owns request execution and policy enforcement. Adapters have no database handle and cannot write canonical entities directly.

Parsing and normalization operate on immutable snapshot bytes and explicit configuration. They must not make hidden network calls, read credentials, use wall-clock time for semantic results, or assign final entity IDs. Extra fetches are explicit bounded targets with provenance.

## Required records and operations

| Record/operation | Required contents or behavior |
| --- | --- |
| `AdapterDescriptor` | Unique key, package/adapter version, supported contract major, supported inputs/output types, capabilities, optional credential requirements, fetch strategy list, quota resources |
| `match(input)` | Pure and fast; returns no-match or score plus recognized input kind; no network/import-time registration side effects |
| `discover(input, context)` | Produces bounded `FetchTarget` records; declares if network is needed; uses injected transport for network discovery |
| `FetchTarget` | Adapter key, resource key, safe URL, strategy, credential reference, cache scope, quota resource, size/time limits; stable target key |
| `fetch(target, context)` | Uses injected transport and snapshot store; returns `FetchResult` with snapshot references, fresh/revalidated/cached/partial status, per-scope availability, next retry time, and sanitized diagnostics |
| `parse(snapshot, config)` | Returns provider-specific `ParsedDocument` plus evidence locators, warnings, parser version, and coverage; deterministic |
| `normalize(parsed, config)` | Returns canonical `CandidateBatch`: local entity keys, namespaced identifiers, claim/edge proposals, locators, and complete/partial/omitted scopes; deterministic |
| `resolve(batch, vault)` | Shared resolver maps local keys to stable IDs or review candidates; repeats uniqueness checks during commit |
| `link(batch, resolutions)` | Emits evidence-backed typed relationships; heuristics are separately labeled inferred claims |
| `persist(resolved_batch, unit_of_work)` | Shared service atomically commits graph/provenance/projections and target completion using an owned job lease |

Each proposal refers to a batch-local entity key or an existing validated ID, never a free-form database write. Evidence identifies source/snapshot, a locator (JSON pointer, filename/line range, or text span), producer/version, and origin (`manual`, `extracted`, `inferred`). Confidence must be in [0, 1] if supplied; unknown confidence is null, not zero.

Coverage is granular: `repository.metadata`, `repository.readme`, and future dependency manifests are distinct scopes. Successfully retrieving metadata while a README request fails cannot mark README extraction complete. Invalid candidates fail the target's validation before canonical writes; reviewable identity ambiguity is a normal result, not an adapter exception.

## Discovery and loading

Discover installed entry-point metadata under `devos.adapters`, plus the built-in registry. Load only a selected/enabled adapter, validate contract compatibility, and catch load/initialization failures so other sources still work. Reject duplicate keys deterministically with an actionable diagnostic. Explicit adapter selection wins; tied automatic matches produce an ambiguity error. Entry points are supported by Python's [package metadata API](https://docs.python.org/3/library/importlib.metadata.html).

No plugin may silently install additional code or execute downloaded repository content. Installed plugins are trusted Python code; initialization guards do not protect against arbitrary process termination or malicious plugins.

## Failures and fallbacks

Return typed failures: `unsupported_input`, `auth_required`, `access_denied`, `not_found`, `rate_limited`, `transient_network`, `invalid_payload`, `unsupported_schema`, and `plugin_unavailable`. Separate target failure from partial output and cached success. Retry only failures designated retryable, with bounded attempts, jitter, timeouts, and durable `next_attempt_at`.

Strategies are declared per adapter. For milestone 1, GitHub supports the official API, then retained permitted snapshots, then explicit manual entry. Scraping and clone-based fetches are future capabilities with their own budgets and extraction scopes. Manual entry is a user action; it is never fabricated as a successful automatic fetch.

An authentication failure does not silently downgrade a private request to unauthenticated public retrieval. A rate limit does not trigger a fresh identity or endpoint to evade the provider's limit. Respect `Retry-After` and provider reset information; distinguish quota/auth/access responses rather than treating every 403 as the same failure. GitHub documents both primary and secondary limits, so a token bucket alone is insufficient. [GitHub rate-limit guidance](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)

A circuit breaker tracks transient provider failures by provider/credential/quota scope. Exclude malformed user input and access-denied/not-found outcomes from availability failures. Half-open probes test recovery. A cooldown or open circuit may return a labeled cached snapshot; it cannot count as fresh evidence for claim removal.

## Replay and retry invariants

- Fetch time and source observation time remain separate from processing time.
- Re-extracting a retained snapshot works offline and records the selected producer version/configuration.
- Re-ingesting the same target does not duplicate identities, edges, or current claims. A new job/attempt/observation may still be recorded.
- Whole-entity atomicity is per fetch target; a batch can end partial.
- Retrying after a committed target returns the recorded result. Retrying before commit resumes from the retained snapshot when available.
- Canceled or expired workers cannot commit after losing lease ownership.
- A failure to persist cannot emit a successful target-completed event.
- Historical replay cannot promote an older source snapshot over a newer one; scope-head generation checks also protect against concurrent jobs for the same source.

Target states are `pending -> running -> committed|failed|canceled`; retryable failures move through `retry_wait -> running` when due, with a new attempt. An expired running attempt becomes interrupted before explicit resume creates its replacement. Parent jobs use `pending`, `running`, `retry_wait`, `complete`, `partial`, `failed`, and `canceled`; terminal summary records counts for each target outcome. Persist transitions with ownership checks and append the corresponding event in the same transaction.

## Required contract verification

Use recorded, sanitized fixtures; default tests make no live API calls. Verify input matching, deterministic parsing, coverage, candidate validation, auth and quota handling, malformed payloads, timeout/cancellation, replay, cache labeling, and plugin initialization failure. Run a shared contract suite against each adapter. Integration tests verify deduplication and transaction/lease behavior through the real SQLite implementation.
