import React, { useState, useEffect, useLayoutEffect, useRef, useCallback, useMemo } from 'react';
import { createPortal } from 'react-dom';
import { useSearchParams } from 'react-router-dom';
import { apiJson, apiFetch } from '../../utils/apiClient';
import { useRBAC } from '../../context/RBACContext';
import { usePerfTier, measureTier } from '../../utils/perfTier';
import './indexer.css';

/* Index Health. Pick any Postgres table, say how its columns are queried, see
   which indexes are missing and build them with a live log. Builds run on the
   server, so several can be in flight (one per table) while you keep working.
   Backend: backend/indexer.py.

   URL: ?pane=indexer&ih_env=dev&ih_db=..&ih_schema=..&ih_table=..&ih_step=usage|results|build
   Prefixed so they never clash with the Postgres pane's db/schema/table. */

const ENVS = ['dev', 'qa', 'prod'];
const JOBS_KEY = 'indexHealth.jobs';
const CHECKED_KEY = 'indexHealth.checked';
const SEP = '\u0001'; // Postgres names can contain '/' or '.'
const P = { env: 'ih_env', db: 'ih_db', schema: 'ih_schema', table: 'ih_table', step: 'ih_step' };
const RED = '#eb1700', BLUE = '#0f68b2', PURPLE = '#8c3bbb', GREEN = '#328714', AMBER = '#c26a00', ORANGE = '#d9480f', MUTED = '#6e6259';
const isMac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
const KBD = isMac ? '⌘K' : 'Ctrl K';

/* ─────────────────────────────── helpers ─────────────────────────────── */

// FastAPI 422s send detail as a list of {msg}; everything else is a string
const msg = (e) => {
  const d = e?.detail;
  if (Array.isArray(d)) return d.map((x) => x?.msg || JSON.stringify(x)).join('; ');
  if (typeof d === 'string') return d;
  return e?.message || String(e);
};
const qs = (o) => new URLSearchParams(Object.entries(o).filter(([, v]) => v != null && v !== '')).toString();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const fmt = (n) => (n ?? 0).toLocaleString();
const compact = (n) => (n == null ? '?' : n >= 1e9 ? `${(n / 1e9).toFixed(1)}B` : n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : `${n}`);
const plural = (n, word, many = `${word}s`) => `${n} ${n === 1 ? word : many}`;
const keyOf = (t) => [t.env, t.database || '', t.schema, t.table].join(SEP);
const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);

const ago = (iso) => {
  if (!iso) return 'Never analyzed';
  const h = Math.round((Date.now() - new Date(iso)) / 36e5);
  if (h < 1) return 'just now';
  return h < 48 ? `${h}h ago` : `${Math.round(h / 24)} days ago`;
};

const store = {
  read(k, d) { try { return JSON.parse(sessionStorage.getItem(k)) ?? d; } catch { return d; } },
  write(k, v) { try { sessionStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } },
};
// jobs survive a reload (the build keeps running server-side); the log is refetched
const readJobs = () => (store.read(JOBS_KEY, []) || []).map((j) => ({ ...j, log: [], next: 0, loaded: false }));
// JSON drops undefined, so the log and poll cursor stay out of storage
const writeJobs = (jobs) => store.write(JOBS_KEY, jobs.map((j) => ({ ...j, log: undefined, next: undefined, loaded: undefined })));

/* ─────────────────────────────── scoring ─────────────────────────────── */

// ponytail: client-side score; move to indexer.py if anything else needs it
const scoreOf = (s) => Math.max(6, Math.min(100, 100 - s.critical * 20 - s.high * 9 - s.redundant * 4 - s.unused * 3));
const meta = (s) => (s == null ? ['Not checked', MUTED] : s >= 85 ? ['Healthy', GREEN] : s >= 65 ? ['Fair', AMBER] : s >= 45 ? ['Needs work', ORANGE] : ['Critical', RED]);

const FEAT = {
  key: 'Add a primary key', stats: 'Refresh planner statistics', vector: 'Index vector search', substring: 'Index substring search',
  words: 'Index full-text search', filter: 'Index lookups and sorting', drop: 'Drop index', extension: 'Install pg_trgm',
};
const featLabel = (f) => FEAT[f] || f;

const titleOf = (m) => {
  const c = m.column;
  if (m.feature === 'drop') {
    const name = m.id.slice(5);
    return m.status === 'unused' ? `${name} is never used` : m.status === 'redundant' ? `${name} is redundant` : `${name} is invalid`;
  }
  if (m.feature === 'vector' && m.priority === 'info') return `Other similarity metrics on ${c}`;
  return {
    key: 'This table has no primary key', stats: 'The planner has no statistics', extension: 'pg_trgm is not installed',
    vector: `Vector search on ${c} reads every row`, substring: `Substring search on ${c} has no trigram index`,
    words: `Full-text search on ${c} has no GIN index`, filter: `Lookups on ${c} scan the whole table`,
  }[m.feature] || featLabel(m.feature);
};

const methodOf = (m) => {
  if (m.feature === 'key') return 'Primary key';
  if (m.feature === 'stats') return 'ANALYZE';
  if (m.feature === 'extension') return 'Extension';
  if (m.feature === 'drop') return 'Drop';
  const u = /USING (\w+)/.exec(m.sql)?.[1] || 'btree';
  if (u === 'hnsw') return `HNSW · ${/_(cosine|l2|ip)_ops/.exec(m.sql)?.[1] || 'cosine'}`;
  if (u === 'gin') return /to_tsvector/.test(m.sql) ? 'GIN · full-text' : 'GIN · trigram';
  return { hash: 'Hash', btree: 'B-tree', gist: 'GiST' }[u] || u;
};

const PRI = {
  critical: { n: 3, color: RED, bg: 'rgba(235,23,0,.08)', tip: 'Critical: queries read the whole table, or the table is missing something basic' },
  high: { n: 2, color: ORANGE, bg: 'rgba(217,72,15,.09)', tip: 'High: a query pattern you marked has no index behind it' },
  info: { n: 1, color: AMBER, bg: 'rgba(194,106,0,.1)', tip: 'Optional: worth doing only if it matches how the table is used' },
};

const USES = [
  { key: 'filter', label: 'Lookup', color: BLUE, ok: (c) => c.lookup, tip: 'Lookup and sort: =, IN, <, >, ORDER BY' },
  { key: 'search', label: 'Text', color: PURPLE, ok: (c) => c.text, tip: "Text search: ILIKE '%term%' and full-text" },
  { key: 'vector', label: 'Vector', color: RED, ok: (c) => c.vector, tip: 'Vector similarity: <=> nearest-neighbour search' },
];
const DIAG = ['Reading the catalog and index stats', 'Matching columns to existing indexes', 'Checking for invalid, duplicate and unused indexes', 'Matching your query patterns', 'Ranking fixes by impact'];

const usageLists = (usage) => {
  const pick = (k) => Object.keys(usage).filter((c) => (usage[c] || []).includes(k));
  return { search: pick('search'), filter: pick('filter'), vector: pick('vector') };
};
const sameUsage = (a, b) => ['search', 'filter', 'vector'].every((k) => [...(a[k] || [])].sort().join() === [...(b[k] || [])].sort().join());

const fetchCheck = (t, lists) => apiJson(`/index-health?${new URLSearchParams({
  env: t.env, database: t.database || '', schema: t.schema, table: t.table,
  // always send all three; empty means "nothing uses it that way"
  search: lists.search.join(','), filter: lists.filter.join(','), vector: lists.vector.join(','),
})}`);

const whyOf = (r) => {
  const s = r.summary;
  const gaps = s.critical + s.high;
  if (gaps) return `${plural(gaps, 'gap')}${s.unused + s.redundant ? ` · ${s.unused + s.redundant} to tidy` : ''}`;
  return s.unused + s.redundant ? `${plural(s.unused + s.redundant, 'index', 'indexes')} to tidy` : 'No gaps found';
};

/* ─────────────────────────────── jobs ─────────────────────────────── */

// The server logs "[n/N] feature column" before each item, and "phase — 45% (...)" while CREATE INDEX runs.
function jobProgress(job) {
  let cur = -1, phase = '', pct = null;
  const bad = new Set();
  job.log.forEach((l) => {
    const m = /^\[(\d+)\/\d+\]/.exec(l.msg);
    if (m) { cur = +m[1] - 1; phase = ''; pct = null; return; }
    if (l.level === 'error' && cur >= 0 && !l.msg.startsWith('finished')) bad.add(cur);
    if (l.level === 'progress') {
      phase = l.msg.split(/ — | · | \(/)[0];
      const p = /— (\d+)%/.exec(l.msg);
      pct = p ? +p[1] : null;
    }
  });
  const fin = !!job.finished;
  const steps = job.items.map((it, i) => {
    let st = 'queued';
    if (i < cur) st = bad.has(i) ? 'failed' : 'ok';
    else if (i === cur) st = !fin ? 'now' : bad.has(i) ? 'failed' : job.status === 'cancelled' ? 'skipped' : 'ok';
    else if (fin) st = job.status === 'done' ? 'ok' : 'skipped';
    return { ...it, st, pct: st === 'ok' ? 100 : st === 'now' ? pct : 0 };
  });
  const done = steps.filter((s) => s.st === 'ok').length;
  const now = steps.find((s) => s.st === 'now');
  // ponytail: a phase's % stands in for the item's; CONCURRENTLY runs 2-3 phases so the bar can step back
  const overall = job.status === 'done' ? 100 : Math.round(((done + (now ? (now.pct || 0) / 100 * 0.9 : 0)) / Math.max(1, steps.length)) * 100);
  return { steps, done, now, phase, overall, failed: steps.filter((s) => s.st === 'failed').length };
}
const secs = (job) => Math.max(0, Math.round(((job.endedAt || Date.now()) - job.t0) / 1000));
const clock = (s) => (s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s` : `${Math.floor(s / 3600)}h ${String(Math.floor(s % 3600 / 60)).padStart(2, '0')}m`);
const itemName = (it) => it.name || `${featLabel(it.feature)}${it.column ? ` · ${it.column}` : ''}`;

/* ─────────────────────────────── icons ─────────────────────────────── */

const PATHS = {
  search: <><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></>,
  db: <><ellipse cx="12" cy="5" rx="8" ry="3" /><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5" /><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3" /></>,
  schema: <><path d="m12 2 10 5-10 5L2 7z" /><path d="m2 17 10 5 10-5" /><path d="m2 12 10 5 10-5" /></>,
  table: <><rect x="3" y="3" width="18" height="18" rx="2" /><path d="M3 9h18M3 15h18M9 9v12" /></>,
  chev: <path d="m9 6 6 6-6 6" />,
  down: <path d="m6 9 6 6 6-6" />,
  right: <path d="M5 12h14M13 6l6 6-6 6" />,
  left: <path d="M19 12H5M11 6l-6 6 6 6" />,
  shield: <path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6z" />,
  refresh: <path d="M3 12a9 9 0 0 1 15.5-6.3L21 8M21 3v5h-5M21 12a9 9 0 0 1-15.5 6.3L3 16M3 21v-5h5" />,
  spark: <path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M6 18l2.5-2.5M15.5 8.5 18 6" />,
  all: <><rect x="3" y="3" width="18" height="18" rx="4" /><path d="m8 12 3 3 5-6" /></>,
  none: <rect x="3" y="3" width="18" height="18" rx="4" />,
  play: <path d="M7 4v16l13-8z" />,
  alert: <><path d="M12 9v4M12 17h.01" /><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" /></>,
  trash: <path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14" />,
  ok: <><circle cx="12" cy="12" r="9" /><path d="m8 12 3 3 5-6" /></>,
  bolt: <path d="M13 2 4 14h7l-1 8 9-12h-7z" />,
  term: <path d="m4 17 6-5-6-5M12 19h8" />,
  x: <path d="M18 6 6 18M6 6l12 12" />,
  check: <path d="m5 12 5 5 9-10" />,
  copy: <><rect x="9" y="9" width="12" height="12" rx="2" /><path d="M5 15V5a2 2 0 0 1 2-2h10" /></>,
  menu: <path d="M4 6h16M4 12h16M4 18h16" />,
  info: <><circle cx="12" cy="12" r="9" /><path d="M12 8h.01M12 11v5" /></>,
  key: <><circle cx="7.5" cy="15.5" r="3.5" /><path d="m10 13 9-9M17 6l3 3M14 9l2 2" /></>,
  edit: <path d="M4 20h4L19 9l-4-4L4 16zM14 6l4 4" />,
};
const I = ({ n, s = 16, w = 1.8, c = '' }) => (
  <svg className={`ih-ic ${c}`} width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={w} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{PATHS[n]}</svg>
);
const Spin = ({ c = '' }) => <span className={`ih-spin ${c}`} role="status" aria-label="Loading" />;

/* ─────────────────────────────── tooltips ─────────────────────────────── */

// One tooltip for the whole page: any [data-tip] inside .ih-scope. Fixed and portaled,
// so cards with overflow:hidden never clip it, and it works on keyboard focus too.
function TipLayer() {
  const [tip, setTip] = useState(null);
  const box = useRef(null);
  const [pos, setPos] = useState(null);

  useEffect(() => {
    let cur = null, timer;
    const find = (e) => e.target?.closest?.('.ih-scope [data-tip]');
    const show = (t) => { cur = t; setPos(null); setTip({ text: t.getAttribute('data-tip'), r: t.getBoundingClientRect() }); };
    const hide = () => { clearTimeout(timer); cur = null; setTip(null); };
    const over = (e) => {
      const t = find(e);
      if (!t || t === cur || !t.getAttribute('data-tip')) return;
      clearTimeout(timer);
      timer = setTimeout(() => show(t), e.type === 'focusin' ? 0 : 260);
    };
    const out = (e) => { const t = find(e); if (!t || t.contains(e.relatedTarget)) return; hide(); };
    document.addEventListener('pointerover', over);
    document.addEventListener('pointerout', out);
    document.addEventListener('focusin', over);
    document.addEventListener('focusout', hide);
    document.addEventListener('pointerdown', hide);
    window.addEventListener('scroll', hide, true);
    window.addEventListener('keydown', hide);
    return () => {
      clearTimeout(timer);
      document.removeEventListener('pointerover', over);
      document.removeEventListener('pointerout', out);
      document.removeEventListener('focusin', over);
      document.removeEventListener('focusout', hide);
      document.removeEventListener('pointerdown', hide);
      window.removeEventListener('scroll', hide, true);
      window.removeEventListener('keydown', hide);
    };
  }, []);

  useLayoutEffect(() => {
    if (!tip || !box.current) return;
    const b = box.current.getBoundingClientRect(), r = tip.r, vw = window.innerWidth;
    let top = r.top - b.height - 8, below = false;
    if (top < 8) { top = r.bottom + 8; below = true; }
    const left = Math.min(vw - b.width - 8, Math.max(8, r.left + r.width / 2 - b.width / 2));
    setPos({ top, left, below, arrow: Math.min(b.width - 12, Math.max(12, r.left + r.width / 2 - left)) });
  }, [tip]);

  if (!tip) return null;
  return createPortal(
    <div ref={box} role="tooltip" className={`ih-tip ${pos?.below ? 'below' : ''}`}
      style={pos ? { top: pos.top, left: pos.left, '--ax': `${pos.arrow}px` } : { top: -999, left: -999 }}>{tip.text}</div>,
    document.body,
  );
}

const Layer = ({ children }) => createPortal(<div className="ih-scope ih-layer">{children}</div>, document.body);

/* ─────────────────────────────── sql ─────────────────────────────── */

const KW = new Set('CREATE UNIQUE INDEX CONCURRENTLY ON USING DROP IF EXISTS ALTER TABLE ADD CONSTRAINT PRIMARY KEY ANALYZE EXTENSION NOT NULL CHECK VALID VALIDATE COLUMN SET IS'.split(' '));
const METH = new Set(['btree', 'gin', 'gist', 'hnsw', 'ivfflat', 'hash', 'brin']);
const tokens = (sql) => sql.split('\n').map((line) => (/^\s*--/.test(line) ? [{ t: line, k: 'cm' }]
  : line.split(/(\s+|[(),;]|"(?:[^"]|"")*"|'[^']*'|<id_column>)/).filter(Boolean).map((t) => ({
    t,
    k: KW.has(t) ? 'kw' : METH.has(t) ? 'mt' : /_ops$/.test(t) ? 'op' : /^[(),;]$/.test(t) ? 'pn' : t === '<id_column>' ? 'hole'
      : /^"/.test(t) ? 'id' : /^'/.test(t) ? 'st' : '',
  }))));

function Sql({ sql }) {
  const toks = useMemo(() => tokens(sql), [sql]);
  const [copied, setCopied] = useState(false);
  const t = useRef();
  useEffect(() => () => clearTimeout(t.current), []);
  const copy = async (e) => {
    e.stopPropagation();
    try { await navigator.clipboard.writeText(sql); } catch { /* blocked in some iframes */ }
    setCopied(true);
    clearTimeout(t.current);
    t.current = setTimeout(() => setCopied(false), 1400);
  };
  return (
    <div className="ih-sql" onClick={(e) => e.stopPropagation()}>
      <pre>{toks.map((line, i) => (
        <React.Fragment key={i}>{i > 0 && '\n'}{line.map((x, j) => <span key={j} className={x.k ? `t-${x.k}` : undefined}>{x.t}</span>)}</React.Fragment>
      ))}</pre>
      <button className={`ih-copy ${copied ? 'done' : ''}`} onClick={copy} data-tip={copied ? 'Copied' : 'Copy SQL'} aria-label="Copy SQL">
        <span className="a"><I n="copy" s={15} /></span><span className="b"><I n="check" s={16} w={2.6} /></span>
      </button>
    </div>
  );
}

/* ─────────────────────────────── rings ─────────────────────────────── */

function useTween(to, ms = 1100) {
  const [v, setV] = useState(to ?? 0);
  const from = useRef(to ?? 0);
  useEffect(() => {
    if (to == null) return undefined;
    const a = from.current, t0 = performance.now();
    let raf = requestAnimationFrame(function step(now) {
      const k = Math.min(1, (now - t0) / ms);
      setV(Math.round(a + (to - a) * (1 - (1 - k) ** 3)));
      if (k < 1) raf = requestAnimationFrame(step);
    });
    from.current = to;
    return () => cancelAnimationFrame(raf);
  }, [to, ms]);
  return to == null ? null : v;
}

function BigRing({ score }) {
  const shown = useTween(score);
  const [label, color] = meta(score);
  return (
    <span className="ih-bigring" data-tip={score == null ? 'No score yet' : `Health ${score} of 100: ${label}`}>
      <svg viewBox="0 0 160 160">
        <circle cx="80" cy="80" r="68" fill="none" stroke="rgba(110,98,89,.12)" strokeWidth="10" />
        <circle cx="80" cy="80" r="68" fill="none" stroke={color} strokeWidth="10" strokeLinecap="round" strokeDasharray="427.3"
          strokeDashoffset={427.3 * (1 - (shown || 0) / 100)} style={{ transition: 'stroke .5s' }} />
      </svg>
      <span className="ih-bigring-mid"><b>{shown ?? '—'}</b><small>of 100</small></span>
    </span>
  );
}

// own component so a count-up re-renders one number, not the whole page
const TweenNum = ({ value }) => { const v = useTween(value); return v ?? '—'; };

const MiniRing = ({ score, size = 42 }) => {
  const color = meta(score)[1];
  return (
    <span className="ih-mini" style={{ width: size, height: size }}>
      <svg viewBox="0 0 46 46" width={size} height={size}>
        <circle cx="23" cy="23" r="18" fill="none" stroke="rgba(110,98,89,.16)" strokeWidth="4" strokeDasharray={score == null ? '3 4' : undefined} />
        {score != null && <circle cx="23" cy="23" r="18" fill="none" stroke={color} strokeWidth="4" strokeLinecap="round" strokeDasharray="113.1" strokeDashoffset={113.1 * (1 - score / 100)} />}
      </svg>
      <b>{score ?? '—'}</b>
    </span>
  );
};

/* ─────────────────────────────── hero radar ─────────────────────────────── */

// Per tier: particles, arcs, max device-pixel ratio, frame cap. Motion is time-based,
// so a lower frame cap never slows it down, it just draws fewer frames.
const RADAR = { full: [84, 38, 1.5, 60], lite: [52, 28, 1, 30], min: [30, 18, 1, 24] };

function HeroRadar({ nodes, active, tier }) {
  const ref = useRef(null);
  const nodesRef = useRef(nodes);
  nodesRef.current = nodes;

  useEffect(() => measureTier(5), []);

  useEffect(() => {
    const c = ref.current;
    if (!c || !active) return undefined;
    const [NP, NA, MAXDPR, FPS] = RADAR[tier] || RADAR.lite;
    const rnd = Math.random;
    const arcs = Array.from({ length: NA }, () => {
      const p = rnd();
      return { r: 0.42 + rnd() * 0.56, s: rnd() * 6.28, len: 0.15 + rnd() * 1.2, v: (rnd() < 0.5 ? -1 : 1) * (0.02 + rnd() * 0.1), lw: 0.7 + rnd() * 2.4,
        col: p < 0.55 ? `rgba(15,104,178,${0.12 + rnd() * 0.38})` : p < 0.8 ? `rgba(235,23,0,${0.1 + rnd() * 0.22})` : `rgba(110,98,89,${0.1 + rnd() * 0.14})` };
    });
    const pts = Array.from({ length: NP }, () => ({ r: 0.4 + rnd() * 0.58, a: rnd() * 6.28, v: (rnd() < 0.5 ? -1 : 1) * (0.01 + rnd() * 0.05), s: 0.7 + rnd() * 1.5, red: rnd() < 0.25 }));
    const x = c.getContext('2d', { alpha: true });
    // Static layers (guide rings, centre glow) are drawn once per resize and blitted every frame.
    const rings = document.createElement('canvas'), glow = document.createElement('canvas');
    let w = 0, hh = 0, dpr = 1, raf = 0, last = 0, visible = true;

    const resize = () => {
      w = c.clientWidth; hh = c.clientHeight;
      if (!w) return;
      dpr = Math.min(MAXDPR, window.devicePixelRatio || 1);
      [c, rings, glow].forEach((k) => { k.width = Math.round(w * dpr); k.height = Math.round(hh * dpr); });
      const cx = w / 2, cy = hh / 2, R = (Math.min(w, hh) / 2) * 0.96;
      const g1 = rings.getContext('2d'); g1.setTransform(dpr, 0, 0, dpr, 0, 0);
      g1.strokeStyle = 'rgba(110,98,89,.16)'; g1.lineWidth = 1;
      [0.4, 0.7, 0.99].forEach((f) => { g1.beginPath(); g1.arc(cx, cy, R * f, 0, 6.283); g1.stroke(); });
      const g2 = glow.getContext('2d'); g2.setTransform(dpr, 0, 0, dpr, 0, 0);
      const cg = g2.createRadialGradient(cx, cy, 0, cx, cy, R * 0.42);
      cg.addColorStop(0, 'rgba(255,255,255,.96)'); cg.addColorStop(0.75, 'rgba(255,255,255,.88)'); cg.addColorStop(1, 'rgba(255,255,255,0)');
      g2.fillStyle = cg; g2.beginPath(); g2.arc(cx, cy, R * 0.42, 0, 6.283); g2.fill();
    };

    const draw = (t) => {
      raf = 0;
      schedule();
      if (t - last < 1000 / FPS - 1) return;
      last = t;
      if (!w) return;
      x.setTransform(1, 0, 0, 1, 0, 0);
      x.clearRect(0, 0, c.width, c.height);
      x.drawImage(rings, 0, 0);
      x.setTransform(dpr, 0, 0, dpr, 0, 0);
      const cx = w / 2, cy = hh / 2, R = (Math.min(w, hh) / 2) * 0.96, time = t / 1000;
      x.lineCap = 'round';
      for (const a of arcs) { const st = a.s + time * a.v; x.beginPath(); x.strokeStyle = a.col; x.lineWidth = a.lw; x.arc(cx, cy, a.r * R, st, st + a.len); x.stroke(); }
      const px = new Float32Array(pts.length), py = new Float32Array(pts.length);
      pts.forEach((p, i) => { const g = p.a + time * p.v; px[i] = cx + Math.cos(g) * p.r * R; py[i] = cy + Math.sin(g) * p.r * R; });
      x.lineWidth = 0.6; x.strokeStyle = 'rgba(15,104,178,.14)'; x.beginPath();
      const L2 = (R * 0.13) ** 2;
      for (let i = 0; i < pts.length; i++) for (let j = i + 1; j < pts.length; j++) {
        const dx = px[i] - px[j], dy = py[i] - py[j];
        if (dx * dx + dy * dy < L2) { x.moveTo(px[i], py[i]); x.lineTo(px[j], py[j]); }
      }
      x.stroke();
      // one path per colour instead of one fill per dot
      [false, true].forEach((red) => {
        x.fillStyle = red ? 'rgba(235,23,0,.45)' : 'rgba(15,104,178,.5)'; x.beginPath();
        pts.forEach((p, i) => { if (p.red === red) { x.moveTo(px[i] + p.s, py[i]); x.arc(px[i], py[i], p.s, 0, 6.283); } });
        x.fill();
      });
      const sw = (time * 0.7) % 6.283;
      if (x.createConicGradient && tier !== 'min') {
        const g = x.createConicGradient(sw - 1.1, cx, cy);
        g.addColorStop(0, 'rgba(235,23,0,0)'); g.addColorStop(1.1 / 6.283, 'rgba(235,23,0,.13)'); g.addColorStop(1.1 / 6.283 + 0.001, 'rgba(235,23,0,0)'); g.addColorStop(1, 'rgba(235,23,0,0)');
        x.fillStyle = g;
      } else x.fillStyle = 'rgba(235,23,0,.06)';
      x.beginPath(); x.moveTo(cx, cy); x.arc(cx, cy, R * 0.99, sw - 1.1, sw); x.closePath(); x.fill();
      x.strokeStyle = 'rgba(235,23,0,.4)'; x.lineWidth = 1.2; x.beginPath();
      x.moveTo(cx + Math.cos(sw) * R * 0.4, cy + Math.sin(sw) * R * 0.4); x.lineTo(cx + Math.cos(sw) * R * 0.99, cy + Math.sin(sw) * R * 0.99); x.stroke();
      x.setTransform(1, 0, 0, 1, 0, 0);
      x.drawImage(glow, 0, 0);
      x.setTransform(dpr, 0, 0, dpr, 0, 0);
      const list = nodesRef.current.slice(0, 12), n = list.length;
      x.font = '400 11px JohnsonText';
      list.forEach((nd, i) => {
        const g = (i * 6.283) / n - 1.2 + time * 0.03, rr = R * 0.64, qx = cx + Math.cos(g) * rr, qy = cy + Math.sin(g) * rr;
        const d = (((sw - g) % 6.283) + 6.283) % 6.283, pulse = d < 0.9 ? 1 - d / 0.9 : 0, col = meta(nd.score)[1];
        x.globalAlpha = 0.18 + 0.5 * pulse; x.fillStyle = col; x.beginPath(); x.arc(qx, qy, 7 + 16 * pulse, 0, 6.283); x.fill(); x.globalAlpha = 1;
        x.fillStyle = '#fff'; x.beginPath(); x.arc(qx, qy, 6.5, 0, 6.283); x.fill(); x.fillStyle = col; x.beginPath(); x.arc(qx, qy, 4.5, 0, 6.283); x.fill();
        const label = nd.table.length > 22 ? `${nd.table.slice(0, 21)}…` : nd.table;
        const tw = x.measureText(label).width;
        let right = Math.cos(g) >= 0;
        if (right && qx + 14 + tw > w - 4) right = false; else if (!right && qx - 14 - tw < 4) right = true;
        let lx = qx + (right ? 14 : -14);
        lx = right ? Math.min(lx, w - 4 - tw) : Math.max(lx, 4 + tw);
        x.textAlign = right ? 'left' : 'right'; x.textBaseline = 'middle';
        x.fillStyle = `rgba(49,44,42,${0.6 + 0.4 * pulse})`; x.fillText(label, lx, qy - 6);
        x.fillStyle = col; x.fillText(`${nd.score}`, lx, qy + 8);
      });
    };
    // only spend frames while it's on screen and the tab is visible
    function schedule() { if (!raf && visible && !document.hidden) raf = requestAnimationFrame(draw); }
    const io = new IntersectionObserver(([e]) => { visible = e.isIntersecting; schedule(); });
    const ro = new ResizeObserver(() => { resize(); schedule(); });
    const onVis = () => schedule();
    io.observe(c); ro.observe(c);
    document.addEventListener('visibilitychange', onVis);
    resize(); schedule();
    return () => { cancelAnimationFrame(raf); io.disconnect(); ro.disconnect(); document.removeEventListener('visibilitychange', onVis); };
  }, [active, tier]);

  return <canvas ref={ref} className="ih-radar" aria-hidden="true" />;
}

/* ─────────────────────────────── page ─────────────────────────────── */

function IndexHealth({ active = true }) {
  const { hasPermission } = useRBAC();
  const tier = usePerfTier(); // full | lite | min, from the app's GPU / frame-rate probe
  const canBuild = hasPermission('buildPGindexer'); // the server checks too; this just hides buttons that would 403
  const [params, setParams] = useSearchParams();

  // URL is the source of truth for where you are
  const env = ENVS.includes(params.get(P.env)) ? params.get(P.env) : 'dev';
  const tParts = [params.get(P.db) || '', params.get(P.schema), params.get(P.table)];
  const tKey = tParts[1] && tParts[2] ? [env, ...tParts].join(SEP) : '';
  const target = useMemo(() => (tKey ? { env, database: tParts[0], schema: tParts[1], table: tParts[2] } : null), [tKey]); // eslint-disable-line react-hooks/exhaustive-deps
  const urlStep = params.get(P.step) || 'usage';

  // react-router hands out a new setter on every URL change; a ref keeps nav stable so effects don't refire
  const setRef = useRef(setParams); setRef.current = setParams;
  const nav = useCallback((upd, replace = false) => {
    setRef.current((prev) => {
      const next = new URLSearchParams(prev);
      Object.entries(upd).forEach(([k, v]) => (v == null || v === '' ? next.delete(P[k]) : next.set(P[k], v)));
      return next;
    }, { replace });
  }, []);

  /* ── catalog ── */
  const [cats, setCats] = useState({}); // env -> { status, data, error }
  // Streams NDJSON: database names first (one skeleton each), then every database as soon as
  // it's read. Unreadable ones never arrive; the server already hid them.
  const catReq = useRef({});
  const loadCatalog = useCallback(async (e) => {
    const my = (catReq.current[e] = (catReq.current[e] || 0) + 1);
    const put = (fn) => setCats((c) => (catReq.current[e] === my ? { ...c, [e]: fn(c[e] || {}) } : c));
    put(() => ({ status: 'loading', pending: [] }));
    const on = (ev) => {
      if (ev.type === 'databases') put((c) => ({ ...c, status: 'streaming', pending: ev.names, data: { databases: [], tables: 0, hidden: 0 } }));
      else if (ev.type === 'db') put((c) => ({
        ...c, pending: c.pending.filter((n) => n !== ev.db.name),
        data: { ...c.data, tables: c.data.tables + ev.db.tables, databases: [...c.data.databases.filter((d) => d.name !== ev.db.name), ev.db].sort((a, b) => a.name.localeCompare(b.name)) },
      }));
      else if (ev.type === 'skip') put((c) => ({ ...c, pending: c.pending.filter((n) => n !== ev.name) }));
      else if (ev.type === 'done') put((c) => ({ ...c, status: ev.error ? 'error' : 'ready', error: ev.error || '', pending: [], data: { ...c.data, hidden: ev.hidden } }));
    };
    try {
      const res = await apiFetch(`/index-health/catalog/stream?${qs({ env: e })}`);
      if (!res.ok) {
        let detail = res.statusText;
        try { detail = (await res.json()).detail || detail; } catch { /* not json */ }
        throw Object.assign(new Error(detail), { detail, status: res.status });
      }
      const reader = res.body.getReader(), dec = new TextDecoder();
      let buf = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, i).trim();
          buf = buf.slice(i + 1);
          if (line) on(JSON.parse(line));
        }
      }
      put((c) => (c.status === 'streaming' ? { ...c, status: 'ready', pending: [] } : c)); // stream closed without 'done'
    } catch (err) {
      put((c) => ({ ...c, status: 'error', error: msg(err), pending: [] }));
    }
  }, []);
  useEffect(() => { if (active && !cats[env]) loadCatalog(env); }, [active, env, cats, loadCatalog]);
  const cat = cats[env] || { status: 'loading' };
  const dbs = useMemo(() => cat.data?.databases || [], [cat.data]);
  const tableMeta = useMemo(() => {
    const m = {};
    dbs.forEach((d) => d.schemas.forEach((s) => s.tables.forEach((t) => { m[[d.name, s.name, t.name].join(SEP)] = t; })));
    return m;
  }, [dbs]);
  const allTables = useMemo(() => dbs.flatMap((d) => d.schemas.flatMap((s) => s.tables.map((t) => ({ env, database: d.name, schema: s.name, table: t.name })))), [dbs, env]);

  /* ── session memory ── */
  const [jobs, setJobs] = useState(readJobs);
  const [checked, setChecked] = useState(() => store.read(CHECKED_KEY, {}) || {});
  useEffect(() => writeJobs(jobs), [jobs]);
  useEffect(() => store.write(CHECKED_KEY, checked), [checked]);
  const jobsRef = useRef(jobs); jobsRef.current = jobs;

  /* ── workspace ── */
  const [cols, setCols] = useState({ status: 'idle' });
  const [usage, setUsage] = useState({});
  const [result, setResult] = useState(null);
  const [checking, setChecking] = useState(false);
  const [diag, setDiag] = useState(null);
  const [runErr, setRunErr] = useState('');
  const [picked, setPicked] = useState(new Set());
  const [keyCol, setKeyCol] = useState('');
  const [rtab, setRtab] = useState(null);
  const [reload, setReload] = useState(0);
  const req = useRef(0); // only the latest table request may write state
  const stepRef = useRef(urlStep); stepRef.current = urlStep;
  const targetRef = useRef(target); targetRef.current = target;

  /* ── chrome ── */
  const [layer, setLayer] = useState(null); // palette | confirm | cancel
  const [drawer, setDrawer] = useState(false);
  const [open, setOpen] = useState({});
  const [toasts, setToasts] = useState([]);
  const [starting, setStarting] = useState(false);
  const [trayOpen, setTrayOpen] = useState(false);

  const job = target ? [...jobs].reverse().find((j) => keyOf(j.target) === keyOf(target)) : null;
  const busy = (k) => jobs.some((j) => !j.finished && keyOf(j.target) === k);

  const toast = useCallback((text, kind = 'ok', action) => {
    const id = Math.random();
    setToasts((t) => [...t.slice(-2), { id, text, kind, action }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), action ? 6500 : 4200);
  }, []);

  const remember = useCallback((t, r) => {
    setChecked((c) => ({ ...c, [keyOf(t)]: { target: t, score: scoreOf(r.summary), why: whyOf(r), at: Date.now() } }));
  }, []);

  const applyResult = useCallback((t, r) => {
    setResult(r);
    setCols((c) => (c.status === 'ready' ? { ...c, info: r.table } : c));
    // the key is never pre-ticked: adding a primary key is a choice, not a default; same for drops
    setPicked(new Set(r.missing.filter((m) => m.buildable && m.priority !== 'info' && m.feature !== 'key').map((m) => m.id)));
    setKeyCol(r.missing.find((m) => m.feature === 'key')?.candidates?.[0] || '');
    remember(t, r);
  }, [remember]);

  // open the tree to the selected table
  useEffect(() => {
    if (target) setOpen((o) => ({ ...o, [target.database]: true, [`${target.database}${SEP}${target.schema}`]: true }));
  }, [target]);

  // load the table whenever the URL points at a new one (also back/forward)
  useEffect(() => {
    if (!target) return;
    const t = target, my = ++req.current;
    setResult(null); setRunErr(''); setDiag(null); setRtab(null); setPicked(new Set()); setChecking(false);
    setCols({ status: 'loading' });
    (async () => {
      let r;
      try {
        r = await apiJson(`/index-health/columns?${qs({ env: t.env, database: t.database, schema: t.schema, table: t.table })}`);
      } catch (e) {
        if (my === req.current) setCols({ status: 'error', error: msg(e), code: e?.status });
        return;
      }
      if (my !== req.current) return;
      setCols({ status: 'ready', columns: r.columns, info: r.table });
      const j = [...jobsRef.current].reverse().find((x) => keyOf(x.target) === keyOf(t));
      const u = j ? j.usage : Object.fromEntries(r.columns.map((c) => [c.name, c.suggested]));
      setUsage(u);
      const want = stepRef.current;
      if (want === 'build' && !j) { nav({ step: null }, true); return; }
      if (want !== 'results' && want !== 'build') return;
      // deep link straight to a diagnosis or a build: re-check quietly
      setChecking(true);
      try {
        const res = await fetchCheck(t, want === 'build' && j ? j.checked : usageLists(u));
        if (my === req.current) applyResult(t, res);
      } catch (e) {
        if (my === req.current) { setRunErr(msg(e)); if (want === 'results') nav({ step: null }, true); }
      } finally {
        if (my === req.current) setChecking(false);
      }
    })();
  }, [target, reload, nav, applyResult]);

  let step = 'usage';
  if (diag != null) step = 'diagnosing';
  else if (urlStep === 'results' && (result || checking)) step = result ? 'results' : 'checking';
  else if (urlStep === 'build' && job) step = 'build';

  const runDiagnosis = async () => {
    const t = target, my = ++req.current;
    setRunErr(''); setDiag(0);
    try {
      // the floor keeps the checklist readable on fast tables
      const [r] = await Promise.all([fetchCheck(t, usageLists(usage)), sleep(1700)]);
      if (my !== req.current) return;
      setDiag(DIAG.length);
      await sleep(320);
      if (my !== req.current) return;
      applyResult(t, r);
      setRtab(null);
      setDiag(null);
      nav({ step: 'results' });
    } catch (e) {
      if (my !== req.current) return;
      setDiag(null);
      setRunErr(msg(e));
    }
  };

  useEffect(() => {
    if (diag == null || diag >= DIAG.length - 1) return undefined;
    const id = setTimeout(() => setDiag((d) => (d == null ? d : Math.min(d + 1, DIAG.length - 1))), 380);
    return () => clearTimeout(id);
  }, [diag]);

  /* ── navigation ── */
  const selectTable = useCallback((t, step = null) => {
    setLayer(null); setDrawer(false);
    nav({ env: t.env, db: t.database, schema: t.schema, table: t.table, step });
    document.querySelector('.ih-main')?.scrollTo?.({ top: 0 });
  }, [nav]);
  const goHome = () => { setDrawer(false); nav({ db: null, schema: null, table: null, step: null }); };
  const setEnv = useCallback((e) => { if (e !== env) nav({ env: e, db: null, schema: null, table: null, step: null }); }, [env, nav]);
  const setStep = useCallback((s) => nav({ step: s === 'usage' ? null : s }), [nav]);
  const openPalette = useCallback(() => setLayer('palette'), []);
  const reloadCatalog = useCallback(() => loadCatalog(env), [loadCatalog, env]);

  /* ── build ── */
  const findings = result ? [...result.missing, ...(result.cleanup || [])] : [];
  const sqlFor = useCallback((m) => (m.feature === 'key' && keyCol ? m.sql.replaceAll('<id_column>', `"${keyCol.replaceAll('"', '""')}"`) : m.sql), [keyCol]);
  const pickedItems = findings.filter((m) => picked.has(m.id));

  const startBuild = async () => {
    const t = target, r = result;
    setStarting(true); setRunErr('');
    try {
      const res = await apiJson('/index-health/build', {
        method: 'POST',
        body: {
          env: t.env, database: t.database, schema_name: t.schema, table: t.table,
          items: pickedItems.map((m) => m.id), search: r.checked.search, filter: r.checked.filter, vector: r.checked.vector,
          key_column: picked.has('key') ? keyCol : undefined,
        },
      });
      const byId = Object.fromEntries(findings.map((m) => [m.id, m]));
      const items = res.items.map((id) => ({
        id, feature: byId[id]?.feature, column: id === 'key' ? keyCol : byId[id]?.column, name: byId[id] ? titleOf(byId[id]) : id,
      }));
      setJobs((js) => [...js, {
        id: res.id, target: t, usage, checked: r.checked, items, status: res.status, log: res.log || [], next: res.next || 0,
        loaded: true, finished: null, t0: Date.now(), before: scoreOf(r.summary),
      }]);
      setLayer(null);
      nav({ step: 'build' });
      toast('Build started on the server. Keep working, you will get a note when it finishes.');
      if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission().catch(() => {});
    } catch (e) {
      setLayer(null);
      toast(msg(e), 'bad');
      setRunErr(msg(e));
    } finally {
      setStarting(false);
    }
  };

  const [cancelling, setCancelling] = useState(null);
  const cancelJob = async (j) => {
    setLayer(null);
    if (!j || j.finished) return;
    setCancelling(j.id);
    try {
      await apiJson(`/index-health/jobs/${j.id}/cancel`, { method: 'POST' });
      setJobs((js) => js.map((x) => (x.id === j.id ? { ...x, cancelRequested: true } : x)));
      toast(`Cancelling ${j.target.table} after the current statement`, 'warn');
    } catch (e) { toast(msg(e), 'bad'); } finally { setCancelling(null); }
  };

  // Re-score with the usage the build was started with; refresh in place if the user is looking at it.
  const onJobFinished = useCallback(async (j) => {
    const label = `${j.target.schema}.${j.target.table}`;
    let after = null, r = null;
    try { r = await fetchCheck(j.target, j.checked); after = scoreOf(r.summary); } catch { /* the toast still goes out */ }
    setJobs((js) => js.map((x) => (x.id === j.id ? { ...x, after } : x)));
    if (r) remember(j.target, r);
    const text = j.status === 'done' ? `${label}: health ${j.before ?? '—'} → ${after ?? '—'}` : `${label}: build ${j.status}`;
    if (document.hidden && 'Notification' in window && Notification.permission === 'granted') new Notification('Index Health', { body: text });
    const here = targetRef.current && keyOf(targetRef.current) === keyOf(j.target);
    if (here && r) applyResult(j.target, r);
    toast(text, j.status === 'done' ? 'ok' : 'bad', here ? null : { label: 'View', run: () => selectTable(j.target, 'build') });
  }, [applyResult, remember, selectTable, toast]);

  // one poller for every job; a job that just finished triggers the re-score once
  useEffect(() => {
    let stop = false, timer;
    const tick = async () => {
      const due = jobsRef.current.filter((j) => !j.finished || !j.loaded);
      await Promise.all(due.map(async (j) => {
        try {
          const v = await apiJson(`/index-health/jobs/${j.id}?since=${j.next}`);
          if (stop) return;
          const ended = v.finished && !j.finished;
          if (j.loaded && !v.log.length && v.status === j.status && !ended) return; // nothing new, skip the re-render
          const upd = { status: v.status, finished: v.finished, loaded: true, next: v.next, log: [...j.log, ...v.log], endedAt: j.endedAt || (v.finished ? Date.now() : undefined) };
          setJobs((js) => js.map((x) => (x.id === j.id ? { ...x, ...upd } : x)));
          if (ended) onJobFinished({ ...j, ...upd });
        } catch (e) {
          if (stop) return;
          // 404: server restarted or the job aged out; other 4xx won't get better by retrying
          if (e?.status >= 400 && e?.status < 500) {
            setJobs((js) => js.map((x) => (x.id === j.id ? {
              ...x, loaded: true, finished: x.finished || 'lost', status: x.finished ? x.status : 'lost',
              log: [...x.log, { ts: '', level: 'warn', msg: msg(e) }],
            } : x)));
          }
        }
      }));
      if (!stop) timer = setTimeout(tick, 1500);
    };
    tick();
    return () => { stop = true; clearTimeout(timer); };
  }, [onJobFinished]);

  /* ── keyboard ── */
  useEffect(() => {
    if (!active) return undefined;
    const onKey = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); setLayer('palette'); }
      if (e.key === 'Escape') { setLayer(null); setDrawer(false); }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [active]);

  /* ── derived ── */
  // memoised so opening a dialog or a poll tick doesn't re-render the landing page or the tree
  const envChecked = useMemo(() => Object.values(checked).filter((c) => c.target.env === env).sort((a, b) => a.score - b.score), [checked, env]);
  const builtCount = useMemo(() => jobs.filter((j) => j.target.env === env && j.finished && j.log.length).reduce((a, j) => a + jobProgress(j).done, 0), [jobs, env]);
  const liveKeys = useMemo(() => new Set(jobs.filter((j) => !j.finished).map((j) => keyOf(j.target))), [jobs]);
  const statusOf = useCallback((k) => (liveKeys.has(k) ? 'busy' : checked[k] ? checked[k].score : null), [liveKeys, checked]);

  const ctx = { env, setEnv, cat, dbs, tableMeta, allTables, checked, envChecked, statusOf, open, setOpen, selectTable, openPalette, reloadCatalog, target };

  // what the streamed catalog already knows about this table; shown before /columns answers
  const tMeta = target ? tableMeta[[target.database, target.schema, target.table].join(SEP)] : null;
  const knownScore = (step === 'results' || step === 'build') && result ? scoreOf(result.summary) : target && checked[keyOf(target)] ? checked[keyOf(target)].score : null;

  return (
    <div className={`ih ih-scope ih-t-${tier} ${target ? 'ws' : ''}`}>
      <TipLayer />
      {target && (
        <>
          <div className={`ih-drawer-scrim ${drawer ? 'on' : ''}`} onClick={() => setDrawer(false)} aria-hidden="true" />
          <Sidebar {...ctx} className={drawer ? 'open' : ''} onClose={() => setDrawer(false)} />
        </>
      )}

      <main className="ih-main">
        <div className="ih-halo" aria-hidden="true" />
        {!target ? (
          <Landing {...ctx} builtCount={builtCount} active={active} tier={tier} />
        ) : (
          <div className="ih-ws">
            <WsHeader target={target} cols={cols} meta={tMeta} result={result} onHome={goHome} onBrowse={() => setDrawer(true)} />
            <Steps step={step} result={result} job={job} setStep={setStep} busy={step === 'diagnosing' || cols.status !== 'ready'} />
            {runErr && step !== 'diagnosing' && (
              <div className="ih-banner bad" role="alert"><I n="alert" /><span>{runErr}</span><button onClick={() => setRunErr('')} aria-label="Dismiss" data-tip="Dismiss"><I n="x" s={14} /></button></div>
            )}
            <div className="ih-cols">
              <div className="ih-panel">
                {cols.status === 'loading' && <UsageSkeleton rows={tMeta?.columns} />}
                {cols.status === 'error' && (
                  <ErrorCard title={cols.code === 404 ? 'Table not found' : 'Could not read this table'} text={cols.error}
                    onRetry={cols.code === 404 ? null : () => setReload((n) => n + 1)} onHome={goHome} />
                )}
                {cols.status === 'ready' && step === 'usage' && <UsagePane columns={cols.columns} usage={usage} setUsage={setUsage} />}
                {step === 'diagnosing' && <DiagPane table={target.table} at={diag} />}
                {cols.status === 'ready' && step === 'checking' && <ResultsSkeleton />}
                {cols.status === 'ready' && step === 'results' && result && (
                  <ResultsPane result={result} picked={picked} setPicked={setPicked} keyCol={keyCol} setKeyCol={setKeyCol} sqlFor={sqlFor}
                    rtab={rtab} setRtab={setRtab} canBuild={canBuild} />
                )}
                {step === 'build' && job && <BuildPane job={job} />}
              </div>
              <Rail step={step} target={target} cols={cols} result={result} job={job} usage={usage} picked={picked} pickedItems={pickedItems}
                keyCol={keyCol} canBuild={canBuild} building={busy(keyOf(target))} score={knownScore} checkedBefore={checked[keyOf(target)]}
                cancelling={cancelling === job?.id}
                onRun={runDiagnosis} onBuild={() => setLayer('confirm')} onCancel={() => setLayer('cancel')} setStep={setStep}
                onPalette={() => setLayer('palette')} />
            </div>
          </div>
        )}
      </main>

      {layer === 'palette' && <Palette env={env} cat={cat} all={allTables} statusOf={statusOf} onSelect={selectTable} onClose={() => setLayer(null)} onRetry={() => loadCatalog(env)} />}
      {layer === 'confirm' && result && (
        <ConfirmBuild target={target} items={pickedItems} sqlFor={sqlFor} starting={starting} onStart={startBuild} onClose={() => setLayer(null)} />
      )}
      {layer === 'cancel' && job && (
        <Confirm title={`Cancel the build on ${job.target.table}?`} text="The statement running now is stopped and its index is left invalid. The next diagnosis offers to drop it. Finished items stay."
          yes="Cancel build" no="Keep building" onYes={() => cancelJob(job)} onClose={() => setLayer(null)} />
      )}
      <Tray jobs={jobs} here={target ? keyOf(target) : ''} open={trayOpen} setOpen={setTrayOpen} onView={(t) => selectTable(t, 'build')} />
      <Layer>
        <div className="ih-toasts" aria-live="polite">
          {toasts.map((t) => (
            <div key={t.id} className={`ih-toast ${t.kind}`}>
              <span className="ih-toast-i"><I n={t.kind === 'ok' ? 'check' : 'alert'} s={12} w={2.6} /></span><span>{t.text}</span>
              {t.action && <button onClick={() => { t.action.run(); setToasts((x) => x.filter((y) => y.id !== t.id)); }}>{t.action.label}</button>}
            </div>
          ))}
        </div>
      </Layer>
    </div>
  );
}

/* ─────────────────────────────── shared bits ─────────────────────────────── */

function EnvSwitch({ env, setEnv, wide }) {
  const ei = ENVS.indexOf(env);
  return (
    <div className={`ih-envs ${env === 'prod' ? 'prod' : ''} ${wide ? 'wide' : ''}`} role="radiogroup" aria-label="Environment">
      <span className="ih-envs-pill" style={{ left: `calc(4px + ${ei} * (100% - 8px) / 3)` }} />
      {ENVS.map((e) => (
        <button key={e} role="radio" aria-checked={env === e} className={env === e ? 'on' : ''} onClick={() => setEnv(e)}
          data-tip={e === 'prod' ? 'Production cluster. Builds go live.' : `${e.toUpperCase()} cluster`}>{e}</button>
      ))}
    </div>
  );
}

function ClusterDot({ cat }) {
  const hidden = cat.data?.hidden || 0;
  const st = cat.status === 'loading' || cat.status === 'streaming' ? 'wait' : cat.status === 'error' ? 'bad' : 'ok';
  const tip = cat.status === 'loading' ? 'Connecting…' : cat.status === 'streaming' ? `Reading ${plural(cat.pending.length, 'database')}…`
    : st === 'bad' ? `Cannot reach the cluster: ${cat.error}`
      : `Connected${hidden ? `. ${plural(hidden, 'database')} hidden: this account can't read any tables there` : ''}`;
  return <span className={`ih-dot ${st}`} data-tip={tip} role="img" aria-label={tip}><span /></span>;
}

const Kbd = ({ children, c = '' }) => <kbd className={`ih-kbd ${c}`}>{children}</kbd>;

const dotStyle = (s) => (s === 'busy' ? null : s == null ? { background: 'transparent', borderColor: 'rgba(110,98,89,.45)' } : { background: meta(s)[1], borderColor: 'transparent' });
const StatusDot = ({ s }) => (s === 'busy'
  ? <Spin c="xs" />
  : <span className="ih-sdot" style={dotStyle(s)} data-tip={s == null ? 'Not checked yet' : `Health ${s}: ${meta(s)[0]}`} />);

function ErrorCard({ title, text, onRetry, onHome }) {
  return (
    <div className="ih-card ih-errcard" role="alert">
      <span className="ih-errcard-i"><I n="alert" s={22} /></span>
      <strong>{title}</strong>
      <span>{text}</span>
      <div className="ih-row-gap">
        {onRetry && <button className="ih-btn" onClick={onRetry}><I n="refresh" s={15} />Try again</button>}
        {onHome && <button className="ih-btn ghost" onClick={onHome}><I n="left" s={15} />Back to overview</button>}
      </div>
    </div>
  );
}

/* ─────────────────────────────── sidebar ─────────────────────────────── */

const Sidebar = React.memo(function Sidebar({ env, setEnv, cat, dbs, open, setOpen, target, statusOf, selectTable, openPalette, reloadCatalog, className, onClose }) {
  const total = cat.data?.tables;
  const streaming = cat.status === 'streaming';
  const toggle = (k) => setOpen((o) => ({ ...o, [k]: !o[k] }));
  const selRef = useRef(null);
  useEffect(() => { selRef.current?.scrollIntoView?.({ block: 'nearest' }); }, [target, cat.status]);

  return (
    <aside className={`ih-side ${className}`} aria-label="Tables">
      <div className="ih-brand">
        <img src="/sh_logo.png" alt="SmartHub" />
        <span className="ih-brand-sep" />
        <div><small>Data Studio</small><b>Index Health</b></div>
        <button className="ih-iconbtn ih-only-drawer" onClick={onClose} aria-label="Close" data-tip="Close"><I n="x" /></button>
      </div>

      <div className="ih-side-env">
        <EnvSwitch env={env} setEnv={setEnv} />
        {env === 'prod' && <div className="ih-prodnote"><I n="shield" s={14} />Production cluster. Builds go live.</div>}
      </div>

      <button className="ih-findbtn" onClick={openPalette} data-tip="Search every table on this cluster">
        <I n="search" /><span>Find a table</span><Kbd>{KBD}</Kbd>
      </button>

      <div className="ih-side-head"><span>Databases</span><span>{total == null || (streaming && !total) ? (cat.status === 'error' ? '' : 'loading…') : `${plural(total, 'table')}${streaming ? '…' : ''}`}</span></div>

      <nav className="ih-tree">
        {cat.status === 'loading' && !cat.data && Array.from({ length: 6 }, (_, i) => <div key={i} className="ih-skel row" style={{ '--w': `${60 + (i * 13) % 35}%` }} />)}
        {cat.status === 'error' && !dbs.length && (
          <div className="ih-tree-err"><I n="alert" s={15} /><span>{cat.error}</span><button className="ih-btn sm" onClick={reloadCatalog}>Retry</button></div>
        )}
        {dbs.map((d) => {
          const dOpen = !!open[d.name];
          return (
            <React.Fragment key={d.name}>
              <button className="ih-node db" onClick={() => toggle(d.name)} aria-expanded={dOpen} data-tip={d.error ? `Could not read ${d.name}: ${d.error}` : undefined}>
                <span className={`ih-chev ${dOpen ? 'open' : ''}`}><I n="chev" s={14} w={2.2} /></span>
                <span className="ih-c-blue"><I n="db" s={15} /></span>
                <span className="ih-node-l">{d.name}</span>
                {d.error ? <span className="ih-c-red"><I n="alert" s={14} /></span> : <span className="ih-node-n">{d.tables}</span>}
              </button>
              {dOpen && d.error && <div className="ih-none">Not readable</div>}
              {dOpen && !d.error && !d.schemas.length && <div className="ih-none">No tables</div>}
              {dOpen && d.schemas.map((s) => {
                const sk = `${d.name}${SEP}${s.name}`, sOpen = !!open[sk];
                return (
                  <React.Fragment key={sk}>
                    <button className="ih-node schema" onClick={() => toggle(sk)} aria-expanded={sOpen}>
                      <span className={`ih-chev sm ${sOpen ? 'open' : ''}`}><I n="chev" s={12} w={2.2} /></span>
                      <span className="ih-c-purple"><I n="schema" s={14} /></span>
                      <span className="ih-node-l">{s.name}</span>
                      <span className="ih-node-n">{s.tables.length}</span>
                    </button>
                    {sOpen && s.tables.map((t) => {
                      const tg = { env, database: d.name, schema: s.name, table: t.name }, k = keyOf(tg);
                      const sel = target && keyOf(target) === k, st = statusOf(k);
                      return (
                        <button key={t.name} ref={sel ? selRef : undefined} className={`ih-node tbl ${sel ? 'sel' : ''}`} onClick={() => selectTable(tg)}
                          aria-current={sel ? 'true' : undefined} data-tip={t.name.length > 26 ? t.name : undefined}>
                          <StatusDot s={st} />
                          <span className="ih-node-l mono">{t.name}</span>
                          <span className="ih-node-n">{typeof st === 'number' ? st : ''}</span>
                        </button>
                      );
                    })}
                  </React.Fragment>
                );
              })}
            </React.Fragment>
          );
        })}
        {cat.pending?.map((n) => (
          <div key={n} className="ih-node db pending" data-tip={`Reading ${n}…`}>
            <span className="ih-chev"><Spin c="xs" /></span><span className="ih-c-blue"><I n="db" s={15} /></span>
            <span className="ih-node-l">{n}</span><span className="ih-skel line" style={{ width: 22 }} />
          </div>
        ))}
        {cat.status === 'ready' && !dbs.length && <div className="ih-none">This account can't read any tables on {env}.</div>}
      </nav>

      <div className="ih-cluster">
        <img src="/icons/postgres_badge.svg" alt="" />
        <div><b>PostgreSQL</b><small>{env} cluster</small></div>
        <ClusterDot cat={cat} />
        <button className="ih-iconbtn" onClick={reloadCatalog} aria-label="Reload table list" data-tip="Reload table list" disabled={cat.status === 'loading' || streaming}>
          <I n="refresh" s={14} c={cat.status === 'loading' || streaming ? 'ih-rot' : ''} />
        </button>
      </div>
    </aside>
  );
});

/* ─────────────────────────────── landing ─────────────────────────────── */

const Landing = React.memo(function Landing({ env, setEnv, cat, dbs, tableMeta, checked, envChecked, statusOf, selectTable, openPalette, reloadCatalog, builtCount, active, tier }) {
  const total = cat.data?.tables;
  const avg = envChecked.length ? Math.round(envChecked.reduce((a, c) => a + c.score, 0) / envChecked.length) : null;
  const [heroLabel, heroColor] = meta(avg);
  const worst = envChecked[0];
  const nodes = useMemo(() => envChecked.map((c) => ({ table: c.target.table, score: c.score })), [envChecked]);

  return (
    <div className="ih-land">
      <header className="ih-land-top ih-fade">
        <div className="ih-crumb"><span>Data Studio</span><span className="o">/</span><b>Index Health</b></div>
        <div className="ih-pillbox">
          <ClusterDot cat={cat} />
          <span>{env.toUpperCase()} · {!cat.data ? (cat.status === 'error' ? 'unreachable' : 'connecting…') : cat.status === 'streaming' ? `${fmt(total)} tables so far…` : cat.status === 'error' && !dbs.length ? 'unreachable' : `${fmt(total)} tables you can read`}</span>
        </div>
      </header>

      <section className="ih-hero">
        <div className="ih-hero-copy">
          <div className="ih-eyepill ih-rise" style={{ '--d': '.05s' }}><span className="ih-reddot" />Index Health · {env.toUpperCase()}</div>
          <h1 className="ih-rise" style={{ '--d': '.14s' }}>Fast queries start with the <em>right</em> index.</h1>
          <p className="ih-rise" style={{ '--d': '.24s' }}>Pick a table, tell us how its columns are queried, and see exactly which indexes are missing. Then build them without locking the table.</p>
          <div className="ih-hero-cta ih-rise" style={{ '--d': '.34s' }}>
            <button className="ih-btn primary lg" onClick={openPalette}>Find a table<Kbd c="inv">{KBD}</Kbd></button>
            {worst && (
              <button className="ih-btn lg" onClick={() => selectTable(worst.target)} data-tip={`Lowest health this session: ${worst.score}`}>
                Review <code className="ih-c-blue">{worst.target.table}</code><I n="right" s={17} />
              </button>
            )}
          </div>
          <div className="ih-stats ih-rise" style={{ '--d': '.44s' }}>
            <div data-tip="Tables you diagnosed in this browser tab"><b>{envChecked.length}</b><small>Checked this session</small></div>
            <i />
            <div data-tip="Health under 55"><b className="ih-c-red">{envChecked.filter((c) => c.score < 55).length}</b><small>Need attention</small></div>
            <i />
            <div data-tip="Fixes completed by builds this session"><b>{builtCount}</b><small>Fixes applied</small></div>
          </div>
        </div>
        <div className="ih-hero-viz ih-fade" style={{ '--d': '.2s' }}>
          <div className="ih-radar-wrap">
            <HeroRadar nodes={nodes} active={active} tier={tier} />
            <div className="ih-radar-mid">
              <small>Session health</small>
              <b><TweenNum value={avg} /></b>
              <span style={{ color: heroColor }}>{heroLabel}</span>
            </div>
          </div>
          <div className="ih-pillbox"><span className="ih-reddot" />{total == null || cat.status === 'streaming' ? `${envChecked.length} checked on ${env}` : `${envChecked.length} of ${fmt(total)} tables checked on ${env}`}</div>
        </div>
      </section>

      <Picker env={env} setEnv={setEnv} cat={cat} dbs={dbs} tableMeta={tableMeta} checked={checked} statusOf={statusOf} selectTable={selectTable} openPalette={openPalette} reloadCatalog={reloadCatalog} />

      <section className="ih-sec">
        <span className="ih-eyebrow">How it works</span>
        <div className="ih-how">
          <div className="ih-howcard ih-rise" style={{ '--d': '.5s' }}>
            <div className="ih-row-gap sm"><span className="ih-tag blue">Lookup</span><span className="ih-tag purple">Text search</span><span className="ih-tag red">Vector</span></div>
            <b className="ih-num">01</b><strong>Describe usage</strong><span>Mark each column as lookup, text search or vector search.</span>
          </div>
          <div className="ih-howcard ih-rise" style={{ '--d': '.58s' }}>
            <div className="ih-bars"><i style={{ width: '86%', background: RED, opacity: 0.85 }} /><i style={{ width: '58%', background: ORANGE, opacity: 0.7 }} /><i style={{ width: '34%', background: AMBER, opacity: 0.55 }} /></div>
            <b className="ih-num">02</b><strong>Get a diagnosis</strong><span>See the gaps, ranked by impact, with the exact SQL.</span>
          </div>
          <div className="ih-howcard ih-rise" style={{ '--d': '.66s' }}>
            <div className="ih-miniterm"><span className="ih-c-green">▸</span>CREATE INDEX CONCURRENTLY<span className="ih-caret" /></div>
            <b className="ih-num">03</b><strong>Build live</strong><span>Run fixes concurrently and watch the log as it happens.</span>
          </div>
        </div>
      </section>

      {envChecked.length > 0 && (
        <section className="ih-sec">
          <div className="ih-sec-head"><span className="ih-eyebrow">Checked this session</span><small>Worst first</small></div>
          <div className="ih-checked">
            {envChecked.map((c, i) => (
              <button key={keyOf(c.target)} className="ih-cc ih-rise" style={{ '--d': `${0.04 * i + 0.1}s` }} onClick={() => selectTable(c.target)}>
                <MiniRing score={c.score} size={46} />
                <span className="ih-cc-t"><code>{c.target.table}</code><small>{c.target.database} · {c.target.schema}</small><span style={{ color: meta(c.score)[1] }}>{c.why}</span></span>
                <I n="right" s={18} c="ih-c-muted" />
              </button>
            ))}
          </div>
        </section>
      )}
    </div>
  );
});

const PICK_MAX = 300;

const Picker = React.memo(function Picker({ env, setEnv, cat, dbs, tableMeta, checked, statusOf, selectTable, openPalette, reloadCatalog }) {
  const [pickDb, setPickDb] = useState(null);
  const [pickSchema, setPickSchema] = useState(null);
  const [f, setF] = useState('');
  useEffect(() => { setPickDb(null); setPickSchema(null); setF(''); }, [env]);

  const readable = dbs.filter((d) => !d.error && d.schemas.length);
  const pDb = dbs.find((d) => d.name === pickDb) || readable[0] || dbs[0];
  const pS = pDb?.schemas.find((s) => s.name === pickSchema) || pDb?.schemas.find((s) => s.name === 'public') || pDb?.schemas[0];
  const worstIn = (db, sc) => {
    const xs = Object.values(checked).filter((c) => c.target.env === env && c.target.database === db && (!sc || c.target.schema === sc));
    return xs.length ? Math.min(...xs.map((c) => c.score)) : null;
  };
  const needle = f.trim().toLowerCase();
  const tables = (pS?.tables || []).filter((t) => !needle || t.name.toLowerCase().includes(needle));
  const loading = !cat.data && cat.status === 'loading';
  const pending = cat.pending || [];

  return (
    <section className="ih-sec ih-rise" style={{ '--d': '.45s' }}>
      <div className="ih-sec-head">
        <div className="ih-col-gap"><span className="ih-eyebrow blue">Choose a table</span><strong className="ih-h2">Where should we look?</strong></div>
        <div className="ih-row-gap">
          <EnvSwitch env={env} setEnv={setEnv} wide />
          <button className="ih-btn" onClick={openPalette} data-tip="Search every table on this cluster"><I n="search" />Search all<Kbd>{KBD}</Kbd></button>
        </div>
      </div>

      {cat.status === 'error' && !dbs.length ? (
        <ErrorCard title={`Could not reach the ${env.toUpperCase()} cluster`} text={cat.error} onRetry={reloadCatalog} />
      ) : (
        <div className="ih-picker">
          <div className="ih-pcol">
            <div className="ih-pcol-h"><span>Database</span><span>{loading ? '' : pending.length ? `${dbs.length} of ${dbs.length + pending.length}` : dbs.length}</span></div>
            <div className="ih-pcol-b">
              {loading && Array.from({ length: 3 }, (_, i) => <div key={i} className="ih-skel card" />)}
              {dbs.map((d) => {
                const on = d === pDb, w = worstIn(d.name);
                return (
                  <button key={d.name} className={`ih-pick ${on ? 'on' : ''}`} onClick={() => { setPickDb(d.name); setPickSchema(null); setF(''); }} disabled={!!d.error}
                    data-tip={d.error ? `Could not read ${d.name}: ${d.error}` : undefined} aria-pressed={on}>
                    <span className="ih-pick-i blue"><I n="db" s={18} /></span>
                    <span className="ih-pick-t"><b className="disp">{d.name}</b><small>{d.error ? 'Not readable' : `${plural(d.schemas.length, 'schema')} · ${plural(d.tables, 'table')}`}</small></span>
                    {d.error ? <span className="ih-c-red"><I n="alert" s={16} /></span>
                      : <span className="ih-sdot lg" style={{ background: w == null ? 'rgba(110,98,89,.2)' : meta(w)[1] }} data-tip={w == null ? 'Nothing checked here yet' : `Lowest health here: ${w}`} />}
                    <span className="ih-pick-go"><I n="chev" s={16} w={2.2} /></span>
                  </button>
                );
              })}
              {pending.map((n) => (
                <div key={n} className="ih-pick pending" data-tip={`Reading ${n}…`}>
                  <span className="ih-pick-i blue"><Spin /></span>
                  <span className="ih-pick-t"><b className="disp">{n}</b><span className="ih-skel line sm" style={{ width: 110 }} /></span>
                </div>
              ))}
              {cat.status === 'ready' && !dbs.length && <div className="ih-none">This account can't read any tables on {env.toUpperCase()}.</div>}
            </div>
          </div>

          <div className="ih-pcol tint">
            <div className="ih-pcol-h"><span>Schema</span><span>{pDb?.schemas.length ?? ''}</span></div>
            <div className="ih-pcol-b">
              {(loading || (!pDb && pending.length > 0)) && Array.from({ length: 2 }, (_, i) => <div key={i} className="ih-skel card" />)}
              {pDb?.schemas.map((s, i) => {
                const on = s === pS, w = worstIn(pDb.name, s.name);
                return (
                  <button key={s.name} className={`ih-pick ${on ? 'on white' : ''} ih-rise`} style={{ '--d': `${0.04 * Math.min(i, 10)}s` }} onClick={() => { setPickSchema(s.name); setF(''); }} aria-pressed={on}>
                    <span className="ih-pick-i purple"><I n="schema" s={18} /></span>
                    <span className="ih-pick-t"><b>{s.name}</b><small>{plural(s.tables.length, 'table')}{w == null ? '' : ` · lowest health ${w}`}</small></span>
                    <span className="ih-pick-go"><I n="chev" s={16} w={2.2} /></span>
                  </button>
                );
              })}
              {pDb && !pDb.error && !pDb.schemas.length && <div className="ih-none">No tables in this database</div>}
            </div>
          </div>

          <div className="ih-pcol wide">
            <div className="ih-pcol-h">
              <span>Tables in <code>{pDb && pS ? `${pDb.name}.${pS.name}` : '…'}</code></span>
              <span>{pS ? plural(tables.length, 'table') : ''}</span>
            </div>
            {(pS?.tables.length || 0) > 8 && (
              <label className="ih-filter"><I n="search" s={14} /><input value={f} onChange={(e) => setF(e.target.value)} placeholder={`Filter ${pS.tables.length} tables`} aria-label="Filter tables" /></label>
            )}
            <div className="ih-pcol-b tables">
              {(loading || (!pDb && pending.length > 0)) && Array.from({ length: 4 }, (_, i) => <div key={i} className="ih-skel tcard" />)}
              {tables.slice(0, PICK_MAX).map((t, i) => {
                const tg = { env, database: pDb.name, schema: pS.name, table: t.name }, k = keyOf(tg), st = statusOf(k);
                const ck = checked[k], score = typeof st === 'number' ? st : null, [label, color] = meta(score);
                const m = tableMeta[[pDb.name, pS.name, t.name].join(SEP)] || t;
                return (
                  <button key={t.name} className="ih-tcard ih-rise" style={{ '--d': `${0.04 * Math.min(i, 12)}s` }} onClick={() => selectTable(tg)}>
                    {st === 'busy' ? <span className="ih-mini" style={{ width: 42, height: 42 }}><Spin /></span> : <MiniRing score={score} />}
                    <span className="ih-tcard-t">
                      <span className="mono ih-ell" data-tip={t.name.length > 34 ? t.name : undefined}>{t.name}</span>
                      <small data-tip="Estimates from pg_class, as of the last VACUUM or ANALYZE">
                        {m.rows == null ? 'never analyzed' : `~${compact(m.rows)} rows`} · {m.size} · {plural(m.columns, 'column')} · {plural(m.indexes, 'index', 'indexes')}
                        {m.kind === 'p' ? ' · partitioned' : m.kind === 'm' ? ' · materialized view' : ''}
                      </small>
                    </span>
                    <span className="ih-tcard-r">
                      <span style={{ color: st === 'busy' ? BLUE : color }}>{st === 'busy' ? 'Building' : label}</span>
                      <small>{st === 'busy' ? 'Build running' : ck ? ck.why : 'Open to diagnose'}</small>
                    </span>
                    <span className="ih-c-red"><I n="right" s={18} /></span>
                  </button>
                );
              })}
              {tables.length > PICK_MAX && <div className="ih-none">Showing {PICK_MAX} of {fmt(tables.length)}. Filter or use Search all to find the rest.</div>}
              {pS && !tables.length && <div className="ih-none">{needle ? `No tables match "${f}"` : 'No tables'}</div>}
            </div>
          </div>
        </div>
      )}
    </section>
  );
});

/* ─────────────────────────────── workspace header + steps ─────────────────────────────── */

function WsHeader({ target, cols, meta, result, onHome, onBrowse }) {
  const early = !cols.info && meta ? { rows_estimate: meta.rows, size: meta.size, kind: meta.kind, early: true } : null;
  const info = cols.info || early;
  const prod = target.env === 'prod';
  const kind = info?.kind === 'p' ? 'Partitioned table' : info?.kind === 'm' ? 'Materialized view' : null;
  const dead = info?.live_rows ? Math.round((info.dead_rows / Math.max(1, info.live_rows + info.dead_rows)) * 100) : 0;
  return (
    <header className="ih-wsh ih-rise">
      <div className="ih-wsh-top">
        <div className="ih-wsh-crumb">
          <button className="ih-iconbtn ih-only-drawer" onClick={onBrowse} aria-label="Browse tables" data-tip="Browse tables"><I n="menu" /></button>
          <span className={`ih-envchip ${prod ? 'prod' : ''}`} data-tip={prod ? 'Production: builds change live data' : `${target.env.toUpperCase()} cluster`}>{target.env}</span>
          <span className="ih-ell">{target.database}</span><span className="o">›</span><span className="ih-ell">{target.schema}</span>
        </div>
        <button className="ih-btn pill" onClick={onHome}><I n="left" />Overview</button>
      </div>
      <h2><span>{target.schema}.</span>{target.table}</h2>
      <div className="ih-chips">
        {!info && cols.status === 'loading' && Array.from({ length: 4 }, (_, i) => <span key={i} className="ih-skel chip" />)}
        {info && (
          <>
            <span className="ih-chip" data-tip={`Estimate from pg_class.reltuples${dead ? `. ${dead}% dead rows waiting for VACUUM` : ''}`}>
              <I n="table" s={15} />{info.rows_estimate != null ? `~${compact(info.rows_estimate)} rows` : 'Row count unknown'}
            </span>
            <span className="ih-chip" data-tip="Table, TOAST and indexes"><I n="db" s={15} />{info.size}</span>
            {info.early ? <span className="ih-skel chip" /> : <span className={`ih-chip ${info.last_analyze ? '' : 'bad'}`} data-tip={info.last_analyze ? `Last ANALYZE: ${new Date(info.last_analyze).toLocaleString()}` : 'The planner is guessing row counts'}>
              <I n="refresh" s={15} />{info.last_analyze ? `Analyzed ${ago(info.last_analyze)}` : 'Never analyzed'}
            </span>}
            {(cols.columns || meta) && <span className="ih-chip"><I n="schema" s={15} />{plural(cols.columns?.length ?? meta.columns, 'column')}{result || meta ? ` · ${plural(result ? result.indexes.length : meta.indexes, 'index', 'indexes')}` : ''}</span>}
            {kind && <span className="ih-chip warn" data-tip={info.kind === 'p' ? 'Indexes are built per partition; most fixes are shown as SQL to run by hand' : 'Views cannot have a primary key'}>{kind}</span>}
          </>
        )}
      </div>
    </header>
  );
}

function Steps({ step, result, job, setStep, busy }) {
  const cur = step === 'usage' ? 0 : step === 'build' ? 2 : 1;
  const running = job && !job.finished;
  const defs = [
    ['usage', 'Describe usage', 'Mark how columns are queried', true, ''],
    ['results', 'Diagnosis', result ? `${plural(result.missing.length, 'finding')} · gaps ranked by impact` : 'Gaps ranked by impact, with SQL', !!result, 'Run a diagnosis first'],
    ['build', 'Build', running ? `${jobProgress(job).done}/${job.items.length} done · running` : job ? `Last build ${job.status}` : 'Concurrent, with a live log', !!job, 'Nothing built on this table yet'],
  ];
  return (
    <nav className="ih-steps ih-rise" style={{ '--d': '.06s' }} aria-label="Steps">
      {defs.map(([id, label, sub, enabled], i) => {
        const act = i === cur, done = i < cur, can = enabled && !act && !busy;
        return (
          <button key={id} className={`ih-step ${act ? 'on' : ''} ${done ? 'done' : ''}`} onClick={() => can && setStep(id)} aria-current={act ? 'step' : undefined}
            aria-disabled={!can && !act} data-tip={!enabled ? defs[i][4] : undefined} style={{ cursor: can ? 'pointer' : 'default', opacity: enabled || act ? 1 : 0.72 }}>
            <span className="ih-step-n">{done ? <I n="check" s={14} w={2.6} /> : `0${i + 1}`}</span>
            <span className="ih-step-t"><b>{label}{id === 'build' && running && <span className="ih-live" />}</b><small>{sub}</small></span>
            <span className="ih-step-bar" style={{ width: act ? '100%' : '0%' }} />
          </button>
        );
      })}
    </nav>
  );
}

/* ─────────────────────────────── usage ─────────────────────────────── */

function UsageSkeleton({ rows }) {
  return (
    <div className="ih-card ih-pop">
      <div className="ih-card-h"><div className="ih-col-gap"><span className="ih-skel line" style={{ width: 240 }} /><span className="ih-skel line sm" style={{ width: 320 }} /></div></div>
      {Array.from({ length: Math.min(rows || 7, 12) }, (_, i) => <div key={i} className="ih-urow skel"><span className="ih-skel line" style={{ width: `${30 + (i * 17) % 40}%` }} /><span className="ih-skel box" /><span className="ih-skel box" /><span className="ih-skel box" /></div>)}
    </div>
  );
}

const UsagePane = React.memo(function UsagePane({ columns, usage, setUsage }) {
  const toggle = (col, k) => setUsage((u) => {
    const cur = u[col] || [];
    return { ...u, [col]: cur.includes(k) ? cur.filter((x) => x !== k) : [...cur, k] };
  });
  const suggest = () => setUsage(Object.fromEntries(columns.map((c) => [c.name, c.suggested])));
  const all = () => setUsage(Object.fromEntries(columns.map((c) => [c.name, c.vector ? ['vector'] : c.text ? ['filter', 'search'] : c.lookup ? ['filter'] : []])));
  const none = () => setUsage(Object.fromEntries(columns.map((c) => [c.name, []])));
  const bulk = (use) => setUsage((u) => {
    const cs = columns.filter(use.ok);
    const on = cs.every((c) => (u[c.name] || []).includes(use.key));
    const next = { ...u };
    cs.forEach((c) => { const rest = (u[c.name] || []).filter((x) => x !== use.key); next[c.name] = on ? rest : [...rest, use.key]; });
    return next;
  });

  return (
    <div className="ih-card ih-pop">
      <div className="ih-card-h">
        <div className="ih-col-gap">
          <strong className="ih-h3">How is each column queried?</strong>
          <span className="ih-sub">Mark every way a column is filtered. Leave it empty if nothing queries it that way. Read-only: nothing changes until you build.</span>
        </div>
        <div className="ih-row-gap sm">
          <button className="ih-btn sm" onClick={suggest} data-tip="Pre-fill from the indexes already on this table"><I n="spark" s={15} />Suggest</button>
          <button className="ih-btn sm" onClick={all} data-tip="Mark every column for every way it can be queried"><I n="all" s={15} />All</button>
          <button className="ih-btn sm" onClick={none} data-tip="Clear every mark"><I n="none" s={15} />None</button>
        </div>
      </div>
      <div className="ih-uhead">
        <span>Column</span>
        {USES.map((u) => (
          <button key={u.key} style={{ color: u.color }} onClick={() => bulk(u)} data-tip={`${u.tip}. Click to toggle the whole column.`}>{u.label}</button>
        ))}
      </div>
      {!columns.length && <div className="ih-none pad">This table has no columns.</div>}
      {columns.map((c, i) => {
        const u = usage[c.name] || [];
        return (
          <div key={c.name} className={`ih-urow ih-rise ${u.length ? 'used' : ''}`} style={{ '--d': `${0.025 * Math.min(i, 20)}s` }}>
            <span className="ih-ucol">
              <span className="ih-row-gap xs"><code className="ih-cname">{c.name}</code>
                {c.suggested.length > 0 && <span className="ih-indexed" data-tip={`Already indexed for ${c.suggested.map((s) => USES.find((x) => x.key === s).label.toLowerCase()).join(' and ')}`}><i />Indexed</span>}
              </span>
              <small>{c.type}</small>
            </span>
            {USES.map((x) => (
              <span key={x.key} className="ih-ucell">
                {x.ok(c) ? (
                  <button className={`ih-tick ${u.includes(x.key) ? 'on' : ''}`} style={{ '--c': x.color }} onClick={() => toggle(c.name, x.key)}
                    aria-pressed={u.includes(x.key)} aria-label={`${c.name}: ${x.label}`} data-tip={`${x.tip}${c.suggested.includes(x.key) ? ' (already indexed)' : ''}`}>
                    <I n="check" s={16} w={2.4} />
                    {c.suggested.includes(x.key) && <span className="ih-tick-has" />}
                  </button>
                ) : <span className="ih-na" data-tip={`${c.type} can't be queried this way`} />}
              </span>
            ))}
          </div>
        );
      })}
    </div>
  );
});

/* ─────────────────────────────── diagnosing ─────────────────────────────── */

function DiagPane({ table, at }) {
  return (
    <div className="ih-card ih-diag ih-pop" aria-live="polite">
      <div className="ih-col-gap"><span className="ih-eyebrow blue">Diagnosing</span><strong className="ih-h3 lg">Reading statistics for <span className="mono">{table}</span></strong></div>
      <div className="ih-prog"><span style={{ width: `${Math.round(((Math.min(at, DIAG.length) + 0.5) / DIAG.length) * 100)}%` }} /><i /></div>
      <ul className="ih-dsteps">
        {DIAG.map((l, i) => (
          <li key={l} className={i < at ? 'done' : i === at ? 'run' : ''}>
            {i < at ? <span className="ih-ds ok"><I n="check" s={11} w={3} /></span> : i === at ? <span className="ih-ds run" /> : <span className="ih-ds" />}
            {l}
          </li>
        ))}
      </ul>
    </div>
  );
}

/* ─────────────────────────────── results ─────────────────────────────── */

function ResultsSkeleton() {
  return (
    <div className="ih-card ih-pop">
      <div className="ih-rtabs skel">{[0, 1, 2].map((i) => <span key={i}><span className="ih-skel line" style={{ width: 90 }} /></span>)}</div>
      {[0, 1].map((i) => <div key={i} className="ih-find skel"><span className="ih-skel box" /><div className="ih-col-gap"><span className="ih-skel line" style={{ width: '50%' }} /><span className="ih-skel line sm" style={{ width: '80%' }} /><span className="ih-skel code" /></div></div>)}
    </div>
  );
}

const ResultsPane = React.memo(function ResultsPane({ result, picked, setPicked, keyCol, setKeyCol, sqlFor, rtab, setRtab, canBuild }) {
  const mF = result.missing, uF = result.cleanup || [];
  const hI = result.indexes.filter((i) => i.status === 'ok');
  const tab = rtab || (mF.length ? 'missing' : uF.length ? 'unused' : 'healthy');
  const TB = [
    ['missing', 'Missing', mF.length, RED, 'rgba(235,23,0,.1)', 'alert', 'Indexes your queries need, plus anything broken'],
    ['unused', 'Unused', uF.length, AMBER, 'rgba(194,106,0,.12)', 'trash', 'Redundant or never-scanned indexes that slow down writes'],
    ['healthy', 'Healthy', hI.length, GREEN, 'rgba(50,135,20,.1)', 'ok', 'Indexes that are valid and in use'],
  ];
  const ti = TB.findIndex((t) => t[0] === tab);
  const [openIx, setOpenIx] = useState(null);
  const flip = (id) => setPicked((p) => { const n = new Set(p); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  const setAll = (L, val) => setPicked((p) => { const n = new Set(p); L.forEach((f) => (val ? n.add(f.id) : n.delete(f.id))); return n; });

  const list = (L, verb) => {
    const able = L.filter((f) => f.buildable);
    const sel = able.filter((f) => picked.has(f.id)).length;
    return (
      <>
        {able.length > 0 && canBuild && (
          <div className="ih-selbar">
            <span>{sel} of {able.length} selected to {verb}</span>
            <button className="ih-btn sm" onClick={() => setAll(able, sel !== able.length)}>{sel === able.length ? 'Deselect all' : 'Select all'}</button>
          </div>
        )}
        {L.map((f, i) => <Finding key={f.id} f={f} i={i} on={picked.has(f.id)} canPick={canBuild && f.buildable} onToggle={() => flip(f.id)} sql={sqlFor(f)} keyCol={keyCol} setKeyCol={setKeyCol} />)}
      </>
    );
  };

  return (
    <div className="ih-card ih-pop">
      <div className="ih-rtabs" role="tablist">
        <span className="ih-rtabs-ink" style={{ left: `calc(${ti} * 100% / 3)` }}><span style={{ background: TB[ti][3] }} /></span>
        {TB.map(([id, label, n, color, bg, icon, tip]) => {
          const on = id === tab;
          return (
            <button key={id} role="tab" aria-selected={on} className={on ? 'on' : ''} style={{ '--c': color }} onClick={() => setRtab(id)} data-tip={tip}>
              <span className="ih-rtab-i" style={{ color, opacity: on ? 1 : 0.55 }}><I n={icon} s={18} /></span>
              <b>{label}</b>
              <span className="ih-badge" style={on ? { background: color, color: '#fff' } : { background: bg, color }}>{n}</span>
            </button>
          );
        })}
      </div>

      {tab === 'missing' && (mF.length ? list(mF, 'build') : (
        <Empty icon="ok" title="Nothing is missing" text="Every query pattern you described has an index behind it." />
      ))}
      {tab === 'unused' && (uF.length ? list(uF, 'drop') : (
        <Empty icon="ok" title="No unused indexes" text="Every index on this table is distinct and has been scanned since the last stats reset." />
      ))}
      {tab === 'healthy' && (hI.length ? (
        <div className="ih-htable" role="table">
          <div className="ih-hrow head" role="row"><span>Index</span><span>Columns · method</span><span>Scans</span><span>State</span></div>
          {hI.map((ix, k) => (
            <React.Fragment key={ix.name}>
              <button className="ih-hrow ih-rise" role="row" style={{ '--d': `${0.03 * Math.min(k, 15)}s` }} onClick={() => setOpenIx(openIx === ix.name ? null : ix.name)} aria-expanded={openIx === ix.name}
                data-tip="Show the definition">
                <span className="ih-hname">{ix.primary && <span className="ih-c-muted" data-tip="Primary key"><I n="key" s={14} /></span>}<code>{ix.name}</code></span>
                <span className="ih-c-muted"><span className="ih-only-sm">Columns: </span>{ix.columns.map((c) => c || '(expr)').join(', ')} · {ix.method}{ix.partial ? ' · partial' : ''}</span>
                <span className="ih-c-muted tnum" data-tip="Index scans since stats were last reset"><span className="ih-only-sm">Scans: </span>{fmt(ix.scans)}</span>
                <span className="ih-state" data-tip={ix.size}><i />{ix.unique ? 'Unique' : 'Healthy'}</span>
              </button>
              {openIx === ix.name && <div className="ih-hdef"><Sql sql={`${ix.definition};`} /><small>{ix.size} on disk</small></div>}
            </React.Fragment>
          ))}
        </div>
      ) : <Empty icon="info" title="No healthy indexes yet" text="This table has no valid, used indexes. Check the Missing tab." tone="warn" />)}
    </div>
  );
});

const Empty = ({ icon, title, text, tone }) => (
  <div className="ih-empty ih-fade">
    <span className={`ih-empty-i ${tone || ''}`}><I n={icon} s={22} /></span>
    <strong>{title}</strong><span>{text}</span>
  </div>
);

function Finding({ f, i, on, canPick, onToggle, sql, keyCol, setKeyCol }) {
  const pr = PRI[f.priority] || PRI.info;
  const tone = f.feature === 'drop' && f.priority === 'info' ? PRI.info : pr;
  const click = () => canPick && onToggle();
  return (
    <div className={`ih-find ih-rise ${on ? 'on' : ''} ${canPick ? '' : 'locked'}`} style={{ '--d': `${0.04 * Math.min(i, 12)}s` }} onClick={click}
      role="checkbox" aria-checked={on} aria-disabled={!canPick} tabIndex={canPick ? 0 : -1} onKeyDown={(e) => { if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); click(); } }}>
      <span className={`ih-check ${on ? 'on' : ''}`} data-tip={!canPick ? (f.buildable ? 'You can see this fix but not run it. Ask an admin for Build Indexes.' : 'Run this by hand; it cannot be built from here') : on ? 'Included in the build' : 'Click to include'}>
        <I n="check" s={13} w={3} />
      </span>
      <div className="ih-find-b">
        <div className="ih-find-top">
          <div className="ih-find-t">
            <strong>{titleOf(f)}</strong>
            <span>{f.why}</span>
          </div>
          <div className="ih-find-m">
            {!f.buildable && <span className="ih-tag muted" data-tip="Needs a manual step; copy the SQL">Run by hand</span>}
            {f.size && <span className="ih-tag muted" data-tip={`${fmt(f.scans)} scans since the last stats reset`}>{f.size}</span>}
            <span className="ih-tag" style={{ background: tone.bg, color: tone.color }}>{methodOf(f)}</span>
            <span className="ih-meter" data-tip={f.feature === 'drop' && f.status ? 'Optional: frees space and speeds up writes' : pr.tip}>
              {[1, 2, 3].map((n) => <i key={n} style={{ background: n <= tone.n ? tone.color : undefined }} />)}
              <small style={{ color: tone.color }}>{f.feature === 'drop' && f.status ? 'tidy' : f.priority}</small>
            </span>
          </div>
        </div>
        {f.feature === 'key' && f.candidates?.length > 0 && (
          <div className="ih-keys" onClick={(e) => e.stopPropagation()}>
            <span>Key column</span>
            {f.candidates.slice(0, 8).map((c, j) => (
              <button key={c} className={c === keyCol ? 'on' : ''} onClick={() => setKeyCol(c)} aria-pressed={c === keyCol}
                data-tip={j === 0 ? 'Most likely id column. The build checks it for empty and duplicate values first.' : 'The build checks it for empty and duplicate values first'}>
                {c}{j === 0 && <small>suggested</small>}
              </button>
            ))}
          </div>
        )}
        <Sql sql={sql} />
      </div>
    </div>
  );
}

/* ─────────────────────────────── rail ─────────────────────────────── */

function Rail({ step, target, cols, result, job, usage, picked, pickedItems, keyCol, canBuild, building, score, checkedBefore, cancelling, onRun, onBuild, onCancel, setStep, onPalette }) {
  const [label, color] = meta(score);
  const marked = Object.values(usage).filter((a) => a.length).length;
  const total = cols.columns?.length || 0;
  const stale = result && !sameUsage(usageLists(usage), result.checked);
  const sub = step === 'results' && result ? `${plural(result.indexes.length, 'index', 'indexes')} checked against ${plural(new Set(Object.values(result.checked).flat()).size, 'column')}`
    : step === 'build' ? 'Updates when the build finishes.'
      : checkedBefore ? 'From earlier this session. Re-run to refresh.' : 'Run a diagnosis to score this table.';

  let body = null;
  if (cols.status === 'error') {
    body = onPalette ? <button className="ih-btn block" onClick={onPalette}><I n="search" />Pick another table</button> : null;
  } else if (step === 'usage' || step === 'checking') {
    const ready = cols.status === 'ready';
    body = (
      <div className="ih-col-gap md">
        <span className="ih-sub">{!ready ? 'Reading columns…' : marked ? `${marked} of ${total} columns marked. The diagnosis checks each one against the indexes in place.` : 'No columns marked: the diagnosis only checks the key, statistics and broken or unused indexes.'}</span>
        <button className="ih-btn primary block" onClick={onRun} disabled={!ready || step === 'checking'}>
          {step === 'checking' ? <Spin c="inv" /> : <I n="play" />}{step === 'checking' ? 'Checking…' : 'Run diagnosis'}<I n="right" s={17} />
        </button>
        {stale && result && <button className="ih-btn ghost block" onClick={() => setStep('results')}>Back to the last diagnosis</button>}
      </div>
    );
  } else if (step === 'diagnosing') {
    body = <span className="ih-sub">Comparing your usage against pg_stat and the index catalog. Nothing is written.</span>;
  } else if (step === 'results' && result) {
    const all = [...result.missing, ...(result.cleanup || [])];
    const n = pickedItems.length, keyMissing = picked.has('key') && !keyCol;
    const drops = pickedItems.filter((m) => m.feature === 'drop').length;
    body = (
      <div className="ih-col-gap md">
        {stale && (
          <div className="ih-banner warn sm"><I n="info" s={15} /><span>Usage changed since this diagnosis.</span><button className="ih-linkbtn" onClick={onRun}>Re-run</button></div>
        )}
        <span className="ih-sub">{all.length ? `${n} of ${all.length} fixes selected${drops ? ` (${drops} to drop)` : ''}. Click a card to include or skip it.` : 'This table is in good shape.'}</span>
        {canBuild && all.some((m) => m.buildable) && (
          <>
            <button className="ih-btn primary block" onClick={onBuild} disabled={!n || building || keyMissing}
              data-tip={building ? 'One build per table at a time' : keyMissing ? 'Pick a key column first' : !n ? 'Select at least one fix' : undefined}>
              <I n="bolt" />{building ? 'Build already running' : `Build ${plural(n, 'fix', 'fixes')}`}<I n="right" s={17} />
            </button>
            <span className="ih-fine">Builds run CONCURRENTLY, so reads and writes continue while they run.</span>
          </>
        )}
        {!canBuild && all.some((m) => m.buildable) && (
          <span className="ih-fine">You can see the fixes but not run them. Ask an admin for <b>Build Indexes</b> under Data Studio → Index Health.</span>
        )}
        {building && <button className="ih-btn block" onClick={() => setStep('build')}><I n="term" />View the running build</button>}
        <button className="ih-btn block" onClick={() => setStep('usage')}><I n="edit" />Edit usage</button>
        <div className="ih-facts">
          <span data-tip="A primary key (or NOT NULL unique btree) to look rows up by"><I n="key" s={14} />Key <b className={result.key.index ? '' : 'ih-c-red'}>{result.key.index ? result.key.columns.join(', ') : 'missing'}</b></span>
          <span data-tip="Needed for trigram text search"><I n={result.extensions.pg_trgm ? 'ok' : 'x'} s={14} />pg_trgm</span>
          <span data-tip="Needed for vector indexes"><I n={result.extensions.vector ? 'ok' : 'x'} s={14} />pgvector</span>
        </div>
      </div>
    );
  } else if (step === 'build' && job) {
    const p = jobProgress(job);
    body = (
      <div className="ih-col-gap md">
        {!job.finished ? (
          <>
            <span className="ih-sub">{p.done} of {job.items.length} statements done. You can leave this page, the build keeps running on the server.</span>
            {canBuild && (
              <button className="ih-btn danger block" onClick={onCancel} disabled={cancelling || job.cancelRequested}>
                {cancelling ? <Spin /> : <I n="x" />}Cancel after current statement
              </button>
            )}
          </>
        ) : (
          <>
            {job.status === 'done' && (
              <div className="ih-beforeafter ih-pop" data-tip="Health before → after">
                <s>{job.before ?? '—'}</s><span>→</span><b style={{ color: meta(job.after)[1] }}>{job.after ?? '…'}</b>
              </div>
            )}
            {job.status !== 'done' && <span className="ih-sub">Build {job.status}. {p.done} of {job.items.length} fixes completed{p.failed ? `, ${p.failed} failed` : ''}. See the log for details.</span>}
            <button className="ih-btn dark block" onClick={() => setStep('results')} disabled={!result}><I n="refresh" />See the new diagnosis</button>
            {onPalette && <button className="ih-btn block" onClick={onPalette}><I n="search" />Check another table</button>}
          </>
        )}
      </div>
    );
  }

  return (
    <aside className="ih-rail ih-rise" style={{ '--d': '.12s' }} aria-label={`Health of ${target.table}`}>
      <div className="ih-rail-score">
        <BigRing score={score} />
        <div className="ih-col-gap sm">
          <span className="ih-eyebrow">Health score</span>
          <strong style={{ color }}>{label}</strong>
          <span className="ih-sub sm">{sub}</span>
        </div>
      </div>
      <hr />
      {body}
    </aside>
  );
}

/* ─────────────────────────────── build ─────────────────────────────── */

const LOGC = { sql: 't-sql', info: 't-info', ok: 't-ok', warn: 't-warn', error: 't-err', progress: 't-prog' };

function BuildPane({ job }) {
  const box = useRef(null);
  const stick = useRef(true);
  const [details, setDetails] = useState(true);
  const [, tick] = useState(0);
  const p = jobProgress(job);
  const running = !job.finished;
  const tot = job.items.length, queued = p.steps.filter((s) => s.st === 'queued').length;
  const cancelled = job.status === 'cancelled';
  const ok = job.status === 'done';
  const color = running ? BLUE : ok ? GREEN : cancelled ? AMBER : RED;

  useEffect(() => { if (!running) return undefined; const id = setInterval(() => tick((n) => n + 1), 1000); return () => clearInterval(id); }, [running]);
  useEffect(() => { if (box.current && stick.current) box.current.scrollTop = box.current.scrollHeight; }, [job.log.length]);
  const onScroll = () => { const b = box.current; stick.current = b.scrollHeight - b.scrollTop - b.clientHeight < 40; };

  const title = running ? `Building ${plural(tot, 'fix', 'fixes')}` : ok ? 'All fixes applied' : cancelled ? 'Build cancelled' : job.status === 'lost' ? 'Build status lost' : 'Build finished with errors';
  const sub = running ? (p.now ? `Now running: ${itemName(p.now)}${p.phase ? ` · ${p.phase}` : ''}` : 'Connecting to the database…')
    : job.status === 'lost' ? 'The server no longer knows this job (it may have restarted). Re-run the diagnosis to see what got built.'
      : `${p.done} of ${tot} statements completed${p.failed ? `, ${p.failed} failed` : ''}`;

  return (
    <>
      <div className="ih-card ih-buildcard ih-pop">
        <div className="ih-build-top">
          <span className="ih-build-i" style={{ color, background: `${color}1a` }}><I n={running ? 'bolt' : ok ? 'ok' : 'alert'} s={20} /></span>
          <div className="ih-col-gap xs grow">
            <strong className="ih-h3 md">{title}</strong>
            <span className="ih-sub">{sub}</span>
          </div>
          <span className="ih-build-pct" style={{ color }}>{p.overall}%</span>
        </div>
        <div className="ih-prog lg">
          <span style={{ width: `${p.overall}%`, background: running ? `linear-gradient(90deg,${BLUE},#3d8fd6)` : color }} />
          {running && <i />}
        </div>
        <div className="ih-build-foot">
          <div className="ih-legend">
            <span><i style={{ background: GREEN }} />{p.done} done</span>
            <span><i style={{ background: BLUE }} />{running && p.now ? 1 : 0} running</span>
            {p.failed > 0 && <span><i style={{ background: RED }} />{p.failed} failed</span>}
            <span><i style={{ background: 'rgba(110,98,89,.35)' }} />{running ? queued : p.steps.filter((s) => s.st === 'skipped').length} {running ? 'queued' : 'skipped'}</span>
          </div>
          <button className="ih-btn sm" onClick={() => setDetails(!details)} aria-expanded={details}>
            {details ? 'Hide details' : 'Show details'}<span className={`ih-chev rot ${details ? 'open' : ''}`}><I n="down" s={15} w={2.2} /></span>
          </button>
        </div>
        {details && (
          <div className="ih-bitems ih-rise">
            {p.steps.map((s) => (
              <div key={s.id} className="ih-bitem">
                <div className="ih-bitem-t">
                  <span className="mono ih-ell" data-tip={itemName(s)}>{itemName(s)}</span>
                  <span className={`st-${s.st}`}>{s.st === 'now' ? (s.pct != null ? `${s.pct}%` : 'running…') : s.st === 'ok' ? (s.feature === 'drop' ? 'Dropped' : 'Valid') : cap(s.st)}</span>
                </div>
                <span className="ih-bitem-bar"><span className={s.st === 'now' && s.pct == null ? 'indet' : ''} style={{ width: `${s.st === 'now' ? (s.pct ?? 30) : s.pct}%`, background: s.st === 'ok' ? GREEN : s.st === 'failed' ? RED : BLUE }} /></span>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="ih-term ih-pop" style={{ '--d': '.08s' }}>
        <div className="ih-term-h">
          <span><I n="term" s={15} />Live log<span className="ih-sdot" style={{ background: running ? RED : GREEN, borderColor: 'transparent' }} data-tip={running ? 'Streaming from the server' : 'Finished'} /></span>
          <span className="tnum">{running ? `${clock(secs(job))} elapsed` : `Finished in ${clock(secs(job))}`}</span>
        </div>
        <div className="ih-log" ref={box} onScroll={onScroll} role="log" aria-live="off">
          {!job.log.length && <div className="ih-logl"><span className="ts" /><span className="t-info">{job.loaded ? 'No log yet.' : 'Fetching the log from the server…'}</span></div>}
          {job.log.map((l, i) => (
            <div key={i} className="ih-logl"><span className="ts">{l.ts}</span><span className={LOGC[l.level] || 't-info'}>{l.level === 'sql' ? '› ' : ''}{l.msg}</span></div>
          ))}
          {running && <span className="ih-caret log" />}
        </div>
      </div>
      <div className="ih-note"><I n="info" s={15} />Cancelling mid-build discards the index in progress and leaves it invalid. The next diagnosis offers to clean it up.</div>
    </>
  );
}

/* ─────────────────────────────── dialogs ─────────────────────────────── */

function useFocusTrap(ref) {
  useEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    const prev = document.activeElement;
    const onKey = (e) => {
      if (e.key !== 'Tab') return;
      const f = [...el.querySelectorAll('button:not([disabled]),input,[tabindex="0"]')];
      if (!f.length) return;
      if (e.shiftKey && document.activeElement === f[0]) { e.preventDefault(); f[f.length - 1].focus(); }
      else if (!e.shiftKey && document.activeElement === f[f.length - 1]) { e.preventDefault(); f[0].focus(); }
    };
    el.addEventListener('keydown', onKey);
    return () => { el.removeEventListener('keydown', onKey); prev?.focus?.(); };
  }, [ref]);
}

function Modal({ children, onClose, label, cls = '' }) {
  const ref = useRef(null);
  useFocusTrap(ref);
  return (
    <Layer>
      <div className="ih-scrim" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
        <div ref={ref} className={`ih-modal ${cls}`} role="dialog" aria-modal="true" aria-label={label}>{children}</div>
      </div>
    </Layer>
  );
}

function Confirm({ title, text, yes, no, onYes, onClose }) {
  return (
    <Modal onClose={onClose} label={title} cls="sm">
      <div className="ih-mh"><strong className="ih-h3">{title}</strong><span className="ih-sub">{text}</span></div>
      <div className="ih-mf"><button className="ih-btn ghost" onClick={onClose} autoFocus>{no}</button><button className="ih-btn danger" onClick={onYes}>{yes}</button></div>
    </Modal>
  );
}

function ConfirmBuild({ target, items, sqlFor, starting, onStart, onClose }) {
  const prod = target.env === 'prod';
  const [sure, setSure] = useState(false);
  const [shake, setShake] = useState(false);
  const ok = !prod || sure;
  const go = () => {
    if (!ok) { setShake(true); setTimeout(() => setShake(false), 380); return; }
    onStart();
  };
  return (
    <Modal onClose={onClose} label="Confirm build" cls={shake ? 'ih-shake' : ''}>
      <div className="ih-mh">
        <div className="ih-row-gap xs"><span className={`ih-envchip ${prod ? 'prod' : ''}`}>{target.env}</span><span className="ih-sub">{target.database}</span></div>
        <strong className="ih-h3">Build {plural(items.length, 'fix', 'fixes')} on <span className="mono">{target.schema}.{target.table}</span></strong>
        <span className="ih-sub">These statements run in order, exactly as shown.</span>
      </div>
      <div className="ih-mb">
        {items.map((m) => (
          <div key={m.id} className="ih-mitem">
            <div className="ih-row-gap xs"><span className="ih-tag" style={{ background: (PRI[m.priority] || PRI.info).bg, color: (PRI[m.priority] || PRI.info).color }}>{methodOf(m)}</span><b>{titleOf(m)}</b></div>
            <Sql sql={sqlFor(m)} />
          </div>
        ))}
        {prod && (
          <div className="ih-guard">
            <strong><I n="alert" s={16} />You are about to change production</strong>
            <span>These changes go live on <b className="mono">{target.schema}.{target.table}</b> in {target.database}.</span>
            <button className={`ih-sure ${sure ? 'on' : ''}`} role="checkbox" aria-checked={sure} onClick={() => setSure(!sure)} autoFocus>
              <span className={`ih-check ${sure ? 'on' : ''}`}><I n="check" s={13} w={3} /></span>Yes, I'm sure. Run this on production.
            </button>
          </div>
        )}
      </div>
      <div className="ih-mf">
        <span className="ih-fine grow"><I n="shield" s={14} />Concurrent builds. Reads and writes keep flowing.</span>
        <button className="ih-btn ghost" onClick={onClose}>Cancel</button>
        <button className="ih-btn primary" onClick={go} disabled={starting} autoFocus={!prod} data-tip={!ok ? 'Tick "Yes, I\'m sure" first' : undefined}>
          {starting ? <Spin c="inv" /> : <I n="bolt" />}{starting ? 'Starting…' : 'Start build'}
        </button>
      </div>
    </Modal>
  );
}

/* ─────────────────────────────── palette ─────────────────────────────── */

function Palette({ env, cat, all, statusOf, onSelect, onClose, onRetry }) {
  const [q, setQ] = useState('');
  const [sel, setSel] = useState(0);
  const listRef = useRef(null);
  const needle = q.trim().toLowerCase();
  const list = useMemo(() => {
    if (!needle) return all.slice(0, 60);
    const hits = all.filter((x) => `${x.database}.${x.schema}.${x.table}`.toLowerCase().includes(needle));
    // table-name hits first, then shortest name
    return hits.sort((a, b) => (b.table.toLowerCase().includes(needle) - a.table.toLowerCase().includes(needle)) || a.table.length - b.table.length).slice(0, 80);
  }, [all, needle]);
  const at = Math.min(sel, Math.max(0, list.length - 1));
  useEffect(() => { listRef.current?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' }); }, [at]);

  const mark = (s) => {
    if (!needle) return s;
    const i = s.toLowerCase().indexOf(needle);
    return i < 0 ? s : <>{s.slice(0, i)}<mark>{s.slice(i, i + needle.length)}</mark>{s.slice(i + needle.length)}</>;
  };
  const onKey = (e) => {
    if (e.key === 'ArrowDown') { setSel(Math.min(at + 1, list.length - 1)); e.preventDefault(); }
    if (e.key === 'ArrowUp') { setSel(Math.max(at - 1, 0)); e.preventDefault(); }
    if (e.key === 'Enter' && list[at]) onSelect(list[at]);
    if (e.key === 'Escape') onClose();
  };

  return (
    <Modal onClose={onClose} label="Find a table" cls="pal">
      <div className="ih-pal-in">
        <span className="ih-c-red"><I n="search" s={20} /></span>
        <input autoFocus value={q} onChange={(e) => { setQ(e.target.value); setSel(0); }} onKeyDown={onKey}
          placeholder={`Search tables on ${env}…`} autoComplete="off" spellCheck="false" aria-label="Search tables" aria-controls="ih-pal-list" />
        <Kbd>esc</Kbd>
      </div>
      <div className="ih-pal-list" ref={listRef} role="listbox" id="ih-pal-list">
        {!cat.data && cat.status === 'loading' && <div className="ih-pal-nil"><Spin />Loading the table list…</div>}
        {cat.status === 'error' && !all.length && <div className="ih-pal-nil bad"><I n="alert" s={15} />{cat.error}<button className="ih-btn sm" onClick={onRetry}>Retry</button></div>}
        {list.map((x, i) => {
          const k = keyOf(x), st = statusOf(k);
          return (
            <button key={k} role="option" aria-selected={i === at} className={`ih-pal-row ${i === at ? 'sel' : ''}`} onClick={() => onSelect(x)} onMouseMove={() => i !== at && setSel(i)}>
              <span className="ih-pick-i muted"><I n="table" s={18} /></span>
              <StatusDot s={st} />
              <span className="ih-pick-t"><span className="mono ih-ell">{mark(x.table)}</span><small>{x.database} · {x.schema}</small></span>
              <span className="ih-pal-s" style={{ color: typeof st === 'number' ? meta(st)[1] : st === 'busy' ? BLUE : MUTED }}>{st === 'busy' ? 'building' : typeof st === 'number' ? `health ${st}` : 'not checked'}</span>
            </button>
          );
        })}
        {cat.data && !list.length && cat.status !== 'streaming' && <div className="ih-pal-nil">{needle ? `No tables match "${q}".` : 'No tables you can read.'}</div>}
        {cat.status === 'streaming' && <div className="ih-pal-nil sm"><Spin c="xs" />Still reading {plural(cat.pending.length, 'database')}, results keep coming in…</div>}
      </div>
      <div className="ih-pal-ft">
        <span><Kbd>↑</Kbd><Kbd>↓</Kbd>move</span><span><Kbd>↵</Kbd>open</span>
        <span className="grow" />
        <span>{cat.data ? (needle ? `${list.length}${list.length === 80 ? '+' : ''} of ${fmt(all.length)}` : `${fmt(all.length)} tables`) : ''}</span>
      </div>
    </Modal>
  );
}

/* ─────────────────────────────── tray ─────────────────────────────── */

// Builds on other tables keep running; this is where you see them.
function Tray({ jobs, here, open, setOpen, onView }) {
  const [gone, setGone] = useState(() => new Set()); // finished builds the user closed
  const others = [...jobs].reverse().filter((j) => keyOf(j.target) !== here && !gone.has(j.id));
  const live = others.filter((j) => !j.finished);
  if (!others.length) return null;
  const close = (e) => { e.stopPropagation(); setOpen(false); setGone((g) => new Set([...g, ...others.filter((j) => j.finished).map((j) => j.id)])); };
  const pct = live.length ? live.reduce((a, j) => a + jobProgress(j).overall, 0) / live.length / 100 : 1;
  return (
    <Layer>
      <aside className={`ih-tray ${open ? 'open' : ''}`} aria-live="polite">
        <div className="ih-tray-top">
        <button className="ih-tray-h" onClick={() => setOpen(!open)} aria-expanded={open}>
          <span className="ih-tray-ring">
            <svg className="ring" viewBox="0 0 36 36"><circle cx="18" cy="18" r="15" fill="none" strokeWidth="3" stroke="rgba(110,98,89,.18)" />
              <circle cx="18" cy="18" r="15" fill="none" strokeWidth="3" stroke={live.length ? BLUE : GREEN} strokeLinecap="round" strokeDasharray="94.2" strokeDashoffset={94.2 * (1 - pct)} /></svg>
            {live.length ? <b>{live.length}</b> : <I n="check" s={14} w={2.6} />}
          </span>
          <span className="ih-col-gap xs grow"><b>{live.length ? `${plural(live.length, 'build')} running` : 'Builds finished'}</b><small>{live.length ? 'On other tables, in the background' : `${others.length} this session`}</small></span>
          <span className={`ih-chev rot ${open ? 'open' : ''}`}><I n="down" s={15} w={2.2} /></span>
        </button>
        {!live.length && <button className="ih-iconbtn" onClick={close} aria-label="Dismiss" data-tip="Dismiss"><I n="x" s={14} /></button>}
        </div>
        {open && (
          <div className="ih-tray-l">
            {others.map((j) => {
              const p = jobProgress(j);
              return (
                <button key={j.id} className="ih-tray-j" onClick={() => onView(j.target)} data-tip="Open this build">
                  <span className="ih-row-gap xs"><span className={`ih-envchip sm ${j.target.env === 'prod' ? 'prod' : ''}`}>{j.target.env}</span><code className="ih-ell">{j.target.schema}.{j.target.table}</code></span>
                  <span className="ih-bitem-bar"><span style={{ width: `${p.overall}%`, background: !j.finished ? BLUE : j.status === 'done' ? GREEN : RED }} /></span>
                  <small>{!j.finished ? `${p.done}/${j.items.length} · ${p.now ? itemName(p.now) : 'starting'}` : j.status === 'done' ? `Done · health ${j.before ?? '—'} → ${j.after ?? '…'}` : `${cap(j.status)} after ${p.done}/${j.items.length}`}</small>
                </button>
              );
            })}
          </div>
        )}
      </aside>
    </Layer>
  );
}

export default IndexHealth;

// Reused by the Data API export page (pages/dataApi/IndexBuilder.jsx) so both look the same.
export {
  I, Spin, Sql, Steps, UsagePane, UsageSkeleton, DiagPane, ResultsPane, ResultsSkeleton, BuildPane, Rail, ConfirmBuild, Confirm,
  DIAG, scoreOf, titleOf, usageLists, compact, plural, ago, msg,
};