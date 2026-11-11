import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { apiFetch } from '../../utils/apiClient';
import { API_BASE_URL } from './dataApiDocs';
import postgresLogo from '/icons/postgres_badge.svg';
import oracleLogo from '/oracle_badge.png';
import denodoLogo from '/denodo.png';
import s3Logo from '/icons/s3_badge.svg';
import snowflakeLogo from '/icons/snowflake_badge.svg';

export { API_BASE_URL };

/* ------------------------------------------------------------------ http */

/* Every call on this page goes through here, so an error always reads the
   same way: the server's own `detail` when it sent one, a plain sentence when
   the network did not answer at all. */
export class RequestError extends Error {
  constructor(message, status, detail) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

const detailText = (detail, status) => {
  if (!detail) return status ? `The server answered ${status}.` : 'Something went wrong.';
  if (typeof detail === 'string') return detail;
  if (detail.message) return detail.message;
  if (Array.isArray(detail)) return detail.map((d) => d.msg || JSON.stringify(d)).join('; ');
  return JSON.stringify(detail);
};

async function send(path, opts) {
  try {
    return await apiFetch(path, opts);
  } catch (e) {
    if (e?.name === 'AbortError') throw e;
    if (e?.status === 401) throw new RequestError('Your session has expired. Sign in again to carry on.', 401);
    throw new RequestError("Couldn't reach the server. Check your connection and try again.", 0);
  }
}

const failure = (res, text, data) => {
  const detail = data?.detail ?? (text && !data ? text.slice(0, 300) : null);
  const msg = res.status === 403 && !detail ? "You don't have access to this." : detailText(detail, res.status);
  return new RequestError(msg, res.status, data?.detail);
};

export async function request(path, { method = 'GET', body, signal, headers } = {}) {
  const res = await send(path, {
    method,
    signal,
    headers: { ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}), ...headers },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : null; } catch { data = null; }
  if (!res.ok) throw failure(res, text, data);
  return data;
}

/* NDJSON reader: calls onLine with each parsed line as it arrives. */
export async function requestStream(path, { signal, onLine }) {
  const res = await send(path, { signal });
  if (!res.ok) {
    const text = await res.text();
    let data;
    try { data = text ? JSON.parse(text) : null; } catch { data = null; }
    throw failure(res, text, data);
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, i).trim();
      buf = buf.slice(i + 1);
      if (line) onLine(JSON.parse(line));
    }
  }
  if (buf.trim()) onLine(JSON.parse(buf));
}

export const isAbort = (e) => e?.name === 'AbortError';

/* ------------------------------------------------------------------ time */

export const ET = 'America/New_York';

export const parseTs = (iso) => {
  if (!iso) return null;
  const tagged = /(Z|[+-]\d{2}:?\d{2})$/.test(iso) ? iso : `${iso}Z`;
  const d = new Date(tagged);
  return Number.isNaN(d.getTime()) ? null : d;
};

export const absolute = (iso, withYear = false) => {
  const d = parseTs(iso);
  if (!d) return '';
  return `${d.toLocaleString('en-US', {
    timeZone: ET, month: 'short', day: 'numeric', ...(withYear ? { year: 'numeric' } : {}),
    hour: 'numeric', minute: '2-digit',
  })} ET`;
};

export const shortDate = (iso) => {
  const d = parseTs(iso);
  return d ? d.toLocaleDateString('en-US', { timeZone: ET, month: 'short', day: 'numeric', year: 'numeric' }) : '';
};

export const clock = (iso) => {
  const d = parseTs(iso);
  return d ? d.toLocaleTimeString('en-US', { timeZone: ET, hour12: false }) : '';
};

export const ageMs = (iso) => {
  const d = parseTs(iso);
  return d ? Math.max(0, Date.now() - d.getTime()) : null;
};

export const relative = (iso, never = 'Never') => {
  const ms = ageMs(iso);
  if (ms == null) return never;
  const mins = Math.round(ms / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins} min ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs} h ago`;
  const days = Math.round(hrs / 24);
  if (days < 45) return `${days} day${days === 1 ? '' : 's'} ago`;
  const months = Math.round(days / 30);
  if (months < 18) return `${months} month${months === 1 ? '' : 's'} ago`;
  return shortDate(iso);
};

export const daysOld = (iso) => {
  const ms = ageMs(iso);
  return ms == null ? null : Math.floor(ms / 86400000);
};

/* Bucket dates are Eastern calendar days already; noon UTC keeps the label on
   the right day when it is formatted back in Eastern. */
export const dayLabel = (isoDate, long = false) =>
  new Date(`${isoDate}T12:00:00Z`).toLocaleDateString('en-US', {
    timeZone: ET, month: 'short', day: 'numeric', ...(long ? { weekday: 'short' } : {}),
  });

export const greeting = () => {
  const h = Number(new Date().toLocaleString('en-US', { timeZone: ET, hour: 'numeric', hour12: false }));
  return h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening';
};

/* --------------------------------------------------------------- numbers */

export const fmt = (n) => {
  const v = Math.round(Number(n) || 0);
  if (Math.abs(v) >= 1e6) return `${(v / 1e6).toFixed(Math.abs(v) >= 1e7 ? 0 : 1)}M`;
  if (Math.abs(v) >= 1e4) return `${Math.round(v / 1e3)}k`;
  if (Math.abs(v) >= 1e3) return `${(v / 1e3).toFixed(1)}k`;
  return String(v);
};

export const fmtDur = (ms) => {
  if (ms == null || Number.isNaN(Number(ms))) return '–';
  const v = Math.round(Number(ms));
  if (v < 1000) return `${v} ms`;
  const sec = v / 1000;
  if (sec < 60) return `${sec.toFixed(sec < 10 ? 1 : 0)} s`;
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  if (m < 60) return `${m}m ${String(s).padStart(2, '0')}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`;
};

export const plural = (n, word, many) => `${Number(n).toLocaleString()} ${n === 1 ? word : (many || `${word}s`)}`;
export const sum = (arr) => arr.reduce((a, b) => a + (Number(b) || 0), 0);

export const pctChange = (now, before) => {
  if (!before) return null;
  return Math.round(((now - before) / before) * 100);
};

export const bytes = (b) => (b >= 1048576 ? `${(b / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1024))} KB`);

/* ------------------------------------------------------------- the API */

/* Mirrors _slugify in data_api.py, reserved words included, so the URL shown
   in the wizard is the URL the API gets. The server may still add -2 to break
   a tie with an existing slug. */
const RESERVED_SLUGS = new Set([
  'list', 'create', 'sources', 'source-databases', 'source-schemas', 'source-tables',
  'source-columns', 'docs', 'openapi', 'redoc', 'overview', 'recent-calls', 'search', 'view',
]);

export const slugify = (s) => {
  const base = (s || '').trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
  if (!base) return 'api';
  return RESERVED_SLUGS.has(base) ? `${base}-api` : base;
};

export const cleanAlias = (s) => (s || '').replace(/[^a-zA-Z0-9_-]/g, '');
export const aliasFromTable = (t) => (t || '').toLowerCase().replace(/\.[a-z0-9]+$/, '').replace(/[^a-z0-9_]/g, '_');

export const KEY_PLACEHOLDER = 'YOUR_API_KEY';
export const maskKey = (key) => (key ? `${key.slice(0, 9)}${'•'.repeat(14)}${key.slice(-4)}` : '');
export const baseUrl = (slug) => `${API_BASE_URL}/data-api/${slug}`;

export const TABS = [
  ['overview', 'Overview'],
  ['tables', 'Tables'],
  ['endpoints', 'Endpoints'],
  ['search', 'Semantic search'],
  ['docs', 'Docs'],
  ['activity', 'Activity'],
];

export const ROW_LIMITS = [50, 100, 200, 500, 1000, 2000, 5000, 10000];

export const FILTER_OPS = [
  ['eq', 'equals'], ['ne', 'not equals'], ['gt', 'greater than'], ['gte', '≥'], ['lt', 'less than'],
  ['lte', '≤'], ['in', 'one of'], ['not_in', 'not one of'], ['contains', 'contains'],
  ['starts_with', 'starts with'], ['is_null', 'is empty'], ['not_null', 'is not empty'],
];
export const NO_VALUE_OPS = ['is_null', 'not_null'];

export const API_COLORS = ['#eb1700', '#0f68b2', '#8c3bbb', '#ff6017', '#328714', '#69d0ff', '#a39992', '#541980'];
export const ALIAS_COLORS = ['#eb1700', '#0f68b2', '#8c3bbb', '#ff6017', '#328714', '#69d0ff'];

/* ---------------------------------------------------------- source systems */

/* Labels come from /sources when it has answered. This only adds the colours
   and badges that the server has no reason to know about. */
const SOURCE_LOOK = {
  snowflake: { label: 'Snowflake', logo: snowflakeLogo, color: '#29b5e8', bg: '#69d0ff33', ink: '#004685' },
  postgresql: { label: 'PostgreSQL', logo: postgresLogo, color: '#0f68b2', bg: '#0f68b21f', ink: '#0f68b2' },
  postgres: { label: 'PostgreSQL', logo: postgresLogo, color: '#0f68b2', bg: '#0f68b21f', ink: '#0f68b2' },
  s3: { label: 'S3', logo: s3Logo, color: '#ff6017', bg: '#ff601722', ink: '#9e3a00' },
  docubridge: { label: 'DocuBridge', logo: postgresLogo, mono: 'DB', color: '#8c3bbb', bg: '#8c3bbb1f', ink: '#541980' },
  rimdocs: { label: 'RIM Docs', logo: oracleLogo, mono: 'RD', color: '#541980', bg: '#5419801a', ink: '#541980' },
  insight7: { label: 'Insight7', logo: oracleLogo, mono: 'I7', color: '#eb1700', bg: '#eb170014', ink: '#9e0000' },
  denodo: { label: 'Denodo', logo: denodoLogo, mono: 'DN', color: '#328714', bg: '#32871418', ink: '#3b5a0d' },
  sqlserver: { label: 'Lift (SQL Server)', mono: 'SQ', color: '#9e0000', bg: '#9e000014', ink: '#9e0000' },
  grail: { label: 'GRAIL', mono: 'GR', color: '#6e6259', bg: '#6e625918', ink: '#312c2a' },
  databricks: { label: 'Databricks', mono: 'DB', color: '#ff6017', bg: '#ff601722', ink: '#9e3a00' },
};

let sourceLabels = {};
export const rememberSourceLabels = (sources) => {
  sourceLabels = Object.fromEntries((sources || []).map((s) => [s.id, s.label]));
};

export const sourceLook = (id) => {
  const key = (id || '').toLowerCase();
  const look = SOURCE_LOOK[key] || {
    mono: (id || '?').slice(0, 2).toUpperCase(), color: '#a39992', bg: '#f1efed', ink: '#6e6259',
  };
  return { ...look, label: sourceLabels[id] || look.label || id || 'Unknown' };
};

export const tablePath = (t) =>
  [sourceLook(t.source_system).label, (t.env || '').toUpperCase(), t.database, t.schema ?? t.db_schema, t.table]
    .filter(Boolean)
    .join(' / ');

/* ----------------------------------------------------------------- clients */

/* Consumer calls carry nothing but the key, so the user agent is the only
   hint about what made a call. Power BI and Excel both send
   Microsoft.Data.Mashup, so they share a tile. */
export const TOOLS = {
  powerbi: { name: 'Power BI / Excel', tile: 'BI', color: '#ff6017' },
  python: { name: 'Python script', tile: 'PY', color: '#0f68b2' },
  tableau: { name: 'Tableau', tile: 'TB', color: '#8c3bbb' },
  postman: { name: 'Postman', tile: 'PM', color: '#eb1700' },
  bruno: { name: 'Bruno', tile: 'BR', color: '#f4aa41' },
  browser: { name: 'Web browser', tile: 'WEB', color: '#0f68b2' },
  curl: { name: 'curl', tile: '>_', color: '#6e6259' },
  java: { name: 'Java client', tile: 'JV', color: '#9e0000' },
  node: { name: 'Node script', tile: 'JS', color: '#328714' },
  databricks: { name: 'Databricks', tile: 'DB', color: '#ff6017' },
  smarthub: { name: 'SmartHub', tile: 'SH', color: '#eb1700' },
  unknown: { name: 'Unknown client', tile: '?', color: '#a39992' },
};

export const toolOf = (ua) => {
  const s = (ua || '').toLowerCase();
  if (!s) return 'unknown';
  if (s.startsWith('smarthub')) return 'smarthub';
  if (s.includes('mashup') || s.includes('powerbi') || s.includes('power bi') || s.includes('msoffice') || s.includes('excel')) return 'powerbi';
  if (s.includes('tableau')) return 'tableau';
  if (s.includes('postman')) return 'postman';
  if (s.includes('bruno')) return 'bruno';
  if (s.startsWith('curl')) return 'curl';
  if (s.includes('python') || s.includes('httpx') || s.includes('urllib') || s.includes('aiohttp')) return 'python';
  if (s.includes('java/') || s.includes('okhttp') || s.includes('apache-httpclient')) return 'java';
  if (s.includes('node') || s.includes('axios') || s.includes('undici')) return 'node';
  if (s.includes('databricks') || s.includes('spark')) return 'databricks';
  if (s.includes('mozilla')) return 'browser';
  return 'unknown';
};

export const toolName = (ua) => TOOLS[toolOf(ua)].name;

export const describeAgent = (ua) => {
  if (!ua) return 'No user agent';
  const s = ua.toLowerCase();
  if (s.startsWith('smarthub')) {
    const inner = ua.replace(/^smarthub\s*\(?/i, '').replace(/\)$/, '');
    return inner ? `SmartHub · ${describeAgent(inner)}` : 'SmartHub';
  }
  if (s.includes('mozilla')) {
    const browser = s.includes('edg/') ? 'Edge' : s.includes('chrome/') ? 'Chrome' : s.includes('firefox/') ? 'Firefox' : s.includes('safari/') ? 'Safari' : 'Browser';
    const os = s.includes('windows') ? 'Windows' : s.includes('mac os') ? 'macOS' : s.includes('linux') ? 'Linux' : '';
    return os ? `${browser} on ${os}` : browser;
  }
  return ua;
};

export const describeIp = (ip) =>
  ['127.0.0.1', '::1', 'localhost'].includes(ip) ? `${ip} (this machine)` : (ip || 'unknown');

/* --------------------------------------------------------------- charts */

export function smoothPath(pts) {
  if (pts.length < 2) return pts.length ? `M${pts[0][0]},${pts[0][1]}` : '';
  let d = `M${pts[0][0]},${pts[0][1]}`;
  for (let i = 0; i < pts.length - 1; i += 1) {
    const p0 = pts[i - 1] || pts[i];
    const p1 = pts[i];
    const p2 = pts[i + 1];
    const p3 = pts[i + 2] || p2;
    const c1x = p1[0] + (p2[0] - p0[0]) / 6;
    const c1y = p1[1] + (p2[1] - p0[1]) / 6;
    const c2x = p2[0] - (p3[0] - p1[0]) / 6;
    const c2y = p2[1] - (p3[1] - p1[1]) / 6;
    d += ` C${c1x},${c1y} ${c2x},${c2y} ${p2[0]},${p2[1]}`;
  }
  return d;
}

/* ------------------------------------------------------------- clipboard */

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand('copy');
      ta.remove();
      return ok;
    } catch {
      return false;
    }
  }
}

export const guideFilename = (name) => `SmartHub_${(name || 'data-api').replace(/\s+/g, '_')}_API_guide.md`;

/* Loads something and keeps the last good answer on screen while a refresh is
   in flight, so a reload never blanks the page. Out-of-order answers (a slow
   first request landing after a fast second one) are dropped. With `reset`,
   an answer only counts for the inputs it was fetched for: change the inputs
   and the old answer is gone at once, not one render later. */
export function useResource(loader, deps, { enabled = true, reset = false } = {}) {
  const key = JSON.stringify(deps);
  const [state, setState] = useState({ data: null, error: null, loading: enabled, key: null });
  const seq = useRef(0);
  const ctrl = useRef(null);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;
  const keyRef = useRef(key);
  keyRef.current = key;

  const run = useCallback(async ({ quiet = false } = {}) => {
    const ticket = ++seq.current;
    const forKey = keyRef.current;
    ctrl.current?.abort();
    const c = new AbortController();
    ctrl.current = c;
    if (!quiet) setState((s) => ({ ...s, loading: true, error: null }));
    try {
      // push lets a streaming loader show partial data before it finishes
      const push = (partial) => {
        if (ticket === seq.current) setState((s) => ({ ...s, data: partial, error: null, key: forKey }));
      };
      const data = await loaderRef.current(c.signal, push);
      if (ticket === seq.current) setState({ data, error: null, loading: false, key: forKey });
      return data;
    } catch (e) {
      if (isAbort(e) || ticket !== seq.current) return undefined;
      setState((s) => ({ ...s, error: e, loading: false, key: forKey }));
      return undefined;
    }
  }, []);

  useEffect(() => {
    if (!enabled) {
      ctrl.current?.abort();
      seq.current += 1;
      setState((s) => ({ ...s, loading: false }));
      return undefined;
    }
    run();
    return () => ctrl.current?.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, key]);

  const setData = useCallback((fn) => setState((s) => ({ ...s, data: typeof fn === 'function' ? fn(s.data) : fn })), []);
  const stale = state.key !== key;
  if (reset && (stale || !enabled)) {
    return { data: null, error: null, loading: enabled, reload: run, setData };
  }
  return { ...state, loading: state.loading || (enabled && state.key == null && !state.error), reload: run, setData };
}

/* Calls fn every `ms` while the tab is visible. Pauses in the background and
   catches up straight away when the tab comes back. */
export function usePoll(fn, ms, enabled = true) {
  const fnRef = useRef(fn);
  fnRef.current = fn;
  useEffect(() => {
    if (!enabled) return undefined;
    let timer = null;
    let stopped = false;
    const tick = async () => {
      if (stopped) return;
      if (!document.hidden) {
        try { await fnRef.current(); } catch { /* the caller shows its own errors */ }
      }
      if (!stopped) timer = setTimeout(tick, ms);
    };
    const onVisible = () => {
      if (!document.hidden) {
        clearTimeout(timer);
        tick();
      }
    };
    timer = setTimeout(tick, ms);
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      stopped = true;
      clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [ms, enabled]);
}

/* 0 → 1 over `ms`, eased, restarting whenever `key` changes. */
export function useProgress(key, ms = 1100) {
  const [p, setP] = useState(0);
  useEffect(() => {
    let raf;
    const t0 = performance.now();
    const tick = (t) => {
      const x = Math.min(1, (t - t0) / ms);
      setP(1 - (1 - x) ** 3);
      if (x < 1) raf = requestAnimationFrame(tick);
    };
    setP(0);
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [key, ms]);
  return p;
}

/* Shows a loading treatment only when loading takes long enough to notice,
   so fast answers don't flash a spinner. */
export function useSlow(loading, delay = 250) {
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    if (!loading) {
      setSlow(false);
      return undefined;
    }
    const t = setTimeout(() => setSlow(true), delay);
    return () => clearTimeout(t);
  }, [loading, delay]);
  return slow;
}

export function useLocalSetting(key, fallback) {
  const [value, setValue] = useState(() => {
    try {
      const raw = localStorage.getItem(key);
      return raw == null ? fallback : JSON.parse(raw);
    } catch {
      return fallback;
    }
  });
  const set = useCallback((v) => {
    setValue(v);
    try { localStorage.setItem(key, JSON.stringify(v)); } catch { /* private mode */ }
  }, [key]);
  return [value, set];
}

/* The key settles into place character by character instead of popping in. */
export function useScramble(target, active, frames = 26) {
  const [text, setText] = useState(target);
  useEffect(() => {
    if (!active || !target) {
      setText(target);
      return undefined;
    }
    const chars = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz0123456789';
    let f = 0;
    const id = setInterval(() => {
      f += 1;
      const k = Math.floor((target.length * f) / frames);
      let s = target.slice(0, k);
      for (let i = k; i < target.length; i += 1) s += chars[Math.floor(Math.random() * chars.length)];
      setText(s);
      if (f >= frames) {
        clearInterval(id);
        setText(target);
      }
    }, 32);
    return () => clearInterval(id);
  }, [target, active, frames]);
  return text;
}


/* The newest consumer calls, kept fresh by asking only for what arrived since
   the last answer. `onNew` sees each new batch (oldest first), which is how
   the gateway animation knows what to fly. */
export function useLiveCalls({ apiId = '', keep = 7, interval = 4000, paused = false, onNew } = {}) {
  const [state, setState] = useState({ calls: [], perMinute: 0, loading: true, error: null });
  const latest = useRef(null);
  const onNewRef = useRef(onNew);
  onNewRef.current = onNew;

  const fetchCalls = useCallback(async (first = false) => {
    const qs = new URLSearchParams({ limit: String(first ? keep : 25) });
    if (apiId) qs.set('api_id', apiId);
    if (!first && latest.current != null) qs.set('after_id', String(latest.current));
    try {
      const d = await request(`/data-api/recent-calls?${qs}`);
      const fresh = d?.calls || [];
      if (d?.latest_id != null) latest.current = d.latest_id;
      setState((s) => ({
        calls: first ? fresh : [...fresh, ...s.calls].slice(0, keep),
        perMinute: d?.per_minute ?? 0,
        loading: false,
        error: null,
        bump: first ? 0 : (s.bump || 0) + (fresh.length ? 1 : 0),
      }));
      if (!first && fresh.length) onNewRef.current?.(fresh.slice().reverse());
    } catch (e) {
      setState((s) => ({ ...s, loading: false, error: e }));
    }
  }, [apiId, keep]);

  useEffect(() => {
    latest.current = null;
    fetchCalls(true);
  }, [fetchCalls]);

  usePoll(() => fetchCalls(false), interval, !paused);
  return { ...state, retry: () => fetchCalls(true) };
}

export const ToastContext = createContext({ toast: () => {} });
export const ConfirmContext = createContext({ confirm: async () => false });
export const PageContext = createContext(null);

export const useToast = () => useContext(ToastContext).toast;
export const useConfirm = () => useContext(ConfirmContext).confirm;
/* What every part of the page needs to know: who is looking, what they may do,
   the source labels, and how to move around. Provided by DataAPIExport. */
export const usePage = () => useContext(PageContext);

/* The picker's value, and turning it (or a stored table) into the TableSpec
   the API takes. */

export const emptyPick = () => ({
  source: '', env: 'prod', database: '', schema: '', table: '', alias: '', limit: 1000, picked: null, cols: null, colState: '',
});

export const pickReady = (P, takenAliases) => {
  const alias = (P.alias || '').trim();
  if (!P.table || !alias || takenAliases.includes(alias.toLowerCase())) return false;
  if (P.colState === 'loading') return false;
  return !P.cols || (P.picked || []).length > 0;
};

export const pickToSpec = (P) => ({
  source_system: P.source,
  env: P.env,
  database: P.database || null,
  db_schema: P.schema || null,
  table: P.table,
  alias: P.alias.trim(),
  row_limit: Number(P.limit) || 1000,
  columns: !P.cols || !P.picked || P.picked.length === P.cols.length ? [] : P.picked,
});

export const tableToSpec = (t) => ({
  id: t.id,
  source_system: t.source_system,
  env: t.env,
  database: t.database || null,
  db_schema: t.schema || null,
  table: t.table,
  alias: t.alias,
  row_limit: t.row_limit,
  columns: t.columns || [],
});