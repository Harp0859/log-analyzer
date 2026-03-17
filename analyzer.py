"""Core log analysis engine - pure logic, no UI."""

import gzip
import re
import json
import urllib.request
import urllib.error
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd

# --- Syslog line parser ---

LINE_RE = re.compile(
    r'^(\w{3}\s+\d+\s+\d+:\d+:\d+\.\d+)\s+'  # timestamp
    r'(\S+)\s+'                                  # hostname
    r'(\S+?)(?:\[(\d+)\])?:\s*'                  # service[pid]:
    r'(.*)$'                                      # message
)

CURRENT_YEAR = datetime.now().year

# --- Severity classification ---

ERROR_PATTERNS = re.compile(
    r'error|ERROR|Error'
    r'|Buffer I/O error'
    r'|Connection timed out'
    r'|Connection refused'
    r"|Couldn't unmount",
    re.IGNORECASE,
)

FATAL_PATTERNS = re.compile(r'fatal|FATAL', re.IGNORECASE)

WARNING_PATTERNS = re.compile(r'warning|WARNING|WARN', re.IGNORECASE)

# --- Normalization regexes ---

NORM_RULES = [
    (re.compile(r'\[\d+\]'), '[PID]'),
    (re.compile(r'\b\d{1,3}(?:\.\d{1,3}){3}\b'), '<IP>'),
    (re.compile(r'/dev/(?:sd[a-z]+|dm-\d+|nvme\d+n\d+)'), '<DEV>'),
    (re.compile(r'session-[\w.]+'), 'session-<ID>'),
    (re.compile(r'vsa-[0-9a-fA-F]+'), 'vsa-<ID>'),
    (re.compile(r'vc-\d+'), 'vc-<N>'),
    (re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'), '<UUID>'),
    (re.compile(r'(?<![a-zA-Z])\d{4,}(?![a-zA-Z])'), '<N>'),
]


def open_log(path):
    """Open a log file, auto-detecting gzip. Returns a text line iterator."""
    path = Path(path)
    if path.suffix == '.gz':
        return gzip.open(path, 'rt', errors='replace')
    return open(path, 'r', errors='replace')


def parse_timestamp(ts_str, year=None):
    """Parse syslog timestamp string into datetime.

    Args:
        year: year to use (syslog has no year). Defaults to current year.
    """
    if year is None:
        year = CURRENT_YEAR
    try:
        dt = datetime.strptime(ts_str.split('.')[0], '%b %d %H:%M:%S')
        return dt.replace(year=year)
    except ValueError:
        return None


def parse_line(line, year=None):
    """Parse a single syslog line into components. Returns dict or None."""
    m = LINE_RE.match(line.strip())
    if not m:
        return None
    ts_str, host, service, pid, msg = m.groups()
    ts = parse_timestamp(ts_str, year)
    if ts is None:
        return None
    return {
        'timestamp': ts,
        'host': host,
        'service': service,
        'pid': pid or '',
        'message': msg,
        'raw': line.strip(),
    }


def classify_severity(msg):
    """Classify a log message into FATAL/ERROR/WARNING/INFO."""
    if FATAL_PATTERNS.search(msg):
        return 'FATAL'
    if ERROR_PATTERNS.search(msg):
        return 'ERROR'
    if WARNING_PATTERNS.search(msg):
        return 'WARNING'
    return 'INFO'


def normalize_message(msg):
    """Normalize a message by replacing variable parts with placeholders."""
    result = msg
    for pattern, replacement in NORM_RULES:
        result = pattern.sub(replacement, result)
    return result


def parse_log_file(source):
    """Parse an entire log file into a pandas DataFrame.

    Args:
        source: file path (str/Path) or file-like object (from st.file_uploader)

    Returns:
        DataFrame with columns: timestamp, host, service, pid, message, raw, severity, pattern
    """
    rows = []

    if hasattr(source, 'read'):
        # file-like object (uploaded file)
        content = source.read()
        if isinstance(content, bytes):
            # check if gzip
            if content[:2] == b'\x1f\x8b':
                import io
                content = gzip.decompress(content).decode('utf-8', errors='replace')
            else:
                content = content.decode('utf-8', errors='replace')
        for line in content.splitlines():
            parsed = parse_line(line)
            if parsed:
                rows.append(parsed)
    else:
        # file path
        with open_log(source) as f:
            for line in f:
                parsed = parse_line(line)
                if parsed:
                    rows.append(parsed)

    if not rows:
        return pd.DataFrame(columns=['timestamp', 'host', 'service', 'pid',
                                     'message', 'raw', 'severity', 'pattern'])

    df = pd.DataFrame(rows)
    df['severity'] = df['message'].apply(classify_severity)
    df['pattern'] = df['message'].apply(normalize_message)
    return df


def filter_by_time(df, start=None, end=None):
    """Filter DataFrame by time range."""
    if df.empty:
        return df
    mask = pd.Series(True, index=df.index)
    if start:
        mask &= df['timestamp'] >= start
    if end:
        mask &= df['timestamp'] <= end
    return df[mask]


def filter_by_severity(df, severities):
    """Filter DataFrame by severity levels."""
    if not severities or df.empty:
        return df
    return df[df['severity'].isin(severities)]


def filter_by_service(df, services):
    """Filter DataFrame by service names."""
    if not services or df.empty:
        return df
    return df[df['service'].isin(services)]


def group_messages(df, top_n=15):
    """Group messages by normalized pattern, return top N per severity.

    Returns dict: {severity: [(count, pattern, example_raw_line), ...]}
    """
    if df.empty:
        return {}

    result = {}
    for sev in ['FATAL', 'ERROR', 'WARNING']:
        sev_df = df[df['severity'] == sev]
        if sev_df.empty:
            continue

        pattern_counts = sev_df['pattern'].value_counts().head(top_n)
        groups = []
        for pattern, count in pattern_counts.items():
            example = sev_df[sev_df['pattern'] == pattern].iloc[0]['raw']
            groups.append((count, pattern, example))
        result[sev] = groups

    return result


def get_service_error_counts(df):
    """Get error+warning counts per service, sorted descending."""
    if df.empty:
        return pd.Series(dtype=int)
    err_df = df[df['severity'].isin(['FATAL', 'ERROR', 'WARNING'])]
    if err_df.empty:
        return pd.Series(dtype=int)
    return err_df['service'].value_counts()


def get_timeline_counts(df, freq='h'):
    """Get error/warning counts bucketed by time interval.

    Returns DataFrame with columns: timestamp, ERROR, WARNING, FATAL
    """
    if df.empty:
        return pd.DataFrame()

    err_df = df[df['severity'].isin(['FATAL', 'ERROR', 'WARNING'])].copy()
    if err_df.empty:
        return pd.DataFrame()

    err_df = err_df.set_index('timestamp')
    pivot = err_df.groupby([pd.Grouper(freq=freq), 'severity']).size().unstack(fill_value=0)
    for col in ['FATAL', 'ERROR', 'WARNING']:
        if col not in pivot.columns:
            pivot[col] = 0
    return pivot.reset_index()


def search_logs(df, query, context_lines=5):
    """Search log lines for a query string.

    Returns list of dicts with 'match_idx', 'line', 'context_before', 'context_after'
    """
    if df.empty or not query:
        return []

    query_lower = query.lower()
    matches = []
    match_indices = df.index[df['raw'].str.lower().str.contains(query_lower, na=False)]

    for idx in match_indices[:200]:  # cap at 200 results
        pos = df.index.get_loc(idx)
        before_start = max(0, pos - context_lines)
        after_end = min(len(df), pos + context_lines + 1)

        matches.append({
            'match_idx': idx,
            'line': df.iloc[pos]['raw'],
            'severity': df.iloc[pos]['severity'],
            'timestamp': df.iloc[pos]['timestamp'],
            'service': df.iloc[pos]['service'],
            'context_before': df.iloc[before_start:pos]['raw'].tolist(),
            'context_after': df.iloc[pos + 1:after_end]['raw'].tolist(),
        })

    return matches


# --- LLM Integration (Qwen via LM Studio) ---

DEFAULT_LM_URL = 'http://localhost:1234/v1'
DEFAULT_MODEL = 'qwen/qwen3.5-9b'


def _llm_call(messages, url=DEFAULT_LM_URL, model=DEFAULT_MODEL):
    """Make a chat completion call to LM Studio. Returns response text or error string."""
    endpoint = f'{url}/chat/completions'
    payload = json.dumps({
        'model': model,
        'messages': messages,
        'temperature': 0.3,
        'max_tokens': 2048,
    }).encode('utf-8')

    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={'Content-Type': 'application/json'},
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            return data['choices'][0]['message']['content']
    except urllib.error.URLError:
        return None
    except Exception as e:
        return f'LLM error: {e}'


def check_llm_available(url=DEFAULT_LM_URL):
    """Check if LM Studio is reachable."""
    try:
        req = urllib.request.Request(f'{url}/models')
        with urllib.request.urlopen(req, timeout=5):
            return True
    except Exception:
        return False


def llm_summarize(grouped_errors, url=DEFAULT_LM_URL, model=DEFAULT_MODEL):
    """Send top error/warning patterns to Qwen for root cause analysis.

    Args:
        grouped_errors: dict from group_messages()

    Returns:
        str: Qwen's analysis or error message
    """
    lines = []
    for sev in ['FATAL', 'ERROR', 'WARNING']:
        if sev not in grouped_errors:
            continue
        lines.append(f'\n## {sev}S:')
        for count, pattern, example in grouped_errors[sev][:15]:
            lines.append(f'  [{count}x] {pattern}')

    if not lines:
        return 'No errors or warnings to analyze.'

    prompt = (
        'You are a storage systems expert analyzing syslog entries from a '
        'Zadara storage node. Below are the top recurring error and warning '
        'patterns with their occurrence counts.\n\n'
        'For each group of related errors:\n'
        '1. Give a short root cause hypothesis\n'
        '2. Rate severity (Critical / High / Medium / Low)\n'
        '3. Suggest what to check or fix\n\n'
        'Group related errors together. Be concise.\n\n'
        '--- ERROR/WARNING PATTERNS ---\n'
        + '\n'.join(lines)
    )

    messages = [{'role': 'user', 'content': prompt}]
    result = _llm_call(messages, url, model)
    if result is None:
        return 'Could not reach LM Studio. Make sure it is running at ' + url
    return result


def llm_chat(user_message, chat_history, error_context, url=DEFAULT_LM_URL, model=DEFAULT_MODEL):
    """Chat with Qwen about the log file.

    Args:
        user_message: the user's question
        chat_history: list of prior {'role': ..., 'content': ...} dicts
        error_context: string summary of top errors to give Qwen context

    Returns:
        str: Qwen's reply or error message
    """
    system_msg = (
        'You are a storage systems expert helping an operator analyze syslog '
        'entries from a Zadara storage node (qa17-sn-1). You have access to '
        'the following summary of the log file\'s top errors and warnings. '
        'Use this context to answer questions. Be concise and practical.\n\n'
        '--- LOG CONTEXT ---\n' + error_context
    )

    messages = [{'role': 'system', 'content': system_msg}]
    messages.extend(chat_history)
    messages.append({'role': 'user', 'content': user_message})

    result = _llm_call(messages, url, model)
    if result is None:
        return 'Could not reach LM Studio. Make sure it is running at ' + url
    return result


def build_error_context(grouped_errors, service_counts):
    """Build a text summary of errors for the AI chat system prompt."""
    lines = []
    for sev in ['FATAL', 'ERROR', 'WARNING']:
        if sev not in grouped_errors:
            continue
        lines.append(f'\n{sev}S:')
        for count, pattern, _ in grouped_errors[sev][:10]:
            lines.append(f'  [{count}x] {pattern}')

    if not service_counts.empty:
        lines.append('\nErrors by service:')
        for svc, cnt in service_counts.head(10).items():
            lines.append(f'  {svc}: {cnt}')

    return '\n'.join(lines) if lines else 'No significant errors found.'


def generate_text_report(df_or_overview, grouped, service_counts, ai_summary=None):
    """Generate a downloadable text report.

    Args:
        df_or_overview: either a DataFrame (file mode) or a dict with
                       total/errors/warnings/fatals/date keys (DB mode)
    """
    lines = []
    lines.append('=' * 60)
    lines.append('LOG ANALYSIS REPORT')
    lines.append('=' * 60)

    if isinstance(df_or_overview, dict):
        ov = df_or_overview
        if 'date' in ov:
            lines.append(f'Date: {ov["date"]}')
        lines.append(f'Total lines: {ov["total"]:,}')
        lines.append('')
        lines.append('--- SEVERITY SUMMARY ---')
        for sev, key in [('FATAL', 'fatals'), ('ERROR', 'errors'),
                         ('WARNING', 'warnings'), ('INFO', 'infos')]:
            lines.append(f'  {sev:8s}: {ov.get(key, 0):,}')
        lines.append('')
    elif hasattr(df_or_overview, 'empty') and not df_or_overview.empty:
        df = df_or_overview
        lines.append(f'Period: {df["timestamp"].min()} - {df["timestamp"].max()}')
        lines.append(f'Total lines parsed: {len(df):,}')
        lines.append('')
        lines.append('--- SEVERITY SUMMARY ---')
        for sev in ['FATAL', 'ERROR', 'WARNING', 'INFO']:
            count = (df['severity'] == sev).sum()
            lines.append(f'  {sev:8s}: {count:,}')
        lines.append('')

    for sev in ['FATAL', 'ERROR', 'WARNING']:
        if sev not in grouped:
            continue
        lines.append(f'--- TOP {sev}S ---')
        for i, (count, pattern, example) in enumerate(grouped[sev], 1):
            lines.append(f'  {i:2d}. [{count:,}x] {pattern[:100]}')
            lines.append(f'      e.g.: {example[:120]}')
        lines.append('')

    if not service_counts.empty:
        lines.append('--- ERRORS BY SERVICE ---')
        for svc, cnt in service_counts.head(15).items():
            lines.append(f'  {svc}: {cnt:,}')
        lines.append('')

    if ai_summary:
        lines.append('--- AI ANALYSIS (Qwen) ---')
        lines.append(ai_summary)
        lines.append('')

    return '\n'.join(lines)
