"""Log Analyzer - Daily Monitoring Dashboard."""

import os
import streamlit as st
import pandas as pd
from pathlib import Path
from datetime import datetime, date, time, timedelta

from analyzer import (
    parse_log_file, filter_by_time, filter_by_severity, filter_by_service,
    group_messages, get_service_error_counts, get_timeline_counts,
    search_logs, llm_summarize, llm_chat, build_error_context,
    check_llm_available, generate_text_report,
    DEFAULT_LM_URL, DEFAULT_MODEL,
)
from db import (
    init_db, query_dates, query_overview, query_top_errors, query_hourly,
    query_service_counts, query_search, get_error_context,
    query_daily_counts, query_top_errors_range, query_service_counts_range,
    query_file_list,
)

st.set_page_config(
    page_title='Log Analyzer',
    page_icon=':bar_chart:',
    layout='wide',
)

# Subtle CSS tweaks
st.markdown('''<style>
    .stMetric { padding: 12px 0; }
    div[data-testid="stExpander"] details summary p { font-size: 0.9rem; }
</style>''', unsafe_allow_html=True)

DB_PATH = 'logs.db'

SEV_COLORS = {'FATAL': '#d62728', 'ERROR': '#ff4b4b', 'WARNING': '#ffa600', 'INFO': '#636efa'}
SEV_EMOJI = {'FATAL': '\U0001f534', 'ERROR': '\U0001f534', 'WARNING': '\U0001f7e1', 'INFO': '\u26aa'}


# --- Cached data ---

@st.cache_data(show_spinner='Parsing log file...')
def cached_parse(file_bytes=None, file_path=None):
    if file_bytes is not None:
        import io
        return parse_log_file(io.BytesIO(file_bytes))
    elif file_path:
        return parse_log_file(file_path)
    return pd.DataFrame()


@st.cache_resource
def get_db_connection():
    if os.path.exists(DB_PATH):
        return init_db(DB_PATH)
    return None


# --- Sidebar ---

def sidebar_controls():
    st.sidebar.header('Log Source')

    has_db = os.path.exists(DB_PATH)
    source_options = ['Upload file', 'Pick from folder']
    if has_db:
        source_options.insert(0, 'Ingested Database')

    source_mode = st.sidebar.radio('Input method', source_options, horizontal=True)

    ctx = {
        'mode': 'db' if source_mode == 'Ingested Database' else 'file',
        'df': pd.DataFrame(),
        'conn': None,
        'selected_date': None,
        'date_range': (None, None),
        'settings': {},
    }

    if source_mode == 'Ingested Database':
        conn = get_db_connection()
        if conn is None:
            st.sidebar.error('Database not found. Run `ingest.py` first.')
            return ctx

        ctx['conn'] = conn
        available_dates = query_dates(conn)
        if not available_dates:
            st.sidebar.warning('No data ingested yet.')
            return ctx

        selected = st.sidebar.selectbox(
            'Date', available_dates,
            format_func=lambda d: d.strftime('%Y-%m-%d (%A)'),
        )
        ctx['selected_date'] = selected

        # Trends range
        with st.sidebar.expander('Trends date range', expanded=False):
            range_preset = st.selectbox(
                'Period', ['All time', 'Last 7 days', 'Last 30 days', 'Last 90 days', 'Custom']
            )
            if range_preset == 'Custom':
                col1, col2 = st.columns(2)
                with col1:
                    d_from = st.date_input('From', value=available_dates[-1])
                with col2:
                    d_to = st.date_input('To', value=available_dates[0])
                ctx['date_range'] = (d_from, d_to)
            elif range_preset == 'All time':
                ctx['date_range'] = (available_dates[-1], available_dates[0])
            else:
                days = {'Last 7 days': 7, 'Last 30 days': 30, 'Last 90 days': 90}[range_preset]
                d_to = available_dates[0]
                d_from = d_to - timedelta(days=days)
                ctx['date_range'] = (d_from, d_to)

        overview = query_overview(conn, selected)
        services = overview.get('services', [])

    elif source_mode == 'Upload file':
        uploaded = st.sidebar.file_uploader(
            'Drop a syslog file (.gz or plain)', type=['gz', 'log', 'txt']
        )
        if uploaded:
            ctx['df'] = cached_parse(file_bytes=uploaded.getvalue())

    else:
        log_dir = st.sidebar.text_input('Log directory', value='/var/log/')
        log_dir_path = Path(log_dir)
        if log_dir_path.is_dir():
            log_files = sorted(
                [f for f in log_dir_path.iterdir()
                 if f.is_file() and ('syslog' in f.name or f.suffix in ('.gz', '.log'))],
                key=lambda f: f.stat().st_mtime, reverse=True,
            )
            if log_files:
                selected_file = st.sidebar.selectbox(
                    'Select log file', log_files, format_func=lambda f: f.name,
                )
                if selected_file:
                    ctx['df'] = cached_parse(file_path=str(selected_file))
            else:
                st.sidebar.warning('No syslog files found.')
        else:
            st.sidebar.warning('Directory not found.')

    # No data loaded
    if ctx['mode'] == 'file' and ctx['df'].empty:
        return ctx
    if ctx['mode'] == 'db' and ctx['conn'] is None:
        return ctx

    # --- Filters ---
    st.sidebar.markdown('---')

    if ctx['mode'] == 'file':
        df = ctx['df']
        min_ts, max_ts = df['timestamp'].min(), df['timestamp'].max()
        col1, col2 = st.sidebar.columns(2)
        with col1:
            start_date = st.date_input('From', value=min_ts.date(),
                                       min_value=min_ts.date(), max_value=max_ts.date())
            start_time = st.time_input('Start time', value=time(0, 0))
        with col2:
            end_date = st.date_input('To', value=max_ts.date(),
                                     min_value=min_ts.date(), max_value=max_ts.date())
            end_time = st.time_input('End time', value=time(23, 59))
        ctx['settings']['start'] = datetime.combine(start_date, start_time)
        ctx['settings']['end'] = datetime.combine(end_date, end_time)
        services = sorted(df['service'].unique())

    severities = st.sidebar.multiselect(
        'Severity', ['FATAL', 'ERROR', 'WARNING', 'INFO'],
        default=['FATAL', 'ERROR', 'WARNING'],
    )
    selected_services = st.sidebar.multiselect('Service', services, default=[])
    top_n = st.sidebar.slider('Top N patterns', 5, 50, 15)

    # AI Settings - collapsed
    with st.sidebar.expander('AI Settings'):
        lm_url = st.text_input('LM Studio URL', value=DEFAULT_LM_URL)
        model = st.text_input('Model', value=DEFAULT_MODEL)

    ctx['settings'].update({
        'severities': severities,
        'services': selected_services,
        'top_n': top_n,
        'lm_url': lm_url,
        'model': model,
    })

    return ctx


# --- Shared helpers ---

def _extract_service(raw_line):
    parts = raw_line.split()
    if len(parts) >= 4:
        return parts[3].split('[')[0].rstrip(':')
    return '?'


def _render_metrics(total, errors, warnings, fatals):
    """Render the 4 metric cards."""
    c1, c2, c3, c4 = st.columns(4)
    c1.metric('Total Lines', f'{total:,}')
    c2.metric('Errors', f'{errors:,}')
    c3.metric('Warnings', f'{warnings:,}')
    c4.metric('Fatals', f'{fatals:,}')


def _render_error_table(table_data):
    """Render a styled error patterns table."""
    if not table_data:
        return
    df = pd.DataFrame(table_data)
    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        column_config={
            'Count': st.column_config.NumberColumn(width='small'),
            'Severity': st.column_config.TextColumn(width='small'),
            'Service': st.column_config.TextColumn(width='small'),
            'Pattern': st.column_config.TextColumn(width='large'),
        },
    )


def _render_service_chart(svc_counts):
    """Render the service breakdown bar chart."""
    if svc_counts.empty:
        return
    chart_df = svc_counts.head(15).reset_index()
    chart_df.columns = ['Service', 'Count']
    st.bar_chart(chart_df, x='Service', y='Count')


def _render_timeline(chart_data, index_col):
    """Render the error/warning timeline area chart."""
    if chart_data.empty:
        st.info('No data to chart.')
        return
    chart_df = chart_data.set_index(index_col)
    cols = [c for c in ['ERROR', 'WARNING', 'FATAL'] if c in chart_df.columns]
    st.area_chart(chart_df[cols], color=[SEV_COLORS.get(c, '#636efa') for c in cols])


def _render_search_result(r, expanded=False, is_db=False):
    """Render a single search result with context."""
    if is_db:
        ts = r['timestamp'][:19] if r['timestamp'] else '?'
        line = r['raw_line']
        sev = r['severity']
        svc = r['service']
        msg = r['message'][:80]
        ctx_before = r['context_before']
        ctx_after = r['context_after']
    else:
        ts = r['timestamp'].strftime('%b %d %H:%M:%S')
        line = r['line']
        sev = r['severity']
        svc = r['service']
        msg = line[:80]
        ctx_before = r.get('context_before', [])
        ctx_after = r.get('context_after', [])

    icon = SEV_EMOJI.get(sev, '\u26aa')
    with st.expander(f"{icon} {ts} **[{svc}]** {msg}", expanded=expanded):
        st.code(line, language='text')
        col1, col2 = st.columns(2)
        with col1:
            if ctx_before:
                st.caption('Before:')
                if isinstance(ctx_before, list):
                    st.text('\n'.join(ctx_before))
                else:
                    st.text(ctx_before)
        with col2:
            if ctx_after:
                st.caption('After:')
                if isinstance(ctx_after, list):
                    st.text('\n'.join(ctx_after))
                else:
                    st.text(ctx_after)


# =====================================================================
# FILE MODE
# =====================================================================

def render_overview_file(ctx):
    df, settings = ctx['df'], ctx['settings']

    filtered = filter_by_time(df, settings['start'], settings['end'])
    filtered = filter_by_severity(filtered, settings['severities'])
    filtered = filter_by_service(filtered, settings['services'])

    if filtered.empty:
        st.warning('No log lines match the current filters.')
        return

    total = len(filtered)
    fatals = int((filtered['severity'] == 'FATAL').sum())
    errors = int((filtered['severity'] == 'ERROR').sum())
    warnings = int((filtered['severity'] == 'WARNING').sum())

    _render_metrics(total, errors, warnings, fatals)

    # Timeline + Service chart side by side
    col_chart, col_svc = st.columns([2, 1])
    with col_chart:
        st.subheader('Timeline')
        timeline = get_timeline_counts(filtered)
        _render_timeline(timeline, 'timestamp')
    with col_svc:
        st.subheader('By Service')
        svc_counts = get_service_error_counts(filtered)
        _render_service_chart(svc_counts)

    # Combined error table
    grouped = group_messages(filtered, top_n=settings['top_n'])
    st.subheader('Top Error Patterns')
    table_data = []
    for sev in ['FATAL', 'ERROR', 'WARNING']:
        if sev not in grouped:
            continue
        for count, pattern, example in grouped[sev]:
            table_data.append({
                'Severity': sev, 'Count': count,
                'Service': _extract_service(example),
                'Pattern': pattern[:120],
            })
    _render_error_table(table_data)

    # AI + Download
    col_ai, col_dl = st.columns(2)
    with col_ai:
        if st.button('Analyze with AI', type='primary', use_container_width=True):
            if not check_llm_available(settings['lm_url']):
                st.error(f'Cannot reach LM Studio at {settings["lm_url"]}.')
            else:
                with st.spinner('Qwen is analyzing...'):
                    summary = llm_summarize(grouped, settings['lm_url'], settings['model'])
                    st.session_state['ai_summary'] = summary
    with col_dl:
        report = generate_text_report(
            filtered, grouped, svc_counts, st.session_state.get('ai_summary'),
        )
        st.download_button('Download Report', data=report,
                          file_name='log_report.txt', mime='text/plain',
                          use_container_width=True)

    if 'ai_summary' in st.session_state:
        st.subheader('AI Analysis')
        st.markdown(st.session_state['ai_summary'])


def render_search_file(ctx):
    df, settings = ctx['df'], ctx['settings']

    filtered = filter_by_time(df, settings['start'], settings['end'])
    filtered = filter_by_severity(filtered, settings['severities'])
    filtered = filter_by_service(filtered, settings['services'])

    query = st.text_input('Search logs', placeholder='e.g. Buffer I/O, CCVM, Connection refused...')
    if not query:
        st.info('Type a search term to find matching log lines with context.')
        return

    matches = search_logs(filtered, query)
    if not matches:
        st.warning(f'No matches for "{query}".')
        return

    st.success(f'{len(matches)} matches (max 200)')
    for i, m in enumerate(matches):
        _render_search_result(m, expanded=(i < 3), is_db=False)


def render_ai_chat_file(ctx):
    df, settings = ctx['df'], ctx['settings']

    lm_available = check_llm_available(settings['lm_url'])
    if not lm_available:
        st.warning(f'LM Studio not reachable at {settings["lm_url"]}.')

    filtered = filter_by_time(df, settings['start'], settings['end'])
    grouped = group_messages(filtered, top_n=15)
    svc_counts = get_service_error_counts(filtered)
    error_context = build_error_context(grouped, svc_counts)

    _render_chat_ui(error_context, settings, lm_available, chat_key='file')


# =====================================================================
# DB MODE
# =====================================================================

def render_overview_db(ctx):
    conn, selected_date, settings = ctx['conn'], ctx['selected_date'], ctx['settings']

    overview = query_overview(conn, selected_date)
    _render_metrics(overview['total'], overview['errors'], overview['warnings'], overview['fatals'])

    # Timeline + Service chart side by side
    col_chart, col_svc = st.columns([2, 1])
    with col_chart:
        st.subheader('Timeline')
        hourly = query_hourly(conn, selected_date)
        _render_timeline(hourly, 'hour')
    with col_svc:
        st.subheader('By Service')
        svc_counts = query_service_counts(conn, selected_date)
        _render_service_chart(svc_counts)

    # Combined error table
    st.subheader('Top Error Patterns')
    svc_filter = settings['services'] or None
    table_data = []
    for sev in ['FATAL', 'ERROR', 'WARNING']:
        if sev not in settings['severities']:
            continue
        top = query_top_errors(conn, selected_date, severity=sev,
                              top_n=settings['top_n'], services=svc_filter)
        for count, pattern, raw, service, severity in top:
            table_data.append({
                'Severity': severity, 'Count': count,
                'Service': service, 'Pattern': pattern[:120],
            })
    _render_error_table(table_data)

    # AI + Download
    col_ai, col_dl = st.columns(2)
    with col_ai:
        if st.button('Analyze with AI', type='primary', use_container_width=True):
            if not check_llm_available(settings['lm_url']):
                st.error(f'Cannot reach LM Studio at {settings["lm_url"]}.')
            else:
                top_all = query_top_errors(conn, selected_date, top_n=15)
                grouped = _db_top_to_grouped(top_all)
                with st.spinner('Qwen is analyzing...'):
                    summary = llm_summarize(grouped, settings['lm_url'], settings['model'])
                    st.session_state['ai_summary'] = summary
    with col_dl:
        top_all = query_top_errors(conn, selected_date, top_n=settings['top_n'])
        grouped = _db_top_to_grouped(top_all)
        ov_report = dict(overview, date=selected_date)
        report = generate_text_report(
            ov_report, grouped, svc_counts, st.session_state.get('ai_summary'),
        )
        st.download_button('Download Report', data=report,
                          file_name=f'log_report_{selected_date}.txt', mime='text/plain',
                          use_container_width=True)

    if 'ai_summary' in st.session_state:
        st.subheader('AI Analysis')
        st.markdown(st.session_state['ai_summary'])


def render_trends(ctx):
    conn = ctx['conn']
    date_from, date_to = ctx['date_range']
    settings = ctx['settings']

    if date_from is None or date_to is None:
        st.info('Select a date range in the sidebar.')
        return

    with st.spinner('Loading trends...'):
        daily = query_daily_counts(conn, date_from, date_to)
        top = query_top_errors_range(conn, date_from, date_to, top_n=settings['top_n'])
        svc = query_service_counts_range(conn, date_from, date_to)

    if daily.empty:
        st.warning('No data for the selected date range.')
        return

    # Daily chart + anomaly side by side
    col_chart, col_anomaly = st.columns([2, 1])
    with col_chart:
        st.subheader('Daily Error Counts')
        _render_timeline(daily, 'date')
    with col_anomaly:
        st.subheader('Anomalies')
        if len(daily) > 1:
            avg_errors = daily['ERROR'].mean()
            anomalies = daily[daily['ERROR'] > avg_errors * 2]
            if not anomalies.empty:
                st.warning(f'{len(anomalies)} day(s) > 2x average ({avg_errors:.0f})')
                for _, row in anomalies.iterrows():
                    d = row['date'].strftime('%Y-%m-%d')
                    st.markdown(f"**{d}**  \n"
                               f"{int(row['ERROR']):,} err / "
                               f"{int(row['WARNING']):,} warn / "
                               f"{int(row['FATAL']):,} fatal")
            else:
                st.success('No anomalies detected.')
        else:
            st.info('Need multiple days for anomaly detection.')

    # Top patterns + Service chart side by side
    col_patterns, col_svc = st.columns([2, 1])
    with col_patterns:
        st.subheader('Top Patterns (All Days)')
        if top:
            table_data = []
            for count, pattern, sev, raw, services in top:
                table_data.append({
                    'Severity': sev, 'Count': count,
                    'Pattern': pattern[:120], 'Services': (services or '')[:40],
                })
            _render_error_table(table_data)
    with col_svc:
        st.subheader('By Service')
        _render_service_chart(svc)

    # Ingested files
    with st.expander('Ingested Files'):
        files = query_file_list(conn)
        if files:
            file_df = pd.DataFrame(files)
            file_df = file_df[['log_date', 'hostname', 'total_lines',
                               'error_count', 'warning_count', 'fatal_count', 'path']]
            file_df.columns = ['Date', 'Host', 'Lines', 'Errors', 'Warnings', 'Fatals', 'Path']
            st.dataframe(file_df, use_container_width=True, hide_index=True)


def render_search_db(ctx):
    conn = ctx['conn']
    date_from, date_to = ctx['date_range']

    query = st.text_input('Search all logs',
                         placeholder='e.g. Buffer I/O, timeout, CCVM, nvmet fatal...')

    col1, col2 = st.columns(2)
    with col1:
        sev_filter = st.selectbox('Severity', ['All', 'FATAL', 'ERROR', 'WARNING'])
    with col2:
        svc_filter = st.text_input('Service filter', placeholder='e.g. kernel')

    if not query:
        st.info('Full-text search across all ingested files. Results include surrounding context.')
        return

    severity = sev_filter if sev_filter != 'All' else None
    service = svc_filter or None

    try:
        with st.spinner('Searching...'):
            results = query_search(conn, query, date_from=date_from, date_to=date_to,
                                  severity=severity, service=service, limit=500)
    except Exception as e:
        st.error(f'Search error: {e}')
        return

    if not results:
        st.warning(f'No matches for "{query}".')
        return

    st.success(f'{len(results)} matches (max 500)')
    for i, r in enumerate(results):
        _render_search_result(r, expanded=(i < 3), is_db=True)


def render_ai_chat_db(ctx):
    conn, selected_date, settings = ctx['conn'], ctx['selected_date'], ctx['settings']

    lm_available = check_llm_available(settings['lm_url'])
    if not lm_available:
        st.warning(f'LM Studio not reachable at {settings["lm_url"]}.')

    top = query_top_errors(conn, selected_date, top_n=15)
    svc_counts = query_service_counts(conn, selected_date)
    grouped = _db_top_to_grouped(top)
    error_context = build_error_context(grouped, svc_counts)

    _render_chat_ui(error_context, settings, lm_available, chat_key=f'db_{selected_date}')


# =====================================================================
# Shared
# =====================================================================

def _db_top_to_grouped(top_errors):
    grouped = {}
    for count, pattern, raw, service, severity in top_errors:
        if severity not in grouped:
            grouped[severity] = []
        grouped[severity].append((count, pattern, raw))
    return grouped


def _send_ai_message(question, error_context, settings, chat_key='default'):
    key = f'chat_{chat_key}'
    st.session_state[key].append({'role': 'user', 'content': question})
    reply = llm_chat(
        question,
        st.session_state[key][:-1],
        error_context,
        settings['lm_url'],
        settings['model'],
    )
    st.session_state[key].append({'role': 'assistant', 'content': reply})


def _render_chat_ui(error_context, settings, lm_available, chat_key='default'):
    key = f'chat_{chat_key}'

    with st.expander('Log context sent to AI'):
        st.code(error_context, language='text')

    if key not in st.session_state:
        st.session_state[key] = []

    # Chat messages with proper bubbles
    for msg in st.session_state[key]:
        with st.chat_message(msg['role']):
            st.markdown(msg['content'])

    # Input (chat_input can't be inside tabs, so use text_input + button)
    input_col, btn_col = st.columns([5, 1])
    with input_col:
        user_input = st.text_input(
            'Ask about the logs',
            placeholder='e.g. Why did CCVM keep restarting?',
            key=f'ai_input_{chat_key}',
            label_visibility='collapsed',
            disabled=not lm_available,
        )
    with btn_col:
        send_clicked = st.button('Send', type='primary',
                                disabled=not lm_available,
                                use_container_width=True,
                                key=f'send_{chat_key}')

    if send_clicked and user_input:
        with st.spinner('Thinking...'):
            _send_ai_message(user_input, error_context, settings, chat_key)
        st.rerun()

    # Clear + Suggestions
    if st.session_state[key]:
        if st.button('Clear chat', key=f'clear_{chat_key}'):
            st.session_state[key] = []
            st.rerun()

    if not st.session_state[key] and lm_available:
        st.caption('Try asking:')
        suggestions = [
            'Summarize the most critical issues',
            'What caused the most errors?',
            'Are any errors correlated?',
            'What should I investigate first?',
        ]
        cols = st.columns(4)
        for i, s in enumerate(suggestions):
            if cols[i].button(s, key=f'sug_{i}_{chat_key}', use_container_width=True):
                with st.spinner('Thinking...'):
                    _send_ai_message(s, error_context, settings, chat_key)
                st.rerun()


# =====================================================================
# Main
# =====================================================================

def main():
    st.title('Log Analyzer')

    ctx = sidebar_controls()

    if ctx['mode'] == 'file' and ctx['df'].empty:
        st.markdown('#### Get started')
        st.markdown('Upload a log file or pick one from a folder in the sidebar.')
        if os.path.exists(DB_PATH):
            st.info('An ingested database was found. Select **Ingested Database** in the sidebar.')
        return

    if ctx['mode'] == 'db' and ctx['conn'] is None:
        st.markdown('#### No database found')
        st.code('python ingest.py --file <logfile>', language='bash')
        return

    if ctx['mode'] == 'db':
        st.caption(f'Viewing: **{ctx["selected_date"]}**')
        tab1, tab2, tab3, tab4 = st.tabs([
            'Overview', 'Trends', 'Search', 'Ask AI'
        ])
        with tab1:
            render_overview_db(ctx)
        with tab2:
            render_trends(ctx)
        with tab3:
            render_search_db(ctx)
        with tab4:
            render_ai_chat_db(ctx)
    else:
        tab1, tab2, tab3 = st.tabs([
            'Overview', 'Search', 'Ask AI'
        ])
        with tab1:
            render_overview_file(ctx)
        with tab2:
            render_search_file(ctx)
        with tab3:
            render_ai_chat_file(ctx)


if __name__ == '__main__':
    main()
