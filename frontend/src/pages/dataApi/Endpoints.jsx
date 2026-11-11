import React, { useEffect, useMemo, useRef, useState } from 'react';
import { ArrowRight, ChevronDown, ChevronRight, Copy, ExternalLink, Plus, X } from 'lucide-react';
import { apiFetch } from '../../utils/apiClient';
import { tableUiUrl } from './dataApiDocs';
import { API_BASE_URL, bytes, copyText, FILTER_OPS, KEY_PLACEHOLDER, NO_VALUE_OPS, request, usePage, useResource, useToast } from './lib';
import { highlightCode, InlineError, JsonView, Seg, Select, Spinner } from './ui';

const enc = encodeURIComponent;
const COPY_FAIL = "Couldn't copy. Your browser blocked the clipboard.";

/* ------------------------------------------------------------ helpers */

/* Live calls from this page are real consumer calls: they carry the key in a
   header, show up in Activity and count toward usage. */
async function call(path, { method = 'GET', key, body, signal } = {}) {
  const started = performance.now();
  const res = await apiFetch(path, {
    method,
    signal,
    headers: { 'X-API-Key': key, ...(body ? { 'Content-Type': 'application/json' } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let json;
  try { json = text ? JSON.parse(text) : null; } catch { json = null; }
  return { http: res.status, ok: res.ok, ms: Math.round(performance.now() - started), text, json, size: text.length };
}

const LANGS = [['curl', 'cURL'], ['python', 'Python'], ['pq', 'Power Query']];

function snippet(e, lang) {
  const K = KEY_PLACEHOLDER;
  const params = Object.entries(e.params || {});
  if (lang === 'python') {
    const p = [`"api_key": "${K}"`, ...params.map(([k, v]) => `"${k}": ${/^\d+$/.test(String(v)) ? v : `"${v}"`}`)].join(', ');
    return e.m === 'GET'
      ? `import requests\n\nr = requests.get(\n    "${e.url}",\n    params={${p}},\n    timeout=60,\n)\nr.raise_for_status()\nprint(r.json())`
      : `import requests\n\nr = requests.post(\n    "${e.url}",\n    headers={"X-API-Key": "${K}"},\n    json=${e.body},\n    timeout=60,\n)\nr.raise_for_status()\nprint(r.json())`;
  }
  if (lang === 'pq') {
    if (e.m !== 'GET') return `// Power Query works best with the GET endpoints.\n// Use Python or cURL for ${e.path}.`;
    const q = [`api_key = "${K}"`, ...params.map(([k, v]) => `${k} = "${v}"`)].join(', ');
    return `let\n    Source = Json.Document(Web.Contents(\n        "${e.url}",\n        [Query = [${q}]]\n    ))${e.id === 'rows' ? ',\n    Rows = Table.FromRecords(Source[rows])\nin\n    Rows' : '\nin\n    Source'}`;
  }
  const qs = new URLSearchParams({ api_key: K, ...Object.fromEntries(params.map(([k, v]) => [k, String(v)])) }).toString();
  return e.m === 'GET'
    ? `curl "${e.url}?${qs.replace(/%20/g, '+')}"`
    : `curl -X POST "${e.url}" \\\n  -H "X-API-Key: ${K}" \\\n  -H "Content-Type: application/json" \\\n  -d '${e.body}'`;
}

const jsonText = (res, limit) => {
  if (res.body != null) return res.body.length > limit ? `${res.body.slice(0, limit)}\n… truncated for display` : res.body;
  if (res.json) {
    const s = JSON.stringify(res.json, null, 2);
    return s.length > limit ? `${s.slice(0, limit)}\n… truncated for display` : s;
  }
  return res.text || '(empty body)';
};

/* ------------------------------------------------------- small pieces */

const Method = ({ m }) => <span className={`ex-m ${m === 'GET' ? 'get' : 'post'}`}>{m}</span>;

const Step = ({ n, children, aside }) => (
  <div className="ex-step">
    <span className="ex-stepn">{n}</span>
    <span className="ex-stept">{children}</span>
    {aside && <span className="ex-stepa">{aside}</span>}
  </div>
);

const CopyIcon = () => <Copy size={13} strokeWidth={1.8} aria-hidden="true" />;

function Shimmer({ lines }) {
  return (
    <div className="ex-shim">
      {lines.map((w) => <div key={w} className="shim" style={{ width: w }} />)}
    </div>
  );
}

function FilterRow({ cols, col, op, val, onChange, onRemove }) {
  const noValue = NO_VALUE_OPS.includes(op);
  let ph = 'value';
  if (noValue) ph = 'no value';
  else if (['in', 'not_in'].includes(op)) ph = 'comma, separated, values';
  return (
    <div className="ex-filter">
      <Select small value={col} onChange={(v) => onChange({ col: v })} options={cols.map((c) => ({ value: c, label: c }))} mono ariaLabel="Filter column" />
      <Select small value={op} onChange={(v) => onChange({ op: v })} options={FILTER_OPS.map(([o, l]) => ({ value: o, label: l }))} ariaLabel="Operator" />
      <input className="field sm" value={val} disabled={noValue} placeholder={ph} onChange={(e) => onChange({ val: e.target.value })} aria-label="Filter value" />
      <button type="button" className="ex-x" onClick={onRemove} aria-label="Remove filter"><X size={14} strokeWidth={1.8} aria-hidden="true" /></button>
    </div>
  );
}

/* Ready-made code for the selected endpoint. Copy swaps in the real key. */
function CodeBlock({ e, lang, setLang, apiKey }) {
  const toast = useToast();
  const code = snippet(e, lang);
  const html = useMemo(() => highlightCode(code, lang), [code, lang]);
  const copyWithKey = async () => {
    const ok = await copyText(code.split(KEY_PLACEHOLDER).join(apiKey));
    toast(ok ? 'Copied with the live key. Treat it as a secret.' : COPY_FAIL, ok ? 'ok' : 'error');
  };
  return (
    <div className="ex-codecard">
      <div className="ex-codebar">
        <div className="ex-langs" role="tablist" aria-label="Language">
          {LANGS.map(([v, l]) => (
            <button key={v} type="button" role="tab" aria-selected={lang === v} className={lang === v ? 'on' : ''} onClick={() => setLang(v)}>{l}</button>
          ))}
        </div>
        <button type="button" className="ex-codecopy" onClick={copyWithKey}><CopyIcon />Copy with key</button>
      </div>
      {/* Every token is escaped inside highlight() before markup is added. */}
      <pre className="ex-code" dangerouslySetInnerHTML={{ __html: html }} />
    </div>
  );
}

/* What will go over the wire, rebuilt on every change until it is sent.
   The key is masked here; the live call carries it in a header. */
function RequestPreview({ method, path, params = [], apiKey, body, sending }) {
  const origin = API_BASE_URL.replace(/\/$/, '');
  const parts = path.split('/').filter(Boolean);
  const head = parts.slice(0, 2).join('/');
  const mid = parts.slice(2, -1);
  const last = parts.length > 2 ? parts[parts.length - 1] : '';
  const headers = [['X-API-Key', apiKey ? `••••••••${apiKey.slice(-4)}` : KEY_PLACEHOLDER]];
  if (body) headers.push(['Content-Type', 'application/json']);

  return (
    <div className={`ex-pv ${sending ? 'sending' : ''}`}>
      <div className="ex-pvtop">
        <span className="ex-pvlive"><i />{sending ? 'Sending this request…' : 'Live preview'}</span>
        <span className="ex-pvhint">{sending ? 'Waiting for the source' : 'Updates as you change the settings'}</span>
      </div>

      <div className="ex-pvurl">
        <div className="ex-pvline">
          <Method m={method} />
          <span className="ex-pvpath">
            <span className="o">{origin}/</span>
            <span className="h">{head}</span>
            {mid.map((x) => <span key={x} className="a">/{x}</span>)}
            {last && <span className="e">/{last}</span>}
          </span>
        </div>
        {params.map(([k, v], i) => (
          <div key={`${k}-${i}`} className="ex-pvq">
            <span className="j">{i === 0 ? '?' : '&'}</span>
            <span className="k">{k}</span>
            <span className="j">=</span>
            <span key={v} className="v">{v === '' ? '""' : v}</span>
          </div>
        ))}
      </div>

      <div className="ex-pvgrid">
        <section>
          <span className="ex-pvlab">Query parameters <b>{params.length}</b></span>
          {params.length ? (
            <div className="ex-pvrows">
              {params.map(([k, v], i) => (
                <div key={`${k}-${i}`} className="ex-pvrow">
                  <span className="k">{k}</span>
                  <span key={v} className="v">{v === '' ? <i>empty</i> : v}</span>
                </div>
              ))}
            </div>
          ) : <p className="ex-pvnone">{body ? 'None. Everything goes in the body.' : 'None needed for this call.'}</p>}
        </section>
        <section>
          <span className="ex-pvlab">Headers <b>{headers.length}</b></span>
          <div className="ex-pvrows">
            {headers.map(([k, v]) => (
              <div key={k} className="ex-pvrow">
                <span className="k">{k}</span>
                <span className="v">{v}</span>
              </div>
            ))}
          </div>
        </section>
      </div>

      {body && (
        <section className="ex-pvbody">
          <span className="ex-pvlab">Body</span>
          <JsonView text={JSON.stringify(body, null, 2)} />
        </section>
      )}
    </div>
  );
}

/* The dark panel on the right. Same look for every endpoint. */
function ResponsePanel({ res, stats, tools, children, onRetry, preview, idleTitle = 'No request sent yet' }) {
  let label = preview ? 'Not sent yet' : 'Waiting';
  let tone = 'idle';
  if (res.status === 'loading') { label = 'Sending…'; tone = 'busy'; }
  else if (res.status === 'error') { label = 'Request failed'; tone = 'bad'; }
  else if (res.status === 'done') { label = `${res.http} ${res.ok ? 'OK' : 'Error'}`; tone = res.ok ? 'good' : 'bad'; }

  return (
    <div className="ex-out">
      <div className="ex-outbar">
        <span className="ex-outtitle">{preview && res.status === 'idle' ? 'Request' : 'Response'}</span>
        <span className={`ex-chipstat ${tone}`}><i />{label}</span>
        {stats && <span className="ex-outstats">{stats}</span>}
        <span className="ex-grow" />
        {tools}
      </div>
      <div className="ex-outbody">
        {preview && (res.status === 'idle' || res.status === 'loading') && React.cloneElement(preview, { sending: res.status === 'loading' })}
        {!preview && res.status === 'idle' && (
          <div className="ex-idle">
            <span className="ex-idle-ic" aria-hidden="true">
              <ArrowRight size={22} strokeWidth={1.6} aria-hidden="true" />
            </span>
            <b>{idleTitle}</b>
            <span>Press <em>Send request</em> and the live answer shows up here.</span>
          </div>
        )}
        {!preview && res.status === 'loading' && <Shimmer lines={['38%', '70%', '62%', '84%', '50%', '68%', '44%', '76%']} />}
        {res.status === 'error' && <div className="ex-outerr"><InlineError error={{ message: res.message }} onRetry={onRetry} /></div>}
        {res.status === 'done' && children}
      </div>
    </div>
  );
}

/* ------------------------------------------- simple GET (tables, columns, count) */

function SimpleRunner({ api, e, code }) {
  const [res, setRes] = useState({ status: 'idle' });
  const ctrl = useRef(null);
  useEffect(() => () => ctrl.current?.abort(), []);

  const send = async () => {
    ctrl.current?.abort();
    const c = new AbortController();
    ctrl.current = c;
    setRes({ status: 'loading' });
    try {
      const r = await call(e.probe, { key: api.api_key, signal: c.signal });
      setRes({ status: 'done', ...r });
    } catch (err) {
      if (err?.name !== 'AbortError') setRes({ status: 'error', message: err?.message || 'Request failed' });
    }
  };

  return (
    <div className="ex-split">
      <div className="ex-left">
        <section className="ex-block">
          <Step n="3">Send it live</Step>
          <div className="ex-req">
            <Method m={e.m} />
            <span className="ex-url">/{api.slug}<b>{e.path}</b></span>
            <button type="button" className="ex-send" onClick={send} disabled={res.status === 'loading'}>
              {res.status === 'loading' ? <><Spinner />Sending</> : 'Send request'}
            </button>
          </div>
          <p className="ex-note">No settings needed for this one. It runs with this API&apos;s key.</p>
        </section>
        <section className="ex-block">
          <Step n="4">Use it in your code</Step>
          {code}
        </section>
      </div>
      <div className="ex-right">
        <ResponsePanel
          res={res}
          onRetry={send}
          stats={res.status === 'done' && `${res.ms} ms · ${bytes(res.size)}`}
          preview={<RequestPreview method={e.m} path={e.probe.split('?')[0]} apiKey={api.api_key} />}
        >
          <JsonView text={jsonText(res, 20000)} />
        </ResponsePanel>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------ fetch rows */

const freshForm = (t) => ({ limit: String(Math.min(t?.row_limit || 25, 25)), offset: '0', search: '', column: '', match: 'substring', matchTouched: false, order: 'key', count: true, cursor: false, filters: [], pick: [] });

function RowsConsole({ api, alias, meta, code }) {
  const toast = useToast();
  const t = (api.tables || []).find((x) => x.alias === alias) || api.tables?.[0];
  const cols = (meta.data?.columns || []).map((c) => c.name);
  const [f, setF] = useState(() => freshForm(t));
  const [optsOpen, setOptsOpen] = useState(false);
  const [view, setView] = useState('json');
  const [res, setRes] = useState({ status: 'idle' });
  const [cursorAt, setCursorAt] = useState('');
  const ctrl = useRef(null);
  useEffect(() => () => ctrl.current?.abort(), []);

  const set = (patch) => setF((x) => ({ ...x, ...patch }));
  const search = f.search.trim();
  const canSeek = !!meta.data?.cursor_paging && !search;
  const seeking = canSeek && f.cursor;
  const readyFilters = f.filters.filter((x) => x.col && (NO_VALUE_OPS.includes(x.op) || x.val !== ''));
  const effMatch = !f.column && f.match !== 'substring' ? 'substring' : f.match;

  const buildQs = (key, cursor) => {
    const p = new URLSearchParams();
    if (key) p.set('api_key', key);
    p.set('limit', f.limit || '0');
    if (seeking) { if (cursor) p.set('cursor', cursor); } else p.set('offset', f.offset || '0');
    if (f.pick.length) p.set('columns', f.pick.join(','));
    readyFilters.forEach((x) => p.append('where', `${x.col}:${x.op}${NO_VALUE_OPS.includes(x.op) ? '' : `:${x.val}`}`));
    if (!f.count) p.set('count', 'false');
    if (search) {
      p.set('search', search);
      if (f.column) p.set('column', f.column);
      if (f.order === 'none') p.set('sort', 'none');
      if (effMatch !== 'substring') p.set('mode', effMatch);
    }
    return p.toString();
  };

  const send = async (cursor = '', offset) => {
    if (!alias) return;
    if (offset != null) set({ offset: String(offset) });
    const p = new URLSearchParams(buildQs('', cursor));
    if (offset != null && !seeking) p.set('offset', String(offset));
    ctrl.current?.abort();
    const c = new AbortController();
    ctrl.current = c;
    setRes({ status: 'loading' });
    try {
      const r = await call(`/data-api/${enc(api.slug)}/${enc(alias)}/rows?${p.toString()}`, { key: api.api_key, signal: c.signal });
      setCursorAt(cursor);
      setRes({ status: 'done', ...r });
    } catch (e) {
      if (e?.name !== 'AbortError') setRes({ status: 'error', message: e?.message || 'Request failed' });
    }
  };

  const pickColumn = (c) => {
    // Once you narrow to one column, whole-word matching is what people expect.
    // Across all columns only substring works.
    if (c && !f.matchTouched && f.match === 'substring') set({ column: c, match: 'words' });
    else if (!c) set({ column: '', match: 'substring' });
    else set({ column: c });
  };

  const body = res.json;
  const rows = Array.isArray(body?.rows) ? body.rows : [];
  const keys = rows.length ? Object.keys(rows[0]) : [];
  const nextOffset = body?.next_offset;
  const nextCursor = body?.next_cursor;
  const nOpts = (effMatch !== 'substring') + (f.order !== 'key') + (!f.count) + (seeking ? 1 : 0) + readyFilters.length + (f.pick.length ? 1 : 0);

  const dupes = f.filters.filter((x) => x.op === 'eq' && x.col).reduce((acc, x) => ({ ...acc, [x.col]: (acc[x.col] || 0) + 1 }), {});
  const dupe = Object.keys(dupes).find((c) => dupes[c] > 1);
  const listy = f.filters.find((x) => ['eq', 'ne'].includes(x.op) && x.val.includes(','));

  const nSearchable = (meta.data?.searchable_columns || []).length;
  let scanHint = 'Paging by offset. The deeper the page, the more the source skips first.';
  if (search && f.column && effMatch === 'exact') scanHint = `Matches ${f.column} exactly. An index on it can answer this without reading the table.`;
  else if (search && f.column) scanHint = `Scans ${f.column} only${f.order === 'none' ? ', unordered' : ''}. Much faster than every column.`;
  else if (search) scanHint = `Scans ${nSearchable || 'every'} searchable column${nSearchable === 1 ? '' : 's'}. Pick one to narrow it.`;
  else if (seeking) scanHint = `Seeking by ${(meta.data?.sort_key || []).join(', ')}. Every page costs the same, however deep you go.`;

  const copyUrl = async () => {
    const ok = await copyText(`${API_BASE_URL}/data-api/${api.slug}/${alias}/rows?${buildQs(api.api_key, cursorAt)}`);
    toast(ok ? 'URL copied. It carries the live key.' : COPY_FAIL, ok ? 'ok' : 'error');
  };

  let colLabel = 'All columns';
  if (meta.loading) colLabel = 'Loading columns…';
  else if (meta.error) colLabel = 'Columns unavailable';
  const colOptions = [{ value: '', label: colLabel }, ...cols.map((c) => ({ value: c, label: c }))];
  const onEnter = (e) => { if (e.key === 'Enter') send(''); };

  let stats = null;
  if (res.status === 'done') {
    stats = `${res.ms} ms${typeof body?.elapsed_ms === 'number' ? ` (${body.elapsed_ms} source)` : ''}`
      + `${typeof body?.row_count === 'number' ? ` · ${body.row_count.toLocaleString()} rows` : ''} · ${bytes(res.size)}`;
  }

  return (
    <div className="ex-split">
      <div className="ex-left">
        <section className="ex-block">
          <Step n="3" aside={<button type="button" className="ex-link" onClick={copyUrl}>Copy URL with key</button>}>Set up and send</Step>

          <form className="ex-req" onSubmit={(e) => { e.preventDefault(); send(''); }}>
            <Method m="GET" />
            <span className="ex-url" title={`/${api.slug}/${alias}/rows?${buildQs('', cursorAt)}`}>
              /{api.slug}/<b>{alias}</b>/rows<span className="ex-qs">?{buildQs('', cursorAt)}</span>
            </span>
            <button type="submit" className="ex-send" disabled={res.status === 'loading' || !alias}>
              {res.status === 'loading' ? <><Spinner />Sending</> : 'Send request'}
            </button>
          </form>

          <div className="ex-params">
            <div>
              <label className="ex-lab" htmlFor="dax-try-limit">Rows per page</label>
              <input id="dax-try-limit" className="field" inputMode="numeric" value={f.limit} onChange={(e) => set({ limit: e.target.value.replace(/\D/g, '') })} onKeyDown={onEnter} />
            </div>
            <div>
              <label className="ex-lab" htmlFor="dax-try-offset">Start at row</label>
              <input id="dax-try-offset" className="field" inputMode="numeric" value={f.offset} disabled={seeking} onChange={(e) => set({ offset: e.target.value.replace(/\D/g, '') })} onKeyDown={onEnter} />
            </div>
            <div className="ex-span2">
              <label className="ex-lab" htmlFor="dax-try-search">Search for</label>
              <input id="dax-try-search" className="field" value={f.search} placeholder="Any text, optional" onChange={(e) => set({ search: e.target.value })} onKeyDown={onEnter} />
            </div>
            <div className="ex-span2">
              <span className="ex-lab">Search in</span>
              <Select value={f.column} onChange={pickColumn} options={colOptions} mono disabled={!cols.length} ariaLabel="Search in column" />
            </div>
          </div>

          <p className="ex-note ex-hintline">{scanHint}</p>

          <details className="ex-opts" open={optsOpen} onToggle={(e) => setOptsOpen(e.currentTarget.open)}>
            <summary>
              <span className="ex-optstitle">More options</span>
              <span className="ex-optssub">Match mode, order, filters, columns</span>
              {nOpts > 0 && <span className="ex-badge">{nOpts} on</span>}
              <ChevronDown className="ex-chev" size={15} strokeWidth={1.8} aria-hidden="true" />
            </summary>
            <div className="ex-optsbody">
              <div className="ex-optgrid">
                <div>
                  <span className="ex-lab">Match</span>
                  <Seg
                    value={effMatch}
                    onChange={(v) => set({ match: v, matchTouched: true })}
                    options={[
                      ['words', 'Whole words', 'Full-text search. "Bern" stops matching "Berlin". Needs a column, and is only fast where that column has a text-search index.', !f.column],
                      ['exact', 'Exact', 'The value must equal the column. The right way to look up a known id. Needs a column.', !f.column],
                      ['substring', 'Contains', 'Literal text anywhere in the value.'],
                    ]}
                    ariaLabel="Match mode"
                  />
                  {!f.column && <p className="ex-note ex-mt6">Whole words and exact need a column in &quot;Search in&quot;.</p>}
                </div>
                <div>
                  <span className="ex-lab">Order</span>
                  <Seg
                    value={f.order}
                    onChange={(v) => set({ order: v })}
                    options={[['key', 'Stable', 'Ordered by the table key, so paging never repeats or skips a row.'], ['none', 'Fastest', 'No ORDER BY. Far faster on a broad search, but row order across pages is not guaranteed.', !search]]}
                    ariaLabel="Order"
                  />
                </div>
              </div>

              <div className="ex-toggles">
                <label className="ex-tog" data-tip="The first page runs a real COUNT(*). Untick to send count=false.">
                  <input type="checkbox" checked={f.count} onChange={(e) => set({ count: e.target.checked })} />
                  <span><b>Include total count</b><small>Adds row_count to the first page</small></span>
                </label>
                <label className={`ex-tog ${canSeek ? '' : 'off'}`} data-tip={canSeek ? 'Page with cursor= instead of offset=. Same rows, but the source seeks straight to the page.' : 'Needs a unique key on the source, and not while searching.'}>
                  <input type="checkbox" checked={seeking} disabled={!canSeek} onChange={(e) => { set({ cursor: e.target.checked }); setCursorAt(''); }} />
                  <span><b>Use cursor paging</b><small>{canSeek ? 'Same speed on every page' : 'Not available right now'}</small></span>
                </label>
              </div>

              <div>
                <div className="ex-rowhead">
                  <span className="ex-lab">Filters</span>
                  <button type="button" className="ex-link" disabled={!cols.length || f.filters.length >= (meta.data?.max_filters || 10)} onClick={() => set({ filters: [...f.filters, { col: cols[0] || '', op: 'eq', val: '' }] })}><Plus size={13} strokeWidth={2} aria-hidden="true" />Add filter</button>
                </div>
                {(dupe || listy) && (
                  <div className="inline-warn ex-mb">
                    {dupe ? `Two "equals" filters on ${dupe} can't both match. Use "one of" for several values.` : 'That value has commas in it. "Equals" matches it as one string; "one of" treats it as a list.'}
                  </div>
                )}
                {!f.filters.length && <p className="ex-empty">No filters. Every row the table exposes is in scope.</p>}
                {f.filters.map((x, i) => (
                  <FilterRow
                    key={i}
                    cols={cols}
                    col={x.col}
                    op={x.op}
                    val={x.val}
                    onChange={(patch) => set({ filters: f.filters.map((y, j) => (j === i ? { ...y, ...patch } : y)) })}
                    onRemove={() => set({ filters: f.filters.filter((_, j) => j !== i) })}
                  />
                ))}
              </div>

              <div>
                <div className="ex-rowhead">
                  <span className="ex-lab">Columns to return</span>
                  <span className="ex-meta">
                    {f.pick.length ? `${f.pick.length} picked` : 'All exposed columns'}
                    {f.pick.length > 0 && <button type="button" className="ex-link ex-ml" onClick={() => set({ pick: [] })}>Clear</button>}
                  </span>
                </div>
                <div className="ex-chips">
                  {cols.map((c) => (
                    <button key={c} type="button" aria-pressed={f.pick.includes(c)} className={`ex-chip ${f.pick.includes(c) ? 'on' : ''}`} onClick={() => set({ pick: f.pick.includes(c) ? f.pick.filter((x) => x !== c) : [...f.pick, c] })}>{c}</button>
                  ))}
                  {meta.loading && <span className="ex-meta ex-inline"><Spinner />Loading columns…</span>}
                </div>
                {meta.data?.hidden_columns?.length > 0 && <p className="ex-note ex-mt6">Hidden by this API: {meta.data.hidden_columns.join(', ')}</p>}
              </div>
            </div>
          </details>

          {meta.error && <InlineError error={meta.error} onRetry={() => meta.reload()} />}
        </section>

        <section className="ex-block">
          <Step n="4">Use it in your code</Step>
          {code}
        </section>
      </div>

      <div className="ex-right">
        <ResponsePanel
          res={res}
          onRetry={() => send(cursorAt)}
          stats={stats}
          preview={<RequestPreview method="GET" path={`/data-api/${api.slug}/${alias}/rows`} params={[...new URLSearchParams(buildQs('', cursorAt))]} apiKey={api.api_key} />}
          tools={res.status === 'done' && (
            <>
              {rows.length > 0 && <Seg value={view} onChange={setView} options={[['json', 'JSON'], ['table', 'Table']]} ariaLabel="Response view" />}
              {res.ok && (
                <span className="ex-pager">
                  <button type="button" className="ex-pbtn" onClick={() => send('', seeking ? undefined : 0)}>First</button>
                  <button
                    type="button"
                    className="ex-pbtn"
                    disabled={seeking ? !nextCursor : !(body?.has_more && nextOffset != null)}
                    onClick={() => (seeking ? send(nextCursor) : send('', nextOffset))}
                  >
                    Next <ArrowRight size={13} strokeWidth={1.8} aria-hidden="true" />
                  </button>
                </span>
              )}
            </>
          )}
        >
          {view === 'table' && rows.length ? (
            <div className="rtable">
              <table>
                <thead><tr>{keys.map((k) => <th key={k}>{k}</th>)}</tr></thead>
                <tbody>
                  {rows.slice(0, 200).map((r, i) => (
                    <tr key={i}>
                      {keys.map((k) => {
                        const v = r[k];
                        let cell = String(v);
                        if (v == null) cell = <span className="ex-null">null</span>;
                        else if (typeof v === 'object') cell = JSON.stringify(v);
                        return <td key={k}>{cell}</td>;
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
              {rows.length > 200 && <p className="ex-note ex-pad">Showing the first 200 of {rows.length} rows on this page.</p>}
            </div>
          ) : (
            <JsonView text={jsonText(res, 60000)} />
          )}
        </ResponsePanel>
      </div>
    </div>
  );
}

/* ------------------------------------------------- vector search / retrieve */

/* Stored embeddings are never generated, copied or written; text queries are
   embedded on the server with the model registered for the column. */
function VectorConsole({ api, alias, meta, mode, code }) {
  const toast = useToast();
  const m = meta.data;
  const vectorColumns = useMemo(() => m?.vector_columns || [], [m]);
  const allColumns = m?.columns || [];
  const retrieval = m?.retrieval || null;
  const maxTopK = m?.vector_max_top_k || 100;

  const [vectorColumn, setVectorColumn] = useState('');
  const [metric, setMetric] = useState('cosine');
  const [topK, setTopK] = useState('10');
  const [searchMode, setSearchMode] = useState('auto');
  const [inputKind, setInputKind] = useState('text');
  const [queryText, setQueryText] = useState('');
  const [vectorText, setVectorText] = useState('');
  const [columnsText, setColumnsText] = useState('');
  const [filters, setFilters] = useState([]);
  const [retrieveMode, setRetrieveMode] = useState('');
  const [res, setRes] = useState({ status: 'idle' });
  const ctrl = useRef(null);

  useEffect(() => {
    ctrl.current?.abort();
    setRes({ status: 'idle' });
    setFilters([]);
    setQueryText('');
    setVectorText('');
    setColumnsText('');
    setRetrieveMode('');
    setVectorColumn(vectorColumns[0]?.name || '');
    setTopK(String(Math.min(10, m?.vector_max_top_k || 10)));
    setInputKind(m?.text_query_columns?.length ? 'text' : 'vector');
  }, [alias, m, vectorColumns]);
  useEffect(() => { ctrl.current?.abort(); setRes({ status: 'idle' }); }, [mode]);
  useEffect(() => () => ctrl.current?.abort(), []);

  if (!m) return null;

  const isRetrieve = mode === 'retrieve' && !!retrieval;
  const selected = vectorColumns.find((c) => c.name === vectorColumn);
  const textAvailable = (m.text_query_columns || []).includes(vectorColumn);
  const useText = isRetrieve || (inputKind === 'text' && textAvailable);

  let parsed = { error: 'Paste the complete query embedding as a JSON numeric array.' };
  if (!useText && vectorText.trim()) {
    try {
      const v = JSON.parse(vectorText);
      if (!Array.isArray(v) || !v.length || !v.every((x) => typeof x === 'number' && Number.isFinite(x))) parsed = { error: 'Use a non-empty JSON array containing only finite numbers.' };
      else if (v.length > (m.vector_max_dimensions || 16000)) parsed = { error: 'The query vector is longer than this API allows.' };
      else parsed = { vector: v };
    } catch {
      parsed = { error: 'The query vector is not valid JSON.' };
    }
  }
  const mismatch = parsed.vector && selected?.dimensions != null && parsed.vector.length !== selected.dimensions;
  const zeroCos = metric === 'cosine' && parsed.vector?.every((x) => x === 0);
  const badTopK = !Number.isInteger(Number(topK)) || Number(topK) < 1 || Number(topK) > maxTopK;
  const needsIndex = !isRetrieve && searchMode === 'auto' && m.vector_requires_index && !selected?.ann_indexes?.some((i) => i.metric === metric);
  const ready = useText ? queryText.trim().length > 0 : (!!parsed.vector && !mismatch && !zeroCos);
  const sending = res.status === 'loading';
  const canSend = isRetrieve ? ready && !badTopK && !sending : !needsIndex && !!selected && ready && !badTopK && !sending;

  const readyFilters = filters.filter((f) => f.col && (NO_VALUE_OPS.includes(f.op) || f.val !== ''));
  const where = readyFilters.map((f) => `${f.col}:${f.op}${NO_VALUE_OPS.includes(f.op) ? '' : `:${f.val}`}`);
  const payload = isRetrieve
    ? { retrieval_queries: [{ query: queryText, k: Number(topK), ...(retrieveMode ? { mode: retrieveMode } : {}), ...(where.length ? { where } : {}) }], search_mode: searchMode }
    : {
      vector_column: vectorColumn,
      ...(useText ? { query_text: queryText } : { query_vector: parsed.vector || [] }),
      top_k: Number(topK),
      metric,
      search_mode: searchMode,
      ...(columnsText.trim() ? { columns: columnsText.split(',').map((c) => c.trim()).filter(Boolean) } : {}),
      ...(where.length ? { where } : {}),
    };
  const path = `/data-api/${enc(api.slug)}/${enc(alias)}/${isRetrieve ? 'retrieve' : 'vector-search'}`;

  const send = async () => {
    if (!canSend) return;
    ctrl.current?.abort();
    const c = new AbortController();
    ctrl.current = c;
    setRes({ status: 'loading' });
    const t0 = performance.now();
    try {
      const r = await apiFetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-API-Key': api.api_key },
        body: JSON.stringify(payload),
        signal: c.signal,
      });
      const text = await r.text();
      let json = null;
      try { json = JSON.parse(text); } catch { json = null; }
      const count = isRetrieve && Array.isArray(json?.results)
        ? json.results.reduce((n, x) => n + (Object.entries(x).find(([k, v]) => k !== 'k' && Array.isArray(v))?.[1]?.length || 0), 0)
        : json?.row_count;
      setRes({ status: 'done', http: r.status, ok: r.ok, ms: Math.round(performance.now() - t0), count, size: text.length, body: json ? JSON.stringify(json, null, 2) : text });
    } catch (e) {
      if (e?.name !== 'AbortError') setRes({ status: 'error', message: e?.message || 'Request failed' });
    }
  };

  const copyJson = async () => {
    const ok = await copyText(JSON.stringify(payload, null, 2));
    toast(ok ? 'Request JSON copied. The key is not in it.' : COPY_FAIL, ok ? 'ok' : 'error');
  };

  const vecHint = parsed.error
    || (mismatch ? `Dimension mismatch: ${parsed.vector.length} sent, ${selected.dimensions} expected.`
      : zeroCos ? 'Cosine search cannot use an all-zero vector.' : `${parsed.vector.length} dimensions.`);

  return (
    <div className="ex-split">
      <div className="ex-left">
        <section className="ex-block">
          <Step n="3" aside={<button type="button" className="ex-link" onClick={copyJson} disabled={!ready}>Copy request JSON</button>}>Ask and send</Step>

          <fieldset disabled={sending} className="ex-fs">
            {useText ? (
              <div>
                <label className="ex-lab" htmlFor="dax-vec-q">Your question</label>
                <textarea id="dax-vec-q" className="field ex-ask" rows={3} value={queryText} onChange={(e) => setQueryText(e.target.value)} placeholder="Ask in plain language, e.g. sterility failures in aseptic filling" />
                <p className="ex-note ex-mt6">
                  {isRetrieve
                    ? `Fuses meaning and keyword results (${retrieval.fusion}). score orders the results; dense_score and sparse_score are null when that search didn't return the item.`
                    : 'Embedded on the server with the model registered for this column.'}
                </p>
              </div>
            ) : (
              <div>
                <label className="ex-lab" htmlFor="dax-vec-v">Query embedding <span className="ex-faint">JSON array</span></label>
                <textarea id="dax-vec-v" className="field ex-ask" rows={4} spellCheck={false} value={vectorText} onChange={(e) => setVectorText(e.target.value)} placeholder="Paste the full array from the same model that built this column." />
                <p className={`ex-note ex-mt6 ${vectorText.trim() && (parsed.error || mismatch || zeroCos) ? 'bad' : ''}`}>{vecHint}</p>
              </div>
            )}

            <div className="ex-fields">
              {!isRetrieve && (
                <div>
                  <span className="ex-lab">Embedding column</span>
                  <Select
                    value={vectorColumn}
                    onChange={setVectorColumn}
                    options={vectorColumns.map((c) => ({ value: c.name, label: c.name, sub: `${c.type}${c.dimensions ? `, ${c.dimensions}d` : ''}` }))}
                    mono
                    ariaLabel="Embedding column"
                  />
                </div>
              )}
              {!isRetrieve && textAvailable && (
                <div>
                  <span className="ex-lab">Ask with</span>
                  <Select value={inputKind} onChange={setInputKind} options={[{ value: 'text', label: 'Text (server embeds)' }, { value: 'vector', label: 'Embedding (JSON array)' }]} ariaLabel="Query input" />
                </div>
              )}
              {!isRetrieve && (
                <div>
                  <span className="ex-lab">Metric</span>
                  <Select value={metric} onChange={setMetric} options={[{ value: 'cosine', label: 'Cosine' }, { value: 'l2', label: 'Euclidean (L2)' }, { value: 'inner_product', label: 'Inner product' }]} ariaLabel="Metric" />
                </div>
              )}
              {isRetrieve && (
                <div>
                  <span className="ex-lab">Retrieval mode</span>
                  <Select value={retrieveMode} onChange={setRetrieveMode} options={[{ value: '', label: `Default (${retrieval.default_mode})` }, ...retrieval.modes.map((x) => ({ value: x, label: x }))]} ariaLabel="Retrieval mode" />
                </div>
              )}
              <div>
                <label className="ex-lab" htmlFor="dax-vec-k">Results <span className="ex-faint">max {maxTopK}</span></label>
                <input id="dax-vec-k" className="field" inputMode="numeric" value={topK} onChange={(e) => setTopK(e.target.value.replace(/\D/g, ''))} />
              </div>
              <div>
                <span className="ex-lab">Execution</span>
                <Select value={searchMode} onChange={setSearchMode} options={[{ value: 'auto', label: 'Auto (existing index)' }, { value: 'exact', label: 'Exact (may scan)' }]} ariaLabel="Search execution" />
              </div>
            </div>

            {needsIndex && <div className="inline-warn">Auto needs a full-column ANN index for this metric. Pick Exact, or ask the database administrator to review the indexes.</div>}

            {!isRetrieve && (
              <div>
                <label className="ex-lab" htmlFor="dax-vec-cols">Columns to return <span className="ex-faint">optional, comma separated</span></label>
                <input id="dax-vec-cols" className="field" value={columnsText} onChange={(e) => setColumnsText(e.target.value)} placeholder="Blank returns the exposed non-bulk columns" />
              </div>
            )}

            <div>
              <div className="ex-rowhead">
                <span className="ex-lab">Filters <span className="ex-faint">all must match</span></span>
                <button type="button" className="ex-link" disabled={!allColumns.length || filters.length >= (m.max_filters || 10)} onClick={() => setFilters([...filters, { col: allColumns[0]?.name || '', op: 'eq', val: '' }])}><Plus size={13} strokeWidth={2} aria-hidden="true" />Add filter</button>
              </div>
              {!filters.length && <p className="ex-empty">No filters.</p>}
              {filters.map((f, i) => (
                <FilterRow
                  key={i}
                  cols={allColumns.map((c) => c.name)}
                  col={f.col}
                  op={f.op}
                  val={f.val}
                  onChange={(patch) => setFilters(filters.map((x, j) => (j === i ? { ...x, ...patch } : x)))}
                  onRemove={() => setFilters(filters.filter((_, j) => j !== i))}
                />
              ))}
            </div>

            <div className="ex-sendrow">
              <button type="button" className="ex-send big" onClick={send} disabled={!canSend}>
                {sending ? <><Spinner />Searching…</> : 'Send request'}
              </button>
              {badTopK
                ? <span className="ex-note bad">Results must be a whole number from 1 to {maxTopK}.</span>
                : (
                  <span className="ex-note">
                    {searchMode === 'auto' ? 'Auto may use an approximate index and can return fewer matches after filtering.' : 'Exact compares every eligible row. It can be expensive on large tables.'}
                    {' '}Null embeddings are skipped.
                  </span>
                )}
            </div>
          </fieldset>
        </section>

        <section className="ex-block">
          <Step n="4">Use it in your code</Step>
          {code}
        </section>
      </div>

      <div className="ex-right">
        <ResponsePanel
          res={res}
          onRetry={send}
          idleTitle="Ask a question first"
          preview={<RequestPreview method="POST" path={`/data-api/${api.slug}/${alias}/${isRetrieve ? 'retrieve' : 'vector-search'}`} apiKey={api.api_key} body={payload} />}
          stats={res.status === 'done' && `${res.ms} ms${typeof res.count === 'number' ? ` · ${res.count} matches` : ''} · ${bytes(res.size)}`}
        >
          <JsonView text={jsonText(res, 40000)} />
        </ResponsePanel>
      </div>
    </div>
  );
}

/* --------------------------------------------------------------- page */

export default function EndpointsTab({ api, params }) {
  const { nav } = usePage();
  const toast = useToast();
  const tables = api.tables || [];
  const alias = tables.some((t) => t.alias === params.alias) ? params.alias : tables[0]?.alias || '';
  const [pick, setPick] = useState('rows');
  const [lang, setLang] = useState('curl');

  const meta = useResource(
    (signal) => request(`/data-api/${enc(api.slug)}/${enc(alias)}/columns`, { signal, headers: { 'X-API-Key': api.api_key } }),
    [api.slug, alias, api.api_key], { enabled: !!alias, reset: true },
  );

  const t = tables.find((x) => x.alias === alias);
  const base = `${API_BASE_URL}/data-api/${api.slug}`;
  const defs = useMemo(() => {
    const out = [{ id: 'tables', group: 'Discover', name: 'List tables', m: 'GET', path: '/tables', desc: 'Every table this API exposes, with its source and row limit. Usually the first call a consumer makes.', url: `${base}/tables`, probe: `/data-api/${enc(api.slug)}/tables` }];
    if (!t) return out;
    const a = t.alias;
    out.push(
      { id: 'columns', group: 'Discover', name: 'Get columns', m: 'GET', path: `/${a}/columns`, desc: 'Column names and types. Enough to build a schema before paging rows.', url: `${base}/${a}/columns`, probe: `/data-api/${enc(api.slug)}/${enc(a)}/columns` },
      { id: 'count', group: 'Discover', name: 'Count rows', m: 'GET', path: `/${a}/count`, desc: 'Total rows, optionally filtered. Add column= next to search to scan one column, much faster on wide tables.', url: `${base}/${a}/count`, probe: `/data-api/${enc(api.slug)}/${enc(a)}/count` },
      { id: 'rows', group: 'Get data', name: 'Fetch rows', m: 'GET', path: `/${a}/rows`, desc: 'The data itself. Order is stable, so keep calling with offset = next_offset until has_more is false.', url: `${base}/${a}/rows`, params: { limit: t.row_limit, offset: 0 } },
    );
    if (meta.data?.vector_search) {
      const vc = meta.data.vector_columns?.[0]?.name || 'embedding';
      out.push({ id: 'vector', group: 'Search', name: 'Vector search', m: 'POST', path: `/${a}/vector-search`, desc: 'Finds the rows closest in meaning to your question, using an embedding column. Send plain text where a model is registered, or a vector.', url: `${base}/${a}/vector-search`, body: JSON.stringify({ vector_column: vc, query_text: 'your question', top_k: 10, metric: 'cosine' }) });
    }
    if (meta.data?.retrieval) {
      out.push({ id: 'retrieve', group: 'Search', name: 'Hybrid retrieve', m: 'POST', path: `/${a}/retrieve`, desc: 'Meaning plus keywords in one call, shaped by the response template. Best for question answering.', url: `${base}/${a}/retrieve`, body: JSON.stringify({ retrieval_queries: [{ query: 'your question', k: 5 }] }) });
    }
    return out;
  }, [t, base, api.slug, meta.data]);

  const sel = defs.find((d) => d.id === pick) || defs.find((d) => d.id === 'rows') || defs[0];
  const groups = defs.reduce((acc, d) => {
    (acc[d.group] = acc[d.group] || []).push(d);
    return acc;
  }, {});
  const noSearch = !meta.loading && meta.data && !meta.data.vector_search && !meta.data.retrieval;

  const copy = async (text, msg) => {
    const ok = await copyText(text);
    toast(ok ? msg : COPY_FAIL, ok ? 'ok' : 'error');
  };

  const code = sel && <CodeBlock e={sel} lang={lang} setLang={setLang} apiKey={api.api_key} />;

  return (
    <div className="epx">
      {!tables.length ? (
        <div className="ex-card ex-none">
          <b>No tables yet</b>
          <span>Add a table to this API and its endpoints show up here.</span>
        </div>
      ) : (
        <>
          <section className="ex-tablesec">
            <Step n="1" aside={<span className="ex-meta">{tables.length} table{tables.length === 1 ? '' : 's'}</span>}>Choose a table</Step>
            <div className="ex-tables" role="radiogroup" aria-label="Table">
              {tables.map((x) => {
                const on = x.alias === alias;
                return (
                  <button
                    key={x.alias}
                    type="button"
                    role="radio"
                    aria-checked={on}
                    className={`ex-tcard ${on ? 'on' : ''}`}
                    onClick={() => nav.set({ alias: x.alias }, { replace: true })}
                  >
                    <span className="ex-tradio" aria-hidden="true" />
                    <span className="ex-tname">/{x.alias}</span>
                    {x.row_limit != null && <span className="ex-tsub">{Number(x.row_limit).toLocaleString()} rows/call</span>}
                  </button>
                );
              })}
            </div>
          </section>

          <section className="ex-explorer">
            <nav className="ex-nav" aria-label="Endpoints">
              <Step n="2">Pick an endpoint</Step>
              {Object.entries(groups).map(([g, list]) => (
                <div key={g} className="ex-navgroup">
                  <span className="ex-navlabel">{g}</span>
                  <div className="ex-navlist">
                  {list.map((d) => (
                    <button
                      key={d.id}
                      type="button"
                      className={`ex-navitem ${sel?.id === d.id ? 'on' : ''}`}
                      aria-current={sel?.id === d.id ? 'true' : undefined}
                      onClick={() => setPick(d.id)}
                    >
                      <Method m={d.m} />
                      <span className="ex-navtext">
                        <b>{d.name}</b>
                        <small>{d.path}</small>
                      </span>
                      <ChevronRight className="ex-navarrow" size={15} strokeWidth={1.8} aria-hidden="true" />
                    </button>
                  ))}
                  </div>
                </div>
              ))}
              {meta.loading && <div className="ex-navnote"><Spinner />Checking for search endpoints…</div>}
              {noSearch && (
                <div className="ex-navnote">
                  Search endpoints need an embedding column.
                  {meta.data.vector_metadata_error ? ` ${meta.data.vector_metadata_error}` : ' They work on PostgreSQL tables with pgvector.'}
                </div>
              )}
            </nav>

            {sel && (
              <div className="ex-detail">
                <div className="ex-dhead">
                  <span className="ex-dgroup">{sel.group}</span>
                  <h3 className="ex-dtitle">{sel.name}</h3>
                  <p className="ex-ddesc">{sel.desc}</p>
                  <div className="ex-route">
                    <Method m={sel.m} />
                    <code title={sel.url}><span className="ex-routebase">{base}</span>{sel.path}</code>
                    <button type="button" className="ex-iconbtn" onClick={() => copy(sel.url, 'Endpoint URL copied.')} aria-label="Copy endpoint URL"><CopyIcon /></button>
                  </div>
                </div>

                {['tables', 'columns', 'count'].includes(sel.id) && <SimpleRunner key={`${alias}-${sel.id}`} api={api} e={sel} code={code} />}
                {sel.id === 'rows' && <RowsConsole key={alias} api={api} alias={alias} meta={meta} code={code} />}
                {['vector', 'retrieve'].includes(sel.id) && <VectorConsole key={alias} api={api} alias={alias} meta={meta} mode={sel.id === 'retrieve' ? 'retrieve' : 'vector-search'} code={code} />}
              </div>
            )}
          </section>

          <section className="ex-card ex-browse">
            <div className="ex-browse-text">
              <b>Prefer clicking around?</b>
              <span>Open any table as a paginated page in your browser. Needs a SmartHub login, the key alone is not enough.</span>
            </div>
            <div className="ex-browse-links">
              {tables.map((x) => (
                <a key={x.alias} className="ex-blink" href={tableUiUrl(api.slug, x.alias, api.api_key)} target="_blank" rel="noreferrer">
                  /{x.alias}<ExternalLink size={13} strokeWidth={1.8} aria-hidden="true" />
                </a>
              ))}
            </div>
          </section>
        </>
      )}
    </div>
  );
}