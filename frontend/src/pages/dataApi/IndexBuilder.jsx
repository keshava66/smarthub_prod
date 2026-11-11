import React, { useCallback, useEffect, useRef, useState } from 'react';
import { request, usePage, useToast } from './lib';
import {
  BuildPane, Confirm, ConfirmBuild, DIAG, DiagPane, I, ResultsPane, ResultsSkeleton, Rail, Spin, Sql, UsagePane, UsageSkeleton,
  ago, compact, plural, scoreOf, titleOf, usageLists,
} from '../dataStudio/indexer';
import '../dataStudio/indexer.css';

/* Index Health inside the export page. Same components and look as Data Studio's
   Index Health tab (pages/dataStudio/indexer.jsx), for the one Postgres table
   being exported: describe usage, diagnose, build, with the same live log.
   Backend: backend/indexer.py. */

const qs = (o) => new URLSearchParams(Object.entries(o).filter(([, v]) => v != null && v !== '')).toString();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const check = (t, lists) => request(`/index-health?${new URLSearchParams({
  ...t, search: (lists.search || []).join(','), filter: (lists.filter || []).join(','), vector: (lists.vector || []).join(','),
})}`);

// pg_size_pretty ("8192 bytes", "16 kB") re-scaled to B / KB / MB / GB / TB
const UNITS = ['B', 'KB', 'MB', 'GB', 'TB'];
const size = (s) => {
  const m = /^([\d.]+)\s*(bytes|kB|MB|GB|TB)$/i.exec(s || '');
  if (!m) return s || '?';
  let n = +m[1] * 1024 ** (m[2].toLowerCase() === 'bytes' ? 0 : UNITS.indexOf(m[2].toUpperCase()));
  let u = 0;
  while (n >= 1024 && u < UNITS.length - 1) { n /= 1024; u += 1; }
  return `${n >= 100 ? Math.round(n) : +n.toFixed(1)} ${UNITS[u]}`;
};

const STATE = { ok: ['Healthy', '#328714'], unused: ['Never used', '#c26a00'], redundant: ['Duplicate', '#d9480f'], invalid: ['Broken', '#eb1700'] };

/* Every index on the table today, before anything is diagnosed. */
function ExistingIndexes({ indexes }) {
  const [openIx, setOpenIx] = useState(null);
  return (
    <div className="ih-card ih-pop">
      <div className="ih-card-h">
        <div className="ih-col-gap">
          <strong className="ih-h3">Existing indexes</strong>
          <span className="ih-sub">{indexes.length ? `${plural(indexes.length, 'index', 'indexes')} on this table. Click one for its definition.` : 'None yet. Every query reads the whole table.'}</span>
        </div>
      </div>
      {indexes.length > 0 && (
        <div className="ih-htable" role="table">
          <div className="ih-hrow head" role="row"><span>Index</span><span>Columns · method</span><span>Scans</span><span>State</span></div>
          {indexes.map((ix, k) => {
            const [label, color] = STATE[ix.status] || STATE.ok;
            return (
              <React.Fragment key={ix.name}>
                <button className="ih-hrow ih-rise" role="row" style={{ '--d': `${0.03 * Math.min(k, 15)}s` }} onClick={() => setOpenIx(openIx === ix.name ? null : ix.name)} aria-expanded={openIx === ix.name}>
                  <span className="ih-hname">{ix.primary && <span className="ih-c-muted" data-tip="Primary key"><I n="key" s={14} /></span>}<code>{ix.name}</code></span>
                  <span className="ih-c-muted">{ix.columns.map((c) => c || '(expr)').join(', ')} · {ix.method}{ix.unique && !ix.primary ? ' · unique' : ''}{ix.partial ? ' · partial' : ''}</span>
                  <span className="ih-c-muted tnum" data-tip={`${(ix.scans ?? 0).toLocaleString()} scans since stats were last reset`}>{compact(ix.scans ?? 0)}</span>
                  <span className="ih-state" style={{ color }} data-tip={ix.note || size(ix.size)}><i style={{ background: color }} />{label}</span>
                </button>
                {openIx === ix.name && <div className="ih-hdef">{ix.note && <small>{ix.note}</small>}<Sql sql={`${ix.definition};`} /><small>{size(ix.size)} on disk</small></div>}
              </React.Fragment>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default function IndexBuilder({ env, database, schema, table }) {
  const { can } = usePage();
  const toast = useToast();
  const canBuild = can('buildPGindexer');
  const target = useRef({ env, database: database || '', schema, table }).current;

  const [open, setOpen] = useState(false);
  const [cols, setCols] = useState({ status: 'idle' });
  const [now, setNow] = useState(null); // table as it is, from a check with no usage
  const [usage, setUsage] = useState({});
  const [step, setStepState] = useState('usage');
  const [diag, setDiag] = useState(null);
  const [result, setResult] = useState(null);
  const [rtab, setRtab] = useState(null);
  const [picked, setPicked] = useState(new Set());
  const [keyCol, setKeyCol] = useState('');
  const [job, setJob] = useState(null);
  const [layer, setLayer] = useState(null); // confirm | cancel
  const [starting, setStarting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [runErr, setRunErr] = useState('');

  const setStep = useCallback((s) => setStepState(s), []);

  // first open: columns for the usage grid, plus the indexes as they are now
  useEffect(() => {
    if (!open || cols.status !== 'idle') return;
    setCols({ status: 'loading' });
    Promise.all([request(`/index-health/columns?${qs(target)}`), check(target, {})])
      .then(([r, n]) => {
        setCols({ status: 'ready', columns: r.columns, info: r.table });
        setUsage(Object.fromEntries(r.columns.map((c) => [c.name, c.suggested])));
        setNow(n);
      })
      .catch((e) => setCols({ status: 'error', error: e.message }));
  }, [open, cols.status, target]);

  const applyResult = useCallback((r) => {
    setResult(r);
    setNow(r);
    // the key and drops are never pre-ticked: both are a choice, not a default
    setPicked(new Set(r.missing.filter((m) => m.buildable && m.priority !== 'info' && m.feature !== 'key').map((m) => m.id)));
    setKeyCol(r.missing.find((m) => m.feature === 'key')?.candidates?.[0] || '');
  }, []);

  const runDiagnosis = async () => {
    setRunErr(''); setDiag(0); setStep('diagnosing');
    try {
      // the floor keeps the checklist readable on fast tables
      const [r] = await Promise.all([check(target, usageLists(usage)), sleep(1700)]);
      setDiag(DIAG.length);
      await sleep(320);
      applyResult(r);
      setRtab(null);
      setStep('results');
    } catch (e) {
      setRunErr(e.message);
      setStep('usage');
    } finally {
      setDiag(null);
    }
  };

  useEffect(() => {
    if (diag == null || diag >= DIAG.length - 1) return undefined;
    const id = setTimeout(() => setDiag((d) => (d == null ? d : Math.min(d + 1, DIAG.length - 1))), 380);
    return () => clearTimeout(id);
  }, [diag]);

  const findings = result ? [...result.missing, ...(result.cleanup || [])] : [];
  const pickedItems = findings.filter((m) => picked.has(m.id));
  const sqlFor = useCallback((m) => (m.feature === 'key' && keyCol ? m.sql.replaceAll('<id_column>', `"${keyCol.replaceAll('"', '""')}"`) : m.sql), [keyCol]);

  const startBuild = async () => {
    const r = result;
    setStarting(true); setRunErr('');
    try {
      const res = await request('/index-health/build', {
        method: 'POST',
        body: {
          env, database: database || null, schema_name: schema, table,
          items: pickedItems.map((m) => m.id), search: r.checked.search, filter: r.checked.filter, vector: r.checked.vector,
          key_column: picked.has('key') ? keyCol : undefined,
        },
      });
      const byId = Object.fromEntries(findings.map((m) => [m.id, m]));
      setJob({
        id: res.id, target, checked: r.checked, status: res.status, log: res.log || [], next: res.next || 0, loaded: true, finished: null,
        t0: Date.now(), before: scoreOf(r.summary),
        items: res.items.map((id) => ({ id, feature: byId[id]?.feature, column: id === 'key' ? keyCol : byId[id]?.column, name: byId[id] ? titleOf(byId[id]) : id })),
      });
      setLayer(null);
      setStep('build');
      toast('Build started on the server. It keeps going if you add the table and move on.');
    } catch (e) {
      setLayer(null);
      setRunErr(e.message);
      toast(e.message, 'error');
    } finally {
      setStarting(false);
    }
  };

  const cancelJob = async () => {
    setLayer(null);
    if (!job || job.finished) return;
    setCancelling(true);
    try {
      await request(`/index-health/jobs/${job.id}/cancel`, { method: 'POST' });
      setJob((j) => ({ ...j, cancelRequested: true }));
      toast(`Cancelling ${table} after the current statement`);
    } catch (e) { toast(e.message, 'error'); } finally { setCancelling(false); }
  };

  // poll the running build; the server keeps going if this unmounts
  const jobRef = useRef(job); jobRef.current = job;
  useEffect(() => {
    if (!job || job.finished) return undefined;
    const id = setTimeout(async () => {
      const j = jobRef.current;
      try {
        const v = await request(`/index-health/jobs/${j.id}?since=${j.next}`);
        const upd = { status: v.status, finished: v.finished, next: v.next, log: [...j.log, ...v.log], endedAt: v.finished ? Date.now() : undefined };
        setJob((x) => ({ ...x, ...upd }));
        if (v.finished) {
          let after = null;
          try { const r = await check(target, j.checked); after = scoreOf(r.summary); applyResult(r); } catch { /* toast still goes out */ }
          setJob((x) => ({ ...x, after }));
          toast(v.status === 'done' ? `${schema}.${table}: health ${j.before ?? '—'} → ${after ?? '—'}` : `${schema}.${table}: build ${v.status}`, v.status === 'done' ? undefined : 'error');
        }
      } catch (e) {
        setJob((x) => ({ ...x, finished: 'lost', status: 'lost', endedAt: Date.now(), log: [...x.log, { ts: '', level: 'warn', msg: e.message }] }));
      }
    }, 1500);
    return () => clearTimeout(id);
  }, [job, target, schema, table, applyResult, toast]);

  useEffect(() => {
    if (!layer) return undefined;
    const onKey = (e) => { if (e.key === 'Escape') setLayer(null); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [layer]);

  const running = !!job && !job.finished;
  const shown = step === 'results' && !result ? 'usage' : step === 'build' && !job ? 'usage' : step;
  const score = (shown === 'results' || shown === 'build') && result ? scoreOf(result.summary) : null;
  const info = cols.info;

  return (
    <div className="ixb">
      <button type="button" className="ixb-head" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <span className="ixb-ico"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M13 2 4 14h7l-1 8 9-12h-7z" /></svg></span>
        <span className="ixb-ht"><b>Index this table</b><span>Optional. Tell us how callers query it and we build the indexes that keep the API fast.</span></span>
        {running && <span className="ixb-live">Building</span>}
        <span className={`ixb-chev ${open ? 'open' : ''}`}><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg></span>
      </button>

      {open && (
        <div className="ih-scope ixb-body">
          <div className="ixb-bar">
            <span className={`ixb-env ${env === 'prod' ? 'prod' : ''}`} data-tip={env === 'prod' ? 'Production: builds change live data' : `${env.toUpperCase()} cluster`}>{env}</span>
            <span className="ixb-meta">
              {!info && cols.status === 'loading' && <span><Spin /> Reading {schema}.{table}…</span>}
              {info && (
                <>
                  <span data-tip="Estimate from pg_class.reltuples">{info.rows_estimate != null ? `~${compact(info.rows_estimate)} rows` : 'Row count unknown'}</span>
                  <span data-tip="Table, TOAST and indexes">{size(info.size)}</span>
                  <span className={info.last_analyze ? '' : 'bad'} data-tip={info.last_analyze ? `Last ANALYZE: ${new Date(info.last_analyze).toLocaleString()}` : 'The planner is guessing row counts'}>
                    {info.last_analyze ? `Analyzed ${ago(info.last_analyze)}` : 'Never analyzed'}
                  </span>
                  <span>{plural(cols.columns.length, 'column')}</span>
                  {now && <span>{plural(now.indexes.length, 'index', 'indexes')}</span>}
                </>
              )}
            </span>
            {cols.status === 'ready' && (
              <span className="ixb-tabs" role="tablist" aria-label="Steps">
                {[
                  ['usage', 'Usage', true],
                  ['results', 'Diagnosis', !!result],
                  ['build', 'Build', !!job],
                ].map(([id, label, ok]) => {
                  const on = shown === id || (id === 'results' && (shown === 'diagnosing' || shown === 'checking'));
                  const busyNow = shown === 'diagnosing' || shown === 'checking';
                  return (
                    <button key={id} type="button" role="tab" aria-selected={on} className={on ? 'on' : ''}
                      disabled={!ok || busyNow} onClick={() => setStep(id)}>
                      {label}
                      {id === 'results' && result && result.missing.length > 0 && <span className="n">{result.missing.length}</span>}
                      {id === 'build' && running && <span className="ih-live" />}
                    </button>
                  );
                })}
              </span>
            )}
          </div>

          {cols.status === 'error' ? (
            <div className="ih-banner bad" role="alert"><I n="alert" /><span>{cols.error}</span><button onClick={() => setCols({ status: 'idle' })} aria-label="Try again" data-tip="Try again"><I n="refresh" s={14} /></button></div>
          ) : (
            <>
              {runErr && shown !== 'diagnosing' && (
                <div className="ih-banner bad" role="alert"><I n="alert" /><span>{runErr}</span><button onClick={() => setRunErr('')} aria-label="Dismiss" data-tip="Dismiss"><I n="x" s={14} /></button></div>
              )}
              <div className="ih-cols">
                <div className="ih-panel">
                  {cols.status === 'loading' && <UsageSkeleton rows={8} />}
                  {cols.status === 'ready' && shown === 'usage' && (
                    <>
                      {now && <ExistingIndexes indexes={now.indexes} />}
                      <UsagePane columns={cols.columns} usage={usage} setUsage={setUsage} />
                    </>
                  )}
                  {shown === 'diagnosing' && <DiagPane table={table} at={diag ?? 0} />}
                  {shown === 'checking' && <ResultsSkeleton />}
                  {shown === 'results' && result && (
                    <ResultsPane result={result} picked={picked} setPicked={setPicked} keyCol={keyCol} setKeyCol={setKeyCol} sqlFor={sqlFor}
                      rtab={rtab} setRtab={setRtab} canBuild={canBuild} />
                  )}
                  {shown === 'build' && job && <BuildPane job={job} />}
                </div>
                <Rail step={shown} target={target} cols={cols} result={result} job={job} usage={usage} picked={picked} pickedItems={pickedItems}
                  keyCol={keyCol} canBuild={canBuild} building={running} score={score} checkedBefore={null} cancelling={cancelling}
                  onRun={runDiagnosis} onBuild={() => setLayer('confirm')} onCancel={() => setLayer('cancel')} setStep={setStep} />
              </div>
            </>
          )}
        </div>
      )}

      {layer === 'confirm' && result && (
        <ConfirmBuild target={target} items={pickedItems} sqlFor={sqlFor} starting={starting} onStart={startBuild} onClose={() => setLayer(null)} />
      )}
      {layer === 'cancel' && job && (
        <Confirm title={`Cancel the build on ${table}?`} text="The statement running now is stopped and its index is left invalid. The next diagnosis offers to drop it. Finished items stay."
          yes="Cancel build" no="Keep building" onYes={cancelJob} onClose={() => setLayer(null)} />
      )}
    </div>
  );
}