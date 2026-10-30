# Data model and identity

Status: implementation specification for the initial relational model. Use UUID strings for internal IDs, UTC timestamps, validated JSON, foreign keys, and schema-versioned migrations.

## Entity and relationship semantics

Initial registered types: `repository`, `package`, `tool`, `hf_model`, `hf_dataset`, `paper`, `note`, `topic`, and `collection`. The initial adapter creates repositories; manual entry can use registered types. A published package has ecosystem/name identity distinct from its source repository. A broad tool concept is explicit, rather than an automatic merge of its artifacts.

Initial relation definitions specify permitted endpoint types, direction, cardinality policy, and whether they participate in particular queries:

| Relation | Direction | Meaning |
| --- | --- | --- |
| `source_repository` | package/tool/model -> repository | Associated source code; not identity equivalence |
| `implements` | repository -> paper | An implementation claim supported by evidence |
| `depends_on` | consumer -> dependency | Declared dependency, with ecosystem/version/scope qualifiers |
| `fine_tuned_from` | derived model -> base model | Model lineage; not an assertion of vulnerability propagation |
| `cites` | citing entity -> cited paper | Citation, not an implementation claim |
| `part_of` | member -> collection | Explicit membership |
| `tagged_with` | entity -> topic | Explicit or inferred topic assignment |
| `mentions` | entity -> entity | Reference whose stronger semantics are unestablished |
| `similar_to` | canonical ordered pair | Symmetric derived similarity; stored once with deterministic endpoint order |

The general graph may contain cycles. Type-specific validation can flag suspicious lineage without rejecting all cyclic graph data. Inferred edges carry supporting claims; confidence belongs to the claim, not to a supposedly universal edge truth.

## Logical tables

`PK` denotes primary key; all `*_id` references below are foreign keys unless explicitly described as external/local identifiers.

| Table | Required fields and constraints |
| --- | --- |
| `entity_types` | `type_key PK`, `schema_version`, metadata schema/validation definition; built-ins registered by migrations |
| `relation_types` | `relation_key PK`, endpoint policy, symmetric flag, qualifier schema/version |
| `entities` | `id PK`, `type_key`, `created_at`, `updated_at`, nullable `merged_into_id`; reject merge-reference cycles |
| `identifiers` | `id PK`, `entity_id`, `namespace`, `normalized_value`, `kind`, `created_at`; UNIQUE(namespace, normalized_value); active authoritative identities and accepted aliases only |
| `sources` | `id PK`, `adapter_key`, `resource_key`, sanitized `source_url`, `created_at`; UNIQUE(adapter_key, resource_key) |
| `artifacts` | `sha256 PK`, `body BLOB`, `byte_length`; immutable bytes with a checked length and hash |
| `snapshots` | `id PK`, `source_id`, nullable artifact hash, `observed_at`, `retrieved_at`, response status, content type, safe ETag/Last-Modified, `cache_scope`, availability status; a failed response cannot masquerade as a fresh artifact |
| `jobs` | `id PK`, input/mode/config fingerprint, state, timestamps, nullable lease owner/token/expiry, `next_attempt_at`, sanitized terminal error |
| `job_targets` | `id PK`, `job_id`, normalized target key, state, attempt count, selected snapshot, committed attempt; UNIQUE(job_id, target key) |
| `attempts` | `id PK`, `job_target_id`, attempt number, lease token, extractor key/version, config fingerprint, start/end, state, coverage manifest, sanitized errors; UNIQUE(job_target_id, attempt number) |
| `attempt_snapshots` | `attempt_id`, resource key, `snapshot_id`, freshness; primary key on attempt/resource; permits replay after interruption before graph commit |
| `edges` | `id PK`, subject entity, relation key, object entity, canonical qualifier JSON/hash, `created_at`; UNIQUE(subject, relation, object, qualifier hash) |
| `claims` | `id PK`, exactly one entity/edge subject FK, predicate, canonical value JSON/hash, `origin`, nullable source/snapshot/attempt references, producer key/version/config fingerprint, extraction scope, confidence, state, first/last seen; source claims require provenance; manual claims require actor/action provenance |
| `claim_observations` | `claim_id`, `attempt_id`, `snapshot_id`, locator JSON, `observed_at`; UNIQUE(claim_id, attempt_id, snapshot_id); multiple spans within locator JSON |
| `scope_heads` | `source_id`, producer family, extraction scope, current snapshot/attempt, generation; composite primary key on source/producer/scope; promotion uses compare-and-swap generation checks |
| `review_items` | `id PK`, candidate entities or incoming identity description, reason/evidence, state, decision/actor/time, nullable resulting canonical ID; stable candidate key prevents duplicate open items |
| `merge_events` | `id PK`, surviving/redirected entity IDs, actor, reason, time, before/after identity and claim mapping; preserves merge audit history |
| `job_events` | `job_id`, monotonic sequence, time, target/stage/status, safe payload; PRIMARY KEY(job_id, sequence) |
| `entity_views` | `entity_id PK`, selected name/description/canonical URL/metadata, contributing claim IDs, projection policy version; rebuildable |
| `entity_search` | FTS5 projection of resolved names, descriptions, README text, and notes; entity ID maps to the corresponding view |

The SQL migration will translate XOR subjects, conditional provenance rules, JSON validation, enum values, uniqueness, and foreign keys into constraints wherever possible. The service enforces constraints that depend on type-registry semantics and canonical JSON. Index identifiers, active claims by subject/predicate/source/scope, job eligibility, and edges in both traversal directions. FTS/projection updates share the graph transaction.

## Claim identity and resolution

A source claim key consists of subject, predicate, canonical value hash, source, origin, producer family, and extraction scope. Use separate partial uniqueness rules for manual claims, whose source may be null; do not rely on nullable columns in a generic SQLite UNIQUE constraint to deduplicate them. Repeated observations update `last_seen_at` and append observation evidence; extractor versions/configurations are recorded on attempts and observations so earlier evidence stays attributable. A changed value produces another claim rather than mutating the old evidence.

For example, a GitHub snapshot can support `repository.description = "upstream text"`, while a manual action supports a different description. Both survive. A deterministic projection chooses an explicit manual override first; otherwise it selects active source claims using the predicate's authority policy, freshness, and stable tie-breaker. Confidence scores are not comparable probabilities across extractors unless calibrated. Conflicts remain inspectable.

When an extractor reports a scope as complete, reconcile that source/producer/scope: previously active claims absent from the successful result become superseded. A partial, failed, cached-stale, or omitted scope never retracts earlier claims. A claim is retracted by its provenance owner or an explicit user action, not because a different source disagrees. Changing producer versions still reconciles the producer family after complete output. Unreferenced edge shells are omitted from current graph views while historical claims remain.

Record promotion separately from processing completion. Each scope head identifies the snapshot and extraction generation currently eligible for display. Replaying an older snapshot records historical derivations without replacing a newer scope head or superseding newer claims. Re-extracting the current retained snapshot can advance its extraction generation after a complete explicit replay; this does not advance source observation time. Concurrent jobs recheck the head generation and snapshot ordering inside the commit transaction. A job lease alone does not protect against two different jobs updating the same source scope.

## Identity rules

1. Match trusted provider IDs first, scoped to provider host and entity type. A GitHub repository ID, not just its mutable owner/name URL, establishes repository identity.
2. Match previously accepted URL aliases only within compatible types. URL normalization is provider-specific: scheme/host normalization and recognized tracking parameters are safe under defined rules; arbitrary path/query rewriting is not.
3. A new fetched provider ID conflicting with an old URL alias creates a review item; it must not merge a recreated/transferred name into the old object automatically.
4. A repository README linking a paper supports `mentions` or an evidenced stronger relation. It does not make the repository an alias of the paper. An HF model and repository remain separate even with matching neighborhoods.
5. Fuzzy names, shared emails, contributors, and graph similarity generate candidates only. Exact email alone is insufficient identity proof.
6. Ambiguity is non-destructive: preserve an unresolved candidate or distinct incoming entity and open review with evidence. A manual merge redirects the old ID, preserves claims/aliases, deduplicates redirected edges, and records a merge event in one transaction. Full merge undo is deferred; no automated fuzzy merges are allowed initially.

An entity may have no URL or external ID, as with a local note. A canonical URL is selected from supported identifier claims and is not the entity's primary key.

## Snapshot lifetime and portability

Identical bodies reuse artifact bytes; observations may create multiple snapshot records. A 304 response points to the previously available artifact and records successful revalidation. A cache hit records the original observation time and current retrieval time separately. Cache keys partition credential visibility using a nonsecret credential reference and access context, so public/private responses are not accidentally mixed. Changing access invalidates that partition.

Raw content retention is configurable and bounded. Never silently prune artifacts pinned by active jobs. Pruning marks replay unavailable while retaining a content hash and provenance record; claims can remain useful even when original bytes are gone. Failed, unauthorized, or deleted-source responses are retrieval outcomes, not permission to erase manual data.

Versioned JSON export includes entities, identifiers, edges, claims, observations, sources, review decisions, and schema/projection versions. An explicit `--include-raw` embeds retained artifacts and snapshot metadata for replay; otherwise export records hashes and availability without promising raw-content recovery. Jobs, credentials, caches, and FTS tables are excluded. Later import must preserve IDs and claims and rebuild projections. Initial JSON export is inspectable data portability; round-trip restoration is a later acceptance requirement.
