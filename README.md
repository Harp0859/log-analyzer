# Log Analyzer

AI-powered log analysis dashboard for operations teams. Think **Lnav with a web UI and an AI brain** — affordable, simple, and self-hosted.

Fills the gap between expensive solutions (Splunk, Datadog) and complex ones (ELK stack) by combining Streamlit, SQLite, and a local LLM into a single tool that runs on your laptop.

![Python](https://img.shields.io/badge/Python-3.8+-blue)
![Streamlit](https://img.shields.io/badge/UI-Streamlit-red)
![SQLite](https://img.shields.io/badge/Storage-SQLite+FTS5-green)
![LLM](https://img.shields.io/badge/AI-Qwen%20via%20LM%20Studio-purple)

---

## Features

- **Smart Error Grouping** — Normalizes PIDs, IPs, UUIDs, and device names to collapse 354K raw lines into ~10K unique patterns
- **AI Summarization** — One-click root cause analysis of top error patterns via local LLM
- **AI Chat** — Multi-turn conversation with full error context pre-loaded
- **Full-Text Search** — FTS5-powered search across millions of log lines with surrounding context
- **Trends & Anomaly Detection** — Cross-file daily error trends with automatic spike detection (>2× average)
- **Context Lines** — Every error stores 5 lines before + 3 lines after, making errors self-contained
- **Two Modes** — Upload a file for quick analysis, or ingest into SQLite for multi-file historical queries
- **Zero Cloud Dependencies** — Runs entirely on your machine with a local LLM
- **No Vector DB or Knowledge Graph** — Uses SQLite + FTS5 for all storage and search; no Pinecone, Weaviate, Chroma, Neo4j, or any external database required

## Target Log Format

Zadara storage node syslog (standard syslog with microsecond timestamps):

```
Feb 25 08:06:27.123456 qa17-sn-1 kernel[1234]: Buffer I/O error on dev nbd0
Feb 25 08:06:27.234567 qa17-sn-1 systemd[1]: Starting cleanup...
```

## Quick Start

### Prerequisites

- Python 3.8+
- [LM Studio](https://lmstudio.ai/) running at `http://localhost:1234/v1` with a loaded model (e.g., `qwen/qwen3.5-9b`)

### Install & Run

```bash
pip install streamlit pandas

# Launch the dashboard
streamlit run app.py
```

### Ingest Log Files (for DB mode)

```bash
# Single file
python ingest.py --file /var/log/syslog.2.gz

# Entire directory
python ingest.py --dir /var/log/zadara/ --year 2026

# Custom database path
python ingest.py --db custom.db --dir /var/log/
```

## Dashboard Tabs

| Tab | Description |
|-----|-------------|
| **Overview** | Daily health metrics, error timeline, top patterns by severity, errors by service, AI analysis button |
| **Trends** | Multi-day error trends, anomaly detection, top patterns across date range (DB mode only) |
| **Search** | Full-text search with severity/service filters and surrounding context lines |
| **Ask AI** | Chat interface with error context pre-loaded — ask questions about your logs |

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Data Sources                          │
│   Local Files (.gz, .log, syslog*)  ·  S3 (future)     │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│              ingest.py  (CLI Tool)                       │
│  Two-pass processing:                                    │
│    Pass 1 → Parse all lines (regex + severity classify)  │
│    Pass 2 → Extract context around errors                │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│            analyzer.py  (Pure Logic Engine)               │
│  parse_log_file() · classify_severity() ·                │
│  normalize_message() · group_messages() ·                │
│  llm_summarize() · llm_chat()                            │
└──────────┬───────────────────────────┬──────────────────┘
           │                           │
           ▼                           ▼
┌────────────────────┐   ┌────────────────────────────────┐
│   SQLite + FTS5    │   │     LM Studio (Local LLM)      │
│    (db.py)         │   │  http://localhost:1234/v1       │
│                    │   │                                  │
│  files             │   │  · Qwen 3.5-9B (summarize/chat)│
│  errors            │   │  · nomic-embed (future RAG)     │
│  hourly_stats      │   │                                  │
│  errors_fts (FTS5) │   └────────────────────────────────┘
└────────┬───────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────┐
│               app.py  (Streamlit Dashboard)              │
│                                                          │
│  ┌───────────┬──────────┬──────────┬──────────┐         │
│  │ Overview  │  Trends  │  Search  │  Ask AI  │         │
│  │           │ (DB only)│          │  (Chat)  │         │
│  └───────────┴──────────┴──────────┴──────────┘         │
│                                                          │
│  FILE MODE: upload → parse in-memory → filter/display    │
│  DB MODE:   query SQLite → trends + search + display     │
└─────────────────────────────────────────────────────────┘
```

## Project Structure

```
log_analyzer/
├── app.py          # Streamlit dashboard (4 tabs, 2 modes)
├── analyzer.py     # Pure logic engine (parsing, classification, LLM calls)
├── db.py           # SQLite schema, queries, FTS5 setup
├── ingest.py       # CLI tool for bulk log ingestion
├── plan.md         # Product roadmap & architecture decisions
├── research.md     # Competitive analysis & technical research
└── logs.db         # SQLite database (created on first ingest)
```

## How It Works

### Severity Classification

Rule-based keyword matching (fast, deterministic, no LLM needed):

| Level | Example Keywords |
|-------|-----------------|
| **FATAL** | `fatal`, `panic`, `critical`, `emergency` |
| **ERROR** | `error`, `fail`, `exception`, `refused` |
| **WARNING** | `warn`, `timeout`, `retry`, `deprecated` |
| **INFO** | Everything else |

### Message Normalization

Raw lines are normalized to create groupable patterns:

```
Before: kernel[4821]: Buffer I/O error on dev nbd0, sector 192.168.1.42
After:  kernel[*]: Buffer I/O error on dev *, sector *

Before: session-c4a3b2d1: connection reset from 10.0.0.5
After:  session-*: connection reset from *
```

This collapses millions of raw lines into thousands of actionable patterns.

### AI Integration

The LLM receives a structured summary (not raw logs) containing:
- Top error patterns with occurrence counts
- Error counts per service
- User's specific question (in chat mode)

This keeps token usage low while providing meaningful analysis.

## Database Schema

```sql
files          -- Metadata per ingested log file
errors         -- Individual ERROR/WARNING/FATAL lines with context
hourly_stats   -- Pre-aggregated counts for fast charting
errors_fts     -- FTS5 virtual table (auto-synced via triggers)
```

**Scale targets:** 1,000 files × 354K lines/file → ~11M error rows, ~3.5 GB

## Implementation Plan

### Done

| Phase | What | Details |
|-------|------|---------|
| **Phase 1** | Core engine + single-file dashboard | `analyzer.py` (parsing, classification, grouping, LLM calls) + `app.py` with Overview, Search, Ask AI tabs. Upload a file → see metrics, charts, AI summary instantly. |
| **Phase 2** | SQLite storage + ingestion CLI | `db.py` (schema, FTS5, queries) + `ingest.py` (two-pass parser with context extraction). Bulk-load thousands of files, query without re-parsing. |

### Next Up

| Phase | What | Details | Why It Matters |
|-------|------|---------|----------------|
| **Phase 3** | Trends tab + cross-file search | Daily error trends across date ranges. Anomaly detection (>2× average = spike). Top patterns across multiple files. | Moves from "what happened today" to "what's getting worse over time" |
| **Phase 4** | S3 integration | `s3sync.py` — auto-download new logs from S3. Cron/systemd timer for daily sync. Config via env vars or `config.yaml`. | Eliminates manual file handling. Morning routine becomes: open browser → see today's health. |

### Future Improvements

| Phase | What | Details | Why It Matters |
|-------|------|---------|----------------|
| **Phase 5a** | Embeddings + RAG | Embed ~10K normalized patterns via `nomic-embed-text` (already in LM Studio). Store 768-dim vectors in SQLite (~30 MB). Semantic search: "disk failures" finds "Buffer I/O error", "nbd0 sector read fail", "nvmet fatal error" even without keyword overlap. RAG: retrieve relevant patterns by embedding similarity, feed to Qwen for better AI answers. | Keyword search misses semantic connections. RAG gives the LLM the right context, not just top-N by count. |
| **Phase 5b** | Error cascade graphs | Detect causal chains via temporal co-occurrence in sliding time windows. Build directed graph: root cause → symptom chain. Visualize with networkx/pyvis in Streamlit. Example: `DRBD disconnect → Filesystem unmount fail → Nova compute fail → CCVM timeout`. | Turns 5 separate errors into 1 incident story. Surfaces root causes automatically. |
| **Phase 6** | Drain3 auto-parsing + deep anomaly detection | Drain3 for automatic log template extraction (no regex needed). LSTM/Transformer-based sequence anomaly detection (DeepLog, LogBERT). Auto-detect new error patterns never seen before. | Handles unknown log formats without manual regex. Catches anomalies that rule-based checks miss. |

### What We're NOT Building (by design)

| Thing | Why Not |
|-------|---------|
| **Vector database** (Pinecone, Weaviate, Chroma, etc.) | ~10K unique patterns × 768 dims = ~30 MB. SQLite handles this trivially. A separate vector DB adds operational complexity for zero benefit at our scale. |
| **Knowledge graph database** (Neo4j, etc.) | Error cascade graphs are small (~10K nodes, ~50K edges). SQLite tables (`error_nodes`, `error_edges`) + networkx in-memory is simpler and faster. No graph DB server to run. |
| **Kubernetes / microservices** | Single-machine tool. One `streamlit run app.py` and you're done. |
| **Cloud-hosted LLM** | Privacy, cost, latency. Local LM Studio keeps everything on your machine at zero cost. |
| **Custom frontend** | Streamlit gives us charts, tables, chat UI, file upload, and sidebar filters with zero frontend code. |

## Configuration

AI settings are configurable in the dashboard sidebar:

| Setting | Default |
|---------|---------|
| LM Studio URL | `http://localhost:1234/v1` |
| Model | `qwen/qwen3.5-9b` |
| Embedding Model | `text-embedding-nomic-embed-text-v1.5` |

## License

MIT
