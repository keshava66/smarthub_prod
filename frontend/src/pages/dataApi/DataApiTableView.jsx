import React, { useState, useEffect, useMemo, useRef } from 'react';
import { ArrowDown, ArrowUp } from 'lucide-react';
import { useParams, useSearchParams, Link } from 'react-router-dom';
import { useAuth } from '../../context/AuthContext';
import { logAuditEvent } from '../../utils/auditLog';
import { apiFetch } from '../../utils/apiClient';
import dashboardIcon from '/icons/JJ_Icon_Home_RGB.svg';
import dataApiIcon from '/icons/JJ_Icon_Network_RGB.svg';
import databaseIcon from '/icons/JJ_Icon_Database_RGB.svg';
import tableIcon from '/icons/JJ_Icon_Data_Visualization.svg';
import chevronRightIcon from '/icons/JJ_Icon_Chevron_Right.svg';

const FILTER_RED = 'brightness(0) saturate(100%) invert(17%) sepia(100%) saturate(7426%) hue-rotate(3deg) brightness(92%) contrast(118%)';
const FILTER_GRAY = 'brightness(0) saturate(100%) invert(42%) sepia(6%) saturate(656%) hue-rotate(346deg) brightness(92%) contrast(88%)';

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || window.location.origin;
const COL_DEFAULT_WIDTH = 280;
const COL_MIN_WIDTH = 60;
const COL_MAX_WIDTH = 800;
const COL_CHAR_PX = 6.2;      // approx px per char at text-xs (data font)
const HEADER_CHAR_PX = 7.6;   // approx px per char at text-xs, bold mono uppercase (wider)
const COL_H_PADDING = 32;     // left + right cell padding (data cells)
// Header cells reserve extra horizontal room the data-cell padding doesn't
// account for: 12px left + 20px right (space held for the sort icon) plus
// the icon itself (~10px) and its gap (~4px) — all eating into the text's
// available width even when the icon is faded out, not just shown.
const HEADER_H_PADDING = 66;
const COL_SAMPLE_ROWS = 100;  // rows sampled per page for width calc
const WIDTH_SAMPLE_CHARS = 300;
const CELL_DISPLAY_MAX_CHARS = 1000;
// A download held every row as a live object until the very end, so a big
// table took the tab down before it produced a file. Rows are now turned into
// CSV text and released as each page lands, and the run stops here regardless:
// past this, a browser is the wrong tool and the API itself is the right one.
const CSV_MAX_ROWS = 500000;
// How many consecutive rate-limit waits an export will sit through before it
// gives up and reports instead of waiting indefinitely.
const CSV_MAX_RATE_WAITS = 10;

/** Resolves true if the delay elapsed, false if `signal` aborted first. */
function sleep(ms, signal) {
  return new Promise(resolve => {
    if (signal?.aborted) return resolve(false);
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort);
      resolve(true);
    }, ms);
    const onAbort = () => { clearTimeout(timer); resolve(false); };
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}

/* Mirrors the operators the endpoint accepts; anything else is a 400 there. */
const VIEW_FILTER_OPS = [
  { op: 'eq', label: 'equals' },
  { op: 'ne', label: 'not equals' },
  { op: 'gt', label: 'greater than' },
  { op: 'gte', label: '≥' },
  { op: 'lt', label: 'less than' },
  { op: 'lte', label: '≤' },
  { op: 'in', label: 'one of' },
  { op: 'not_in', label: 'not one of' },
  { op: 'contains', label: 'contains' },
  { op: 'starts_with', label: 'starts with' },
  { op: 'is_null', label: 'is empty' },
  { op: 'not_null', label: 'is not empty' },
];

function fitColumnWidths(columns, rows) {
  const widths = {};
  const capChars = Math.ceil((COL_MAX_WIDTH - COL_H_PADDING) / COL_CHAR_PX);
  columns.forEach((col, ci) => {
    let maxDataChars = 0;
    const sample = rows.slice(0, COL_SAMPLE_ROWS);
    for (const row of sample) {
      const cell = row[col];
      if (cell === null || cell === undefined) continue;
      const raw = typeof cell === 'string' ? cell : formatCellValue(cell);
      const prefix = raw.length > WIDTH_SAMPLE_CHARS ? raw.slice(0, WIDTH_SAMPLE_CHARS) : raw;
      const longest = prefix.split('\n').reduce((a, b) => (a.length > b.length ? a : b), '');
      if (longest.length > maxDataChars) maxDataChars = longest.length;
      if (maxDataChars >= capChars) break; // already enough to hit COL_MAX_WIDTH — stop scanning this column
    }
    const dataRaw = Math.round(maxDataChars * COL_CHAR_PX + COL_H_PADDING);
    const headerRaw = Math.round(col.length * HEADER_CHAR_PX + HEADER_H_PADDING);
    const raw = Math.max(dataRaw, headerRaw);
    widths[ci] = Math.min(COL_MAX_WIDTH, Math.max(COL_MIN_WIDTH, raw));
  });
  return widths;
}

// Small inline SVGs so this page has zero dependency on the app's icon set —
// it's meant to work standalone, without an authenticated session.
const ChevronLeft = (p) => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" {...p}>
    <polyline points="15 18 9 12 15 6" />
  </svg>
);
const ChevronRight = (p) => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" {...p}>
    <polyline points="9 18 15 12 9 6" />
  </svg>
);
const SortIcon = (p) => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" {...p}>
    <path d="M3 7h13M3 12h9M3 17h5" />
  </svg>
);
const AlertIcon = (p) => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" {...p}>
    <path d="M12 9v4m0 4h.01M10.29 3.86l-8.18 14.18A1.5 1.5 0 0 0 3.32 20h17.36a1.5 1.5 0 0 0 1.21-1.96L13.71 3.86a1.5 1.5 0 0 0-2.42 0z" />
  </svg>
);
const WrapIcon = (p) => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" {...p}>
    <path d="M3 6h18M3 12h13a3 3 0 1 1 0 6h-4m0 0l2-2m-2 2l2 2M3 18h5" />
  </svg>
);
const DownloadIcon = (p) => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" {...p}>
    <path d="M12 3v12m0 0l-4-4m4 4l4-4M4 19h16" />
  </svg>
);
const CloseIcon = (p) => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" {...p}>
    <path d="M18 6L6 18M6 6l12 12" />
  </svg>
);
const SearchIcon = (p) => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" {...p}>
    <circle cx="11" cy="11" r="7" />
    <path d="M21 21l-4.35-4.35" />
  </svg>
);

function useDebounced(value, delay = 400) {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(t);
  }, [value, delay]);
  return debounced;
}

// VARIANT/OBJECT/ARRAY columns (e.g. Snowflake VARIANT) come back as real
// JSON objects/arrays, not strings — String(obj) would just print
// "[object Object]". Serialize those properly instead.
function formatCellValue(v) {
  if (v === null || v === undefined) return '';
  if (typeof v === 'object') {
    try { return JSON.stringify(v); } catch { return String(v); }
  }
  return String(v);
}

function csvEscape(value) {
  const s = formatCellValue(value);
  return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}

// Case-insensitive highlight of the search term(s). Takes one term or a list
// (word search). No terms = plain text back.
const HIGHLIGHT_MAX_TEXT_LEN = 3000;
const HIGHLIGHT_MAX_MATCHES = 50;
function highlightMatch(text, terms) {
  const list = (Array.isArray(terms) ? terms : [terms]).filter(Boolean);
  if (!list.length) return text;
  const str = String(text);
  if (str.length > HIGHLIGHT_MAX_TEXT_LEN) return str;
  const escaped = list
    .sort((a, b) => b.length - a.length)
    .map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  const parts = str.split(new RegExp(`(${escaped.join('|')})`, 'ig'));
  if (parts.length === 1) return str;
  const lowered = new Set(list.map(t => t.toLowerCase()));
  let matchCount = 0;
  return parts.map((part, i) => {
    if (!lowered.has(part.toLowerCase())) return part;
    matchCount++;
    if (matchCount > HIGHLIGHT_MAX_MATCHES) return part;
    return <mark key={i} className="bg-jj-yellow-01 text-jj-gray-08 rounded-sm px-0.5">{part}</mark>;
  });
}

// SCREAMING_SNAKE_CASE column names have no spaces, so the browser treats
// them as one unbreakable word — without a hint, a forced wrap in a narrow
// column falls back to splitting mid-letter. <wbr/> after each underscore
// gives it a sane place to break instead.
function renderColName(col) {
  const parts = col.split('_');
  return parts.map((part, i) => (
    <React.Fragment key={i}>
      {part}
      {i < parts.length - 1 && <>_<wbr /></>}
    </React.Fragment>
  ));
}

// This page opens standalone (shared link, no sidebar/nav), so this is the
// only orientation cue and the only way back into the app. apiName is
// fetched from the backend and null until it resolves — falls back to the
// raw id rather than blocking the crumb on it.
function Breadcrumb({ apiId, apiName, alias }) {
  const crumbs = [
    { label: 'SmartHub', icon: dashboardIcon, to: '/dashboard' },
    { label: 'Data API', icon: dataApiIcon, to: '/data-api' },
    { label: apiName || apiId, icon: databaseIcon, to: '/data-api' },
    { label: alias, icon: tableIcon, current: true },
  ];

  return (
    <nav
      aria-label="Breadcrumb"
      className="flex-shrink-0 flex items-center px-5 py-2.5 border-b border-jj-gray-02 bg-white shadow-sm overflow-x-auto"
    >
      <ol className="flex items-center gap-1 min-w-0">
        {crumbs.map((c, i) => (
          <li key={i} className="flex items-center gap-1 min-w-0">
            {i > 0 && (
              <img src={chevronRightIcon} alt="" className="w-3 h-3 mx-1 flex-shrink-0" style={{ filter: FILTER_GRAY, opacity: 0.5 }} />
            )}
            {c.current ? (
              <span
                title={c.label}
                className="flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-jj-red bg-opacity-10 font-johnson-text text-xs font-bold text-jj-red max-w-[240px]"
              >
                <img src={c.icon} alt="" className="w-3.5 h-3.5 flex-shrink-0" style={{ filter: FILTER_RED }} />
                <span className="truncate">{c.label}</span>
              </span>
            ) : (
              <Link
                to={c.to}
                title={c.label}
                className="group flex items-center gap-1.5 px-2.5 py-1 rounded-full font-johnson-text text-xs font-bold text-jj-gray-06 hover:text-jj-red hover:bg-jj-gray-01 transition-colors max-w-[220px]"
              >
                <img
                  src={c.icon}
                  alt=""
                  className="w-3.5 h-3.5 flex-shrink-0 opacity-70 group-hover:opacity-100 transition-opacity"
                  style={{ filter: FILTER_GRAY }}
                />
                <span className="truncate">{c.label}</span>
              </Link>
            )}
          </li>
        ))}
      </ol>
    </nav>
  );
}

const SEARCH_MODE_LABELS = {
  substring: { label: 'Contains', title: 'Text anywhere in the value' },
  exact: { label: 'Exact', title: 'Value must equal the search (fast on indexed columns)' },
  words: { label: 'Words', title: 'Whole words, any order (uses the full-text index)' },
};

const PAGE_SIZES = [50, 100, 250, 500];

const colsKeyOf = (cols) => cols.join('|');

function buildQuery({ search, searchColumn, searchMode, filters, columns, sort }) {
  let q = '';
  if (search) {
    q += `&search=${encodeURIComponent(search)}`;
    if (searchColumn) {
      q += `&column=${encodeURIComponent(searchColumn)}`;
      if (searchMode !== 'substring') q += `&mode=${searchMode}`;
    }
  }
  for (const f of filters) {
    const expr = NO_VALUE_OPS.includes(f.op) ? `${f.column}:${f.op}` : `${f.column}:${f.op}:${f.value}`;
    q += `&where=${encodeURIComponent(expr)}`;
  }
  if (columns) q += `&columns=${encodeURIComponent(columns.join(','))}`;
  if (sort) q += `&sort_by=${encodeURIComponent(sort.col)}&sort_dir=${sort.dir}`;
  return q;
}

async function getJson(url, signal) {
  const res = await fetch(url, { signal, headers: { 'X-Client-App': 'SmartHub' } });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}`);
    err.status = res.status;
    // Carried through so a caller that can wait (the CSV export) knows how long
    // rather than guessing or giving up.
    const retry = parseInt(res.headers.get('Retry-After') || '', 10);
    if (Number.isFinite(retry) && retry > 0) err.retryAfter = retry;
    throw err;
  }
  return data;
}

const NO_VALUE_OPS = ['is_null', 'not_null'];

function ColumnPicker({ allColumns, visible, bulk, onChange }) {
  const [open, setOpen] = useState(false);
  const boxRef = useRef(null);

  useEffect(() => {
    if (!open) return;
    const close = (e) => { if (boxRef.current && !boxRef.current.contains(e.target)) setOpen(false); };
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, [open]);

  const shown = new Set(visible);
  const toggle = (c) => {
    const next = shown.has(c) ? allColumns.filter(x => x !== c && shown.has(x)) : allColumns.filter(x => x === c || shown.has(x));
    if (next.length) onChange(next);
  };

  return (
    <div className="relative flex-shrink-0" ref={boxRef}>
      <button
        onClick={() => setOpen(o => !o)}
        disabled={!allColumns.length}
        title="Pick which columns to load. Fewer columns = smaller, faster responses."
        className={`px-3 py-1.5 rounded-lg font-johnson-text text-xs font-bold border transition-colors disabled:opacity-40 ${
          visible.length < allColumns.length
            ? 'bg-jj-gray-08 border-jj-gray-08 text-white'
            : 'bg-white border-jj-gray-03 text-jj-gray-07 hover:bg-jj-gray-01'
        }`}
      >
        Columns{allColumns.length ? ` (${visible.length}/${allColumns.length})` : ''}
      </button>
      {open && (
        <div className="absolute right-0 mt-1 w-64 max-h-80 overflow-auto bg-white border border-jj-gray-03 rounded-lg shadow-xl z-40 p-2">
          <div className="flex items-center gap-3 px-1 pb-2 mb-1 border-b border-jj-gray-02">
            <button onClick={() => onChange(allColumns)} className="font-johnson-text text-[11px] text-jj-blue-03 hover:text-jj-red">
              show all
            </button>
            {bulk.length > 0 && (
              <button
                onClick={() => { const next = allColumns.filter(c => !bulk.includes(c)); if (next.length) onChange(next); }}
                className="font-johnson-text text-[11px] text-jj-blue-03 hover:text-jj-red"
                title="Vectors, blobs and arrays — usually most of the payload"
              >
                hide large columns
              </button>
            )}
          </div>
          {allColumns.map(c => (
            <label key={c} className="flex items-center gap-2 px-1 py-1 rounded hover:bg-jj-gray-01 cursor-pointer">
              <input type="checkbox" checked={shown.has(c)} onChange={() => toggle(c)} />
              <span className="font-johnson-text text-[11px] text-jj-gray-08 truncate" title={c}>{c}</span>
              {bulk.includes(c) && <span className="ml-auto font-johnson-text text-[10px] text-jj-gray-05">large</span>}
            </label>
          ))}
        </div>
      )}
    </div>
  );
}

function DataApiTableView() {
  const { apiId, alias } = useParams();
  const [searchParams] = useSearchParams();
  const apiKey = searchParams.get('api_key') || '';
  const { user } = useAuth();
  const userEmail = user?.email || user?.username || null;

  const base = `${API_BASE_URL}/data-api/${encodeURIComponent(apiId)}/${encodeURIComponent(alias)}`;
  const keyParam = `api_key=${encodeURIComponent(apiKey)}`;

  // Display name for the breadcrumb. Non-critical, falls back to the raw id.
  const [apiName, setApiName] = useState(null);
  useEffect(() => {
    let cancelled = false;
    apiFetch(`/data-api/${apiId}`)
      .then(res => (res.ok ? res.json() : null))
      .then(data => { if (!cancelled && data) setApiName(data.name); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [apiId]);

  // Page is SSO-gated, so this is the only place a view gets tied to a person.
  // Ref guard stops StrictMode's double mount writing two rows.
  const loggedViewKeyRef = useRef(null);
  useEffect(() => {
    if (!userEmail) return;
    const key = `${apiId}/${alias}`;
    if (loggedViewKeyRef.current === key) return;
    loggedViewKeyRef.current = key;
    logAuditEvent('data_api_table_viewed', userEmail, key, `Opened browsable table view for '${alias}'`);
  }, [userEmail, apiId, alias]);

  /* ── table metadata (/columns) ── fetched alongside the first page, never
     blocks it. Until it lands, column names come from the rows. */
  const [meta, setMeta] = useState(null);
  const [seenColumns, setSeenColumns] = useState([]);
  useEffect(() => {
    if (!apiKey) return;
    const ctrl = new AbortController();
    getJson(`${base}/columns?${keyParam}`, ctrl.signal)
      .then(setMeta)
      .catch(() => {}); // rows call reports auth/alias problems on its own
    return () => ctrl.abort();
  }, [base, keyParam, apiKey]);

  const allColumns = useMemo(
    () => (meta?.columns?.length ? meta.columns.map(c => c.name) : seenColumns),
    [meta, seenColumns]
  );
  const bulkColumns = useMemo(() => meta?.bulk_columns || [], [meta]);
  const sortable = useMemo(() => (meta?.sortable_columns ? new Set(meta.sortable_columns) : null), [meta]);
  const searchModes = useMemo(() => meta?.search_modes || ['substring', 'exact'], [meta]);
  const rowLimit = meta?.row_limit || null;
  const filterOps = useMemo(
    () => (meta?.filter_ops ? VIEW_FILTER_OPS.filter(o => meta.filter_ops.includes(o.op)) : VIEW_FILTER_OPS),
    [meta]
  );
  const maxFilters = meta?.max_filters || 10;

  /* ── query state ── */
  const [pageSize, setPageSize] = useState(100);
  const pageSizes = useMemo(() => {
    if (!rowLimit) return PAGE_SIZES;
    const ok = PAGE_SIZES.filter(n => n <= rowLimit);
    return ok.length ? ok : [rowLimit];
  }, [rowLimit]);
  // A page size above the table's row_limit would be silently capped by the
  // API, so snap it down once the limit is known.
  useEffect(() => {
    if (!pageSizes.includes(pageSize)) setPageSize(pageSizes[pageSizes.length - 1]);
  }, [pageSizes, pageSize]);

  const [search, setSearch] = useState('');
  const debouncedSearch = useDebounced(search).trim();
  const [searchColumn, setSearchColumn] = useState(''); // '' = every column
  const [searchMode, setSearchMode] = useState('substring');
  useEffect(() => {
    if (!searchColumn || !searchModes.includes(searchMode)) setSearchMode('substring');
  }, [searchColumn, searchModes, searchMode]);

  const [filters, setFilters] = useState([]); // [{ column, op, value }]
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [visibleCols, setVisibleCols] = useState(null); // null = all
  const [sort, setSort] = useState(null); // { col, dir } | null

  // Half-typed conditions aren't sent.
  const readyFilters = useMemo(
    () => filters.filter(f => f.column && (NO_VALUE_OPS.includes(f.op) || f.value !== '')),
    [filters]
  );

  const shownColumns = useMemo(() => {
    if (!visibleCols) return allColumns;
    const keep = new Set(visibleCols);
    const list = allColumns.filter(c => keep.has(c));
    return list.length ? list : allColumns;
  }, [visibleCols, allColumns]);
  const columnSubset = visibleCols && shownColumns.length < allColumns.length ? shownColumns : null;

  const queryParams = useMemo(
    () => buildQuery({
      search: debouncedSearch,
      searchColumn,
      searchMode,
      filters: readyFilters,
      columns: columnSubset,
      sort,
    }),
    [debouncedSearch, searchColumn, searchMode, readyFilters, columnSubset, sort]
  );
  // The count only depends on which rows match, not on columns/sort/paging.
  const countParams = useMemo(
    () => buildQuery({ search: debouncedSearch, searchColumn, searchMode, filters: readyFilters }),
    [debouncedSearch, searchColumn, searchMode, readyFilters]
  );
  const queryKey = `${pageSize}${queryParams}`;

  // Offset is stored with the query it belongs to, so a new search starts on
  // page 1 in the same render instead of fetching the old page first.
  const [pos, setPos] = useState({ key: queryKey, offset: 0 });
  const offset = pos.key === queryKey ? pos.offset : 0;
  const setOffset = (o) => setPos({ key: queryKey, offset: Math.max(0, o) });

  // Only log a search once it has settled.
  const loggedSearchRef = useRef(null);
  useEffect(() => {
    if (!userEmail || !debouncedSearch) return;
    const key = `${apiId}/${alias}/${debouncedSearch}`;
    if (loggedSearchRef.current === key) return;
    loggedSearchRef.current = key;
    const where = searchColumn ? ` in column "${searchColumn}"` : '';
    logAuditEvent('data_api_table_searched', userEmail, `${apiId}/${alias}`, `Searched for "${debouncedSearch}"${where}`);
  }, [userEmail, apiId, alias, debouncedSearch, searchColumn]);

  /* ── rows ── */
  const [rows, setRows] = useState([]);
  const [rowsKey, setRowsKey] = useState('');
  const [step, setStep] = useState(pageSize);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [fatal, setFatal] = useState(apiKey ? null : 'Missing api_key in the URL.');
  const [timing, setTiming] = useState(null);
  const [reloadTick, setReloadTick] = useState(0);

  // next_cursor per page start. Seeking is as fast on page 5,000 as on page 1.
  const cursorsRef = useRef(new Map());

  useEffect(() => {
    if (!apiKey) { setLoading(false); return; }
    const ctrl = new AbortController();
    const cursor = cursorsRef.current.get(`${queryKey}|${offset}`);
    const pageParam = cursor ? `&cursor=${encodeURIComponent(cursor)}` : `&offset=${offset}`;
    const started = performance.now();
    setLoading(true);
    setError(null);

    getJson(`${base}/rows?${keyParam}&limit=${pageSize}${pageParam}&count=false${queryParams}`, ctrl.signal)
      .then(data => {
        const got = data.rows || [];
        const limit = data.limit || pageSize;
        if (data.next_cursor) cursorsRef.current.set(`${queryKey}|${offset + limit}`, data.next_cursor);
        if (got.length) setSeenColumns(prev => {
          const keys = Object.keys(got[0]);
          return prev.join('|') === keys.join('|') ? prev : keys;
        });
        setRows(got);
        setRowsKey(`${queryKey}|${offset}`);
        setStep(limit);
        setHasMore(!!data.has_more && got.length > 0);
        setTiming({ server: data.elapsed_ms, total: Math.round(performance.now() - started) });
        setLoading(false);
      })
      .catch(e => {
        if (e.name === 'AbortError') return;
        if ([401, 403, 404].includes(e.status)) setFatal(e.message);
        else setError(e.message);
        setRows([]);
        setHasMore(false);
        setLoading(false);
      });
    return () => ctrl.abort();
  }, [base, keyParam, apiKey, queryKey, offset, reloadTick]); // eslint-disable-line react-hooks/exhaustive-deps

  /* ── total ── separate call so rows never wait on a COUNT(*). Plain table:
     instant estimate first, exact number when it lands. */
  const [total, setTotal] = useState(null);
  const [totalIsEstimate, setTotalIsEstimate] = useState(false);
  const [counting, setCounting] = useState(false);

  useEffect(() => {
    if (!apiKey) return;
    const ctrl = new AbortController();
    const url = (extra) => `${base}/count?${keyParam}${countParams}${extra}`;
    setTotal(null);
    setTotalIsEstimate(false);
    setCounting(true);
    (async () => {
      try {
        if (!countParams) {
          const est = await getJson(url('&estimate=true'), ctrl.signal);
          setTotal(est.total_rows);
          setTotalIsEstimate(!!est.estimated);
          if (!est.estimated) { setCounting(false); return; }
        }
        const exact = await getJson(url(''), ctrl.signal);
        setTotal(exact.total_rows);
        setTotalIsEstimate(false);
        setCounting(false);
      } catch (e) {
        if (e.name !== 'AbortError') setCounting(false);
      }
    })();
    return () => ctrl.abort();
  }, [base, keyParam, apiKey, countParams, reloadTick]);

  const exactTotal = total != null && !totalIsEstimate ? total : null;
  const atEnd = exactTotal != null && offset + step >= exactTotal;
  const canNext = hasMore && !atEnd;

  /* ── CSV download ── */
  const [downloading, setDownloading] = useState(false);
  const [downloadedRows, setDownloadedRows] = useState(0);
  const downloadCtrlRef = useRef(null);

  const downloadCsv = async () => {
    if (downloading || !apiKey) return;
    const ctrl = new AbortController();
    downloadCtrlRef.current = ctrl;
    setDownloading(true);
    setDownloadedRows(0);
    try {
      const chunk = Math.min(2000, rowLimit || 2000);
      // Rows become CSV text one page at a time; the row objects themselves are
      // dropped as soon as they're written, so memory tracks the output size
      // rather than the whole result set.
      let cols = null;
      const lines = [];
      let rowsWritten = 0;
      let off = 0;
      let cursor = '';
      let more = true;
      let truncated = false;
      let waits = 0;

      while (more) {
        // Cursor when the API hands one out, offset otherwise.
        const page = cursor ? `&cursor=${encodeURIComponent(cursor)}` : `&offset=${off}`;
        const url = `${base}/rows?${keyParam}&limit=${chunk}${page}&count=false${queryParams}`;
        let data;
        try {
          data = await getJson(url, ctrl.signal);
        } catch (e) {
          // The export is exactly the thing the rate limit is there to slow
          // down, so wait it out rather than failing a half-finished download.
          // Bounded, or a server stuck on 429 would spin here forever.
          if (e.status === 429 && e.retryAfter && waits < CSV_MAX_RATE_WAITS) {
            waits += 1;
            const wokeUp = await sleep(Math.min(e.retryAfter, 60) * 1000, ctrl.signal);
            if (!wokeUp) throw Object.assign(new Error('Cancelled'), { name: 'AbortError' });
            continue;
          }
          throw e;
        }
        waits = 0;
        const got = data.rows || [];
        if (!cols) cols = got.length ? Object.keys(got[0]) : shownColumns;
        let dropped = false;
        for (const r of got) {
          if (rowsWritten >= CSV_MAX_ROWS) { dropped = true; break; }
          lines.push(cols.map(c => csvEscape(r[c])).join(','));
          rowsWritten += 1;
        }
        off += got.length;
        cursor = data.next_cursor || '';
        more = !!data.has_more && got.length > 0;
        setDownloadedRows(rowsWritten);
        if (rowsWritten >= CSV_MAX_ROWS) {
          // Rows left behind either in this page or in pages after it. Both
          // mean the file is short, and saying so only when `more` was true
          // would stay silent on a table that ends mid-page.
          truncated = dropped || more;
          break;
        }
      }

      if (!cols) cols = shownColumns;
      const header = cols.map(csvEscape).join(',');
      const blob = new Blob([[header, ...lines].join('\n')], { type: 'text/csv;charset=utf-8;' });
      const suffix = truncated ? '_first500k' : (debouncedSearch || readyFilters.length) ? '_filtered' : '';
      const filename = `${alias}${suffix}.csv`;
      const a = Object.assign(document.createElement('a'), { href: URL.createObjectURL(blob), download: filename });
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
      if (truncated) {
        setError(
          `That table has more than ${CSV_MAX_ROWS.toLocaleString()} matching rows. ` +
          `The file holds the first ${rowsWritten.toLocaleString()} — narrow it with a filter, ` +
          `or pull the rest from the API directly.`
        );
      }
      if (userEmail) {
        logAuditEvent(
          'data_api_table_downloaded', userEmail, `${apiId}/${alias}`,
          `Downloaded ${filename} (${rowsWritten} rows${truncated ? ', truncated at the row cap' : ''})`
        );
      }
    } catch (e) {
      if (e.name !== 'AbortError') setError(`Download failed: ${e.message}`);
    } finally {
      downloadCtrlRef.current = null;
      setDownloading(false);
    }
  };
  const cancelDownload = () => downloadCtrlRef.current?.abort();
  useEffect(() => () => downloadCtrlRef.current?.abort(), []);

  /* ── filters ── */
  const addFilter = () =>
    setFilters(prev => [...prev, { column: allColumns[0] || '', op: 'eq', value: '' }]);
  const setFilter = (i, patch) =>
    setFilters(prev => prev.map((f, n) => (n === i ? { ...f, ...patch } : f)));
  const dropFilter = (i) => setFilters(prev => prev.filter((_, n) => n !== i));

  /* ── layout ── */
  const columns = rows.length ? Object.keys(rows[0]) : shownColumns;
  const [wordWrap, setWordWrap] = useState(false);
  const [cellModal, setCellModal] = useState(null);
  const [colWidths, setColWidths] = useState({});
  const dragRef = useRef(null);
  const [uiBusy, setUiBusy] = useState(false);
  const busyRef = useRef(false);

  const autoWidths = useMemo(() => fitColumnWidths(columns, rows), [rows, colsKeyOf(columns)]); // eslint-disable-line react-hooks/exhaustive-deps
  const widthFor = (ci) => colWidths[ci] ?? autoWidths[ci] ?? COL_DEFAULT_WIDTH;

  // Different column set → manual widths no longer line up.
  const colsKey = columns.join('|');
  const prevColsKey = useRef('');
  useEffect(() => {
    if (colsKey !== prevColsKey.current) {
      prevColsKey.current = colsKey;
      setColWidths({});
    }
  }, [colsKey]);

  const onResize = (e, ci) => {
    e.preventDefault();
    e.stopPropagation();
    dragRef.current = { ci, startX: e.clientX, startWidth: widthFor(ci) };
    const onMove = (ev) => {
      if (!dragRef.current) return;
      const { ci: c, startX, startWidth } = dragRef.current;
      const next = Math.min(COL_MAX_WIDTH, Math.max(COL_MIN_WIDTH, startWidth + (ev.clientX - startX)));
      setColWidths(prev => ({ ...prev, [c]: next }));
    };
    const onUp = () => {
      dragRef.current = null;
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  };

  useEffect(() => {
    if (busyRef.current) {
      busyRef.current = false;
      setUiBusy(false);
    }
  }, [wordWrap]);

  const toggleWordWrap = () => {
    busyRef.current = true;
    setUiBusy(true);
    requestAnimationFrame(() => requestAnimationFrame(() => setWordWrap(w => !w)));
  };

  const headerRowRef = useRef(null);
  const [headerHeight, setHeaderHeight] = useState(40);
  const scrollPaneRef = useRef(null);
  const [scrollPaneWidth, setScrollPaneWidth] = useState(0);
  const hasTable = rows.length > 0;
  useEffect(() => {
    const head = headerRowRef.current;
    const pane = scrollPaneRef.current;
    const observers = [];
    if (head) {
      const ro = new ResizeObserver(entries => { for (const en of entries) setHeaderHeight(en.contentRect.height); });
      ro.observe(head);
      observers.push(ro);
    }
    if (pane) {
      const ro = new ResizeObserver(entries => { for (const en of entries) setScrollPaneWidth(en.contentRect.width); });
      ro.observe(pane);
      observers.push(ro);
    }
    return () => observers.forEach(o => o.disconnect());
  }, [hasTable, fatal]);

  // Scroll back to the top when a new page lands.
  useEffect(() => {
    if (scrollPaneRef.current) scrollPaneRef.current.scrollTop = 0;
  }, [rowsKey]);

  const lastColIdx = columns.length - 1;
  const tableContentWidth = 40 + columns.reduce((sum, _, ci) => sum + widthFor(ci), 0);
  const stretchSlack = Math.max(0, scrollPaneWidth - tableContentWidth);
  const renderWidthFor = (ci) =>
    ci === lastColIdx && colWidths[lastColIdx] == null ? widthFor(ci) + stretchSlack : widthFor(ci);

  const canSort = (col) => !sortable || sortable.has(col);
  const toggleSort = (col) => {
    if (!canSort(col)) return;
    setSort(s => {
      if (!s || s.col !== col) return { col, dir: 'asc' };
      if (s.dir === 'asc') return { col, dir: 'desc' };
      return null;
    });
  };

  /* ── paging ── */
  const totalPages = total != null ? Math.max(1, Math.ceil(total / step)) : null;
  const currentPage = Math.floor(offset / step) + 1;
  const rangeStart = rows.length ? offset + 1 : 0;
  const rangeEnd = offset + rows.length;

  const goFirst = () => setOffset(0);
  const goPrev = () => setOffset(offset - step);
  const goNext = () => setOffset(offset + step);
  const goLast = () => { if (exactTotal != null) setOffset((Math.ceil(exactTotal / step) - 1) * step); };

  const [pageJump, setPageJump] = useState('1');
  useEffect(() => { setPageJump(String(currentPage)); }, [currentPage]);
  const submitPageJump = () => {
    const n = parseInt(pageJump, 10);
    if (!Number.isFinite(n) || n < 1) { setPageJump(String(currentPage)); return; }
    const maxPage = exactTotal != null ? Math.max(1, Math.ceil(exactTotal / step)) : null;
    const clamped = maxPage ? Math.min(n, maxPage) : n;
    if (clamped !== currentPage) setOffset((clamped - 1) * step);
    else setPageJump(String(currentPage));
  };

  const highlightTerms = useMemo(() => {
    if (!debouncedSearch) return [];
    if (searchColumn && searchMode === 'words') return debouncedSearch.split(/\s+/).filter(Boolean);
    return [debouncedSearch];
  }, [debouncedSearch, searchColumn, searchMode]);
  const termsFor = (col) => (!searchColumn || col === searchColumn ? highlightTerms : []);

  const filtering = !!(debouncedSearch || readyFilters.length);

  if (fatal) {
    return (
      <div className="min-h-screen bg-jj-gray-01 flex flex-col">
        <Breadcrumb apiId={apiId} apiName={apiName} alias={alias} />
        <div className="flex-1 flex items-center justify-center p-6">
          <div className="bg-white rounded-2xl shadow-lg p-8 max-w-md text-center">
            <AlertIcon className="w-10 h-10 mx-auto mb-3 text-jj-maroon-05" />
            <p className="font-johnson-display text-lg text-jj-gray-08 mb-1">Can't load this table</p>
            <p className="font-johnson-text text-sm text-jj-gray-06">{fatal}</p>
          </div>
        </div>
      </div>
    );
  }

  const totalLabel = total == null
    ? (counting ? 'counting…' : null)
    : `${totalIsEstimate ? '~' : ''}${total.toLocaleString()} ${filtering ? 'matching' : 'total'} row${total !== 1 ? 's' : ''}`;

  return (
    <div className="h-screen flex flex-col bg-jj-gray-01">
      <Breadcrumb apiId={apiId} apiName={apiName} alias={alias} />

      {/* Header — title/count, search, columns, filters, download, wrap */}
      <div className="flex-shrink-0 flex items-center gap-3 px-5 py-3 border-b border-jj-gray-02 bg-jj-gray-01 flex-wrap">
        <div className="flex items-center gap-2.5 flex-shrink-0">
          <span className="font-johnson-display text-sm font-bold text-jj-gray-08">/{alias}</span>
          {totalLabel && (
            <span
              className="font-johnson-text text-xs text-jj-gray-05 whitespace-nowrap"
              title={totalIsEstimate ? 'Estimate from table statistics — exact count on its way' : undefined}
            >
              {totalLabel}
            </span>
          )}
        </div>

        <div className="relative flex-shrink-0" style={{ minWidth: '220px' }}>
          <SearchIcon className="w-3.5 h-3.5 text-jj-gray-05 absolute left-2.5 top-1/2 -translate-y-1/2 pointer-events-none" />
          <input
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="Search this table…"
            className="w-full pl-8 pr-7 py-1.5 rounded-lg border border-jj-gray-03 bg-white font-johnson-text text-xs focus:outline-none focus:ring-2 focus:ring-jj-blue-03"
          />
          {search && (
            <button
              onClick={() => setSearch('')}
              aria-label="Clear search"
              className="absolute right-2 top-1/2 -translate-y-1/2 p-0.5 rounded hover:bg-jj-gray-01 transition-colors"
            >
              <CloseIcon className="w-3 h-3 text-jj-gray-06" />
            </button>
          )}
        </div>

        {/* One column is much faster than all of them on big tables. */}
        <select
          value={searchColumn}
          onChange={e => setSearchColumn(e.target.value)}
          title="Search only this column (faster on large tables)"
          className="flex-shrink-0 py-1.5 px-2 rounded-lg border border-jj-gray-03 bg-white font-johnson-text text-xs focus:outline-none focus:ring-2 focus:ring-jj-blue-03"
        >
          <option value="">All columns</option>
          {allColumns.map(c => (
            <option key={c} value={c}>{c}</option>
          ))}
        </select>

        {searchColumn && searchModes.length > 1 && (
          <div className="flex gap-0.5 p-0.5 rounded-lg bg-white border border-jj-gray-03 flex-shrink-0">
            {searchModes.map(m => (
              <button
                key={m}
                onClick={() => setSearchMode(m)}
                title={SEARCH_MODE_LABELS[m]?.title}
                className={`px-2 py-1 rounded-md font-johnson-text text-[11px] transition-colors ${
                  searchMode === m ? 'bg-jj-gray-08 text-white' : 'text-jj-gray-07 hover:bg-jj-gray-01'
                }`}
              >
                {SEARCH_MODE_LABELS[m]?.label || m}
              </button>
            ))}
          </div>
        )}

        <button
          onClick={() => { if (!filtersOpen && filters.length === 0) addFilter(); setFiltersOpen(o => !o); }}
          disabled={!allColumns.length}
          title="Narrow the table by column values"
          className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg font-johnson-text text-xs font-bold border transition-colors flex-shrink-0 disabled:opacity-40 ${
            readyFilters.length
              ? 'bg-jj-gray-08 border-jj-gray-08 text-white'
              : 'bg-white border-jj-gray-03 text-jj-gray-07 hover:bg-jj-gray-01'
          }`}
        >
          Filters{readyFilters.length ? ` (${readyFilters.length})` : ''}
        </button>

        <ColumnPicker
          allColumns={allColumns}
          visible={shownColumns}
          bulk={bulkColumns}
          onChange={next => setVisibleCols(next.length === allColumns.length ? null : next)}
        />

        {sort && (
          <button
            onClick={() => setSort(null)}
            title="Clear sort"
            className="flex items-center gap-1 px-2.5 py-1.5 rounded-lg bg-white border border-jj-gray-03 font-johnson-text text-[11px] text-jj-gray-07 hover:bg-jj-gray-01 flex-shrink-0"
          >
            Sorted by <b className="font-johnson-text">{sort.col}</b> {sort.dir === 'asc' ? <ArrowUp size={13} strokeWidth={1.8} aria-hidden="true" /> : <ArrowDown size={13} strokeWidth={1.8} aria-hidden="true" />}
            <CloseIcon className="w-3 h-3 text-jj-gray-06 ml-0.5" />
          </button>
        )}

        <div className="flex-1" />

        {timing && (
          <span
            className="font-johnson-text text-[11px] text-jj-gray-05 whitespace-nowrap flex-shrink-0"
            title="API time on the server / total time including network"
          >
            {timing.server != null ? `${timing.server} ms` : ''}{timing.server != null ? ' · ' : ''}{timing.total} ms total
          </span>
        )}

        {!downloading ? (
          <button
            onClick={downloadCsv}
            disabled={loading && !rows.length}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg font-johnson-text text-xs font-bold text-jj-gray-07 bg-white border border-jj-gray-03 hover:bg-jj-gray-01 transition-colors flex-shrink-0 disabled:opacity-40"
          >
            <DownloadIcon className="w-3.5 h-3.5 text-jj-gray-07" /> Download CSV
          </button>
        ) : (
          <div className="flex items-center gap-2 bg-jj-gray-01 border border-jj-gray-02 rounded-lg px-3 py-1.5 min-w-[160px] flex-shrink-0">
            <div className="animate-spin rounded-full h-3.5 w-3.5 border-b-2 border-jj-red flex-shrink-0" />
            <span className="font-johnson-text text-xs text-jj-gray-08">
              {total
                ? `${Math.min(99, Math.round((downloadedRows / total) * 100))}%`
                : `${downloadedRows.toLocaleString()} rows`}
            </span>
            <button onClick={cancelDownload} title="Cancel download" className="ml-auto p-0.5 rounded hover:bg-jj-gray-02">
              <CloseIcon className="w-3 h-3 text-jj-gray-06" />
            </button>
          </div>
        )}

        <button
          onClick={toggleWordWrap}
          title={wordWrap ? 'Collapse to single line' : 'Wrap cell text'}
          className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg font-johnson-text text-xs font-bold transition-colors flex-shrink-0 ${
            wordWrap ? 'bg-jj-red text-white' : 'bg-white border border-jj-gray-03 text-jj-gray-07 hover:bg-jj-gray-01'
          }`}
        >
          <WrapIcon className={`w-3.5 h-3.5 ${wordWrap ? 'text-white' : 'text-jj-gray-07'}`} /> Word wrap
        </button>
      </div>

      {/* Filters — column / operator / value, all AND'd, same as repeated `where`. */}
      {filtersOpen && (
        <div className="flex-shrink-0 border-b border-jj-gray-02 bg-jj-gray-01 px-4 py-2.5 space-y-1.5">
          {filters.map((f, i) => (
            <div key={i} className="flex items-center gap-1.5">
              <select
                value={f.column}
                onChange={e => setFilter(i, { column: e.target.value })}
                className="py-1.5 px-2 w-52 rounded-lg border border-jj-gray-03 bg-white font-johnson-text text-xs focus:outline-none focus:ring-2 focus:ring-jj-blue-03"
              >
                {!f.column && <option value="">pick a column</option>}
                {allColumns.map(c => <option key={c} value={c}>{c}</option>)}
              </select>
              <select
                value={f.op}
                onChange={e => setFilter(i, { op: e.target.value })}
                className="py-1.5 px-2 w-36 rounded-lg border border-jj-gray-03 bg-white font-johnson-text text-xs focus:outline-none focus:ring-2 focus:ring-jj-blue-03"
              >
                {filterOps.map(o => <option key={o.op} value={o.op}>{o.label}</option>)}
              </select>
              {!NO_VALUE_OPS.includes(f.op) && (
                <input
                  value={f.value}
                  onChange={e => setFilter(i, { value: e.target.value })}
                  placeholder={['in', 'not_in'].includes(f.op) ? 'comma, separated, values' : 'value'}
                  className="flex-1 py-1.5 px-2.5 rounded-lg border border-jj-gray-03 bg-white font-johnson-text text-xs focus:outline-none focus:ring-2 focus:ring-jj-blue-03"
                />
              )}
              <button
                onClick={() => dropFilter(i)}
                title="Remove"
                className="px-2 py-1 rounded hover:bg-jj-gray-02 font-johnson-text text-xs text-jj-gray-06 transition-colors"
              >
                ×
              </button>
            </div>
          ))}

          {/* Two equals on one column never match, and a comma in an equals
              value is nearly always meant as "one of". */}
          {(() => {
            const dupes = filters
              .filter(f => f.op === 'eq' && f.column && f.value !== '')
              .reduce((acc, f) => ({ ...acc, [f.column]: (acc[f.column] || 0) + 1 }), {});
            const dupe = Object.keys(dupes).find(c => dupes[c] > 1);
            const listy = filters.find(f => ['eq', 'ne'].includes(f.op) && f.value.includes(','));
            if (!dupe && !listy) return null;
            return (
              <p className="font-johnson-text text-[11px] text-jj-maroon-05 pt-0.5">
                {dupe
                  ? `Two "equals" filters on ${dupe} can't both match — use "one of" for several values.`
                  : `That value has commas in it. "Equals" matches it as one whole string; "one of" treats it as a list.`}
              </p>
            );
          })()}

          <div className="flex items-center gap-3 pt-0.5">
            <button
              onClick={addFilter}
              disabled={filters.length >= maxFilters}
              className="font-johnson-text text-[11px] text-jj-blue-03 hover:text-jj-red transition-colors disabled:opacity-40"
            >
              + add filter
            </button>
            {filters.length > 0 && (
              <button
                onClick={() => { setFilters([]); setFiltersOpen(false); }}
                className="font-johnson-text text-[11px] text-jj-gray-06 hover:text-jj-red transition-colors"
              >
                clear all
              </button>
            )}
            <span className="font-johnson-text text-[11px] text-jj-gray-05">
              All conditions must match. Up to {maxFilters}.
            </span>
          </div>
        </div>
      )}

      {error && (
        <div className="flex-shrink-0 flex items-center gap-2 px-5 py-2 bg-red-50 border-b border-jj-gray-02">
          <AlertIcon className="w-4 h-4 text-jj-maroon-05 flex-shrink-0" />
          <span className="font-johnson-text text-xs text-jj-maroon-05 flex-1">{error}</span>
          <button
            onClick={() => setReloadTick(t => t + 1)}
            className="font-johnson-text text-[11px] text-jj-blue-03 hover:text-jj-red"
          >
            retry
          </button>
          <button onClick={() => setError(null)} aria-label="Dismiss" className="p-0.5 rounded hover:bg-jj-gray-02">
            <CloseIcon className="w-3 h-3 text-jj-gray-06" />
          </button>
        </div>
      )}

      {/* Table */}
      <div className="flex-1 min-h-0 relative">
        {loading && (
          <div className={`absolute inset-0 flex items-center justify-center z-20 ${rows.length ? 'bg-white bg-opacity-50' : 'bg-white bg-opacity-80'}`}>
            <div className="animate-spin rounded-full h-10 w-10 border-b-2 border-jj-red" />
          </div>
        )}
        {!loading && uiBusy && (
          <div className="absolute left-0 right-0 bottom-0 bg-white z-20 overflow-hidden" style={{ top: `${headerHeight}px` }}>
            {Array.from({ length: 18 }).map((_, ri) => (
              <div key={ri} className="flex border-b border-jj-gray-02" style={{ height: '33px', backgroundColor: ri % 2 === 0 ? '#ffffff' : '#fafaf9' }}>
                <div className="flex-shrink-0 border-r border-jj-gray-04 flex items-center justify-center" style={{ width: '40px' }}>
                  <div className="h-2 w-3 rounded bg-jj-gray-02 animate-pulse" />
                </div>
                {columns.map((c, ci) => (
                  <div key={ci} className="flex-shrink-0 border-r border-jj-gray-02 px-3 py-2 flex items-center" style={{ width: `${renderWidthFor(ci)}px` }}>
                    <div
                      className="h-2.5 rounded bg-jj-gray-02 animate-pulse"
                      style={{ width: `${40 + ((ri * 7 + ci * 13) % 50)}%`, animationDelay: `${(ri * 20) % 300}ms` }}
                    />
                  </div>
                ))}
              </div>
            ))}
          </div>
        )}
        <div className="h-full overflow-auto" ref={scrollPaneRef}>
          {!loading && rows.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-full gap-3 p-8">
              <p className="font-johnson-display text-lg text-jj-gray-08">
                {error
                  ? 'Nothing to show.'
                  : offset > 0
                    ? 'No more rows.'
                    : debouncedSearch
                      ? `No rows match "${debouncedSearch}".`
                      : readyFilters.length ? 'No rows match these filters.' : 'No rows.'}
              </p>
              {offset > 0 && !error && (
                <button onClick={goFirst} className="font-johnson-text text-xs text-jj-blue-03 hover:text-jj-red">
                  Back to first page
                </button>
              )}
            </div>
          ) : (
            <table style={{ tableLayout: 'fixed', width: `${40 + columns.reduce((sum, _, ci) => sum + renderWidthFor(ci), 0)}px`, borderCollapse: 'separate', borderSpacing: 0 }}>
              <colgroup>
                <col style={{ width: '40px' }} />
                {columns.map((c, ci) => <col key={c} style={{ width: `${renderWidthFor(ci)}px` }} />)}
              </colgroup>
              <thead>
                <tr ref={headerRowRef} style={{ position: 'sticky', top: 0, zIndex: 2 }}>
                  <th
                    style={{ position: 'sticky', left: 0, zIndex: 3, borderBottom: '2px solid #eb1700', width: '40px', minWidth: '40px' }}
                    className="bg-jj-gray-01 border-r border-jj-gray-04 px-2 py-2.5 text-center"
                  >
                    <span className="font-johnson-text text-[10px] text-jj-gray-04">#</span>
                  </th>
                  {columns.map((col, ci) => {
                    const active = sort?.col === col;
                    const sortOk = canSort(col);
                    return (
                      <th
                        key={col}
                        style={{ borderBottom: '2px solid #eb1700', width: `${renderWidthFor(ci)}px` }}
                        className="bg-jj-gray-01 border-r border-jj-gray-04 px-0 py-0 relative select-none group/col"
                      >
                        <div
                          className={`px-3 py-2.5 transition-colors ${sortOk ? 'cursor-pointer hover:bg-jj-gray-02' : 'cursor-default'}`}
                          style={{ paddingRight: '1.25rem' }}
                          onClick={() => toggleSort(col)}
                          title={sortOk ? `Sort the whole table by ${col}` : `${col} can't be sorted`}
                        >
                          <div className="flex items-start gap-1 min-w-0">
                            <span
                              className="font-johnson-text text-xs font-bold uppercase tracking-wide block whitespace-normal break-words flex-1 min-w-0"
                              style={{ color: active ? '#eb1700' : '#312c2a' }}
                            >
                              {renderColName(col)}
                            </span>
                            {sortOk && (
                              <span className={`flex-shrink-0 transition-opacity mt-0.5 ${active ? 'opacity-100' : 'opacity-0 group-hover/col:opacity-40'}`}>
                                {active && sort.dir === 'asc'
                                  ? <ChevronLeft className="w-2.5 h-2.5 -rotate-90 text-jj-red" />
                                  : active
                                    ? <ChevronLeft className="w-2.5 h-2.5 rotate-90 text-jj-red" />
                                    : <SortIcon className="w-2.5 h-2.5 text-jj-gray-06" />}
                              </span>
                            )}
                          </div>
                        </div>

                        <div
                          onMouseDown={e => onResize(e, ci)}
                          onClick={e => e.stopPropagation()}
                          className="absolute top-0 right-0 h-full w-2 cursor-col-resize group/handle flex items-center justify-center z-10"
                        >
                          <div className="w-0.5 h-4 bg-jj-gray-03 rounded-full group-hover/handle:bg-jj-red group-hover/handle:h-full transition-all duration-150" />
                        </div>
                      </th>
                    );
                  })}
                </tr>
              </thead>
              <tbody>
                {rows.map((row, i) => {
                  const isEven = i % 2 === 0;
                  return (
                    <tr key={i} className="hover:bg-red-50 transition-colors" style={{ backgroundColor: isEven ? '#ffffff' : '#fafaf9' }}>
                      <td
                        style={{ position: 'sticky', left: 0, zIndex: 1, backgroundColor: isEven ? '#f1efed' : '#e8e6e3' }}
                        className={`border-b border-r border-jj-gray-04 px-2 py-2.5 text-center select-none ${wordWrap ? 'align-top' : ''}`}
                      >
                        <span className="font-johnson-text text-[10px] text-jj-gray-05 leading-none">{offset + i + 1}</span>
                      </td>
                      {columns.map((col, ci) => {
                        const fullText = formatCellValue(row[col]);
                        const isHuge = fullText.length > CELL_DISPLAY_MAX_CHARS;
                        const text = isHuge ? fullText.slice(0, CELL_DISPLAY_MAX_CHARS) + '…' : fullText;
                        const isClipped = isHuge || fullText.includes('\n') || (text.length * COL_CHAR_PX + COL_H_PADDING) > renderWidthFor(ci);
                        const isNull = row[col] === null || row[col] === undefined;
                        return (
                          <td
                            key={col}
                            onClick={() => { if (isClipped) setCellModal({ column: col, value: fullText }); }}
                            className={`border-b border-r border-jj-gray-02 px-3 py-2 font-johnson-text text-xs text-jj-gray-08 ${
                              wordWrap ? 'whitespace-normal break-words align-top' : 'truncate'
                            } ${isClipped ? 'cursor-pointer hover:underline hover:text-jj-blue-03' : ''}`}
                            title={isHuge ? undefined : text}
                          >
                            {isNull
                              ? <span className="text-jj-gray-04 italic">null</span>
                              : highlightMatch(text, termsFor(col))}
                          </td>
                        );
                      })}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </div>

      {/* Footer — range | rows per page, pager, page jump */}
      <div className="flex-shrink-0 flex items-center gap-3 px-5 py-2.5 bg-white border-t border-jj-gray-02 flex-wrap">
        <span className="font-johnson-text text-xs text-jj-gray-06 flex-shrink-0">
          {rows.length > 0 && (
            <>Rows <b className="text-jj-gray-08">{rangeStart.toLocaleString()}</b>–<b className="text-jj-gray-08">{rangeEnd.toLocaleString()}</b>
              {total != null && <> of <b className="text-jj-gray-08">{totalIsEstimate ? '~' : ''}{total.toLocaleString()}</b></>}
            </>
          )}
        </span>

        <div className="flex-1" />

        <label className="font-johnson-text text-xs text-jj-gray-06 flex items-center gap-1.5 flex-shrink-0">
          Rows per page
          <select
            value={pageSize}
            onChange={e => setPageSize(Number(e.target.value))}
            className="px-2 py-1 rounded border border-jj-gray-03 bg-white font-johnson-text text-xs"
          >
            {pageSizes.map(n => <option key={n} value={n}>{n}</option>)}
          </select>
        </label>

        <div className="flex items-center gap-1 flex-shrink-0">
          <button
            disabled={offset === 0 || loading}
            onClick={goFirst}
            className="px-2.5 py-1 rounded border border-jj-gray-03 bg-white font-johnson-text text-xs hover:bg-jj-gray-02 disabled:opacity-40"
          >
            First
          </button>
          <button
            disabled={offset === 0 || loading}
            onClick={goPrev}
            aria-label="Previous page"
            className="px-2 py-1 rounded border border-jj-gray-03 bg-white hover:bg-jj-gray-02 disabled:opacity-40"
          >
            <ChevronLeft className="w-3.5 h-3.5 text-jj-gray-07" />
          </button>

          <span className="font-johnson-text text-xs text-jj-gray-06 flex items-center gap-1.5 px-1 whitespace-nowrap">
            Page
            <input
              type="text"
              inputMode="numeric"
              value={pageJump}
              disabled={loading}
              onChange={e => setPageJump(e.target.value.replace(/[^0-9]/g, ''))}
              onBlur={submitPageJump}
              onKeyDown={e => { if (e.key === 'Enter') e.currentTarget.blur(); }}
              className="w-14 px-1.5 py-1 rounded border border-jj-gray-03 bg-white font-johnson-text text-xs text-center focus:outline-none focus:ring-2 focus:ring-jj-blue-03"
            />
            {totalPages ? `of ${totalIsEstimate ? '~' : ''}${totalPages.toLocaleString()}` : ''}
          </span>

          <button
            disabled={!canNext || loading}
            onClick={goNext}
            aria-label="Next page"
            className="px-2 py-1 rounded border border-jj-gray-03 bg-white hover:bg-jj-gray-02 disabled:opacity-40"
          >
            <ChevronRight className="w-3.5 h-3.5 text-jj-gray-07" />
          </button>
          <button
            disabled={exactTotal == null || !canNext || loading}
            onClick={goLast}
            title={exactTotal == null ? 'Available once the row count is in' : undefined}
            className="px-2.5 py-1 rounded border border-jj-gray-03 bg-white font-johnson-text text-xs hover:bg-jj-gray-02 disabled:opacity-40"
          >
            Last
          </button>
        </div>
      </div>

      {/* Full value for clipped cells */}
      {cellModal && (
        <div
          className="fixed inset-0 bg-black bg-opacity-40 z-50 flex items-center justify-center p-4"
          onClick={() => setCellModal(null)}
        >
          <div
            className="bg-white rounded-xl shadow-2xl max-w-2xl w-full max-h-[80vh] flex flex-col"
            onClick={e => e.stopPropagation()}
          >
            <div className="flex items-center justify-between px-5 py-3 border-b border-jj-gray-02 flex-shrink-0">
              <span className="font-johnson-text text-xs font-bold uppercase tracking-wide text-jj-gray-08">{cellModal.column}</span>
              <div className="flex items-center gap-1">
                <button
                  onClick={() => navigator.clipboard?.writeText(cellModal.value).catch(() => {})}
                  className="px-2 py-1 rounded font-johnson-text text-[11px] text-jj-blue-03 hover:bg-jj-gray-01"
                >
                  Copy
                </button>
                <button onClick={() => setCellModal(null)} aria-label="Close" className="p-1 rounded hover:bg-jj-gray-01 transition-colors">
                  <CloseIcon className="w-4 h-4 text-jj-gray-06" />
                </button>
              </div>
            </div>
            <div className="p-5 overflow-auto font-johnson-text text-sm text-jj-gray-08 whitespace-pre-wrap break-words">
              {(() => {
                let display = cellModal.value;
                try {
                  const parsed = JSON.parse(cellModal.value);
                  if (parsed && typeof parsed === 'object') display = JSON.stringify(parsed, null, 2);
                } catch { /* not JSON */ }
                return highlightMatch(display, termsFor(cellModal.column));
              })()}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default DataApiTableView;