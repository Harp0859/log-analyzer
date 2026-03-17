# Research Notes - Log Analysis Landscape

## Open Source Tools Compared

### Tier 1: Full-Featured (Heavy)
- **ELK Stack**: Lucene search (best), Java/JVM, 16GB+ RAM, complex shard mgmt
- **Graylog**: Syslog-native, built-in UI, but still needs ES + MongoDB

### Tier 2: Lightweight Alternatives (Growing Fast)
- **Grafana Loki**: "Prometheus for logs", no full-text index, cheap S3 storage, needs Grafana
- **OpenObserve**: Rust, single binary, Parquet on S3, 2GB RAM, young project
- **VictoriaLogs**: Single binary, from VictoriaMetrics team
- **Quickwit**: Rust, Tantivy search on S3, no built-in UI

### Tier 3: Analytics-First
- **ClickHouse**: Columnar OLAP, blazing fast aggregations, 10-15x compression
- **SigNoz**: OTel-native, ClickHouse backend, unified observability
- **DuckDB + Parquet**: In-process analytics, zero-config, great for ad-hoc

### Tier 4: CLI/Local
- **Lnav**: Terminal UI, SQLite-based, beloved by ops people

## AI/ML for Logs - Academic & Industry

### Log Parsing
- **Drain3** (IBM): Online streaming parser, 50K+ logs/sec, production-ready
- **LLM-based parsing** (2023-2025): Zero-shot generalization, but slow/expensive
- **Best practice**: Drain3 for high-throughput + LLM fallback for unknown formats

### Anomaly Detection
- DeepLog (2017): LSTM on log sequences
- LogAnomaly (2019): Template2Vec embeddings
- LogRobust (2019): Robust to log evolution
- LogBERT (2021): BERT-style masked language modeling
- **Practical**: Statistical baselines (z-score on hourly counts) cover 80% of value

### Root Cause Analysis
- Topology-based (Dynatrace): service dependency graph + backward traversal
- Correlation-based: Granger causality, temporal co-occurrence
- LLM-based RCA: Feed incident context to LLM for reasoning (surprisingly effective)
- **Our approach**: Error cascade graph + LLM reasoning

### Embeddings for Logs
- General models struggle with log jargon (stack traces, hex, paths)
- Fine-tuning on log data significantly improves results
- **Embed templates, not raw lines** (key insight)
- 384-768 dimensions is sweet spot for logs
- Chunk by service+time window or by template group
- nomic-embed-text works well for local deployment

### RAG for Logs
- Pattern: parse -> chunk -> embed -> vector store -> retrieve -> LLM answer
- Key challenge: what to embed (raw lines too noisy, summarized groups best)
- Multi-level: templates (similarity), time-windowed summaries (what happened), incident summaries (why)

## Market Insights
- Cost is #1 pain point (Splunk $100K-$1M/yr)
- ELK operational complexity is #2
- "I just want grep with a nice UI" is a real market
- AI/ML virtually absent in open source - biggest opportunity
- Single-binary tools win adoption
- Small teams use grep/tail more than they admit
