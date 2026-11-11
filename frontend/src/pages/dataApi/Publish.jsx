import React, { useEffect, useMemo, useRef, useState } from 'react';
import { ADDR_RE, RecipientField, splitAddresses } from '../components/CommunicationParts';
import { ArrowLeft, ArrowRight, Bold, Check, ChevronDown, ChevronRight, CircleAlert, CircleCheck, Copy, Database, ExternalLink, FileText, Heading, Italic, KeyRound, Layers, Link2, List, ListOrdered, Lock, Mail, Paperclip, Sparkles, Table2, Trash2, X } from 'lucide-react';
import { useSearchParams } from 'react-router-dom';
import { buildConsumerDoc, downloadTextFile, tableUiUrl, tableUrl } from './dataApiDocs';
import { aliasFromTable, baseUrl, bytes, cleanAlias, copyText, emptyPick, guideFilename, maskKey, pickReady, pickToSpec, plural, request, slugify, tablePath, useConfirm, usePage, useResource, useScramble, useToast } from './lib';
import { Drawer, DrawerHead, ErrorBox, Orb, RowLimitField, Seg, Sk, SourceLogo, Spinner, Steps } from './ui';
import IndexBuilder from './IndexBuilder';

const ENV_ORDER = ['dev', 'qa', 'prod'];
const qs = (o) => new URLSearchParams(Object.entries(o).filter(([, v]) => v)).toString();


/* Icon for each level of the drill-down browser. */
function ColIcon({ title }) {
  const t = (title || '').toUpperCase();
  const props = { size: 14, strokeWidth: 1.8, className: 'bico', 'aria-hidden': true };
  if (t === 'DATABASE' || t === 'BUCKET') return <Database {...props} />;
  if (t === 'SCHEMA') return <Layers {...props} />;
  if (t === 'OBJECT') return <FileText {...props} />;
  return <Table2 {...props} />;
}

/* One column of the drill-down browser: a filterable list with its own
   loading and error state. */
function BrowserColumn({ title, res, value, onPick, emptyText, used, renderMeta }) {
  const [q, setQ] = useState('');
  const items = useMemo(() => {
    const list = res?.data || [];
    return q ? list.filter((x) => x.toLowerCase().includes(q.toLowerCase())) : list;
  }, [res?.data, q]);
  return (
    <div className="bcol">
      <div className="bh"><span>{title}</span><span>{res?.data ? res.data.length : ''}</span></div>
      <input className="bf" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter…" aria-label={`Filter ${title.toLowerCase()}`} disabled={!res} />
      <div className="bl" role="listbox" aria-label={title}>
        {!res && <div className="bempty">{emptyText}</div>}
        {res?.loading && !res.data && [0, 1, 2, 3].map((i) => <div key={i} style={{ padding: '9px 10px' }}><Sk h={12} w={`${60 + ((i * 13) % 30)}%`} /></div>)}
        {res?.error && (
          <div className="bempty" style={{ color: 'var(--red)' }}>
            {res.error.message}
            <br />
            <button type="button" className="link" onClick={() => res.reload()}>Try again</button>
          </div>
        )}
        {res?.data && !items.length && <div className="bempty">{q ? 'No match' : 'Nothing here'}</div>}
        {items.map((x) => (
          <button
            key={x}
            type="button"
            role="option"
            aria-selected={x === value}
            className={`bi ${x === value ? 'on' : ''}`}
            onClick={() => onPick(x)}
          >
            <ColIcon title={title} /> <span className="nm">{x}</span>
            {used?.(x) ? <span className="used">on this API</span> : renderMeta ? <span className="used">{renderMeta(x)}</span> : <span className="ch"><ChevronRight size={14} strokeWidth={1.8} aria-hidden="true" /></span>}
          </button>
        ))}
      </div>
    </div>
  );
}

/* Source → environment → database → schema → table, then the alias, page size
   and which columns it exposes. Calls onChange with everything needed to add
   the table, or null while it isn't ready. */
export function TablePicker({ value, onChange, takenAliases, slugPreview, keepAlias = false }) {
  const { sources, can } = usePage();
  const P = value;
  const set = (patch) => onChange({ ...P, ...patch });
  // When editing an existing alias, moving to another table keeps its name.
  const nextAlias = (fallback) => (keepAlias ? P.alias : fallback);
  const src = (sources.data || []).find((s) => s.id === P.source);
  const levels = src?.levels || [];
  const envs = ENV_ORDER.filter((e) => (src?.environments || []).includes(e));
  const needDb = levels.includes('database');
  const needSchema = levels.includes('schema');

  const dbs = useResource(
    (signal) => request(`/data-api/source-databases?${qs({ source_system: P.source, env: P.env })}`, { signal }).then((d) => d?.databases || []),
    [P.source, P.env], { enabled: !!P.source && needDb, reset: true },
  );
  const schemas = useResource(
    (signal) => request(`/data-api/source-schemas?${qs({ source_system: P.source, env: P.env, database: P.database })}`, { signal }).then((d) => d?.schemas || []),
    [P.source, P.env, P.database], { enabled: !!P.source && needSchema && (!needDb || !!P.database), reset: true },
  );
  const tables = useResource(
    (signal) => request(`/data-api/source-tables?${qs({ source_system: P.source, env: P.env, database: P.database, schema: P.schema })}`, { signal }).then((d) => d?.tables || []),
    [P.source, P.env, P.database, P.schema], { enabled: !!P.source && (!needDb || !!P.database) && (!needSchema || !!P.schema), reset: true },
  );
  const cols = useResource(
    (signal) => request(`/data-api/source-columns?${qs({ source_system: P.source, env: P.env, database: P.database, schema: P.schema, table: P.table })}`, { signal }).then((d) => d?.columns || []),
    [P.source, P.env, P.database, P.schema, P.table], { enabled: !!P.table, reset: true },
  );

  /* Everything starts ticked once the columns arrive: an alias exposing the
     whole table is what a consumer expects unless someone narrows it. */
  const colList = cols.data;
  const colState = !P.table ? '' : cols.loading ? 'loading' : cols.error ? 'error' : colList ? 'done' : 'loading';
  useEffect(() => {
    if (P.colState === colState && P.cols === (colList || null)) return;
    onChange({
      ...P,
      colState,
      cols: colList || null,
      picked: colList ? (P.picked ?? colList.map((c) => c.name)) : P.picked,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [colState, colList]);

  const alias = P.alias.trim();
  const aliasTaken = !!alias && takenAliases.includes(alias.toLowerCase());
  const bulkOn = (colList || []).filter((c) => c.bulk && (P.picked || []).includes(c.name));
  const lbl = { database: P.source === 's3' ? 'BUCKET' : 'DATABASE', schema: 'SCHEMA', table: P.source === 's3' ? 'OBJECT' : 'TABLE' };
  const cascade = [needDb && 'database', needSchema && 'schema', 'table'].filter(Boolean);

  const sec = (n, title, on, body) => (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12, opacity: on ? 1 : 0.45, transition: 'opacity .3s' }}>
      <div className="row" style={{ gap: 10 }}>
        <span style={{ fontFamily: 'JohnsonDisplay', fontStyle: 'italic', fontWeight: 300, fontSize: 22, color: on ? 'var(--red)' : 'var(--faint)' }}>{n}</span>
        <span style={{ fontWeight: 700, fontSize: 14 }}>{title}</span>
      </div>
      {on && body}
    </div>
  );

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 24 }}>
      {sec('01', 'Source system', true, (
        <>
          {sources.loading && !sources.data && <div className="srcgrid">{[0, 1, 2, 3].map((i) => <Sk key={i} h={96} r={16} />)}</div>}
          {sources.error && !sources.data && <ErrorBox error={sources.error} onRetry={() => sources.reload()} title="Couldn't list the source systems" />}
          {sources.data && (
            <div className="srcgrid">
              {sources.data.map((s) => (
                <button
                  key={s.id}
                  type="button"
                  className={`srccard ${P.source === s.id ? 'on' : ''}`}
                  disabled={!s.configured}
                  style={!s.configured ? { opacity: 0.45, cursor: 'not-allowed' } : undefined}
                  data-tip={s.pending ? 'Checking the connection' : !s.configured ? 'Not configured on this server' : undefined}
                  onClick={() => onChange({ ...emptyPick(), source: s.id, env: (s.environments || []).includes('prod') ? 'prod' : (s.environments || [])[0] || 'prod', ...(keepAlias ? { alias: P.alias, limit: P.limit } : {}) })}
                >
                  <SourceLogo id={s.id} size={34} />
                  <b>{s.label}</b>
                  <span>{s.pending ? 'checking…' : s.configured ? (s.environments || []).map((e) => e.toUpperCase()).join(' · ') || 'single environment' : 'not configured'}</span>
                </button>
              ))}
            </div>
          )}
        </>
      ))}

      {sec('02', 'Find the table', !!P.source, (
        <>
          {envs.length > 1 && (
            <div className="row" style={{ gap: 10 }}>
              <span style={{ fontSize: 12, color: 'var(--muted)' }}>Environment</span>
              <Seg value={P.env} onChange={(e) => onChange({ ...P, env: e, database: '', schema: '', table: '', alias: nextAlias(''), picked: null })} options={envs.map((e) => [e, e.toUpperCase()])} btnStyle={{ padding: '6px 14px' }} ariaLabel="Environment" />
            </div>
          )}
          <div className="browser" style={{ gridTemplateColumns: `repeat(${cascade.length},minmax(0,1fr))` }}>
            {needDb && (
              <BrowserColumn title={lbl.database} res={dbs} value={P.database} emptyText="" onPick={(x) => set({ database: x, schema: '', table: '', alias: nextAlias(''), picked: null })} />
            )}
            {needSchema && (
              <BrowserColumn
                title={lbl.schema}
                res={!needDb || P.database ? schemas : null}
                value={P.schema}
                emptyText="Pick the level on the left first"
                onPick={(x) => set({ schema: x, table: '', alias: nextAlias(''), picked: null })}
              />
            )}
            <BrowserColumn
              title={lbl.table}
              res={(!needDb || P.database) && (!needSchema || P.schema) ? tables : null}
              value={P.table}
              emptyText="Pick the levels on the left first"
              used={(x) => takenAliases.includes(aliasFromTable(x))}
              renderMeta={() => ''}
              onPick={(x) => set({ table: x, alias: nextAlias(aliasFromTable(x)), picked: null })}
            />
          </div>
        </>
      ))}

      {sec('03', 'Shape the endpoint', !!P.table, (
        <>
          <div className="cfg">
            <div>
              <label className="lab" htmlFor="dax-pk-alias">Alias, used in the URL</label>
              <input id="dax-pk-alias" className="field mono" value={P.alias} onChange={(e) => set({ alias: cleanAlias(e.target.value) })} placeholder="e.g. customers" style={{ fontSize: 14 }} maxLength={64} />
              {aliasTaken
                ? <div className="hint" style={{ color: 'var(--red)' }}>That alias is already used on this API.</div>
                : !alias
                  ? <div className="hint" style={{ color: 'var(--red)' }}>An alias is required.</div>
                  : <div className="hint"><code>/data-api/{slugPreview}/<b style={{ color: 'var(--red)', fontWeight: 400 }}>{alias}</b>/rows</code></div>}
            </div>
            <div>
              <label className="lab" htmlFor="dax-pk-limit">Rows per page</label>
              <RowLimitField id="dax-pk-limit" value={Number(P.limit)} onChange={(n) => set({ limit: n })} />
              <div className="hint">Caps one call. Callers page through the rest with offset.</div>
            </div>
          </div>
          <ColumnChooser cols={cols} picked={P.picked} onPick={(picked) => set({ picked })} bulkOn={bulkOn} />
        </>
      ))}

      {P.source === 'postgresql' && P.table && can('viewPGindexer') && sec('04', 'Make it fast', true, (
        <IndexBuilder key={[P.env, P.database, P.schema, P.table].join('\u0001')} env={P.env} database={P.database} schema={P.schema} table={P.table} />
      ))}
    </div>
  );
}

export function ColumnChooser({ cols, picked, onPick, bulkOn, before }) {
  const list = cols.data || [];
  const sel = picked || [];
  return (
    <div>
      <div className="between" style={{ alignItems: 'center', marginBottom: 8 }}>
        <span className="lab" style={{ margin: 0 }}>Columns exposed</span>
        {cols.data && (
          <div className="row" style={{ gap: 12 }}>
            <button type="button" className="link" onClick={() => onPick(list.map((c) => c.name))}>all</button>
            <button type="button" className="link" onClick={() => onPick(list.filter((c) => !c.bulk).map((c) => c.name))} data-tip="Untick embeddings, blobs and arrays">drop bulk</button>
            <span style={{ fontSize: 11, color: 'var(--muted)' }}>{sel.length} of {list.length}</span>
          </div>
        )}
      </div>
      {cols.loading && !cols.data && <div className="row" style={{ gap: 8, fontSize: 12, color: 'var(--muted)' }}><Spinner />Reading columns…</div>}
      {cols.error && (
        <div className="inline-warn">
          Couldn&apos;t read the column list ({cols.error.message}). The alias will expose every column the source has.{' '}
          <button type="button" className="link" onClick={() => cols.reload()}>Try again</button>
        </div>
      )}
      {cols.data && (
        <>
          <div className="row" style={{ flexWrap: 'wrap', gap: 6 }}>
            {list.map((c) => {
              const on = sel.includes(c.name);
              const was = before ? before.includes(c.name) : on;
              return (
                <button
                  key={c.name}
                  type="button"
                  aria-pressed={on}
                  className={`colchip ${on ? 'on' : ''} ${c.bulk ? 'bulk' : ''} ${on !== was ? (on ? 'add' : 'del') : ''}`}
                  data-tip={`${c.type}${c.bulk ? ' · large values' : ''}`}
                  onClick={() => onPick(on ? sel.filter((x) => x !== c.name) : [...sel, c.name])}
                >
                  {on !== was ? (on ? '+ ' : '− ') : ''}{c.name}
                  {c.bulk && <span style={{ display: 'inline-block', width: 5, height: 5, borderRadius: '50%', background: 'currentColor', marginLeft: 6, verticalAlign: 2 }} />}
                </button>
              );
            })}
          </div>
          <div className="hint">
            {!sel.length && <span style={{ color: 'var(--red)' }}>Pick at least one column. </span>}
            {bulkOn.length
              ? <span style={{ color: 'var(--red)' }}>{bulkOn.map((c) => c.name).join(', ')} hold large values, so every page carries them.</span>
              : 'Unticked columns cannot be read or searched by any caller.'}
          </div>
        </>
      )}
    </div>
  );
}

const TITLES = [
  <>What is this API <em>for?</em></>,
  <>Which tables does it <em>expose?</em></>,
  <>Review and <em>publish.</em></>,
];
const LAUNCH = ['Validating tables against the source', 'Issuing the API key', 'Warming endpoints', 'Going live'];

/* The server breaks a slug tie by adding -2, -3 … so show the URL it will
   actually get. */
const freeSlug = (base, taken) => {
  if (!taken.includes(base)) return base;
  let n = 2;
  while (taken.includes(`${base}-${n}`)) n += 1;
  return `${base}-${n}`;
};

function Receipt({ name, slug, tables }) {
  return (
    <aside className="receipt">
      <span className="kicker" style={{ color: 'var(--blue)', position: 'relative' }}>LIVE PREVIEW</span>
      <span style={{ fontFamily: 'JohnsonDisplay', fontSize: 26, lineHeight: 1.1, wordBreak: 'break-word', position: 'relative' }}>{name.trim() || 'Untitled API'}</span>
      <code style={{ fontSize: 12, color: 'var(--blue)', wordBreak: 'break-all', position: 'relative' }}>/data-api/{slug}</code>
      <div style={{ height: 1, background: 'var(--line)' }} />
      <span style={{ fontSize: 10, letterSpacing: '.2em', color: '#a39992' }}>ENDPOINTS</span>
      {tables.length ? tables.map((t) => (
        <div key={t.alias} className="row" style={{ gap: 8, fontSize: 12, animation: 'dax-rise .3s both', position: 'relative' }}>
          <SourceLogo id={t.source_system} size={16} />
          <code style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>/{t.alias}</code>
          <span style={{ color: '#a39992' }}>{(t.env || '').toUpperCase()}</span>
        </div>
      )) : <span style={{ fontSize: 12, color: '#81766f' }}>No tables picked yet</span>}
      <div style={{ height: 1, background: 'var(--line)' }} />
      <div className="between" style={{ fontSize: 12, color: 'var(--ink2)', position: 'relative' }}><span>Key</span><code>sh_live_•••• on publish</code></div>
      <div className="between" style={{ fontSize: 12, color: 'var(--ink2)', position: 'relative' }}><span>Auth</span><span>api_key or X-API-Key</span></div>
      <div className="between" style={{ fontSize: 12, color: 'var(--ink2)', position: 'relative' }}><span>Access</span><span>read-only</span></div>
    </aside>
  );
}

export default function CreateWizard() {
  const { overview, nav, refreshOverview, showHandoff } = usePage();
  const toast = useToast();
  const confirm = useConfirm();
  const [sp] = useSearchParams();
  const [name, setName] = useState('');
  const [desc, setDesc] = useState('');
  const [tables, setTables] = useState([]);
  const [P, setP] = useState(emptyPick);
  const [consent, setConsent] = useState(false);
  const [publishing, setPublishing] = useState(null);
  const [err, setErr] = useState(null);
  const nameRef = useRef(null);

  const takenSlugs = (overview.data?.apis || []).map((a) => a.slug);
  const base = slugify(name);
  const slug = freeSlug(base, takenSlugs);
  const clash = name.trim() && base !== slug;

  const reachable = !name.trim() ? 1 : !tables.length ? 2 : 3;
  const asked = Number(sp.get('step')) || 1;
  const step = Math.max(1, Math.min(asked, reachable, 3));

  /* A step you can't be on yet (a reload drops the draft) falls back to the
     furthest one you can. */
  useEffect(() => {
    if (asked !== step) nav.set({ step: step === 1 ? null : step }, { replace: true });
  }, [asked, step, nav]);

  useEffect(() => { if (step === 1) nameRef.current?.focus(); }, [step]);

  const dirty = !!(name.trim() || desc.trim() || tables.length || P.source);
  const goto = (n) => nav.set({ step: n === 1 ? null : n });
  const canNext = step === 1 ? !!name.trim() : step === 2 ? tables.length > 0 : consent && !publishing;
  const taken = tables.map((t) => t.alias.toLowerCase());
  const pickOk = pickReady(P, taken);

  const cancel = async () => {
    if (dirty && !publishing) {
      const ok = await confirm({ title: 'Discard this draft?', text: 'Nothing has been published yet. The name and tables you picked will be lost.', okLabel: 'Discard', danger: true, icon: <Trash2 size={20} strokeWidth={1.8} aria-hidden="true" /> });
      if (!ok) return;
    }
    nav.home();
  };

  const addPicked = () => {
    if (!pickOk) return;
    const spec = pickToSpec(P);
    setTables((t) => [...t, spec]);
    setP((x) => ({ ...emptyPick(), source: x.source, env: x.env, database: x.database, schema: x.schema }));
    toast(`/${spec.alias} added`);
  };

  const publish = async () => {
    if (!consent || publishing) return;
    setErr(null);
    setPublishing({ at: 0 });
    try {
      const created = await request('/data-api/create', { method: 'POST', body: { name: name.trim(), description: desc.trim(), tables } });
      for (let i = 1; i <= LAUNCH.length; i += 1) {
        setPublishing({ at: i });
        await new Promise((r) => setTimeout(r, 320));
      }
      refreshOverview();
      showHandoff(created, { slug: created.slug, rotated: false });
    } catch (e) {
      setPublishing(null);
      setErr(e);
      toast(`Couldn't publish: ${e.message}`, 'error');
    }
  };

  const next = () => {
    if (step === 1 && !name.trim()) { toast('Give the API a name first', 'error'); return; }
    if (step === 2 && !tables.length) { toast('Add at least one table', 'error'); return; }
    if (step === 3 && !consent) { toast('Tick the box to confirm first', 'error'); return; }
    if (step === 3) { publish(); return; }
    goto(step + 1);
  };

  const steps = [
    ['01', 'Details', 'Name and purpose'],
    ['02', 'Tables', tables.length ? plural(tables.length, 'alias', 'aliases') : 'Pick what to expose'],
    ['03', 'Review', 'Check and publish'],
  ];

  return (
    <div className="ws">
      <div className="wsmain">
        <div className="texture" />
        <div className="g1" />
        <div className="g2" />

        {publishing && (
          <div className="launch2" role="status" aria-live="polite">
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 26, textAlign: 'center' }}>
              <Orb size={150} inset={34}><span style={{ fontFamily: 'JohnsonDisplay', fontSize: 20, color: 'var(--red)' }}>API</span></Orb>
              <span style={{ fontFamily: 'JohnsonDisplay', fontSize: 30 }}>{name.trim()}</span>
              <Steps steps={LAUNCH} at={publishing.at} />
            </div>
          </div>
        )}

        <div className="dh" style={{ paddingBottom: 4 }}>
          <div className="between" style={{ alignItems: 'center' }}>
            <nav className="crumbs" aria-label="Breadcrumb">
              <b>GRA SMARTHUB</b><span>/</span>
              <button type="button" className="tlink c" onClick={cancel}>DATA API EXPORT</button><span>/</span>
              <span style={{ color: 'var(--ink)' }}>NEW API</span>
            </nav>
            <button type="button" className="btn" onClick={cancel} disabled={!!publishing}>Cancel</button>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            <span className="kicker">STEP {step} OF 3</span>
            <h1 className="h-l" style={{ fontSize: 'clamp(34px,3.6vw,50px)' }}>{TITLES[step - 1]}</h1>
          </div>
        </div>

        <div className="cw">
          <div className="cleft">
            <nav className="vstep" aria-label="Steps">
              {steps.map(([n, l, s], i) => (
                <button
                  key={n}
                  type="button"
                  className={`vs ${step === i + 1 ? 'on' : ''} ${step > i + 1 ? 'done' : ''}`}
                  aria-current={step === i + 1 ? 'step' : undefined}
                  disabled={i + 1 > reachable || !!publishing}
                  onClick={() => goto(i + 1)}
                >
                  <span className="vn">{n}</span>
                  <span><b>{l}</b><span>{s}</span></span>
                </button>
              ))}
            </nav>
            <Receipt name={name} slug={slug} tables={tables} />
          </div>

          <div className="cwbody" style={{ display: 'flex', flexDirection: 'column', gap: 16, minWidth: 0, animation: 'dax-rise .4s both' }} key={step}>
            {step === 1 && (
              <div className="panel" style={{ padding: '30px 32px', gap: 26 }}>
                <div>
                  <label className="lab" htmlFor="dax-w-name">API name</label>
                  <input
                    id="dax-w-name"
                    ref={nameRef}
                    className="bigin"
                    value={name}
                    maxLength={120}
                    placeholder="e.g. Shipment Tracking"
                    autoComplete="off"
                    onChange={(e) => setName(e.target.value)}
                    onKeyDown={(e) => { if (e.key === 'Enter') next(); }}
                  />
                  <div className="hint" style={{ fontSize: 12 }}>
                    {!name.trim()
                      ? 'The URL is made from the name and never changes afterwards.'
                      : clash
                        ? <><span style={{ color: '#9e3a00' }}>/data-api/{base} is taken,</span> so this one lives at <code>/data-api/{slug}</code></>
                        : <><span style={{ color: 'var(--green)', display: 'inline-flex' }}><CircleCheck size={14} strokeWidth={1.8} aria-hidden="true" /></span> Lives at <code>/data-api/{slug}</code></>}
                  </div>
                </div>
                <div>
                  <label className="lab" htmlFor="dax-w-desc">What it&apos;s for, and who gets the key</label>
                  <textarea id="dax-w-desc" className="field" rows={3} maxLength={1000} value={desc} onChange={(e) => setDesc(e.target.value)} placeholder="Quarterly territory pulls for commercial analytics" style={{ fontSize: 14, padding: '12px 14px', resize: 'vertical' }} />
                  <div className="hint">Shows on the API&apos;s page and in the consumer guide.</div>
                </div>
              </div>
            )}

            {step === 2 && (
              <>
                <div className="panel" style={{ padding: '26px 28px' }}>
                  <TablePicker value={P} onChange={setP} takenAliases={taken} slugPreview={slug} />
                  <div className="row">
                    <button type="button" className="btn dark" style={{ padding: '11px 16px' }} onClick={addPicked} disabled={!pickOk}>
                      {P.colState === 'loading' ? <><Spinner />Reading columns…</> : `+ Add /${P.alias.trim() || 'alias'}`}
                    </button>
                  </div>
                </div>
                <div className="panel">
                  <div className="ptl"><span className="t" style={{ fontSize: 17 }}>Added to this API</span><span className="s">{plural(tables.length, 'alias', 'aliases')}</span></div>
                  {tables.map((t, i) => (
                    <div key={t.alias} className="row" style={{ padding: '10px 12px', borderRadius: 12, background: 'var(--bg)', animation: 'dax-rise .35s both' }}>
                      <SourceLogo id={t.source_system} size={22} />
                      <code style={{ fontSize: 13, width: 150, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>/{t.alias}</code>
                      <span style={{ flex: 1, fontSize: 12, color: 'var(--ink2)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{tablePath({ ...t, schema: t.db_schema })}</span>
                      <span style={{ fontSize: 11, color: 'var(--muted)', whiteSpace: 'nowrap' }}>{t.row_limit.toLocaleString()} / page · {t.columns.length ? `${t.columns.length} cols` : 'all cols'}</span>
                      <button type="button" className="link" style={{ color: 'var(--muted)' }} onClick={() => setTables(tables.filter((_, j) => j !== i))} aria-label={`Remove /${t.alias}`}><X size={14} strokeWidth={1.8} aria-hidden="true" /></button>
                    </div>
                  ))}
                  {!tables.length && <span className="hint" style={{ margin: 0 }}>Nothing yet. Add at least one table above.</span>}
                </div>
              </>
            )}

            {step === 3 && (
              <div className="panel" style={{ padding: '28px 30px', gap: 20 }}>
                <div style={{ display: 'grid', gridTemplateColumns: '140px 1fr', gap: '12px 18px', fontSize: 13.5 }}>
                  <span style={{ color: 'var(--muted)' }}>Name</span><span>{name.trim()}</span>
                  <span style={{ color: 'var(--muted)' }}>Description</span><span style={{ color: 'var(--ink2)' }}>{desc.trim() || 'No description'}</span>
                  <span style={{ color: 'var(--muted)' }}>Base URL</span><code style={{ color: 'var(--blue)', wordBreak: 'break-all' }}>{baseUrl(slug)}</code>
                  <span style={{ color: 'var(--muted)' }}>Key</span><span style={{ color: 'var(--ink2)' }}>One key, issued when you publish. Rotate it any time.</span>
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                  {tables.map((t) => (
                    <div key={t.alias} className="row" style={{ gap: 12, padding: '12px 14px', borderRadius: 12, background: 'var(--bg)' }}>
                      <span className="method get" style={{ minWidth: 0 }}>GET</span>
                      <code style={{ fontSize: 13, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>/data-api/{slug}/{t.alias}/rows</code>
                      <SourceLogo id={t.source_system} size={20} />
                      <span style={{ fontSize: 12, color: 'var(--muted)' }}>{(t.env || '').toUpperCase()}</span>
                    </div>
                  ))}
                </div>
                <label className="consent">
                  <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
                  <span>I understand these endpoints are reachable <b style={{ fontWeight: 700, color: 'var(--ink)' }}>without a SmartHub login</b>. Anyone with the key can read the aliases above, read-only.</span>
                </label>
                {err && <div className="inline-err" role="alert"><span><CircleAlert size={15} strokeWidth={1.8} aria-hidden="true" /></span><span><b>Not published.</b> {err.message}</span></div>}
              </div>
            )}

            <div className="between">
              <button type="button" className="btn" style={{ padding: '12px 18px', fontSize: 13, borderRadius: 11, visibility: step > 1 ? 'visible' : 'hidden' }} onClick={() => goto(step - 1)} disabled={!!publishing}><ArrowLeft size={14} strokeWidth={1.8} aria-hidden="true" /> Back</button>
              <button
                type="button"
                className="cta"
                style={{ padding: '13px 14px 13px 22px', fontSize: 14, ...(canNext ? {} : { background: 'var(--line3)', boxShadow: 'none', cursor: 'not-allowed' }) }}
                aria-disabled={!canNext}
                onClick={next}
              >
                {step === 3 ? (publishing ? 'Publishing…' : 'Publish API') : 'Continue'} <span className="arr"><ArrowRight size={14} strokeWidth={1.8} aria-hidden="true" /></span>
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function Confetti() {
  const ref = useRef(null);
  useEffect(() => {
    const cv = ref.current;
    if (!cv || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return undefined;
    const ctx = cv.getContext('2d');
    const W = (cv.width = cv.clientWidth);
    const H = (cv.height = cv.clientHeight);
    const cols = ['#eb1700', '#0f68b2', '#8c3bbb', '#ff6017', '#328714', '#69d0ff', '#fbe058'];
    const ps = Array.from({ length: 160 }, () => ({
      x: W / 2 + (Math.random() - 0.5) * 120, y: H * 0.38, vx: (Math.random() - 0.5) * 14, vy: -Math.random() * 13 - 4,
      r: Math.random() * 6 + 3, c: cols[Math.floor(Math.random() * cols.length)], a: Math.random() * 6, va: (Math.random() - 0.5) * 0.3,
    }));
    let f = 0;
    let raf = 0;
    const tick = () => {
      if (f > 220) return;
      f += 1;
      ctx.clearRect(0, 0, W, H);
      ps.forEach((p) => {
        p.vy += 0.32;
        p.vx *= 0.99;
        p.x += p.vx;
        p.y += p.vy;
        p.a += p.va;
        ctx.save();
        ctx.translate(p.x, p.y);
        ctx.rotate(p.a);
        ctx.globalAlpha = Math.max(0, 1 - f / 220);
        ctx.fillStyle = p.c;
        ctx.fillRect(-p.r / 2, -p.r / 4, p.r, p.r / 2);
        ctx.restore();
      });
      raf = requestAnimationFrame(tick);
    };
    const start = setTimeout(tick, 150);
    return () => {
      clearTimeout(start);
      cancelAnimationFrame(raf);
    };
  }, []);
  return <canvas ref={ref} className="confetti" aria-hidden="true" />;
}

/* Shown once, straight after a key is issued (publish or rotate). Not
   addressable: a reload or a shared link lands on the API page instead. */
export function KeyHandoff({ payload, onDone }) {
  const { emailKey } = usePage();
  const toast = useToast();
  const [stored, setStored] = useState(false);
  const [copied, setCopied] = useState(false);
  const [settled, setSettled] = useState(false);
  const key = useScramble(settled ? payload.api_key : maskKey(payload.api_key), settled);
  const tables = payload.tables || [];

  useEffect(() => {
    const t = setTimeout(() => setSettled(true), 450);
    return () => clearTimeout(t);
  }, []);

  /* Leaving with the key unsaved is the one mistake this screen exists to
     prevent, so the browser asks before the tab closes. */
  useEffect(() => {
    if (stored) return undefined;
    const warn = (e) => { e.preventDefault(); e.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [stored]);

  const copy = async (text, msg) => {
    const ok = await copyText(text);
    toast(ok ? msg : "Couldn't copy. Select the key and copy it by hand.", ok ? 'ok' : 'error');
    return ok;
  };

  const copyKey = async () => {
    if (await copy(payload.api_key, 'Key copied.')) {
      setCopied(true);
      setStored(true);
    }
  };

  const download = () => {
    downloadTextFile(guideFilename(payload.name), buildConsumerDoc(payload.name || 'Data API Export', payload.slug, payload.api_key, tables));
    toast('Consumer guide downloaded.');
  };

  return (
    <div className="ws">
      <div className="wsmain">
        <div style={{ position: 'relative', minHeight: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '40px 24px', overflow: 'hidden' }}>
          <div className="bgimg" style={{ opacity: 0.55 }} />
          <Confetti />
          <div className="hcard" role="dialog" aria-label={payload.rotated ? 'New API key' : 'API published'}>
            <div style={{ position: 'relative', width: 78, height: 78 }}>
              <span style={{ position: 'absolute', inset: 0, borderRadius: '50%', border: '2px solid var(--green)', animation: 'dax-ring 1.6s ease-out infinite' }} />
              <span style={{ position: 'absolute', inset: 0, borderRadius: '50%', background: 'var(--green)', color: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 34, animation: 'dax-pop .6s both' }}><Check size={34} strokeWidth={2.4} aria-hidden="true" /></span>
            </div>
            <span className="kicker">{payload.rotated ? 'KEY ROTATED' : 'PUBLISHED'}</span>
            <h1 className="h-l" style={{ fontSize: 40 }}>
              {payload.rotated ? <>A fresh key for <em>{payload.name}.</em></> : <><em>{payload.name}</em> is live.</>}
            </h1>
            <p style={{ margin: 0, fontSize: 14, color: 'var(--ink2)', lineHeight: 1.6, maxWidth: 500 }}>
              {payload.rotated
                ? 'The old key stopped working just now. Send this one to every caller.'
                : 'Copy the key now and share it over an encrypted channel. It also stays on the Overview tab.'}
            </p>
            <div style={{ width: '100%', padding: 20, borderRadius: 16, background: 'linear-gradient(135deg,#fff,#fdf1ef)', border: '1px solid #f3d6d2', display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
              <span style={{ color: 'var(--red)', display: 'inline-flex' }}><KeyRound size={20} strokeWidth={1.8} aria-hidden="true" /></span>
              <code style={{ flex: 1, fontSize: 17, letterSpacing: '.05em', color: 'var(--ink)', wordBreak: 'break-all', textAlign: 'left', userSelect: 'all' }} aria-label="API key">{key}</code>
              <button type="button" className="kbtn w" onClick={copyKey}>{copied ? <>Copied <Check size={13} strokeWidth={1.8} aria-hidden="true" /></> : <><Copy size={13} strokeWidth={1.8} aria-hidden="true" /> Copy key</>}</button>
              <button type="button" className="kbtn" onClick={() => emailKey(payload)}><Mail size={13} strokeWidth={1.8} aria-hidden="true" /> Email it</button>
            </div>
            {tables.length > 0 && (
              <div style={{ width: '100%', display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(260px,1fr))', gap: 12, textAlign: 'left' }}>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                  <span className="label">SHARE URLS · CARRY THE KEY</span>
                  {tables.map((t) => (
                    <div key={t.alias} className="row" style={{ padding: '7px 7px 7px 12px', borderRadius: 10, background: 'var(--bg)' }}>
                      <code style={{ flex: 1, fontSize: 11.5, color: 'var(--ink2)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>/{t.alias}/rows?api_key=…</code>
                      <button type="button" className="btn" style={{ padding: '5px 10px' }} onClick={() => copy(tableUrl(payload.slug, t.alias, payload.api_key), 'URL copied. It carries the live key.')}>Copy</button>
                    </div>
                  ))}
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                  <span className="label">BROWSABLE VIEW · NEEDS LOGIN</span>
                  {tables.map((t) => (
                    <div key={t.alias} className="row" style={{ padding: '7px 7px 7px 12px', borderRadius: 10, background: 'var(--bg)' }}>
                      <code style={{ flex: 1, fontSize: 11.5, color: 'var(--ink2)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>/data-api-view/{payload.slug}/{t.alias}</code>
                      <a className="btn dark" style={{ padding: '5px 10px', textDecoration: 'none' }} href={tableUiUrl(payload.slug, t.alias, payload.api_key)} target="_blank" rel="noreferrer">Open <ExternalLink size={12} strokeWidth={1.8} aria-hidden="true" /></a>
                    </div>
                  ))}
                </div>
              </div>
            )}
            <label className="consent" style={{ width: '100%', textAlign: 'left', borderColor: 'var(--line)', background: 'var(--paper)' }}>
              <input type="checkbox" checked={stored} onChange={(e) => setStored(e.target.checked)} />
              <span>I&apos;ve stored the key somewhere safe.</span>
            </label>
            <div className="row" style={{ gap: 10, flexWrap: 'wrap', justifyContent: 'center' }}>
              <button type="button" className="ghost" style={{ padding: '13px 18px', fontSize: 13.5 }} onClick={download}>Download consumer guide</button>
              <button
                type="button"
                className="cta"
                style={{ padding: '13px 14px 13px 22px', fontSize: 14, ...(stored ? {} : { background: 'var(--line3)', boxShadow: 'none', cursor: 'not-allowed' }) }}
                aria-disabled={!stored}
                onClick={() => (stored ? onDone() : toast('Copy the key or tick the box first. This screen is the easiest place to grab it.', 'error'))}
              >
                Open the API <span className="arr"><ArrowRight size={14} strokeWidth={1.8} aria-hidden="true" /></span>
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

const enc = encodeURIComponent;
const VARIANTS = [['short', 'Shorter'], ['formal', 'More formal'], ['python', '+ Python example'], ['powerbi', '+ Power BI steps'], ['default', 'Start over']];

const firstName = (email) => {
  const n = (email || '').split('@')[0].split(/[._-]+/)[0] || '';
  return n ? n.charAt(0).toUpperCase() + n.slice(1) : '';
};

const joinAnd = (xs) => (xs.length < 2 ? xs.join('') : `${xs.slice(0, -1).join(', ')} and ${xs[xs.length - 1]}`);

/* What the API can actually do, read off the capabilities endpoint. When that
   couldn't be reached, only what is certain (rows and filters) is claimed. */
function featuresOf(api, caps) {
  const list = caps?.tables || [];
  const any = (k) => list.some((t) => t[k]);
  const text = list.some((t) => t.searchable > 0);
  return {
    text,
    words: any('word_search'),
    vector: any('vector_search'),
    textVector: any('text_vector_search'),
    retrieval: any('retrieval'),
    known: list.length > 0,
  };
}

/* Drafts the key email from what this API offers: the subject, the banner,
   the red headline and the body all name the search it really has.
   {{key}} and {{base_url}} stay as placeholders all the way to the server,
   which fills them in for the copy that goes to the mail server only. */
function draftKeyEmail(api, caps, to, variant = 'default') {
  const f = featuresOf(api, caps);
  const name = api.name;
  const kinds = [f.text && 'Clear-Text', f.vector && 'Vector'].filter(Boolean);
  let title = kinds.length ? `${joinAnd(kinds)} Search` : 'Data';
  if (f.retrieval) title = kinds.length ? `${title} with Hybrid Retrieval` : 'Hybrid Retrieval';
  const subject = `“${name}” API – ${kinds.length || f.retrieval ? `${title} Available` : 'Now Available'}`;
  const brand = {
    banner_title: 'Welcome to GRA SmartHub!',
    banner_subtitle: 'where Regulatory Data meets Intelligence',
    headline: `“${name}” ${title} API is now available!`,
  };

  const hi = `Hi ${to.length === 1 && firstName(to[0]) ? firstName(to[0]) : 'Team'},`;
  const first = api.tables?.[0]?.alias || 'alias';
  const searchWords = joinAnd([f.text && 'clear-text', f.vector && 'vector similarity'].filter(Boolean));
  const intro = `You now have access to the **“${name}” API**${searchWords ? `, including ${searchWords} search` : ''}${f.retrieval ? `${searchWords ? ' and' : ', including'} hybrid retrieval` : ''}.`;

  const can = [`- **Browse rows** with paging, sorting and filters on every table`];
  if (f.text) can.push(`- **Clear-text search** across the searchable columns${f.words ? ', with whole-word matching where an index exists' : ''}`);
  if (f.vector) can.push(`- **Vector similarity search**: ${f.textVector ? 'ask in plain language, or send an embedding' : 'send an embedding'} to /${first}/vector-search`);
  if (f.retrieval) can.push(`- **Hybrid retrieval**: meaning and keywords together in one call to /${first}/retrieve`);
  const tables = (api.tables || []).map((t) => `- /${t.alias}`).join('\n');

  const keyBlock = `### Your API key\n\n{{key}}\n\nTreat it like a password: anyone holding it can read every table on this API.`;
  const tail = `If anything stops working, or someone on your side leaves, reply here and we'll rotate the key.`;

  if (variant === 'short') {
    return { subject, brand, body: `${hi}\n\n${intro}\n\n${keyBlock}\n\nSend it as an X-API-Key header on every call.` };
  }
  if (variant === 'formal') {
    return {
      subject, brand,
      body: `${hi}\n\nAccess to the **“${name}” API** has been provisioned for you.\n\n${keyBlock}\n\n### Capabilities\n\n${can.join('\n')}\n\n### Tables\n\n${tables}\n\nPlease store the key securely and do not share it further. It can be revoked at any time if it is exposed.`,
    };
  }
  let extra = '';
  if (variant === 'python') {
    extra = f.vector && f.textVector
      ? `\n\n### Quick start in Python\n\n    import requests\n    r = requests.post("{{base_url}}/${first}/vector-search",\n                      headers={"X-API-Key": "<your key>"},\n                      json={"query_text": "your question", "top_k": 5})\n    print(r.json())`
      : `\n\n### Quick start in Python\n\n    import requests\n    r = requests.get("{{base_url}}/${first}/rows",\n                     headers={"X-API-Key": "<your key>"}, params={"limit": 100})\n    rows = r.json()["rows"]`;
  }
  if (variant === 'powerbi') {
    extra = `\n\n### In Power BI\n\nGet data, then Web, paste {{base_url}}/${first}/rows?api_key=<your key> and expand the "rows" column.`;
  }
  return {
    subject, brand,
    body: `${hi}\n\n${intro}\n\n${keyBlock}\n\n### What you can do\n\n${can.join('\n')}\n\n### Tables\n\n${tables}${extra}\n\nThe consumer guide has paging, filters and ready-to-run snippets for Python, Power BI and cURL.\n\n${tail}`,
  };
}

/* What goes to the Communication Center template: the banner and headline
   from the draft, the message, and the base URL and guide under Resources.
   Used by both the preview and the send, so they always match. The layout
   never carries the key. */
export function composeKeyEmail(api, body, guide, brand) {
  const base = baseUrl(api.slug);
  const docs = `${window.location.origin}/data-api?api=${encodeURIComponent(api.slug)}&tab=docs`;
  const resources = [];
  if (/^https?:\/\//.test(base)) {
    resources.push({ title: 'Base URL', description: 'Every endpoint on this API starts here. Send the key as an X-API-Key header on each call.', link_text: base, link_url: base });
  }
  resources.push({
    title: 'Consumer guide',
    description: `Paging, filters and ready-to-run snippets for Python, Power BI and cURL.${guide ? ' The guide is attached to this email too.' : ''}`,
    link_text: 'Open the docs in SmartHub',
    link_url: docs,
  });
  const b = brand || {};
  const layout = { resources };
  ['banner_title', 'banner_subtitle', 'headline'].forEach((k) => { if ((b[k] || '').trim()) layout[k] = b[k].trim(); });
  return { body: String(body || ''), layout };
}

/* The message exactly as Communication Center renders and sends it: the same
   house template, logo and footer, built by the server so it can't drift. */
function useHouseEmail({ api, body, keyText, typing, guide, brand, subject }) {
  const [out, setOut] = useState({ html: '', subject: '', state: 'idle' });
  const ticket = useRef(0);
  const composed = useMemo(() => composeKeyEmail(api, body, guide, brand), [api, body, guide, brand]);
  const filled = useMemo(
    () => composed.body.split('{{key}}').join(keyText).split('{{base_url}}').join(baseUrl(api.slug)),
    [composed.body, keyText, api.slug],
  );
  const layoutJson = JSON.stringify(composed.layout);

  useEffect(() => {
    const mine = ticket.current + 1;
    ticket.current = mine;
    if (!filled.trim()) { setOut({ html: '', subject: '', state: 'idle' }); return undefined; }
    setOut((o) => ({ ...o, state: 'loading' }));
    const timer = setTimeout(() => {
      request('/api/communications/preview', { method: 'POST', body: { category: 'custom', subject: subject || '', body: filled, layout: JSON.parse(layoutJson) } })
        .then((r) => { if (mine === ticket.current) setOut({ html: r?.html || '', subject: r?.subject || subject, state: 'ok' }); })
        .catch(() => { if (mine === ticket.current) setOut((o) => ({ ...o, state: 'error' })); });
    }, typing ? 600 : 300);
    return () => clearTimeout(timer);
  }, [filled, layoutJson, typing, subject]);
  return out;
}

function HouseFrame({ html, state }) {
  const [height, setHeight] = useState(520);
  const frame = useRef(null);
  // The frame is always as tall as the email, so it never shows a scrollbar of
  // its own; it is measured again whenever its width changes (desktop/mobile).
  // Measures the email itself (not the frame), so the frame never has to be
  // collapsed to find its height. Collapsing it made the panel's scrollbar
  // come and go, which changed the width and started the measuring again.
  const fit = () => {
    try {
      const doc = frame.current?.contentDocument;
      if (!doc?.body) return;
      doc.documentElement.style.overflow = 'hidden';
      const content = doc.body.firstElementChild || doc.body;
      const next = Math.max(320, Math.ceil(content.getBoundingClientRect().height));
      setHeight((h) => (Math.abs(h - next) > 1 ? next : h));
    } catch {
      setHeight(640);
    }
  };
  useEffect(() => {
    const el = frame.current;
    if (!el || typeof ResizeObserver === 'undefined') return undefined;
    let width = el.clientWidth;
    const ro = new ResizeObserver(() => {
      if (el.clientWidth !== width) { width = el.clientWidth; fit(); }
    });
    ro.observe(el);
    return () => ro.disconnect();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [html]);
  return (
    <div className="mhouse">
      {state === 'loading' && <span className="mhouse-bar" aria-hidden="true" />}
      {html ? (
        <iframe
          ref={frame}
          title="Email preview"
          srcDoc={html}
          onLoad={fit}
          // No scripts; same-origin only so the frame can be sized to its content.
          // It is a picture of the email: nothing inside is clickable or focusable.
          sandbox="allow-same-origin"
          tabIndex={-1}
          aria-hidden="true"
          style={{ height, opacity: state === 'loading' ? 0.7 : 1 }}
        />
      ) : (
        <p className="mhouse-empty">{state === 'error' ? "Couldn't render the preview. The email still uses the Communication Center template." : 'The draft appears here as it is written…'}</p>
      )}
    </div>
  );
}

/* The preview, framed like Communication Center's: a window (or a phone),
   the subject, the sender, and the rendered email underneath. */
function Preview({ api, m, opts, sender, device }) {
  const lab = opts.labels.find((l) => l.id === m.label);
  const key = m.showKey && api.api_key ? api.api_key : maskKey(api.api_key || 'sh_live_xxxxxxxxxxxxxxxxxxxxxxxx');
  const mail = useHouseEmail({ api, body: m.body, keyText: key, typing: m.typing, guide: m.guide, brand: m.brand, subject: m.subject });
  const mobile = device === 'mobile';
  return (
    <div className={`mdevice ${mobile ? 'mobile' : ''}`}>
      <div className="mscreen">
      <div className="mchrome">
        {mobile ? <span className="mnotch" aria-hidden="true" /> : ['#ff5f57', '#febc2e', '#28c840'].map((c) => <i key={c} style={{ background: c }} />)}
      </div>
      <div className="menv">
        <div className="msubject">{mail.subject || m.subject || <span style={{ color: 'var(--faint)' }}>(no subject)</span>}</div>
        <div className="mfrom">
          <span className="mav2">SH</span>
          <span className="mfrom-who"><b>GRA SmartHub</b><span>{sender || ''}</span></span>
          {lab && <span className="mlabchip"><Lock size={11} strokeWidth={1.8} aria-hidden="true" />{lab.path.replace(/\\/g, ' / ')}</span>}
        </div>
        <div className="mto2"><span>To</span>{m.to.length ? m.to.map((x) => <i key={x}>{x}</i>) : <em>add a recipient</em>}</div>
        {m.cc.length > 0 && <div className="mto2"><span>Cc</span>{m.cc.map((x) => <i key={x}>{x}</i>)}</div>}
        {m.bcc?.length > 0 && <div className="mto2"><span>Bcc</span>{m.bcc.map((x) => <i key={x}>{x}</i>)}</div>}
        {(m.guide || m.files?.length > 0) && (
          <div className="matts">
            {m.guide && (
              <div className="matt"><span><FileText size={18} strokeWidth={1.8} aria-hidden="true" /></span><div><b style={{ fontWeight: 700 }}>{guideFilename(api.name)}</b><span>Consumer guide · {bytes(m.guideSize)}</span></div></div>
            )}
            {(m.files || []).map((f) => (
              <div key={`${f.name}-${f.size}`} className="matt"><span><Paperclip size={16} strokeWidth={1.8} aria-hidden="true" /></span><div><b style={{ fontWeight: 700 }}>{f.name}</b><span>{bytes(f.size)}</span></div></div>
            ))}
          </div>
        )}
      </div>
      <HouseFrame html={mail.html} state={mail.state} />
      </div>
    </div>
  );
}

/* ------------------------------------------------- formatted message editor */

/* The message is shown formatted (bold, headings, lists, a chip for each
   token) but stored as the same markdown-lite the Communication Center
   template reads, so the server, the AI rewrite and the {{key}} check all
   see exactly what they did before. */
const escHtml = (t) => t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const TOKEN_LABEL = { '{{key}}': 'API key', '{{base_url}}': 'Base URL' };

function mdInline(text) {
  return escHtml(text)
    .replace(/\{\{(key|base_url)\}\}/g, (m) => `<span class="rtok" contenteditable="false" data-token="${m}">${TOKEN_LABEL[m]}</span>`)
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2">$1</a>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>');
}

function mdToHtml(md) {
  const lines = String(md || '').replace(/\r\n?/g, '\n').split('\n');
  const out = [];
  let para = [];
  let list = null;
  const flushPara = () => { if (para.length) out.push(`<p>${para.map(mdInline).join('<br>')}</p>`); para = []; };
  const flushList = () => { if (list) out.push(`<${list.tag}>${list.items.map((i) => `<li>${mdInline(i)}</li>`).join('')}</${list.tag}>`); list = null; };
  lines.forEach((raw) => {
    const line = raw.trim();
    const h = line.match(/^#{1,3}\s+(.*)$/);
    const b = line.match(/^[-*]\s+(.*)$/);
    const n = line.match(/^\d+[.)]\s+(.*)$/);
    if (!line) { flushPara(); flushList(); return; }
    if (h) { flushPara(); flushList(); out.push(`<h3>${mdInline(h[1])}</h3>`); return; }
    if (b || n) {
      flushPara();
      const tag = b ? 'ul' : 'ol';
      if (list && list.tag !== tag) flushList();
      if (!list) list = { tag, items: [] };
      list.items.push((b || n)[1]);
      return;
    }
    flushList();
    para.push(raw.replace(/^\s{4,}/, '    '));
  });
  flushPara(); flushList();
  return out.join('');
}

function inlineMd(node) {
  let s = '';
  node.childNodes.forEach((c) => {
    if (c.nodeType === 3) { s += c.textContent.replace(/ /g, ' '); return; }
    if (c.nodeType !== 1) return;
    const tag = c.tagName.toLowerCase();
    if (c.dataset?.token) { s += c.dataset.token; return; }
    if (tag === 'br') { s += '\n'; return; }
    const inner = inlineMd(c);
    if (!inner.trim()) { s += inner; return; }
    // Spaces at the edges of a selection stay outside the markers, or
    // "Welcome **aboard**" would come out as "Welcome**aboard**".
    const [, lead, core, trail] = inner.match(/^(\s*)([\s\S]*?)(\s*)$/);
    if (tag === 'b' || tag === 'strong') s += `${lead}**${core}**${trail}`;
    else if (tag === 'i' || tag === 'em') s += `${lead}*${core}*${trail}`;
    else if (tag === 'a' && c.getAttribute('href')) s += `${lead}[${core}](${c.getAttribute('href')})${trail}`;
    else if (/^(p|div|h[1-6]|ul|ol|li)$/.test(tag)) s += `\n${blockMd(c)}\n`;
    else s += inner;
  });
  return s;
}

function blockMd(root) {
  const blocks = [];
  let loose = '';
  const flush = () => { if (loose.trim()) blocks.push(loose.replace(/^\n+|\n+$/g, '')); loose = ''; };
  root.childNodes.forEach((c) => {
    const tag = c.nodeType === 1 ? c.tagName.toLowerCase() : '';
    if (/^h[1-6]$/.test(tag)) { flush(); blocks.push(`### ${inlineMd(c).trim()}`); }
    else if (tag === 'ul' || tag === 'ol') {
      flush();
      const items = [...c.children].filter((li) => li.tagName.toLowerCase() === 'li');
      blocks.push(items.map((li, i) => `${tag === 'ul' ? '-' : `${i + 1}.`} ${inlineMd(li).replace(/\n+/g, ' ').trim()}`).join('\n'));
    } else if (tag === 'p' || tag === 'div') {
      flush();
      const t = inlineMd(c).replace(/^\n+|\n+$/g, '');
      if (t.trim()) blocks.push(t);
    } else {
      loose += c.nodeType === 1 ? inlineMd({ childNodes: [c] }) : c.textContent;
    }
  });
  flush();
  return blocks.join('\n\n');
}

function RichBody({ value, onChange, readOnly, placeholder, invalid }) {
  const box = useRef(null);
  const last = useRef(null);

  // Outside changes (a draft typing itself in, an AI rewrite) re-render the
  // editor; the user's own typing is only read back, never re-rendered, so
  // the caret stays where it is.
  useEffect(() => {
    if (!box.current || value === last.current) return;
    box.current.innerHTML = mdToHtml(value);
    last.current = value;
  }, [value]);

  const emit = () => {
    const md = blockMd(box.current);
    last.current = md;
    onChange(md);
  };
  const run = (cmd, arg) => {
    if (readOnly) return;
    box.current.focus();
    document.execCommand(cmd, false, arg);
    emit();
  };
  const heading = () => {
    const cur = String(document.queryCommandValue('formatBlock') || '').toLowerCase();
    run('formatBlock', /h[1-6]/.test(cur) ? 'p' : 'h3');
  };
  const link = () => {
    const url = window.prompt('Link address (https://…)');
    if (url && /^https?:\/\//i.test(url)) run('createLink', url);
  };
  // Pasted content comes in as plain text, so foreign formatting can't sneak
  // into an email that goes out under a Highly Restricted label.
  const paste = (e) => {
    e.preventDefault();
    document.execCommand('insertText', false, e.clipboardData.getData('text/plain'));
    emit();
  };
  const keep = (e) => e.preventDefault();

  return (
    <div className={`rbody ${invalid ? 'bad' : ''} ${readOnly ? 'ro' : ''}`}>
      <div className="rtools" role="toolbar" aria-label="Formatting">
        <button type="button" onMouseDown={keep} onClick={() => run('bold')} aria-label="Bold" title="Bold"><Bold size={14} strokeWidth={2} aria-hidden="true" /></button>
        <button type="button" onMouseDown={keep} onClick={() => run('italic')} aria-label="Italic" title="Italic"><Italic size={14} strokeWidth={2} aria-hidden="true" /></button>
        <i className="rsep" aria-hidden="true" />
        <button type="button" onMouseDown={keep} onClick={heading} aria-label="Heading" title="Heading"><Heading size={14} strokeWidth={2} aria-hidden="true" /></button>
        <button type="button" onMouseDown={keep} onClick={() => run('insertUnorderedList')} aria-label="Bulleted list" title="Bulleted list"><List size={14} strokeWidth={2} aria-hidden="true" /></button>
        <button type="button" onMouseDown={keep} onClick={() => run('insertOrderedList')} aria-label="Numbered list" title="Numbered list"><ListOrdered size={14} strokeWidth={2} aria-hidden="true" /></button>
        <i className="rsep" aria-hidden="true" />
        <button type="button" onMouseDown={keep} onClick={link} aria-label="Link" title="Link"><Link2 size={14} strokeWidth={2} aria-hidden="true" /></button>
        <span className="rhint">The drafter only ever sees the key chip, never the key</span>
      </div>
      <div
        ref={box}
        className="redit"
        contentEditable={!readOnly}
        suppressContentEditableWarning
        role="textbox"
        aria-multiline="true"
        aria-label="Message"
        data-ph={placeholder}
        onInput={emit}
        onPaste={paste}
      />
    </div>
  );
}

/* Sensitivity as a drop-up beside the send buttons, the way Communication
   Center does it. Only Highly Restricted labels ever reach this list. */
function SensitivityMenu({ labels, value, onChange, disabled }) {
  const [open, setOpen] = useState(false);
  const box = useRef(null);
  useEffect(() => {
    if (!open) return undefined;
    const away = (e) => { if (box.current && !box.current.contains(e.target)) setOpen(false); };
    const esc = (e) => { if (e.key === 'Escape') { e.stopPropagation(); setOpen(false); } };
    document.addEventListener('mousedown', away);
    document.addEventListener('keydown', esc, true);
    return () => { document.removeEventListener('mousedown', away); document.removeEventListener('keydown', esc, true); };
  }, [open]);
  const current = labels.find((l) => l.id === value);
  const name = (l) => l.path.split('\\').map((x) => x.trim()).join(' · ');
  return (
    <div className="msens" ref={box}>
      <button type="button" className={`msens-btn ${current ? '' : 'bad'} ${open ? 'open' : ''}`} onClick={() => setOpen((v) => !v)} disabled={disabled} aria-haspopup="menu" aria-expanded={open} title={current ? `Sensitivity: ${name(current)}` : 'Pick a sensitivity label'}>
        <Lock size={13} strokeWidth={1.8} aria-hidden="true" />
        <span>{current ? name(current) : 'Pick a label'}</span>
        <ChevronDown size={13} strokeWidth={1.8} aria-hidden="true" />
      </button>
      {open && (
        <div className="msens-menu" role="menu">
          <div className="msens-cap">Sensitivity</div>
          {labels.map((l) => (
            <button key={l.id} type="button" role="menuitemradio" aria-checked={l.id === value} className={l.id === value ? 'on' : ''} onClick={() => { onChange(l.id); setOpen(false); }}>
              <Lock size={14} strokeWidth={1.8} aria-hidden="true" />
              <span><b>{l.path.split('\\')[0].trim()}</b><small>{l.path.split('\\').slice(1).join(' / ').trim() || l.label}{l.protection === 'encrypt' ? ' · only recipients can open it' : ' · recipients cannot forward it'}</small></span>
              {l.id === value && <Check size={14} strokeWidth={2.2} aria-hidden="true" />}
            </button>
          ))}
          {!labels.length && <p className="msens-none">No Highly Restricted label is configured, so a key can&apos;t be emailed from here.</p>}
          <div className="msens-foot">Only Highly Restricted labels are offered for a key.</div>
        </div>
      )}
    </div>
  );
}

export function EmailKeyDrawer({ api, onClose }) {
  const { can } = usePage();
  const toast = useToast();
  const opts = useResource((signal) => request(`/data-api/${enc(api.id || api.slug)}/email-key`, { signal }), [api.id, api.slug]);
  // What each table can do, so the draft names the search this API really has.
  const caps = useResource((signal) => request(`/data-api/${enc(api.id || api.slug)}/capabilities`, { signal }), [api.id, api.slug]);
  const guideMd = useMemo(() => buildConsumerDoc(api.name || 'Data API Export', api.slug, '', api.tables || []), [api]);
  const [m, setM] = useState({
    stage: 'loading', to: [], cc: [], bcc: [], copies: false, aiOpen: false, brand: null, subjectTouched: false, device: 'desktop', files: [], dragging: false, subject: `Your API key for ${api.name}`, body: '', label: '', guide: true,
    showKey: false, variant: 'default', typing: false, guideSize: guideMd.length, ask: '',
  });
  const [step, setStep] = useState(0);
  const [err, setErr] = useState(null);
  const [aiBusy, setAiBusy] = useState(false);
  const typer = useRef(null);
  const set = (patch) => setM((x) => ({ ...x, ...patch }));
  const fileInput = useRef(null);


  const typeBody = (text) => {
    clearInterval(typer.current);
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
      set({ body: text, typing: false });
      return;
    }
    let i = 0;
    set({ body: '', typing: true });
    typer.current = setInterval(() => {
      i = Math.min(text.length, i + 7);
      setM((x) => ({ ...x, body: text.slice(0, i), typing: i < text.length }));
      if (i >= text.length) clearInterval(typer.current);
    }, 16);
  };
  useEffect(() => () => clearInterval(typer.current), []);

  /* The connecting steps follow the real request; the last one is the local
     draft being written. */
  useEffect(() => {
    if (opts.loading) { setStep(0); return undefined; }
    if (opts.error || !opts.data || m.stage !== 'loading') return undefined;
    setStep(1);
    if (caps.loading) return undefined;
    setStep(2);
    const t1 = setTimeout(() => setStep(3), 380);
    const t2 = setTimeout(() => {
      const d = draftKeyEmail(api, caps.data, []);
      set({ stage: 'ready', label: opts.data.default_label || opts.data.labels?.[0]?.id || '', subject: d.subject, brand: d.brand });
      typeBody(d.body);
    }, 760);
    return () => { clearTimeout(t1); clearTimeout(t2); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opts.loading, opts.error, opts.data, caps.loading]);


  const o = opts.data;

  /* Files the sender adds go with the email, held to Communication Center's
     limits. They are read in the browser and sent base64 inside the request. */
  const addFiles = async (list) => {
    const picked = [...(list || [])];
    if (!picked.length) return;
    const maxFiles = o?.max_attachments || 5;
    const maxBytes = (o?.max_attachment_mb || 10) * 1024 * 1024;
    if (m.files.length + picked.length > maxFiles) { toast(`At most ${maxFiles} attachments`, 'error'); return; }
    const used = m.files.reduce((n, f) => n + f.size, 0) + (m.guide ? m.guideSize : 0);
    const adding = picked.reduce((n, f) => n + f.size, 0);
    if (used + adding > maxBytes) { toast(`Attachments can't go over ${o?.max_attachment_mb || 10} MB in total`, 'error'); return; }
    const read = (file) => new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve({ name: file.name, size: file.size, data: String(r.result).split(',')[1] || '' });
      r.onerror = () => reject(r.error);
      r.readAsDataURL(file);
    });
    try {
      const loaded = await Promise.all(picked.map(read));
      setM((x) => ({ ...x, files: [...x.files, ...loaded.filter((f) => !x.files.some((g) => g.name === f.name && g.size === f.size))] }));
    } catch {
      toast("Couldn't read that file", 'error');
    }
  };
  const limit = o?.max_recipients || 25;
  // Communication Center's standing Cc goes on every send; shown so nobody is surprised.
  const typed = [...m.to, ...m.cc].map((a) => a.toLowerCase());
  // The sender is copied on every real send too.
  const standing = [...new Set([...(o?.default_cc || []), ...(o?.you ? [o.you] : [])].map((a) => a.toLowerCase()))]
    .filter((a) => !typed.includes(a));
  // The automatic copies aren't shown, but they still count toward the limit.
  const typedAll = [...m.to, ...m.cc, ...m.bcc];
  const everyone = [...typedAll, ...standing];
  const badAddr = typedAll.find((a) => !ADDR_RE.test(a));
  const outside = o?.allowed_domains?.length
    ? typedAll.find((a) => ADDR_RE.test(a) && !o.allowed_domains.some((d) => {
      const dom = a.split('@')[1].toLowerCase();
      return dom === d.toLowerCase() || dom.endsWith(`.${d.toLowerCase()}`);
    }))
    : null;
  const tooMany = everyone.length > limit;
  const setList = (field) => (value) => set({ [field]: splitAddresses(value) });
  const showCopies = m.copies || m.cc.length > 0 || m.bcc.length > 0;

  const lab = o?.labels?.find((l) => l.id === m.label);
  const hasKey = m.body.includes('{{key}}');
  const why = m.typing ? '' : !o?.labels?.length ? 'No Highly Restricted label is set up for the mailer.'
    : !m.to.length ? 'Add at least one recipient'
      : badAddr ? `${badAddr} isn't a valid email address`
      : outside ? `${outside} is outside the allowed domains (${o.allowed_domains.join(', ')})`
      : tooMany ? `A key can go to at most ${limit} people at once`
      : !lab ? 'Pick a Highly Restricted label'
        : !m.subject.trim() ? 'Add a subject'
          : !hasKey ? 'Put {{key}} back in the message, on a line of its own'
            : o && m.body.length > o.max_body_chars ? `The message is over ${o.max_body_chars.toLocaleString()} characters`
              : o && m.subject.length > o.max_subject_chars ? `The subject is over ${o.max_subject_chars} characters` : '';
  const canSend = !why && !m.typing && m.stage === 'ready';
  const canTest = !!lab && !!m.subject.trim() && hasKey && !m.typing && m.stage === 'ready';

  const send = async (test) => {
    if (test ? !canTest : !canSend) return;
    setErr(null);
    set({ stage: 'sending', test });
    try {
      const r = await request(`/data-api/${enc(api.id || api.slug)}/email-key`, {
        method: 'POST',
        body: {
          to: m.to, cc: m.cc, bcc: m.bcc, subject: m.subject.trim(), body: composeKeyEmail(api, m.body, m.guide, m.brand).body,
          layout: composeKeyEmail(api, m.body, m.guide, m.brand).layout, sensitivity: m.label,
          base_url: baseUrl(api.slug), files: m.files.map(({ name, data }) => ({ name, data })), attach_guide: m.guide, guide_markdown: m.guide ? guideMd : '', test_only: !!test,
        },
      });
      set({ stage: 'sent', test: !!test, result: r });
      toast(test ? 'Test sent to you' : 'Encrypted email sent');
    } catch (e) {
      setErr(e);
      set({ stage: 'ready' });
    }
  };

  const rework = async () => {
    const ask = m.ask.trim();
    if (!ask) { toast('Say what to change first', 'error'); return; }
    setAiBusy(true);
    try {
      const d = await request('/api/communications/draft', {
        method: 'POST',
        body: {
          mode: 'rewrite', category: 'custom', subject: m.subject, body: m.body,
          prompt: `${ask}\n\nKeep the placeholders {{key}} and {{base_url}} exactly as written. {{key}} must stay on a line of its own. Never write an API key.`,
        },
      });
      const body = d?.body || '';
      if (!body.includes('{{key}}')) {
        toast('The rewrite dropped the {{key}} placeholder, so it was not applied. Try asking differently.', 'error');
      } else {
        typeBody(body);
        set({ ask: '', variant: '' });
      }
    } catch (e) {
      toast(e.status === 503 ? "AI drafting isn't set up on this server. The ready-made versions still work." : `Couldn't rewrite: ${e.message}`, 'error');
    } finally {
      setAiBusy(false);
    }
  };

  const close = () => {
    if (m.stage === 'sending') return;
    clearInterval(typer.current);
    onClose();
  };

  const connecting = ['Opening Communication Center', 'Loading your sensitivity labels', 'Checking what this API can do', 'Drafting the message'];

  return (
    <Drawer onClose={close} wide className="mailx" label="Email the API key" busy={m.stage === 'sending'}>
      <DrawerHead kicker={`COMMUNICATION CENTER · ${api.name.toUpperCase()}`} title={<>Email the key, <em>encrypted.</em></>} onClose={close} />

      {opts.error && !o && (
        <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <ErrorBox error={opts.error} onRetry={() => opts.reload()} title="Couldn't reach Communication Center" />
        </div>
      )}

      {!opts.error && m.stage === 'loading' && (
        <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 24 }}>
            <Orb size={130} inset={30}><Mail size={26} strokeWidth={1.8} aria-hidden="true" /></Orb>
            <Steps steps={connecting} at={step} />
          </div>
        </div>
      )}

      {m.stage === 'sent' && (
        <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 30 }}>
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 16, textAlign: 'center', maxWidth: 460, animation: 'dax-rise .5s both' }}>
            <div style={{ position: 'relative', width: 74, height: 74 }}>
              <span style={{ position: 'absolute', inset: 0, borderRadius: '50%', border: '2px solid var(--green)', animation: 'dax-ring 1.6s ease-out infinite' }} />
              <span style={{ position: 'absolute', inset: 0, borderRadius: '50%', background: 'var(--green)', color: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 30, animation: 'dax-pop .6s both' }}><Check size={30} strokeWidth={2.4} aria-hidden="true" /></span>
            </div>
            <span className="kicker">{m.test ? 'TEST SENT' : 'SENT ENCRYPTED'}</span>
            <span className="h-l" style={{ fontSize: 32 }}>
              {m.test ? <>Check <em>your inbox.</em></> : <>On its way to <em>{m.to.length === 1 ? (firstName(m.to[0]) || m.to[0]) : plural(m.result?.recipients?.length || m.to.length, 'person', 'people')}.</em></>}
            </span>
            <span style={{ fontSize: 13.5, color: 'var(--ink2)', lineHeight: 1.6 }}>
              {m.test
                ? `The test went only to ${o?.sender || 'you'}, with the same label and layout.`
                : `Labelled ${m.result?.label || lab?.path || ''}. The send is in Communication Center history with the key masked, and on this API's Activity tab.`}
            </span>
            <div className="row" style={{ gap: 10, marginTop: 6, flexWrap: 'wrap', justifyContent: 'center' }}>
              {m.test
                ? <button type="button" className="btn" onClick={() => set({ stage: 'ready', test: false })}>Back to the draft</button>
                : can?.('sendCommunications') && <a className="btn" style={{ textDecoration: 'none' }} href="/communication-center?tab=history" target="_blank" rel="noreferrer">Open in Communication Center <ExternalLink size={12} strokeWidth={1.8} aria-hidden="true" /></a>}
              <button type="button" className="btn red" onClick={close}>Done</button>
            </div>
          </div>
        </div>
      )}

      {(m.stage === 'ready' || m.stage === 'sending') && o && (
        <>
          <div className="mgrid">
            <fieldset className="mform" disabled={m.stage === 'sending'} style={{ border: 0, margin: 0 }}>
              <section className="mcard">
                <div className="mcard-h">
                  <span className="mstep">1</span>
                  <b>Recipients</b>
                  <span className="mcard-sub">Who gets the key</span>
                  {!showCopies && (
                    <button type="button" className="mghost" onClick={() => set({ copies: true })}>Cc / Bcc</button>
                  )}
                </div>
                <div className="mrecips">
                  <RecipientField label="To" value={m.to.join(', ')} onChange={setList('to')} placeholder="Type a name or email, then press Enter" />
                  {showCopies && (
                    <>
                      <RecipientField label="Cc" value={m.cc.join(', ')} onChange={setList('cc')} placeholder="Optional" />
                      <RecipientField label="Bcc" value={m.bcc.join(', ')} onChange={setList('bcc')} placeholder="Optional" />
                    </>
                  )}
                </div>
                <div className="mtally" aria-live="polite">
                  <span><b>{plural(typedAll.length, 'person', 'people')}</b> will receive this{badAddr ? <em> · 1 not valid</em> : null}</span>
                  <span className="mtally-bar" aria-hidden="true"><i style={{ width: `${Math.min(100, (typedAll.length / limit) * 100)}%`, background: tooMany ? 'var(--red)' : undefined }} /></span>
                  <span className="mcard-sub">Limit {limit}{o.allowed_domains?.length ? ` · ${o.allowed_domains.join(', ')} only` : ''}</span>
                </div>
              </section>

              <section className="mcard">
                <div className="mcard-h">
                  <span className="mstep">2</span>
                  <b>Message</b>
                  <div className="mai">
                    <button type="button" className="mai-go" disabled={m.typing || aiBusy} onClick={() => set({ aiOpen: !m.aiOpen })}>
                      <Sparkles size={13} strokeWidth={1.8} aria-hidden="true" />Rewrite with AI
                    </button>
                    {VARIANTS.map(([v, l]) => (
                      <button key={v} type="button" className={`mai-chip ${m.variant === v ? 'on' : ''}`} disabled={m.typing || aiBusy} onClick={() => { const d = draftKeyEmail(api, caps.data, m.to, v); set({ variant: v, brand: d.brand, ...(m.subjectTouched ? {} : { subject: d.subject }) }); typeBody(d.body); }}>{l}</button>
                    ))}
                  </div>
                </div>

                {m.aiOpen && (
                  <form className="mai-ask" onSubmit={(e) => { e.preventDefault(); rework(); }}>
                    <Sparkles size={14} strokeWidth={1.8} aria-hidden="true" />
                    <input autoFocus value={m.ask} onChange={(e) => set({ ask: e.target.value })} placeholder="Say what to change, e.g. mention who to contact if it stops working" aria-label="What to change" maxLength={500} />
                    <button type="submit" disabled={m.typing || aiBusy || !m.ask.trim()}>{aiBusy ? <><Spinner />Writing</> : 'Rewrite'}</button>
                  </form>
                )}

                <label className="mlab" htmlFor="dax-m-subj">Subject</label>
                <input id="dax-m-subj" className="msubj" value={m.subject} maxLength={o.max_subject_chars || 300} onChange={(e) => set({ subject: e.target.value, subjectTouched: true })} />

                <div className="mlab-row">
                  <span className="mlab">Body</span>
                  <span className={`mstatus ${m.typing || aiBusy ? 'busy' : ''}`}>
                    {m.typing || aiBusy
                      ? <><span className="tdot" />{aiBusy ? 'AI is rewriting…' : 'Drafting…'}</>
                      : <>Tokens <code>{'{{key}}'}</code> <code>{'{{base_url}}'}</code></>}
                  </span>
                </div>
                <div className={`mbody-ed ${m.typing ? 'typing' : ''}`}>
                  <RichBody
                    value={m.body}
                    readOnly={m.typing}
                    onChange={(body) => { if (!m.typing) set({ body, variant: '' }); }}
                    placeholder="Write the message. Keep the key chip on a line of its own."
                    invalid={!!o && m.body.length > o.max_body_chars}
                  />
                </div>
                {!hasKey && !m.typing && (
                  <div className="row" style={{ gap: 8, marginTop: 8 }}>
                    <span className="hint" style={{ margin: 0, color: 'var(--red)' }}>The message needs {'{{key}}'} where the key goes.</span>
                    <button type="button" className="link" onClick={() => set({ body: m.body.replace(/\n*$/, '\n\n{{key}}\n') })}>Put it back</button>
                  </div>
                )}
                <label className="check mguide"><input type="checkbox" checked={m.guide} onChange={(e) => set({ guide: e.target.checked })} /> Attach the consumer guide</label>

                <div
                  className={`mdrop ${m.dragging ? 'over' : ''}`}
                  onDragOver={(e) => { e.preventDefault(); if (!m.dragging) set({ dragging: true }); }}
                  onDragLeave={() => set({ dragging: false })}
                  onDrop={(e) => { e.preventDefault(); set({ dragging: false }); addFiles(e.dataTransfer.files); }}
                >
                  <Paperclip size={16} strokeWidth={1.8} aria-hidden="true" />
                  <span>Drop files here or <button type="button" className="link" onClick={() => fileInput.current?.click()}>browse</button></span>
                  <small>Up to {o.max_attachments || 5} files · {o.max_attachment_mb || 10} MB in total{m.guide ? ', guide included' : ''}</small>
                  <input ref={fileInput} type="file" multiple hidden onChange={(e) => { addFiles(e.target.files); e.target.value = ''; }} />
                </div>
                {m.files.length > 0 && (
                  <ul className="mfiles">
                    {m.files.map((f, i) => (
                      <li key={`${f.name}-${f.size}`}>
                        <FileText size={15} strokeWidth={1.8} aria-hidden="true" />
                        <span className="mfile-name" title={f.name}>{f.name}</span>
                        <span className="mfile-size">{bytes(f.size)}</span>
                        <button type="button" onClick={() => set({ files: m.files.filter((_, j) => j !== i) })} aria-label={`Remove ${f.name}`}><X size={13} strokeWidth={1.8} aria-hidden="true" /></button>
                      </li>
                    ))}
                  </ul>
                )}
              </section>

              <section className="mcard">
                <div className="mcard-h">
                  <span className="mstep">3</span>
                  <b>Branding</b>
                  <span className="mcard-sub">Banner and headline, drafted from what this API can do</span>
                </div>
                <div className="mbrand-grid">
                  <label><span className="mlab">Banner title</span><input className="msubj" value={m.brand?.banner_title || ''} maxLength={300} onChange={(e) => set({ brand: { ...m.brand, banner_title: e.target.value } })} /></label>
                  <label><span className="mlab">Banner subtitle</span><input className="msubj" value={m.brand?.banner_subtitle || ''} maxLength={300} onChange={(e) => set({ brand: { ...m.brand, banner_subtitle: e.target.value } })} /></label>
                  <label className="wide"><span className="mlab">Headline</span><input className="msubj" value={m.brand?.headline || ''} maxLength={300} onChange={(e) => set({ brand: { ...m.brand, headline: e.target.value } })} /></label>
                </div>
                {caps.data?.tables?.some((t) => t.error) && <p className="hint" style={{ margin: 0 }}>Some tables couldn&apos;t be checked, so the draft only claims what was confirmed.</p>}
              </section>
            </fieldset>
            <div className="mprevwrap">
              <div className="between" style={{ alignItems: 'center', marginBottom: 12 }}>
                <span className="row" style={{ gap: 8 }}><span className="mlive" /><span style={{ fontSize: 10.5, letterSpacing: '.22em', color: 'var(--ink2)' }}>LIVE PREVIEW</span></span>
                <span className="row" style={{ gap: 12 }}>
                  <label className="check" style={{ fontSize: 11.5 }}><input type="checkbox" checked={m.showKey} onChange={(e) => set({ showKey: e.target.checked })} disabled={!api.api_key} /> show full key</label>
                  <span className="mdev" role="group" aria-label="Preview size">
                    {[['desktop', 'Desktop'], ['mobile', 'Mobile']].map(([id, l]) => (
                      <button key={id} type="button" aria-pressed={m.device === id} className={m.device === id ? 'on' : ''} onClick={() => set({ device: id })}>{l}</button>
                    ))}
                  </span>
                </span>
              </div>
              <Preview api={api} m={m} opts={o} sender={o.sender} device={m.device} />
              <span className="hint" style={{ textAlign: 'center', display: 'block', marginTop: 10 }}>The real key is filled in by the server at send time, inside the encrypted message only. History keeps a masked copy.</span>
            </div>
          </div>
          <div className="dft" style={{ flexWrap: 'wrap' }}>
            <span style={{ fontSize: 12, color: err || why ? 'var(--red)' : 'var(--muted)', flex: 1, minWidth: 220 }} role={err ? 'alert' : undefined}>
              {err ? `Not sent: ${err.message}` : m.typing ? 'Drafting. You can edit as soon as it finishes.' : why || `Sends from ${o.sender || 'SmartHub'}, labelled ${lab?.path.replace(/\\/g, '/') || ''}.`}
            </span>
            <SensitivityMenu labels={o.labels} value={m.label} onChange={(label) => set({ label })} disabled={m.stage === 'sending'} />
            <button type="button" className="btn" onClick={() => send(true)} disabled={!canTest} data-tip={`Goes only to ${o.sender || 'you'}`}>
              {m.stage === 'sending' && m.test ? <><Spinner />Sending test</> : 'Send test to me'}
            </button>
            <button type="button" className="btn red" style={{ padding: '11px 18px' }} onClick={() => send(false)} disabled={!canSend}>
              {m.stage === 'sending' && !m.test ? <><Spinner />Sending…</> : <><Lock size={13} strokeWidth={1.8} aria-hidden="true" /> Send encrypted</>}
            </button>
          </div>
        </>
      )}
    </Drawer>
  );
}