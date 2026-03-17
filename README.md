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

## Roadmap

- [x] **Phase 1** — Core analyzer + single-file dashboard
- [x] **Phase 2** — SQLite storage + ingestion CLI
- [ ] **Phase 3** — Trends tab + cross-file search
- [ ] **Phase 4** — S3 integration for automatic log sync
- [ ] **Phase 5a** — Embeddings + RAG for semantic search
- [ ] **Phase 5b** — Error cascade graphs (cause → symptom chains)
- [ ] **Phase 6** — Drain3 auto-parsing + anomaly detection

## Configuration

AI settings are configurable in the dashboard sidebar:

| Setting | Default |
|---------|---------|
| LM Studio URL | `http://localhost:1234/v1` |
| Model | `qwen/qwen3.5-9b` |
| Embedding Model | `text-embedding-nomic-embed-text-v1.5` |

## License

MIT
