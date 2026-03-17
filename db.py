"""SQLite database layer for log storage and querying."""

import sqlite3
from datetime import date, datetime
from pathlib import Path

import pandas as pd


def init_db(db_path='logs.db'):
    """Create tables, indexes, and FTS5 virtual table if they don't exist.

    Returns an sqlite3.Connection with WAL mode enabled.
    """
    conn = sqlite3.connect(db_path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')

    conn.executescript('''
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY,
            path TEXT UNIQUE,
            hostname TEXT,
            log_date DATE,
            total_lines INTEGER,
            error_count INTEGER,
            warning_count INTEGER,
            fatal_count INTEGER,
            info_count INTEGER,
            ingested_at TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS errors (
            id INTEGER PRIMARY KEY,
            file_id INTEGER REFERENCES files(id),
            timestamp TIMESTAMP,
            hostname TEXT,
            service TEXT,
            severity TEXT,
            message TEXT,
            pattern TEXT,
            raw_line TEXT,
            context_before TEXT,
            context_after TEXT
        );

        CREATE TABLE IF NOT EXISTS hourly_stats (
            file_id INTEGER REFERENCES files(id),
            hour TIMESTAMP,
            severity TEXT,
            service TEXT,
            count INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_errors_timestamp ON errors(timestamp);
        CREATE INDEX IF NOT EXISTS idx_errors_severity ON errors(severity);
        CREATE INDEX IF NOT EXISTS idx_errors_service ON errors(service);
        CREATE INDEX IF NOT EXISTS idx_errors_pattern ON errors(pattern);
        CREATE INDEX IF NOT EXISTS idx_errors_file_id ON errors(file_id);
        CREATE INDEX IF NOT EXISTS idx_errors_sev_ts ON errors(severity, timestamp);
        CREATE INDEX IF NOT EXISTS idx_errors_file_sev ON errors(file_id, severity);
        CREATE INDEX IF NOT EXISTS idx_hourly_hour ON hourly_stats(hour);
        CREATE INDEX IF NOT EXISTS idx_files_date ON files(log_date);
    ''')

    # FTS5 virtual table - check if it exists first since CREATE ... IF NOT EXISTS
    # isn't supported for virtual tables in all SQLite versions
    has_fts = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='errors_fts'"
    ).fetchone()
    if not has_fts:
        conn.execute('''
            CREATE VIRTUAL TABLE errors_fts USING fts5(
                message, raw_line, content=errors, content_rowid=id
            )
        ''')
        # Triggers to keep FTS index in sync
        conn.executescript('''
            CREATE TRIGGER errors_ai AFTER INSERT ON errors BEGIN
                INSERT INTO errors_fts(rowid, message, raw_line)
                VALUES (new.id, new.message, new.raw_line);
            END;

            CREATE TRIGGER errors_ad AFTER DELETE ON errors BEGIN
                INSERT INTO errors_fts(errors_fts, rowid, message, raw_line)
                VALUES ('delete', old.id, old.message, old.raw_line);
            END;

            CREATE TRIGGER errors_au AFTER UPDATE ON errors BEGIN
                INSERT INTO errors_fts(errors_fts, rowid, message, raw_line)
                VALUES ('delete', old.id, old.message, old.raw_line);
                INSERT INTO errors_fts(rowid, message, raw_line)
                VALUES (new.id, new.message, new.raw_line);
            END;
        ''')

    conn.commit()
    return conn


def is_file_ingested(conn, path):
    """Check if a file path has already been ingested."""
    row = conn.execute(
        'SELECT 1 FROM files WHERE path = ?', (str(Path(path).resolve()),)
    ).fetchone()
    return row is not None


def ingest_file(conn, file_path, total_lines, error_rows, hourly_counts):
    """Insert file metadata, error rows, and hourly stats in one transaction.

    Args:
        conn: sqlite3.Connection
        file_path: path to the log file
        total_lines: total number of parsed lines
        error_rows: list of dicts with keys: timestamp, hostname, service,
                    severity, message, pattern, raw_line, context_before, context_after
        hourly_counts: dict of (hour, severity, service) -> count
    """
    resolved = str(Path(file_path).resolve())

    # Count severities from hourly_counts
    sev_totals = {'ERROR': 0, 'WARNING': 0, 'FATAL': 0, 'INFO': 0}
    hostname = None
    log_date = None
    for (hour, severity, service), count in hourly_counts.items():
        if severity in sev_totals:
            sev_totals[severity] += count
        if hostname is None and error_rows:
            hostname = error_rows[0].get('hostname')
        if log_date is None:
            log_date = hour.date() if hasattr(hour, 'date') else None

    # Fallback hostname/date from error_rows
    if error_rows:
        if hostname is None:
            hostname = error_rows[0].get('hostname')
        if log_date is None and error_rows[0].get('timestamp'):
            log_date = error_rows[0]['timestamp'].date()

    with conn:
        cursor = conn.execute(
            '''INSERT INTO files (path, hostname, log_date, total_lines,
               error_count, warning_count, fatal_count, info_count, ingested_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (resolved, hostname, log_date, total_lines,
             sev_totals['ERROR'], sev_totals['WARNING'],
             sev_totals['FATAL'], sev_totals['INFO'],
             datetime.now().isoformat())
        )
        file_id = cursor.lastrowid

        if error_rows:
            conn.executemany(
                '''INSERT INTO errors (file_id, timestamp, hostname, service,
                   severity, message, pattern, raw_line, context_before, context_after)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                [(file_id,
                  row['timestamp'].isoformat() if hasattr(row['timestamp'], 'isoformat') else row['timestamp'],
                  row['hostname'], row['service'], row['severity'],
                  row['message'], row['pattern'], row['raw_line'],
                  row['context_before'], row['context_after'])
                 for row in error_rows]
            )

        if hourly_counts:
            conn.executemany(
                '''INSERT INTO hourly_stats (file_id, hour, severity, service, count)
                   VALUES (?, ?, ?, ?, ?)''',
                [(file_id,
                  hour.isoformat() if hasattr(hour, 'isoformat') else hour,
                  severity, service, count)
                 for (hour, severity, service), count in hourly_counts.items()]
            )


def query_dates(conn):
    """List all dates that have ingested data, sorted descending."""
    rows = conn.execute(
        'SELECT DISTINCT log_date FROM files WHERE log_date IS NOT NULL ORDER BY log_date DESC'
    ).fetchall()
    results = []
    for (d,) in rows:
        if isinstance(d, str):
            results.append(date.fromisoformat(d))
        else:
            results.append(d)
    return results


def query_overview(conn, target_date):
    """Get totals for a single day across all ingested files.

    Returns dict with keys: total, errors, warnings, fatals, infos, services
    """
    date_str = target_date.isoformat() if hasattr(target_date, 'isoformat') else target_date

    row = conn.execute(
        '''SELECT COALESCE(SUM(total_lines), 0),
                  COALESCE(SUM(error_count), 0),
                  COALESCE(SUM(warning_count), 0),
                  COALESCE(SUM(fatal_count), 0),
                  COALESCE(SUM(info_count), 0)
           FROM files WHERE log_date = ?''',
        (date_str,)
    ).fetchone()

    services = conn.execute(
        '''SELECT DISTINCT service FROM errors e
           JOIN files f ON e.file_id = f.id
           WHERE f.log_date = ?
           ORDER BY service''',
        (date_str,)
    ).fetchall()

    return {
        'total': row[0],
        'errors': row[1],
        'warnings': row[2],
        'fatals': row[3],
        'infos': row[4],
        'services': [s[0] for s in services],
    }


def query_top_errors(conn, target_date, severity=None, top_n=15, services=None):
    """Top error patterns for a day, grouped by normalized pattern.

    Returns list of (count, pattern, example_raw, service, severity) tuples.
    """
    date_str = target_date.isoformat() if hasattr(target_date, 'isoformat') else target_date

    conditions = ['f.log_date = ?']
    params = [date_str]

    if severity:
        conditions.append('e.severity = ?')
        params.append(severity)
    else:
        conditions.append("e.severity IN ('FATAL', 'ERROR', 'WARNING')")

    if services:
        placeholders = ','.join('?' * len(services))
        conditions.append(f'e.service IN ({placeholders})')
        params.extend(services)

    where = ' AND '.join(conditions)
    params.append(top_n)

    rows = conn.execute(
        f'''SELECT COUNT(*) as cnt, e.pattern,
                   MIN(e.raw_line) as example, MIN(e.service) as svc, e.severity
            FROM errors e JOIN files f ON e.file_id = f.id
            WHERE {where}
            GROUP BY e.pattern, e.severity
            ORDER BY cnt DESC LIMIT ?''',
        params
    ).fetchall()

    return [(count, pat, raw, svc, sev) for count, pat, raw, svc, sev in rows]


def query_hourly(conn, target_date):
    """Hourly counts by severity for a timeline chart.

    Returns a DataFrame with columns: hour, FATAL, ERROR, WARNING, INFO
    """
    date_str = target_date.isoformat() if hasattr(target_date, 'isoformat') else target_date

    rows = conn.execute(
        '''SELECT hs.hour, hs.severity, SUM(hs.count) as total
           FROM hourly_stats hs JOIN files f ON hs.file_id = f.id
           WHERE f.log_date = ?
           GROUP BY hs.hour, hs.severity
           ORDER BY hs.hour''',
        (date_str,)
    ).fetchall()

    if not rows:
        return pd.DataFrame(columns=['hour', 'FATAL', 'ERROR', 'WARNING', 'INFO'])

    data = {}
    for hour_str, severity, total in rows:
        if hour_str not in data:
            data[hour_str] = {'hour': hour_str, 'FATAL': 0, 'ERROR': 0, 'WARNING': 0, 'INFO': 0}
        if severity in data[hour_str]:
            data[hour_str][severity] = total

    df = pd.DataFrame(list(data.values()))
    df['hour'] = pd.to_datetime(df['hour'])
    df = df.sort_values('hour').reset_index(drop=True)

    # Fill gaps: generate all hours from min to max so the chart is continuous
    if len(df) > 1:
        all_hours = pd.date_range(df['hour'].min(), df['hour'].max(), freq='h')
        full_df = pd.DataFrame({'hour': all_hours})
        df = full_df.merge(df, on='hour', how='left').fillna(0)
        for col in ['FATAL', 'ERROR', 'WARNING', 'INFO']:
            df[col] = df[col].astype(int)

    return df


def query_service_counts(conn, target_date):
    """Error+warning counts per service for a day, sorted descending.

    Returns a pandas Series.
    """
    date_str = target_date.isoformat() if hasattr(target_date, 'isoformat') else target_date

    rows = conn.execute(
        '''SELECT e.service, COUNT(*) as cnt
           FROM errors e JOIN files f ON e.file_id = f.id
           WHERE f.log_date = ? AND e.severity IN ('FATAL', 'ERROR', 'WARNING')
           GROUP BY e.service ORDER BY cnt DESC''',
        (date_str,)
    ).fetchall()

    if not rows:
        return pd.Series(dtype=int)
    return pd.Series(
        {service: count for service, count in rows},
        name='count'
    )


def query_search(conn, query, date_from=None, date_to=None,
                 severity=None, service=None, limit=500):
    """FTS5 full-text search across all ingested files.

    Returns list of dicts with keys: id, timestamp, hostname, service,
    severity, message, raw_line, context_before, context_after
    """
    # Build the query with optional filters via JOIN
    conditions = []
    params = []

    # FTS match - strip quotes then wrap each term in "" to handle special chars
    clean = query.replace('"', '').replace("'", '')
    safe_query = ' '.join(f'"{term}"' for term in clean.split() if term)
    conditions.append("errors_fts MATCH ?")
    params.append(safe_query)

    sql = '''
        SELECT e.id, e.timestamp, e.hostname, e.service, e.severity,
               e.message, e.raw_line, e.context_before, e.context_after
        FROM errors_fts
        JOIN errors e ON e.id = errors_fts.rowid
        JOIN files f ON e.file_id = f.id
    '''

    where_extra = []
    if date_from:
        where_extra.append('f.log_date >= ?')
        params.append(date_from.isoformat() if hasattr(date_from, 'isoformat') else date_from)
    if date_to:
        where_extra.append('f.log_date <= ?')
        params.append(date_to.isoformat() if hasattr(date_to, 'isoformat') else date_to)
    if severity:
        where_extra.append('e.severity = ?')
        params.append(severity)
    if service:
        where_extra.append('e.service = ?')
        params.append(service)

    where_clause = ' AND '.join(conditions + where_extra)
    sql += f' WHERE {where_clause} ORDER BY e.timestamp DESC LIMIT ?'
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    return [
        {
            'id': r[0], 'timestamp': r[1], 'hostname': r[2],
            'service': r[3], 'severity': r[4], 'message': r[5],
            'raw_line': r[6], 'context_before': r[7], 'context_after': r[8],
        }
        for r in rows
    ]


def get_error_context(conn, error_id):
    """Fetch a single error with its context lines.

    Returns dict with all error columns, or None if not found.
    """
    row = conn.execute(
        '''SELECT id, file_id, timestamp, hostname, service, severity,
                  message, pattern, raw_line, context_before, context_after
           FROM errors WHERE id = ?''',
        (error_id,)
    ).fetchone()

    if not row:
        return None

    return {
        'id': row[0], 'file_id': row[1], 'timestamp': row[2],
        'hostname': row[3], 'service': row[4], 'severity': row[5],
        'message': row[6], 'pattern': row[7], 'raw_line': row[8],
        'context_before': row[9], 'context_after': row[10],
    }


# --- Trend queries (date ranges) ---

def _date_str(d):
    return d.isoformat() if hasattr(d, 'isoformat') else d


def query_daily_counts(conn, date_from=None, date_to=None):
    """Daily severity counts across all files for a date range.

    Returns DataFrame with columns: date, FATAL, ERROR, WARNING, INFO
    """
    conditions = []
    params = []
    if date_from:
        conditions.append('log_date >= ?')
        params.append(_date_str(date_from))
    if date_to:
        conditions.append('log_date <= ?')
        params.append(_date_str(date_to))

    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''

    rows = conn.execute(
        f'''SELECT log_date,
                   COALESCE(SUM(fatal_count), 0),
                   COALESCE(SUM(error_count), 0),
                   COALESCE(SUM(warning_count), 0),
                   COALESCE(SUM(info_count), 0)
            FROM files {where}
            GROUP BY log_date ORDER BY log_date''',
        params
    ).fetchall()

    if not rows:
        return pd.DataFrame(columns=['date', 'FATAL', 'ERROR', 'WARNING', 'INFO'])

    df = pd.DataFrame(rows, columns=['date', 'FATAL', 'ERROR', 'WARNING', 'INFO'])
    df['date'] = pd.to_datetime(df['date'])
    return df


def query_top_errors_range(conn, date_from=None, date_to=None, top_n=15):
    """Top error patterns across a date range.

    Returns list of (count, pattern, severity, example_raw, services_csv).
    """
    conditions = ['e.severity IN (\'FATAL\', \'ERROR\', \'WARNING\')']
    params = []
    if date_from:
        conditions.append('f.log_date >= ?')
        params.append(_date_str(date_from))
    if date_to:
        conditions.append('f.log_date <= ?')
        params.append(_date_str(date_to))

    where = ' AND '.join(conditions)
    params.append(top_n)

    rows = conn.execute(
        f'''SELECT COUNT(*) as cnt, e.pattern, e.severity,
                   MIN(e.raw_line), GROUP_CONCAT(DISTINCT e.service)
            FROM errors e JOIN files f ON e.file_id = f.id
            WHERE {where}
            GROUP BY e.pattern, e.severity
            ORDER BY cnt DESC LIMIT ?''',
        params
    ).fetchall()

    return [(c, pat, sev, raw, svcs) for c, pat, sev, raw, svcs in rows]


def query_service_counts_range(conn, date_from=None, date_to=None):
    """Error+warning counts per service across a date range, sorted descending.

    Returns a pandas Series.
    """
    conditions = ['e.severity IN (\'FATAL\', \'ERROR\', \'WARNING\')']
    params = []
    if date_from:
        conditions.append('f.log_date >= ?')
        params.append(_date_str(date_from))
    if date_to:
        conditions.append('f.log_date <= ?')
        params.append(_date_str(date_to))

    where = ' AND '.join(conditions)

    rows = conn.execute(
        f'''SELECT e.service, COUNT(*) as cnt
            FROM errors e JOIN files f ON e.file_id = f.id
            WHERE {where}
            GROUP BY e.service ORDER BY cnt DESC''',
        params
    ).fetchall()

    if not rows:
        return pd.Series(dtype=int)
    return pd.Series({svc: cnt for svc, cnt in rows}, name='count')


def query_file_list(conn):
    """List all ingested files with metadata."""
    rows = conn.execute(
        '''SELECT id, path, hostname, log_date, total_lines,
                  error_count, warning_count, fatal_count, ingested_at
           FROM files ORDER BY log_date DESC, path'''
    ).fetchall()
    return [
        {'id': r[0], 'path': r[1], 'hostname': r[2], 'log_date': r[3],
         'total_lines': r[4], 'error_count': r[5], 'warning_count': r[6],
         'fatal_count': r[7], 'ingested_at': r[8]}
        for r in rows
    ]
