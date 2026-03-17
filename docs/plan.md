# Log Analyzer - Full System Plan

## The Problem

An ops team has 1000s of syslog files from Zadara storage nodes, stored
in S3 (date-organized). They need a product that:

1. **Auto-ingests** new logs from S3 daily
2. **Shows today's health** at a glance (errors, warnings, what broke)
3. **Shows trends** over weeks/months (are errors getting worse?)
4. **Searches** across all history (when did this error first appear?)
5. **AI chat** to ask questions about any time period

---

## Architecture Overview

```
  S3 Bucket                    Local System
  (raw .gz logs)               (where dashboard runs)

  /2026-02-25/                +--------------------------+
    syslog.gz   --ingest-->   |  SQLite Database         |
  /2026-02-26/                |  - files (metadata)      |
    syslog.gz   --ingest-->   |  - errors (ERR/WARN/FAT) |
  /2026-02-27/                |  - hourly_stats (counts)  |
    syslog.gz   --ingest-->   +-----------+--------------+
  ...                                     |
  /2026-12-31/                            v
    syslog.gz                 +--------------------------+
                              |  Streamlit Dashboard     |
  Also supports:              |  Tab 1: Overview (today) |
  - Local folder              |  Tab 2: Trends (history) |
  - Manual upload             |  Tab 3: Search (all)     |
                              |  Tab 4: Ask AI (Qwen)    |
                              +--------------------------+
```

---

## Why SQLite (not parse-every-time)

With 1000 files x 354K lines = **354 million lines**. Can't parse that
on every page load.

**Solution: parse once, query forever.**

- On ingest: parse file, classify severity, normalize patterns
- Store ERROR/WARNING/FATAL lines + **context lines** around them in DB
- Store hourly aggregated counts for all severities (for charts)
- Raw files stay on S3/local for on-demand deep search
- SQLite is built into Python, no server, single file, handles millions of rows

### Context Lines: Why "Noise" Matters

**Key insight from syslog analysis:** The INFO lines right before an error
are the story of what caused it. They're not noise - they're context.

Example - without context you see:
```
08:06:27 Filesystem(FS_ON_DRBD): ERROR: Couldn't unmount /mnt/nova
```

With context (5 lines before) you see the full story:
```
08:06:21 Filesystem(FS_ON_DRBD): INFO: Running stop for /dev/drbd0 on /mnt/nova
08:06:27 Filesystem(FS_ON_DRBD): INFO: Trying to unmount /mnt/nova
08:06:27 Filesystem(FS_ON_DRBD): ERROR: Couldn't unmount /mnt/nova  ← NOW you know why
08:06:27 Filesystem(FS_ON_DRBD): INFO: sending signal TERM to: root 117412 -bash
08:06:28 Filesystem(FS_ON_DRBD): ERROR: Couldn't unmount /mnt/nova  ← retry failed too
```

Another example:
```
07:41:58 udev: DEVNAME=/dev/nbd0 ACTION=change ...   ← disk state changing
07:41:59 kernel: Buffer I/O error on dev nbd0         ← NOW you know what triggered it
```

**Strategy: store context WITH errors, not just errors.**

On ingest, for every ERROR/WARNING/FATAL line:
- Capture N lines before (default 5) and N lines after (default 3)
- Store them in a `context` table linked to the error
- These context lines are what makes root cause analysis possible
- The LLM gets much better answers when it sees the surrounding context

**Database schema:**
```sql
-- Track which files have been ingested
CREATE TABLE files (
    id          INTEGER PRIMARY KEY,
    path        TEXT UNIQUE,        -- S3 key or local path
    hostname    TEXT,               -- extracted from log lines
    log_date    DATE,               -- date of the log
    total_lines INTEGER,
    error_count INTEGER,
    warning_count INTEGER,
    fatal_count INTEGER,
    info_count  INTEGER,
    ingested_at TIMESTAMP
);

-- Store individual error/warning/fatal lines (searchable)
CREATE TABLE errors (
    id          INTEGER PRIMARY KEY,
    file_id     INTEGER REFERENCES files(id),
    timestamp   TIMESTAMP,
    hostname    TEXT,
    service     TEXT,
    severity    TEXT,               -- FATAL, ERROR, WARNING
    message     TEXT,
    pattern     TEXT,               -- normalized message
    raw_line    TEXT,
    context_before TEXT,            -- 5 lines before (newline-separated)
    context_after  TEXT             -- 3 lines after (newline-separated)
);

-- Hourly aggregates for timeline charts (all severities)
CREATE TABLE hourly_stats (
    file_id     INTEGER REFERENCES files(id),
    hour        TIMESTAMP,
    severity    TEXT,
    service     TEXT,
    count       INTEGER
);

-- Indexes for fast queries
CREATE INDEX idx_errors_severity ON errors(severity);
CREATE INDEX idx_errors_timestamp ON errors(timestamp);
CREATE INDEX idx_errors_service ON errors(service);
CREATE INDEX idx_errors_pattern ON errors(pattern);
CREATE INDEX idx_hourly_hour ON hourly_stats(hour);
```

**Storage with context (revised estimate for 1000 files):**
- errors table: ~11M rows x ~500 bytes (including context) = ~5.5 GB
- Context adds ~60% more storage but makes every error self-contained
- Worth it: you can investigate any error without going back to the raw file

**Storage estimate for 1000 files:**
- errors table: ~11M rows x 300 bytes = ~3.3 GB
- hourly_stats: ~1000 files x 24 hours x 4 severities x 40 services = ~4M rows = ~100 MB
- Total: ~3.5 GB SQLite file (very manageable)

---

## How a Normal Person Uses This Daily

1. **Logs arrive in S3 overnight** (or from local folder)
2. **Auto-sync runs** (cron or systemd timer) - ingests new files into SQLite
3. **Morning: open browser** -> `http://localhost:8501`
4. **Overview tab** - today's stats, error spikes, top errors, AI summary
5. **Trends tab** - "errors increased 3x this week" / "new error pattern appeared Tuesday"
6. **Search tab** - "show me every 'nvmet fatal error' across all files"
7. **Ask AI tab** - "compare this week's errors to last week"

---

## Dashboard Layout (4 tabs)

### Tab 1: Overview (today's health)
```
+---------------------------------------------------------------+
|  SIDEBAR                |  MAIN AREA                          |
|                         |                                     |
|  --- Data Source ---     |  [TOTAL]  [ERRORS]  [WARNS]  [FATAL]|
|  [Upload File]          |   354K     2472      824       12   |
|  [Pick from folder]     |                                     |
|  [Sync from S3]         |  --- Error/Warning Timeline ---     |
|                         |  [area chart by hour]               |
|  --- Date Picker ---    |                                     |
|  [2026-02-25]           |  --- Top Errors (grouped) ---      |
|                         |  | # | Count | Pattern | Service | |
|  --- Filters ---        |  | 1 | 487   | Buffer..|  kernel | |
|  Time: [from] [to]      |  | 2 | 231   | CCVM ...|  crmd   | |
|  Severity: [x]ERR [x]W  |  ...                               |
|  Service: [dropdown]     |                                     |
|  Top N: [15]            |  --- Top Warnings (grouped) ---    |
|                         |  --- Errors by Service (bar) ---   |
|  --- AI Settings ---    |                                     |
|  LM Studio URL          |  [Analyze with AI]  [Download Report]|
|  Model name             |  --- AI Summary ---                |
|                         |  (Qwen root cause analysis)         |
+---------------------------------------------------------------+
```

### Tab 2: Trends (across all ingested files)
- Date range picker: last 7 / 30 / 90 days or custom
- **Error rate over time** - daily error counts as line/bar chart
- **New vs recurring errors** - patterns that appeared for the first time
- **Top 10 error patterns over time** - stacked area chart
- **Per-service health** - heatmap (service x date, color = error count)
- **Anomaly detection** - days where error count deviated >2x from average
- All data comes from SQLite (fast, no re-parsing)

### Tab 3: Search (across all history)
- Full-text search box
- Searches across ALL ingested files (SQLite query)
- Filter by: date range, severity, service, hostname
- Results: timestamp, service, severity, message, source file
- Click a result → **expand to see context lines** (stored in DB, no raw file needed)
  - 5 lines before (the "what was happening" story)
  - 3 lines after (the "what happened next")
- Capped at 500 results with pagination

### Tab 4: Ask AI
- Chat interface (text_input + send button)
- Pre-loaded with error summary from selected date range
- Can ask about trends: "are errors getting worse?"
- Can ask about incidents: "what happened on Feb 25 at 08:00?"
- Can compare: "how does today compare to last week?"
- Uses Qwen via LM Studio

---

## Processing Pipeline

See `flow_diagram.tldr` for visual diagram of single-file processing.

### Step 1 - Ingestion (S3 or local)
- **S3 sync:** configurable bucket + prefix, uses boto3
  - List objects, compare against `files` table, download new ones
  - Supports any folder structure (auto-detects date from filename/path)
- **Local folder:** scan directory for new/modified files
- **Upload:** drag-and-drop in dashboard (for ad-hoc analysis)
- Ingestion is **idempotent** - re-running skips already-ingested files

### Step 2 - Parse & Classify (same as current analyzer.py)
- Regex parse: timestamp, hostname, service, pid, message
- Classify severity: FATAL/ERROR/WARNING/INFO (keyword rules)
- Normalize patterns: strip PIDs, IPs, devices, UUIDs

### Step 3 - Store in SQLite (with context)
- Insert file metadata into `files` table
- Insert ERROR/WARNING/FATAL lines into `errors` table
  - **For each error: also store 5 lines before + 3 lines after**
  - Keep a rolling buffer of recent lines during parsing
  - Context lines make every error self-contained and investigable
- Insert hourly aggregated counts into `hourly_stats` table
- INFO lines: only counted in hourly_stats, not stored individually
  (but they ARE stored as context when they surround an error)
- Wrap in a transaction for speed (bulk insert)

### Step 4 - Query & Display
- Overview queries: filter by file_id (single day)
- Trend queries: aggregate across date ranges
- Search queries: LIKE or FTS on errors table
- All rendered in Streamlit

### Step 5 - LLM Integration
- Same as before: Qwen via LM Studio
- Auto-summary button on Overview
- Free-form chat on Ask AI tab
- Context now includes trend data ("errors increased 3x since Tuesday")

---

## File Structure

```
log_analyzer/
  plan.md              # this file
  flow_diagram.tldr    # architecture diagram (tldraw)
  app.py               # Streamlit dashboard (4 tabs)
  analyzer.py          # core parsing engine (parse, classify, group)
  db.py                # SQLite database layer (ingest, query, schema)
  s3sync.py            # S3 connector (list, download, sync)
  config.py            # configuration (S3 creds, paths, LM Studio URL)
  ingest.py            # CLI tool for manual/cron ingestion
  syslog.2.gz          # sample log file
  logs.db              # SQLite database (auto-created)
```

**Dependencies:**
- `streamlit` - dashboard UI
- `boto3` - S3 access (only if using S3)
- `pandas` - bundled with streamlit
- stdlib: `sqlite3`, `gzip`, `urllib.request`, `json`, `re`

---

## Key Functions

### analyzer.py (pure parsing logic - already built)
```
open_log(path)              - auto-detect gz, return line iterator
parse_line(line)            - regex -> dict
parse_log_file(path)        - parse entire file -> DataFrame
classify_severity(msg)      - keyword matching -> FATAL/ERROR/WARNING/INFO
normalize_message(msg)      - strip variable parts -> pattern
group_messages(df)          - group by pattern, count, sort
```

### db.py (database layer - new)
```
init_db(path)               - create tables if not exist
is_file_ingested(path)      - check if file already in DB
ingest_file(db, file_path, hostname, log_date) - parse + store
query_overview(db, date)    - stats for a single day
query_trends(db, start, end)- aggregated stats over date range
query_top_errors(db, date, top_n) - top error patterns for a day
query_search(db, query, filters)  - full-text search across history
query_services(db, date)    - error counts per service
query_hourly(db, start, end)- hourly counts for timeline charts
get_ingested_dates(db)      - list all dates with data
```

### s3sync.py (S3 connector - new)
```
list_log_files(bucket, prefix) - list .gz files in S3
download_file(bucket, key, local_path) - download one file
sync(bucket, prefix, local_cache, db) - download new + ingest
```

### config.py (configuration - new)
```
Reads from config.yaml or environment variables:
  S3_BUCKET, S3_PREFIX, AWS_PROFILE
  LOG_DIR (local fallback)
  LM_STUDIO_URL, LM_STUDIO_MODEL
  DB_PATH (default: ./logs.db)
```

### ingest.py (CLI ingestion tool - new)
```
# Run manually or via cron
python ingest.py                    # sync from S3
python ingest.py --local /var/log/  # ingest from local folder
python ingest.py --file syslog.2.gz # ingest single file
```

### app.py (Streamlit dashboard - extend current)
```
main()                      - page config, sidebar, tab routing
render_overview_tab(db, date)      - single-day view
render_trends_tab(db)              - multi-day charts
render_search_tab(db)              - cross-file search
render_ai_tab(db, date_range)      - chat with Qwen
sidebar_controls(db)               - date picker, filters, sync button
```

---

## Running

```bash
# one-time setup
pip install streamlit boto3

# configure (create config.yaml or set env vars)
export S3_BUCKET=my-logs-bucket
export S3_PREFIX=zadara-logs/
export LM_STUDIO_URL=http://localhost:1234/v1

# initial ingestion (or let cron do it)
python ingest.py

# daily use - just open browser
streamlit run app.py

# cron job for auto-sync (add to crontab)
# 0 6 * * * cd /path/to/log_analyzer && python ingest.py >> ingest.log 2>&1
```

---

## Implementation Order

Phase 1 (done): analyzer.py + single-file app.py (current state)
Phase 2: db.py + ingest.py (SQLite + FTS5 storage, local folder ingestion)
Phase 3: Update app.py with Trends tab + cross-file Search + anomaly badges
Phase 4: s3sync.py + config.py (S3 integration)
Phase 5a: Embeddings + semantic search + RAG (nomic-embed via LM Studio)
Phase 5b: Error cascade graph (temporal co-occurrence, networkx visualization)
Phase 6 (future): Drain3 auto-parsing, deep learning anomaly detection

---

## Verification

1. `python ingest.py --file syslog.2.gz` -> ingests into logs.db
2. `python ingest.py --local .` -> finds and ingests all .gz files
3. `streamlit run app.py` -> dashboard loads, date picker shows ingested dates
4. Pick a date -> Overview tab shows that day's stats
5. Switch to Trends tab -> see error rate chart across all ingested days
6. Search tab -> search "Buffer I/O" across all files -> see results with dates
7. Click "Analyze with AI" -> Qwen summary appears
8. Ask AI tab -> "what caused the CCVM restarts?" -> get Qwen answer
9. Click "Sync from S3" in sidebar -> pulls new files (needs S3 config)

---

## Syslog Analysis: What We're Actually Dealing With

Based on deep analysis of syslog.2 (354K lines, Feb 25-28, qa17-sn-1).

### Volume Breakdown

| Service | Lines | % | Signal Value |
|---------|-------|---|-------------|
| systemd/udev (disk events) | 139K | 39% | Low normally, HIGH as error context |
| zadara_eventlogd | 30K | 8% | Medium - VSA events inside |
| zadara_mag_be.sh (S3 uploads) | 30K | 8% | Low normally, context for S3 issues |
| zadara_snmonitor (health polls) | 29K | 8% | Low normally, connection errors = signal |
| zadara_snreq.py | 21K | 6% | Low normally |
| kernel (nvmet, block I/O) | 17K | 5% | **HIGH** - hardware errors |
| ccvm-control | 12K | 3% | **HIGH** - CCVM lifecycle |
| root (megacli RAID checks) | 11K | 3% | Low normally, context for disk failures |
| Other (40+ services) | ~65K | 18% | Mixed |

**Key insight: 75% of lines look like noise in isolation, but become critical
context when they appear right before an error.**

### The Real Incidents in This Log

**1. CCVM crash loop** (most impactful)
- CCVM_start times out after 240s → cluster stops it → retry → timeout again
- 8+ cycles between 07:44 and 08:30 Feb 25
- Cascades to: NOVA_COMPUTE, RabbitMQ, NVMe sessions

**2. NVMe controller fatal errors** (hardware)
- `nvmet_fatal_error_handler: ctrl X fatal error occurred!` - 400+ hits
- Across controllers 2-16, concentrated during CCVM instability
- Session creation failures for VSA volumes

**3. Block I/O errors on nbd0** (storage path broken)
- `Buffer I/O error on dev nbd0` - 108 pairs (read + I/O)
- Network block device not responding

**4. Inter-service connection resets** (RPC failures)
- `protobuf-c rpc server: connection reset by peer` - 800+ across services
- Services can't talk to each other during instability

**5. HA cluster instability**
- DRBD filesystem unmount fails on /mnt/nova
- Heartbeat packet loss to qa17-sn-2
- RabbitMQ returning unexpected status code 70

### What This Tells Us About Product Needs

1. **Context lines are essential** - INFO before ERROR tells the story
2. **Error cascade detection** - these 5 incidents are ONE cascade
3. **Service-specific parsers** - udev (KEY=VALUE), genservice (sn-ha format),
   zadara services ([pid] [module] func[line]: msg)
4. **HA service state tracking** - start/stop/timeout/fail-count per resource
5. **Entity mapping** - IP→hostname, VSA ID→volume name cross-references
6. **Error spike detection** - Feb 25 08:28 = 45 errors/min vs baseline 2/hour
7. **The "what happened" narrative** - LLM should produce:
   > "Between 07:44-08:30, CCVM failed to start 8 times (240s timeout).
   > This caused NOVA_COMPUTE failure, RabbitMQ instability, and 400+
   > NVMe fatal errors. Root cause: investigate why CCVM can't start."

---

## Research: Competitive Landscape & Product Strategy

### The Market Gap

The log analysis market has a clear gap:

```
"Too expensive"                          "Too complex to run"
  Splunk ($100K-$1M/yr)                   ELK Stack (need ES cluster)
  Datadog (per-GB pricing)                Graylog (ES + MongoDB + JVM)
  New Relic                               Kafka + ClickHouse (DIY)
         \                                    /
          \______ THE GAP WE TARGET _________/
                 |                         |
                 | Affordable              |
                 | Simple (single binary)  |
                 | AI-powered (our edge)   |
                 | Self-hosted             |
                 |_________________________|
```

### Competitive Analysis

| Tool | Strengths | Weaknesses | Our Advantage |
|------|-----------|------------|---------------|
| **ELK Stack** | Best full-text search (Lucene), huge ecosystem | 16GB+ RAM, complex ops, JVM tuning, shard management | Zero ops burden, single-file DB |
| **Grafana Loki** | Cheap storage (S3), lightweight, Grafana integration | No full-text index (slow grep), requires Grafana, label cardinality issues | Better search (FTS5), built-in UI, AI chat |
| **Graylog** | Syslog-native, built-in UI, processing pipelines | Still needs ES cluster, paywalled features, aging UI | No ES dependency, AI-powered analysis |
| **ClickHouse** | Blazing fast analytics, 10-15x compression | No built-in UI, schema required, complex cluster ops | Batteries-included dashboard |
| **OpenObserve** | Single binary (Rust), Parquet on S3, 2GB RAM | Young project, less mature search, limited AI | Mature AI features, domain-specific |
| **SigNoz** | OTel-native, unified metrics/traces/logs | Requires OTel instrumentation, ClickHouse ops burden | Works with plain syslog, zero instrumentation |
| **Quickwit** | Tantivy search on S3, stateless searchers | No built-in UI or alerting, young project | Complete product with UI + AI |
| **Lnav** | Beloved terminal UI, SQLite-based, single binary | Terminal only, no web, no AI, single-machine | Web dashboard, AI analysis, multi-file trends |

### What SREs Actually Want (from community research)

1. **Cost** is the #1 pain point universally
2. **"I just want to grep my logs with a nice UI"** - most teams want search, not ML
3. **Operational simplicity** - single binary > Kubernetes deployment
4. **Fast search during incidents** - < 5 seconds for common queries
5. **80% of Splunk at 10% of the cost** is the sweet spot
6. **Predictable pricing** - not per-GB-ingested that punishes verbose logging

### AI/ML in Log Analysis (State of the Art)

**The landscape:**
- Open source tools have virtually NO AI/ML features
- Elastic has paid ML (anomaly detection, NLP) but $$$
- Splunk AI Assistant is proprietary
- Datadog Bits AI is cloud-only
- **This is our biggest differentiator opportunity**

**Key AI techniques for logs:**

| Technique | What It Does | Complexity | Value |
|-----------|-------------|------------|-------|
| Drain3 log parsing | Auto-extract templates from unstructured logs | Low | High |
| Statistical anomaly | Z-score on hourly counts to flag spikes | Low | Medium |
| Embedding-based search | Semantic "find similar errors" via vectors | Medium | High |
| RAG for chat | Retrieve relevant logs, feed to LLM for answers | Medium | Very High |
| Error cascade graphs | Detect causal chains via temporal co-occurrence | Medium | High |
| LLM root cause analysis | Feed error context to LLM, get diagnosis | Low | High |
| Deep learning anomaly | LSTM/Transformer on log sequences (DeepLog, LogBERT) | High | Medium |

**Recommended AI roadmap (build in this order):**
1. Rule-based classification + LLM summarization (Phase 1 - DONE)
2. Statistical anomaly detection on hourly counts (Phase 3)
3. Embedding-based semantic search via nomic-embed (Phase 5a)
4. RAG: retrieve relevant errors by embedding, feed to Qwen (Phase 5a)
5. Error cascade graph from temporal co-occurrence (Phase 5b)
6. Drain3 auto-parsing for unknown log formats (Phase 6)

### Product Positioning

**Our position: "Lnav with a web UI and AI brain"**

- **Like Lnav**: single-file DB (SQLite), single-machine, no cluster needed, fast
- **Unlike Lnav**: web dashboard (Streamlit), shareable, multi-file trends
- **Like Splunk**: AI analysis, pattern grouping, search, dashboards
- **Unlike Splunk**: free, self-hosted, runs on a laptop, no per-GB pricing
- **Like Loki**: cheap storage, handles thousands of files
- **Unlike Loki**: full-text search, built-in UI, no Grafana needed

**Target users:**
- Small-medium ops teams (5-20 people)
- Infrastructure with traditional syslog (storage, networking, bare metal)
- Teams currently using "SSH + grep" or outgrowing basic tools
- Teams that tried ELK and gave up on ops complexity
- Teams that can't justify Splunk/Datadog pricing

### Storage Backend Decision

Research confirmed SQLite is the right choice for our scale:

| Scale | Best Backend | Our Choice |
|-------|-------------|------------|
| < 1GB total | SQLite + FTS5 or grep | **SQLite + FTS5** |
| 1-50GB, single machine | DuckDB on Parquet | SQLite (simpler, good enough) |
| 10-500GB, need real-time | ClickHouse single node | Future consideration |
| 500GB+, multi-machine | ClickHouse cluster / Loki | Out of scope |

**Why SQLite wins for us:**
- Zero server, single file, Python stdlib (`sqlite3`)
- FTS5 full-text search is fast for <50M rows
- 1000 files x ~11K errors/file = ~11M rows = well within SQLite comfort zone
- Estimated DB size: ~3.5 GB (very manageable)
- Can always migrate to DuckDB/ClickHouse later if needed

### Architecture Refinements from Research

**Key takeaway: embed normalized templates, not raw lines**
- Embedding raw log lines is noisy and expensive
- Embed the ~10K unique normalized patterns instead
- 10K patterns x 768 dims x 4 bytes = ~30 MB (fits in SQLite)

**Key takeaway: hybrid search (keyword + semantic)**
- Traditional keyword/regex for exact matches
- Embedding similarity for "find related errors"
- Combine results for best coverage

**Key takeaway: chunk by service + time window for RAG**
- Don't embed individual lines
- Summarize error groups per service per hour
- Embed the summaries for RAG context retrieval

---

## Future: Knowledge Graph + Embeddings (Phase 5)

Ideas under consideration. Not building yet - captured here for planning.

### Idea 1: Error Cascade Graph

Automatically detect error chains from temporal co-occurrence.

**How it works:**
- Scan errors within sliding time windows (e.g. 60 seconds)
- If error pattern A consistently appears before pattern B, create
  a "precedes" edge with confidence score
- Build a directed graph: A → B → C (root cause → symptom chain)

**Example from our logs:**
```
  DRBD disconnect (kernel)
       ↓ precedes (95% confidence)
  Filesystem unmount failed (Filesystem/FS_ON_DRBD)
       ↓ precedes (90% confidence)
  Nova compute start failed (genservice/NOVA_COMPUTE)
       ↓ precedes (88% confidence)
  CCVM start timed out (crmd)
       ↓ precedes (85% confidence)
  FeHeartbeat failed (zadara_eventlogd)
```

**Storage:**
```sql
-- Additional tables for knowledge graph
CREATE TABLE error_nodes (
    id          INTEGER PRIMARY KEY,
    pattern     TEXT UNIQUE,       -- normalized error pattern
    service     TEXT,
    severity    TEXT,
    first_seen  TIMESTAMP,
    total_count INTEGER,
    embedding   BLOB              -- vector from nomic-embed (768 dims)
);

CREATE TABLE error_edges (
    source_id   INTEGER REFERENCES error_nodes(id),
    target_id   INTEGER REFERENCES error_nodes(id),
    relation    TEXT,             -- 'precedes', 'correlates', 'same_service'
    confidence  REAL,            -- 0.0 - 1.0
    co_occurrences INTEGER,      -- how many times seen together
    avg_delay_sec REAL,          -- average time between A and B
    PRIMARY KEY (source_id, target_id, relation)
);
```

**Dashboard integration:**
- New "Cascade" view: interactive graph visualization (using graphviz
  or pyvis in Streamlit)
- Click an error node → see what caused it and what it breaks
- Highlight "root cause" nodes (nodes with no incoming 'precedes' edges
  but many outgoing ones)
- Feed the cascade chain to Qwen: "DRBD disconnect caused a 4-step
  failure cascade affecting CCVM" → much better root cause analysis

**Libraries:** `networkx` (graph logic), `graphviz` or `pyvis` (visualization)

---

### Idea 2: Semantic Search with Embeddings (RAG)

Use the `text-embedding-nomic-embed-text-v1.5` model already running in
LM Studio to embed error patterns and enable meaning-based search.

**How it works:**
- On ingest: embed each unique error pattern → 768-dim vector
- Store vectors in `error_nodes.embedding` column
- On search: embed the query, find nearest neighbors (cosine similarity)
- On AI chat: retrieve top-K relevant error patterns via embedding
  similarity, feed to Qwen as context (RAG)

**What it enables:**
- Search "disk failures" → finds "Buffer I/O error", "nbd0 sector read
  fail", "nvmet fatal error" even though keywords don't match
- Better AI answers: Qwen gets semantically relevant context, not just
  top-N by count
- Automatic clustering: group semantically similar error patterns
  without manual regex rules

**Embedding API call:**
```
POST http://localhost:1234/v1/embeddings
{"model": "text-embedding-nomic-embed-text-v1.5", "input": "Buffer I/O error on dev dm-67"}
→ {"data": [{"embedding": [0.012, -0.034, ...]}]}  # 768 dims
```

**Storage:** Vectors stored as BLOBs in SQLite. For 10K unique patterns
x 768 floats x 4 bytes = ~30 MB. Cosine similarity computed in Python
(fast enough for 10K vectors).

**Libraries:** None extra (urllib for API, numpy for cosine sim)

---

### Idea 3: Anomaly Detection via Graph Patterns

Once the knowledge graph has a baseline of "normal" error correlations,
detect anomalies automatically.

**What to detect:**
- **New node:** error pattern never seen before in any prior file
- **New edge:** two services failing together that never correlated before
- **Missing edge:** an expected correlation broke (topology changed?)
- **Count anomaly:** a pattern that normally occurs 5x/day suddenly
  appears 500x (use hourly_stats + z-score)

**Dashboard integration:**
- "Anomalies" badge on Overview tab: "3 new patterns detected today"
- Anomaly list with severity ranking
- Feed anomalies to Qwen: "These 3 patterns are new as of today,
  what could explain them?"

---

### How These Three Connect

```
  Raw Logs (S3)
       ↓
  Parse + Classify (analyzer.py)
       ↓
  Store in SQLite (db.py)
       ↓
  ┌────────────────────────────────────────┐
  │  Knowledge Graph Layer                 │
  │                                        │
  │  error_nodes ←──embed──→ Vectors       │
  │       ↕                    ↕            │
  │  error_edges              RAG Search    │
  │  (cascades)               (semantic)    │
  │       ↕                    ↕            │
  │  Anomaly Detection        AI Chat      │
  │  (new/missing patterns)   (Qwen+RAG)   │
  └────────────────────────────────────────┘
       ↓
  Streamlit Dashboard
  (Overview, Trends, Search, Cascade Graph, Ask AI)
```
