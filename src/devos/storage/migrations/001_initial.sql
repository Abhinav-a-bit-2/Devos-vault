CREATE TABLE entity_types (type_key TEXT PRIMARY KEY, schema_version INTEGER NOT NULL);
CREATE TABLE relation_types (relation_key TEXT PRIMARY KEY, policy TEXT NOT NULL CHECK(json_valid(policy)));
CREATE TABLE entities (
  id TEXT PRIMARY KEY, type_key TEXT NOT NULL REFERENCES entity_types(type_key),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  merged_into_id TEXT REFERENCES entities(id) CHECK(merged_into_id IS NULL OR merged_into_id <> id)
);
CREATE TABLE identifiers (
  id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entities(id),
  namespace TEXT NOT NULL, normalized_value TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('authoritative','alias')), created_at TEXT NOT NULL,
  UNIQUE(namespace,normalized_value)
);
CREATE TABLE sources (
  id TEXT PRIMARY KEY, adapter_key TEXT NOT NULL, resource_key TEXT NOT NULL,
  source_url TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(adapter_key,resource_key)
);
CREATE TABLE artifacts (sha256 TEXT PRIMARY KEY, body BLOB NOT NULL, byte_length INTEGER NOT NULL CHECK(byte_length=length(body)));
CREATE TABLE snapshots (
  id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
  artifact_hash TEXT REFERENCES artifacts(sha256), observed_at TEXT NOT NULL,
  retrieved_at TEXT NOT NULL, status INTEGER NOT NULL, content_type TEXT NOT NULL,
  etag TEXT, last_modified TEXT, cache_scope TEXT NOT NULL,
  availability TEXT NOT NULL CHECK(availability IN ('available','pruned'))
);
CREATE INDEX snapshot_lookup ON snapshots(source_id,cache_scope,retrieved_at DESC,id);
CREATE TABLE jobs (
  id TEXT PRIMARY KEY, mode TEXT NOT NULL CHECK(mode IN ('fetch','replay')),
  config_json TEXT NOT NULL CHECK(json_valid(config_json)), state TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE job_targets (
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), input TEXT NOT NULL,
  state TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
  lease_token TEXT, lease_expires REAL, next_attempt_at REAL, error_json TEXT,
  result_json TEXT, UNIQUE(job_id,input)
);
CREATE INDEX job_eligibility ON job_targets(state,next_attempt_at,lease_expires);
CREATE TABLE attempts (
  id TEXT PRIMARY KEY, job_target_id TEXT NOT NULL REFERENCES job_targets(id), number INTEGER NOT NULL,
  lease_token TEXT NOT NULL, producer_key TEXT NOT NULL, producer_version TEXT NOT NULL,
  config_fingerprint TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT, state TEXT NOT NULL,
  coverage_json TEXT CHECK(coverage_json IS NULL OR json_valid(coverage_json)),
  UNIQUE(job_target_id,number)
);
CREATE TABLE attempt_snapshots (
  attempt_id TEXT NOT NULL REFERENCES attempts(id), resource_key TEXT NOT NULL,
  snapshot_id TEXT NOT NULL REFERENCES snapshots(id), freshness TEXT NOT NULL,
  PRIMARY KEY(attempt_id,resource_key)
);
CREATE TABLE edges (
  id TEXT PRIMARY KEY, subject_id TEXT NOT NULL REFERENCES entities(id),
  relation_key TEXT NOT NULL REFERENCES relation_types(relation_key),
  object_id TEXT NOT NULL REFERENCES entities(id), qualifiers_json TEXT NOT NULL CHECK(json_valid(qualifiers_json)),
  qualifier_hash TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(subject_id,relation_key,object_id,qualifier_hash)
);
CREATE INDEX edges_reverse ON edges(object_id,relation_key,subject_id);
CREATE TABLE claims (
  id TEXT PRIMARY KEY, claim_key TEXT NOT NULL UNIQUE,
  entity_id TEXT REFERENCES entities(id), edge_id TEXT REFERENCES edges(id),
  predicate TEXT NOT NULL, value_json TEXT NOT NULL CHECK(json_valid(value_json)),
  origin TEXT NOT NULL CHECK(origin IN ('manual','extracted','inferred')),
  source_id TEXT REFERENCES sources(id), snapshot_id TEXT REFERENCES snapshots(id),
  attempt_id TEXT REFERENCES attempts(id), actor TEXT, producer_key TEXT NOT NULL,
  producer_version TEXT NOT NULL, config_fingerprint TEXT NOT NULL, scope TEXT NOT NULL,
  confidence REAL CHECK(confidence IS NULL OR (confidence>=0 AND confidence<=1)),
  state TEXT NOT NULL CHECK(state IN ('active','superseded','historical','retracted')),
  first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
  CHECK((entity_id IS NOT NULL)+(edge_id IS NOT NULL)=1),
  CHECK((origin='manual' AND actor IS NOT NULL) OR
        (origin<>'manual' AND source_id IS NOT NULL AND snapshot_id IS NOT NULL AND attempt_id IS NOT NULL))
);
CREATE INDEX claims_entity ON claims(entity_id,state,predicate);
CREATE INDEX claims_source ON claims(source_id,producer_key,scope,state);
CREATE INDEX claims_edge ON claims(edge_id,state);
CREATE TABLE claim_observations (
  claim_id TEXT NOT NULL REFERENCES claims(id), attempt_id TEXT NOT NULL REFERENCES attempts(id),
  snapshot_id TEXT NOT NULL REFERENCES snapshots(id), locator_json TEXT NOT NULL CHECK(json_valid(locator_json)),
  observed_at TEXT NOT NULL, PRIMARY KEY(claim_id,attempt_id,snapshot_id)
);
CREATE TABLE scope_heads (
  source_id TEXT NOT NULL REFERENCES sources(id), producer_key TEXT NOT NULL, scope TEXT NOT NULL,
  snapshot_id TEXT NOT NULL REFERENCES snapshots(id), attempt_id TEXT NOT NULL REFERENCES attempts(id),
  generation INTEGER NOT NULL, PRIMARY KEY(source_id,producer_key,scope)
);
CREATE TABLE review_items (
  id TEXT PRIMARY KEY, candidate_key TEXT NOT NULL UNIQUE, reason TEXT NOT NULL,
  evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json)), state TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL
);
CREATE TABLE merge_events (
  id TEXT PRIMARY KEY, surviving_id TEXT NOT NULL REFERENCES entities(id),
  redirected_id TEXT NOT NULL REFERENCES entities(id), actor TEXT NOT NULL,
  reason TEXT NOT NULL, created_at TEXT NOT NULL, mapping_json TEXT NOT NULL CHECK(json_valid(mapping_json))
);
CREATE TABLE job_events (
  job_id TEXT NOT NULL REFERENCES jobs(id), sequence INTEGER NOT NULL, target_id TEXT REFERENCES job_targets(id),
  time TEXT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
  PRIMARY KEY(job_id,sequence)
);
CREATE TABLE entity_views (
  entity_id TEXT PRIMARY KEY REFERENCES entities(id), name TEXT NOT NULL, description TEXT NOT NULL,
  canonical_url TEXT, metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json)),
  values_json TEXT NOT NULL CHECK(json_valid(values_json)), claim_ids_json TEXT NOT NULL CHECK(json_valid(claim_ids_json)),
  policy_version INTEGER NOT NULL
);
CREATE VIRTUAL TABLE entity_search USING fts5(entity_id UNINDEXED,name,description,readme,body,tokenize='unicode61');
CREATE TABLE provider_state (
  quota_key TEXT PRIMARY KEY, next_request_at REAL NOT NULL DEFAULT 0,
  failures INTEGER NOT NULL DEFAULT 0, circuit_until REAL NOT NULL DEFAULT 0
);
