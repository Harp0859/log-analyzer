"""CLI tool to ingest log files into SQLite for the dashboard."""

import argparse
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from analyzer import open_log, parse_line, classify_severity, normalize_message
from db import init_db, is_file_ingested, ingest_file


def infer_year(file_path):
    """Infer the log year from file modification time."""
    mtime = os.path.getmtime(file_path)
    return datetime.fromtimestamp(mtime).year


def parse_file_for_ingest(file_path, context_before_n=5, context_after_n=3, year=None):
    """Parse a log file, returning error rows with context and hourly stats.

    Two-pass approach:
      Pass 1: parse all lines into a list (354K lines ~ 200MB, acceptable)
      Pass 2: for each error/warning/fatal, grab surrounding context lines

    Args:
        year: year for timestamps (syslog has no year). If None, inferred from file mtime.

    Returns:
        (total_lines, error_rows, hourly_counts)
        - total_lines: int
        - error_rows: list of dicts ready for db.ingest_file
        - hourly_counts: dict of (hour, severity, service) -> count
    """
    if year is None:
        year = infer_year(file_path)

    # Pass 1: parse all lines
    all_lines = []
    with open_log(file_path) as f:
        for line in f:
            parsed = parse_line(line, year=year)
            if parsed:
                parsed['severity'] = classify_severity(parsed['message'])
                parsed['pattern'] = normalize_message(parsed['message'])
                all_lines.append(parsed)
            else:
                # Unparseable line - still useful as context
                all_lines.append({'raw': line.strip(), 'severity': 'UNPARSED'})

    # Pass 2: collect errors with context + aggregate hourly stats
    error_rows = []
    hourly_counts = defaultdict(int)

    for i, entry in enumerate(all_lines):
        if entry['severity'] == 'UNPARSED':
            continue

        # Aggregate hourly stats for ALL severities
        hour = entry['timestamp'].replace(minute=0, second=0, microsecond=0)
        hourly_counts[(hour, entry['severity'], entry['service'])] += 1

        # Store errors/warnings/fatals with context
        if entry['severity'] in ('FATAL', 'ERROR', 'WARNING'):
            before_start = max(0, i - context_before_n)
            after_end = min(len(all_lines), i + context_after_n + 1)

            ctx_before = [all_lines[j]['raw'] for j in range(before_start, i)]
            ctx_after = [all_lines[j]['raw'] for j in range(i + 1, after_end)]

            error_rows.append({
                'timestamp': entry['timestamp'],
                'hostname': entry['host'],
                'service': entry['service'],
                'severity': entry['severity'],
                'message': entry['message'],
                'pattern': entry['pattern'],
                'raw_line': entry['raw'],
                'context_before': '\n'.join(ctx_before),
                'context_after': '\n'.join(ctx_after),
            })

    return len(all_lines), error_rows, hourly_counts


def find_log_files(directory):
    """Find syslog files in a directory.

    Matches: syslog*, *.gz, *.log
    """
    directory = Path(directory)
    files = []
    for pattern in ['syslog*', '*.gz', '*.log']:
        files.extend(directory.glob(pattern))
    # Deduplicate and sort
    return sorted(set(f for f in files if f.is_file()))


def ingest_one(conn, file_path, year=None):
    """Ingest a single file. Returns True if ingested, False if skipped."""
    resolved = str(Path(file_path).resolve())

    if is_file_ingested(conn, file_path):
        print(f"  SKIP (already ingested): {resolved}")
        return False

    t0 = time.time()
    file_year = year or infer_year(file_path)
    print(f"  Parsing: {resolved} (year={file_year}) ...", end=' ', flush=True)

    total_lines, error_rows, hourly_counts = parse_file_for_ingest(file_path, year=file_year)
    t_parse = time.time() - t0

    print(f"{total_lines:,} lines, {len(error_rows):,} errors/warnings in {t_parse:.1f}s")
    print(f"  Inserting into DB ...", end=' ', flush=True)

    t1 = time.time()
    ingest_file(conn, file_path, total_lines, error_rows, hourly_counts)
    t_insert = time.time() - t1

    print(f"done in {t_insert:.1f}s")
    return True


def main():
    parser = argparse.ArgumentParser(
        description='Ingest log files into SQLite for the log analyzer dashboard.'
    )
    parser.add_argument('--file', help='Single file to ingest')
    parser.add_argument('--dir', help='Directory to scan for log files')
    parser.add_argument('--db', default='logs.db', help='Database path (default: logs.db)')
    parser.add_argument('--year', type=int, default=None,
                        help='Year for timestamps (default: from file mtime)')
    args = parser.parse_args()

    if not args.file and not args.dir:
        parser.error('Provide --file or --dir')

    conn = init_db(args.db)
    ingested = 0
    skipped = 0

    if args.file:
        if not os.path.isfile(args.file):
            print(f"Error: file not found: {args.file}", file=sys.stderr)
            sys.exit(1)
        files = [args.file]
    else:
        if not os.path.isdir(args.dir):
            print(f"Error: directory not found: {args.dir}", file=sys.stderr)
            sys.exit(1)
        files = find_log_files(args.dir)
        if not files:
            print(f"No log files found in {args.dir}")
            sys.exit(0)
        print(f"Found {len(files)} log file(s) in {args.dir}")

    t_total = time.time()
    for f in files:
        if ingest_one(conn, f, year=args.year):
            ingested += 1
        else:
            skipped += 1

    elapsed = time.time() - t_total
    print(f"\nDone: {ingested} ingested, {skipped} skipped in {elapsed:.1f}s")
    print(f"Database: {os.path.abspath(args.db)}")
    conn.close()


if __name__ == '__main__':
    main()
