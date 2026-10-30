# Review of infrastructure and graph recommendations

Reviewed 2026-10-26 against the accepted local-first architecture. Decisions below are project judgments; the linked primary sources establish algorithm/API behavior, not proof that a particular architecture will meet an unmeasured workload.

## Infrastructure

| Recommendation | Decision | Application to Devos |
| --- | --- | --- |
| Event-driven microservices as the default | Defer | Independent stages need explicit contracts and bounded concurrency. Separate services introduce deployment, delivery/replay, and partial-commit coordination before a distributed workload exists. Start with a modular monolith and durable jobs. |
| Kafka/RabbitMQ between every stage | Defer | Use stage records, job events, snapshots, and idempotent target commits. A future distributed worker system needs an outbox, consumer deduplication, ordering/backpressure policy, and operational monitoring. A broker alone does not establish resilience. |
| Retain raw responses for replay | Adopt now | Immutable artifacts and fetch observations in SQLite preserve input for parser/normalizer changes. Network refresh and offline re-extraction are separate operations. |
| MongoDB for raw content | Defer | Raw JSON, text, and BLOBs do not require another database. Keep a snapshot-storage port for later large-artifact storage. |
| Native graph database | Defer | Indexed relational edges and bounded traversal are the initial model. Revisit with representative latency/write-contention evidence and a migration plan. |
| Entry points and strategies | Adopt now | Discover metadata, load selected/enabled adapters, validate contracts, and use declared per-source strategies. Registry behavior is deterministic; loading an installed plugin executes trusted code. |
| Circuit breaker and rate limiting | Adopt now, locally | Bound concurrency and coordinate cooldowns within a vault; respect provider account/resource limits and response headers. Avoid treating auth/input errors as provider outages. |
| Redis-backed distributed token bucket | Defer | Useful only when quota coordination across worker hosts is required. Token buckets still need provider-specific cooldown and secondary-limit handling. |
| One Docker container per stage | Defer | A later all-in-one container may aid self-hosting. Stage containers and third-party adapter isolation require a versioned cross-process protocol, not merely an entry point. |

SQLite's documented emphasis on local storage supports our starting choice. It does not make a native graph database wrong for a future deployment. [SQLite use cases](https://www.sqlite.org/whentouse.html)

## Resolve: evidence before clustering

**Weakly connected components: do not use on the knowledge graph to merge entities.** WCC finds connectivity ignoring edge direction; it does not compare properties. Even in a separate fuzzy-match candidate graph, A matching B and B matching C does not prove A and C are identical. WCC may group candidates for review. Only already verified equivalence links can safely be collapsed transitively. Emails/aliases provide candidate evidence and person identity is outside the initial adapter scope. [WCC reference](https://networkx.org/documentation/stable/reference/algorithms/generated/networkx.algorithms.components.weakly_connected_components.html)

**Bipartite matching: limited future use.** Matching imposes one-to-one assignment: each node participates in at most one selected edge. Bibliographic resolution often allows many extracted references to resolve to the same paper, so global one-to-one matching across references can force incorrect results. Start with independent candidate ranking, strong identifier lookup, minimum evidence thresholds, and abstention. Use matching only for a task whose uniqueness constraint is real; allow unmatched items. [Matching definition](https://networkx.org/documentation/stable/reference/algorithms/generated/networkx.algorithms.matching.max_weight_matching.html)

**Node2Vec: defer to similarity experiments.** The algorithm learns representations preserving network neighborhoods. The paper evaluates classification and link prediction; it does not establish identity equivalence. A repository and an HF model with similar neighbors remain distinct artifact types. Any later embedding-based similarity contributes a scored suggestion with model/version evidence, never an automatic cross-type merge. [Original Node2Vec paper](https://arxiv.org/abs/1607.00653)

None of these algorithms defines the domain schema. Entity/relation types and identity constraints remain explicit, versioned product decisions.

## Link: distinguish declarations from inferences

**Jaccard/Adamic-Adar: defer, then expose explainable similarity.** Compare chosen feature sets such as outgoing package dependencies, not arbitrary neighbors in the heterogeneous graph. For Jaccard, 80% overlap needs a specified denominator: intersection divided by union. Scores are not calibrated probabilities and do not establish identity, implementations, or dependencies. Restrict candidates and discount ubiquitous features. An inferred `similar_to` result should store the projection, algorithm version, score, support, and time. NetworkX's documented Jaccard function expects an undirected graph, so it cannot be applied unchanged to the directed typed vault graph. [Jaccard reference](https://networkx.org/documentation/stable/reference/algorithms/generated/networkx.algorithms.link_prediction.jaccard_coefficient.html)

**SCC and topological sorting: diagnostics after ingestion.** Preserve faithfully extracted cycles. Prevent infinite traversal through a visited set, deduplicated work, depth/node budgets, and cancellation. SCC can identify cyclic components later; a component with more than one node is cyclic, while a singleton is cyclic only if it has a self-loop. Topological ordering is defined for a DAG; condense SCCs first when an ordered component graph is useful. Run analysis on the intended dependency projection rather than every relation. The pipeline's execution DAG is separate from the cyclic knowledge graph. [Topological-sort requirements](https://networkx.org/documentation/stable/reference/algorithms/generated/networkx.algorithms.dag.topological_sort.html)

## Post-ingestion analytics

**PageRank: later, as a modest evaluated ranking signal.** Its score depends on incoming-link structure. With consumer -> dependency and citing -> cited directions, influence flows toward referenced nodes. Define separate or deliberately weighted projections for citations and dependencies; exclude tags, similarity, and collection membership by default. A famous library or paper absent from the vault cannot rank, and an incomplete graph can bias scores. Combine an evaluated signal with keyword relevance rather than replacing search relevance. [PageRank reference](https://networkx.org/documentation/stable/reference/algorithms/generated/networkx.algorithms.link_analysis.pagerank_alg.pagerank.html)

**Louvain/Leiden: later, for suggested groups.** Community detection finds partitions; it does not invent semantic names such as "LLM Agents." Naming is a separate heuristic or user step. Choose a typed projection and weights, record algorithm settings/seed and graph revision, and expose unstable groupings as suggestions. Keep manual collections and tags separate. Louvain explicitly optimizes modularity heuristically and its resolution controls community scale. [Louvain reference](https://networkx.org/documentation/stable/reference/algorithms/generated/networkx.algorithms.community.louvain.louvain_communities.html)

**BFS: prioritize after reliable dependency extraction, with corrected direction.** Given `A depends_on B`, an impact query starting at B traverses incoming dependency edges to discover A. Ordinary outgoing traversal answers what B depends on. Lineage queries similarly traverse incoming `fine_tuned_from` edges when starting at a base model. Use an explicit relation allowlist, edge qualifiers, visited IDs, and bounded traversal; return evidence paths and a completeness indicator when limits are reached. Reachable nodes are potentially affected, not confirmed vulnerable: version constraints, lockfiles, optional/dev dependencies, actual deployments, and model-specific mechanisms matter. Dependency and model-lineage reachability should be presented with their distinct semantics. [Reverse reachability / ancestors](https://networkx.org/documentation/stable/reference/algorithms/generated/networkx.algorithms.dag.ancestors.html)

## Priority

Adopt raw replay, plugin contracts, local quota control, and failure isolation in milestone 1. Add explicit dependency/citation extraction and bounded reverse traversal next. Evaluate similarity, SCC diagnostics, PageRank, and communities once the graph contains reliable source evidence. Consider distributed infrastructure only after measuring a workload that needs it.
