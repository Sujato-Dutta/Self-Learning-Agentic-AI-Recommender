from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUESTS = Counter("smartreco_http_requests_total", "HTTP requests", ["method", "path", "status"])
HTTP_LATENCY = Histogram("smartreco_http_request_duration_seconds", "HTTP latency", ["path"])
HTTP_ACTIVE = Gauge("smartreco_http_active_requests", "Active HTTP requests")
EVENT_BATCHES = Counter("smartreco_event_batches_total", "Event batches received")
EVENTS_RECEIVED = Counter("smartreco_events_received_total", "Events accepted")
EVENT_DUPLICATES = Counter("smartreco_event_duplicates_total", "Duplicate events")
EVENT_FAILURES = Counter("smartreco_event_validation_failures_total", "Event validation failures")
EVENT_LATENCY = Histogram("smartreco_event_ingestion_seconds", "Event ingestion latency")
RECOMMENDATION_RUNS = Counter("smartreco_recommendation_runs_total", "Recommendation runs", ["status"])
RECOMMENDATION_LATENCY = Histogram("smartreco_recommendation_duration_seconds", "Recommendation latency")
CACHE_HITS = Counter("smartreco_cache_hits_total", "Cache hits", ["cache"])
CACHE_MISSES = Counter("smartreco_cache_misses_total", "Cache misses", ["cache"])
MESH_CALLS = Counter("smartreco_mesh_calls_total", "Mesh API calls", ["operation", "status"])
MESH_AVOIDED = Counter("smartreco_mesh_calls_avoided_total", "Mesh calls avoided", ["reason"])
MESH_LATENCY = Histogram("smartreco_mesh_request_duration_seconds", "Mesh request latency", ["operation"])
MESH_TOKENS = Counter("smartreco_mesh_tokens_total", "Mesh token usage", ["kind"])
RETRIEVAL_LATENCY = Histogram("smartreco_retrieval_duration_seconds", "Retrieval latency")
RERANK_LATENCY = Histogram("smartreco_reranking_duration_seconds", "Reranking latency")
CANDIDATE_COUNT = Histogram("smartreco_retrieval_candidates", "Retrieved candidate count")
GROUNDING_FAILURES = Counter("smartreco_grounding_failures_total", "Grounding failures")
SYNC_SUCCESS = Counter("smartreco_pinecone_sync_total", "Pinecone sync operations", ["status", "operation"])
SYNC_PENDING = Gauge("smartreco_outbox_pending", "Pending vector outbox jobs")
SYNC_DEAD_LETTER = Gauge("smartreco_outbox_dead_letter", "Dead-letter vector jobs")
SYNC_LAG = Gauge("smartreco_vector_sync_lag_seconds", "Oldest pending vector sync lag")
RECO_ENGAGEMENT = Counter("smartreco_recommendation_engagement_total", "Recommendation engagement", ["action"])
NBA_DECISIONS = Counter(
    "smartreco_next_best_action_decisions_total", "Next-best-action policy decisions", ["action", "strategy"]
)
NBA_PROPENSITY = Histogram(
    "smartreco_purchase_propensity", "Purchase propensity supplied to the action policy", ["stage"],
    buckets=(0, .1, .2, .3, .4, .5, .6, .7, .8, .9, 1),
)
NBA_EXPECTED_REVENUE = Histogram(
    "smartreco_next_best_action_expected_revenue", "Expected revenue at decision time", ["action"],
    buckets=(0, 100, 500, 1000, 2500, 5000, 7500, 10000, 15000, 25000, 50000),
)
NBA_REWARDS = Counter(
    "smartreco_next_best_action_rewards_total", "Attributed next-best-action rewards", ["event_type", "sign"]
)
SCHEDULER_RUNS = Counter("smartreco_scheduler_runs_total", "Scheduler runs", ["job", "status"])
