import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { CircleAlert, Plus, Sparkles } from 'lucide-react';
import { apiFetch } from '../../utils/apiClient';
import { request, RequestError, useResource, useToast } from './lib';
import { ErrorBox, InlineError, Seg, Select, Sk, Spinner, Toggle } from './ui';

/* Semantic search is configured entirely here; nothing about a table, model or
   response shape lives in code. Two scopes:
     - per alias (this API): on/off, columns, modes, tuning, response template
     - shared library: embedding models, which model built which column
       (shared by every API that publishes the table), versioned templates.
   Every save is validated by the server against the live source catalog and
   rejected with a report if anything would not work. */

const enc = encodeURIComponent;
const MODES = [['hybrid', 'Hybrid (meaning + keywords)'], ['dense', 'Meaning only'], ['sparse', 'Keywords only']];
const MODE_SHORT = { hybrid: 'Hybrid', dense: 'Meaning', sparse: 'Keywords' };
const DEFAULTS = {
  enabled: true, vector_column: '', metric: 'cosine', text_column: '', text_search_config: '',
  modes: null, default_mode: '', rrf_k: 60, dense_weight: 1, sparse_weight: 1, candidate_pool: 50,
  keyword_timeout_ms: 3000, semantic_timeout_ms: '', allow_exact: false, contract_id: '',
};
const CONTRACT_STARTER = JSON.stringify({
  matches_key: 'matches',
  lookups: { doc: { schema: 'schema_name', table: 'document_table', key: 'document_key', on: 'document_key' } },
  fields: { chunk_id: 'chunk_id', chunk_text: 'chunk_text' },
  objects: { document_metadata: { title: 'doc.title' } },
}, null, 2);

/* The settings endpoints answer 422 with a full report; keep it whole. */
async function call(path, { method = 'GET', body } = {}) {
  try {
    const data = await request(path, { method, body });
    return { ok: true, data };
  } catch (e) {
    if (e instanceof RequestError) return { ok: false, status: e.status, detail: e.detail, message: e.message };
    return { ok: false, status: 0, message: e?.message || 'Request failed' };
  }
}

function Report({ report }) {
  if (!report) return null;
  const { errors = [], warnings = [], info = {} } = report;
  const rows = info.row_estimate;
  return (
    <div className="hitbox">
      {errors.map((e) => <div key={e} className="inline-err" role="alert"><span><CircleAlert size={15} strokeWidth={1.8} aria-hidden="true" /></span><span>{e}</span></div>)}
      {warnings.map((w) => <div key={w} className="inline-warn">{w}</div>)}
      {!errors.length && <div className="inline-ok">{report.saved ? 'Saved. ' : 'All checks passed. '}{info.note || ''}</div>}
      {(rows !== undefined || info.vector || info.keyword || info.embedding_test) && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(240px,1fr))', gap: '4px 18px', fontSize: 11.5, color: 'var(--ink2)', padding: '10px 12px', borderRadius: 10, background: 'var(--paper)', border: '1px solid var(--line)' }}>
          {rows !== undefined && <span>Rows (planner estimate): {rows === null ? 'not analyzed' : rows.toLocaleString()}</span>}
          {info.model && <span>Model: {info.model}</span>}
          {info.vector && <span>Vectors: {info.vector.type}({info.vector.dimensions ?? 'variable'}), pgvector {info.vector.pgvector}</span>}
          {info.vector && <span>ANN indexes: {info.vector.ann_indexes.join(', ') || 'none'}</span>}
          {info.keyword && <span>Keyword: {info.keyword.column} ({info.keyword.config})</span>}
          {info.contract && <span>Response template: {info.contract}</span>}
          {info.shared_with && <span>Column also used by: {info.shared_with.join(', ')}</span>}
          {info.embedding_test && <span>Embedding test: {info.embedding_test.dimensions} dims in {info.embedding_test.ms} ms</span>}
        </div>
      )}
      {(info.semantic_plan || info.keyword_plan) && (
        <details className="opts">
          <summary>Query plans (planner only, nothing was run)</summary>
          <div className="ob"><pre className="code" style={{ borderRadius: 10, fontSize: 11, overflowX: 'auto', whiteSpace: 'pre' }}>{[...(info.semantic_plan || []), '', ...(info.keyword_plan || [])].join('\n')}</pre></div>
        </details>
      )}
    </div>
  );
}

const fieldInput = (label, value, onChange, props = {}) => (
  <div>
    <span className="lab">{label}</span>
    <input className="field" value={value ?? ''} onChange={(e) => onChange(e.target.value)} {...props} />
  </div>
);

function AliasEditor({ apiId, table, library, onDone }) {
  const toast = useToast();
  const [state, setState] = useState(null);
  const [loadErr, setLoadErr] = useState(null);
  const [form, setForm] = useState(DEFAULTS);
  const [modelId, setModelId] = useState('');
  const [rebuilt, setRebuilt] = useState(false);
  const [report, setReport] = useState(null);
  const [busy, setBusy] = useState('');
  const [testEmbedding, setTestEmbedding] = useState(false);

  const load = useCallback(async () => {
    setLoadErr(null);
    const r = await call(`/data-api/${enc(apiId)}/tables/${enc(table.id)}/search`);
    if (!r.ok) { setLoadErr(new Error(r.message)); return; }
    setState(r.data);
    const saved = r.data.settings || {};
    const opts = r.data.options || {};
    const next = { ...DEFAULTS, ...saved };
    next.vector_column = saved.vector_column ?? (opts.vector_columns?.[0]?.name || '');
    next.text_column = saved.text_column ?? (opts.text_columns?.find((c) => c.gin_configs.length)?.name || '');
    next.text_search_config = saved.text_search_config || '';
    next.contract_id = saved.contract_id || '';
    next.semantic_timeout_ms = saved.semantic_timeout_ms ?? '';
    setForm(next);
    setModelId(r.data.registrations?.[next.vector_column]?.model_id || '');
  }, [apiId, table.id]);
  useEffect(() => { load(); }, [load]);

  const opts = state?.options || {};
  const set = (k, v) => { setForm((f) => ({ ...f, [k]: v })); setReport(null); };
  const vec = (opts.vector_columns || []).find((c) => c.name === form.vector_column);
  const txt = (opts.text_columns || []).find((c) => c.name === form.text_column);
  const registered = state?.registrations?.[form.vector_column];
  const changingModel = registered && modelId && registered.model_id !== modelId;
  const available = useMemo(() => {
    const a = [];
    if (form.vector_column) a.push('dense');
    if (form.text_column) a.push('sparse');
    if (a.length === 2) a.unshift('hybrid');
    return a;
  }, [form.vector_column, form.text_column]);
  const modes = (form.modes || available).filter((m) => available.includes(m));
  const models = (library.models || []).filter((m) => !vec?.dimensions || m.dimensions === vec.dimensions);

  const payload = () => ({
    settings: {
      ...form,
      vector_column: form.vector_column || null,
      text_column: form.text_column || null,
      text_search_config: form.text_search_config || null,
      contract_id: form.contract_id || null,
      modes: modes.length ? modes : null,
      default_mode: modes.includes(form.default_mode) ? form.default_mode : null,
      rrf_k: Number(form.rrf_k),
      dense_weight: Number(form.dense_weight),
      sparse_weight: Number(form.sparse_weight),
      candidate_pool: Number(form.candidate_pool),
      keyword_timeout_ms: Number(form.keyword_timeout_ms),
      semantic_timeout_ms: form.semantic_timeout_ms === '' || form.semantic_timeout_ms == null ? null : Number(form.semantic_timeout_ms),
    },
    registration: form.vector_column && modelId ? { [form.vector_column]: modelId } : null,
    replace_registration: rebuilt,
  });

  const validate = async () => {
    setBusy('validate');
    const r = await call(`/data-api/${enc(apiId)}/tables/${enc(table.id)}/search/validate`, { method: 'POST', body: { ...payload(), planner: true, test_embedding: testEmbedding } });
    setBusy('');
    setReport(r.ok ? r.data : { errors: [r.message] });
  };
  const save = async () => {
    setBusy('save');
    const r = await call(`/data-api/${enc(apiId)}/tables/${enc(table.id)}/search`, { method: 'PUT', body: payload() });
    setBusy('');
    if (r.ok) { setReport(r.data); toast(`Semantic search saved for /${table.alias}`); onDone(true); return; }
    setReport(r.status === 422 && r.detail?.errors ? r.detail : { errors: [r.message] });
  };
  const remove = async () => {
    setBusy('remove');
    const r = await call(`/data-api/${enc(apiId)}/tables/${enc(table.id)}/search`, { method: 'DELETE' });
    setBusy('');
    if (r.ok) { toast(`Semantic search removed from /${table.alias}`); onDone(true); } else toast(r.message, 'error');
  };

  if (loadErr) return <div style={{ padding: '0 24px 20px' }}><InlineError error={loadErr} onRetry={load} /></div>;
  if (!state) return <div style={{ padding: '0 24px 20px' }} className="row"><Spinner /><span style={{ fontSize: 12, color: 'var(--muted)' }}>Reading the table&apos;s catalog…</span></div>;
  if (!opts.supported) return <p style={{ padding: '0 24px 20px', margin: 0, fontSize: 12.5, color: 'var(--ink2)' }}>{opts.reason || 'Semantic search is not available for this table.'}</p>;

  return (
    <div style={{ padding: '18px 24px 22px', borderTop: '1px solid var(--bg)', display: 'flex', flexDirection: 'column', gap: 16, background: 'var(--paper)' }}>
      <div className="row" style={{ flexWrap: 'wrap', gap: 16, fontSize: 11.5, color: 'var(--ink2)' }}>
        <span>Rows (estimate): <b style={{ fontWeight: 700 }}>{opts.row_estimate == null ? 'not analyzed' : opts.row_estimate.toLocaleString()}</b></span>
        <span>Unique key: <b style={{ fontWeight: 700 }}>{opts.unique_key?.length ? opts.unique_key.join(', ') : 'none (required)'}</b></span>
        <span style={{ flex: 1 }} />
        <label className="row" style={{ gap: 10, fontSize: 12.5 }}>{form.enabled ? 'On' : 'Off'} <Toggle on={form.enabled} onChange={(v) => set('enabled', v)} label="Search enabled for this alias" /></label>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(220px,1fr))', gap: 14 }}>
        <div>
          <span className="lab">Embedding column</span>
          <Select
            value={form.vector_column}
            onChange={(v) => { set('vector_column', v); setModelId(state.registrations?.[v]?.model_id || ''); setRebuilt(false); }}
            options={[{ value: '', label: 'None (keywords only)' }, ...(opts.vector_columns || []).map((c) => ({ value: c.name, label: c.name, sub: `${c.type}(${c.dimensions ?? 'variable'}) · index ${c.ann_metrics.join('/') || 'none'}` }))]}
            mono
            ariaLabel="Embedding column"
          />
        </div>
        <div>
          <span className="lab">Model that built it (shared)</span>
          <Select
            value={modelId}
            onChange={(v) => { setModelId(v); setReport(null); }}
            disabled={!form.vector_column}
            placeholder={models.length ? 'Choose a model' : 'No model with these dimensions'}
            options={models.map((m) => ({ value: m.id, label: m.name, sub: `${m.deployment}, ${m.dimensions}d` }))}
            ariaLabel="Embedding model"
          />
          {registered && <div className="hint">Registered: {registered.model_name}. Every API publishing this table uses it.</div>}
          {changingModel && (
            <label className="check" style={{ marginTop: 6, color: '#9e3a00', alignItems: 'flex-start' }}>
              <input type="checkbox" checked={rebuilt} onChange={(e) => setRebuilt(e.target.checked)} />
              I confirm this column&apos;s vectors were rebuilt with the new model.
            </label>
          )}
        </div>
        <div>
          <span className="lab">Metric</span>
          <Select value={form.metric} onChange={(v) => set('metric', v)} disabled={!form.vector_column} options={[{ value: 'cosine', label: 'Cosine' }, { value: 'inner_product', label: 'Inner product' }]} ariaLabel="Metric" />
        </div>
        <div>
          <span className="lab">Keyword column</span>
          <Select
            value={form.text_column}
            onChange={(v) => { set('text_column', v); set('text_search_config', ''); }}
            options={[{ value: '', label: 'None (meaning only)' }, ...(opts.text_columns || []).map((c) => ({ value: c.name, label: c.name, sub: c.gin_configs.length ? `index: ${c.gin_configs.join(', ')}` : 'no full-text index' }))]}
            mono
            ariaLabel="Keyword column"
          />
        </div>
        <div>
          <span className="lab">Text search configuration</span>
          <Select
            value={form.text_search_config}
            onChange={(v) => set('text_search_config', v)}
            disabled={!form.text_column}
            options={[
              { value: '', label: "Use the index's configuration" },
              ...(txt?.gin_configs || []).filter((c) => !c.startsWith('(')).map((c) => ({ value: c, label: c })),
              ...(txt?.type === 'tsvector' ? ['english', 'simple'].map((c) => ({ value: c, label: c })) : []),
            ]}
            ariaLabel="Text search configuration"
          />
        </div>
        <div>
          <span className="lab">Response template</span>
          <Select
            value={form.contract_id}
            onChange={(v) => set('contract_id', v)}
            options={[{ value: '', label: 'Default (rank, scores, row)' }, ...(library.contracts || []).map((c) => ({ value: c.id, label: `${c.name} v${c.version}` }))]}
            ariaLabel="Response template"
          />
        </div>
      </div>

      <div className="row" style={{ gap: 18, flexWrap: 'wrap' }}>
        <span className="lab" style={{ margin: 0 }}>Modes callers may use</span>
        {MODES.filter(([id]) => available.includes(id)).map(([id, label]) => (
          <label key={id} className="check">
            <input type="checkbox" checked={modes.includes(id)} onChange={(e) => set('modes', e.target.checked ? [...modes, id] : modes.filter((x) => x !== id))} /> {label}
          </label>
        ))}
        {!available.length && <span className="hint" style={{ margin: 0 }}>Pick an embedding or keyword column first.</span>}
        <span style={{ flex: 1 }} />
        {modes.length > 0 && (
          <>
            <span style={{ fontSize: 12, color: 'var(--muted)' }}>default</span>
            <Seg value={modes.includes(form.default_mode) ? form.default_mode : modes[0]} onChange={(v) => set('default_mode', v)} options={modes.map((x) => [x, x])} ariaLabel="Default mode" />
          </>
        )}
      </div>

      <details className="opts">
        <summary>Tuning and limits</summary>
        <div className="ob">
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(170px,1fr))', gap: 12 }}>
            {fieldInput('Fusion constant (RRF k)', form.rrf_k, (v) => set('rrf_k', v), { inputMode: 'numeric' })}
            {fieldInput('Meaning weight', form.dense_weight, (v) => set('dense_weight', v), { inputMode: 'decimal' })}
            {fieldInput('Keyword weight', form.sparse_weight, (v) => set('sparse_weight', v), { inputMode: 'decimal' })}
            {fieldInput('Candidates per search (≤1000)', form.candidate_pool, (v) => set('candidate_pool', v), { inputMode: 'numeric' })}
            {fieldInput('Keyword timeout (ms)', form.keyword_timeout_ms, (v) => set('keyword_timeout_ms', v), { inputMode: 'numeric' })}
            {fieldInput('Semantic timeout (ms)', form.semantic_timeout_ms, (v) => set('semantic_timeout_ms', v), { inputMode: 'numeric', placeholder: `server default ${library.defaults?.semantic_timeout_ms ?? 5000}` })}
          </div>
          <span className="hint" style={{ margin: 0 }}>
            If keyword search runs past its timeout, results fall back to meaning only with a warning. If semantic search runs past its timeout the request fails and names that step: check the plan with Validate before raising it.
          </span>
          <label className="check">
            <input type="checkbox" checked={form.allow_exact} onChange={(e) => set('allow_exact', e.target.checked)} />
            Allow exact search (reads every row: about {opts.row_estimate == null ? '?' : opts.row_estimate.toLocaleString()} here). Keep it off for large tables.
          </label>
        </div>
      </details>

      <Report report={report} />

      <div className="row" style={{ flexWrap: 'wrap', gap: 8 }}>
        <button type="button" className="btn dark" onClick={save} disabled={!!busy || (changingModel && !rebuilt)}>{busy === 'save' ? <><Spinner />Saving…</> : 'Save settings'}</button>
        <button type="button" className="btn" onClick={validate} disabled={!!busy}>{busy === 'validate' ? <><Spinner />Checking…</> : 'Validate'}</button>
        <label className="check"><input type="checkbox" checked={testEmbedding} onChange={(e) => setTestEmbedding(e.target.checked)} /> include one test embedding call</label>
        <span style={{ flex: 1 }} />
        {state.settings && <button type="button" className="btn danger" onClick={remove} disabled={!!busy}>{busy === 'remove' ? 'Removing…' : 'Remove search settings'}</button>}
        <button type="button" className="btn" onClick={() => onDone(false)} disabled={!!busy}>Close</button>
      </div>
    </div>
  );
}

const EMPTY_MODEL = { name: '', deployment: '', dimensions: 1536, endpoint: '', api_version: '', api_key_env: 'AZURE_OPENAI_EMBED_API_KEY', query_prefix: '', send_dimensions: false };

function ModelForm({ initial, allowedHosts, onSaved, onCancel }) {
  const toast = useToast();
  const [m, setM] = useState(initial || EMPTY_MODEL);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const set = (k, v) => setM((x) => ({ ...x, [k]: v }));
  const submit = async () => {
    setBusy(true);
    setErr('');
    const body = { ...m, dimensions: Number(m.dimensions) };
    const r = initial?.id
      ? await call(`/data-api/search/models/${enc(initial.id)}`, { method: 'PUT', body })
      : await call('/data-api/search/models', { method: 'POST', body });
    setBusy(false);
    if (r.ok) { toast(`Model ${body.name} saved`); onSaved(); } else setErr(r.message);
  };
  return (
    <div style={{ padding: 18, borderRadius: 14, border: '1px solid var(--line)', background: 'var(--paper)', display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(200px,1fr))', gap: 12 }}>
        {fieldInput('Name', m.name, (v) => set('name', v), { placeholder: 'rimdocs-embed' })}
        {fieldInput('Azure deployment', m.deployment, (v) => set('deployment', v), { placeholder: 'text-embedding-3-small' })}
        {fieldInput('Dimensions it returns', m.dimensions, (v) => set('dimensions', v), { inputMode: 'numeric' })}
        {fieldInput('API key variable (server)', m.api_key_env, (v) => set('api_key_env', v))}
        {fieldInput('Endpoint (blank = server default)', m.endpoint, (v) => set('endpoint', v), { placeholder: 'https://…' })}
        {fieldInput('API version (blank = server default)', m.api_version, (v) => set('api_version', v), { placeholder: '2024-08-01-preview' })}
        {fieldInput('Query prefix (rare)', m.query_prefix, (v) => set('query_prefix', v))}
      </div>
      <label className="check"><input type="checkbox" checked={m.send_dimensions} onChange={(e) => set('send_dimensions', e.target.checked)} /> Ask the model for this many dimensions (only if the stored vectors were made the same way)</label>
      <span className="hint" style={{ margin: 0 }}>Keys are never stored here: the server reads the named variable. Allowed endpoint hosts: {allowedHosts.join(', ') || 'none configured'}.</span>
      {err && <div className="inline-err" role="alert"><span><CircleAlert size={15} strokeWidth={1.8} aria-hidden="true" /></span><span>{err}</span></div>}
      <div className="row"><button type="button" className="btn dark" onClick={submit} disabled={busy || !m.name.trim()}>{busy ? 'Saving…' : 'Save model'}</button><button type="button" className="btn" onClick={onCancel}>Cancel</button></div>
    </div>
  );
}

function ContractForm({ base, onSaved, onCancel }) {
  const toast = useToast();
  const [name, setName] = useState(base?.name || '');
  const [description, setDescription] = useState('');
  const [text, setText] = useState(base ? JSON.stringify(base.definition, null, 2) : CONTRACT_STARTER);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const submit = async () => {
    let definition;
    try { definition = JSON.parse(text); } catch (e) { setErr(`Not valid JSON: ${e.message}`); return; }
    setBusy(true);
    setErr('');
    const r = await call('/data-api/search/contracts', { method: 'POST', body: { name, description, definition } });
    setBusy(false);
    if (r.ok) { toast(`Saved ${r.data.name} v${r.data.version}`); onSaved(); } else setErr(r.message);
  };
  return (
    <div style={{ padding: 18, borderRadius: 14, border: '1px solid var(--line)', background: 'var(--paper)', display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(220px,1fr))', gap: 12 }}>
        {fieldInput(`Name${base ? ` (saves as v${base.version + 1})` : ''}`, name, setName, { disabled: !!base })}
        {fieldInput('Description', description, setDescription)}
      </div>
      <div>
        <span className="lab">Definition (JSON)</span>
        <textarea className="field mono" style={{ minHeight: 220, fontSize: 12 }} value={text} onChange={(e) => setText(e.target.value)} spellCheck={false} />
        <div className="hint">
          fields and objects map output names to a column (<code>chunk_text</code>) or a lookup column (<code>doc.title</code>). Each lookup joins one table in the same database on a key that is unique there. Templates never change once saved; saving under an existing name makes the next version, so APIs on an earlier version are unaffected.
        </div>
      </div>
      {err && <div className="inline-err" role="alert"><span><CircleAlert size={15} strokeWidth={1.8} aria-hidden="true" /></span><span>{err}</span></div>}
      <div className="row"><button type="button" className="btn dark" onClick={submit} disabled={busy || !name.trim()}>{busy ? 'Saving…' : 'Save template'}</button><button type="button" className="btn" onClick={onCancel}>Cancel</button></div>
    </div>
  );
}

function Library({ library, reload }) {
  const toast = useToast();
  const [modelForm, setModelForm] = useState(null);
  const [contractForm, setContractForm] = useState(null);
  const [busy, setBusy] = useState('');
  const act = async (key, path, okText) => {
    setBusy(key);
    const r = await call(path, { method: 'DELETE' });
    setBusy('');
    if (r.ok) { toast(okText); reload(); } else toast(r.message, 'error');
  };
  const test = async (m) => {
    setBusy(`test-${m.id}`);
    const r = await call(`/data-api/search/models/${enc(m.id)}/test`, { method: 'POST' });
    setBusy('');
    if (r.ok && r.data.ok) toast(`${m.name}: ${r.data.dimensions} dimensions in ${r.data.ms} ms`);
    else toast(`${m.name}: ${r.data?.message || r.message}`, 'error');
  };
  return (
    <div className="panel">
      <div className="between" style={{ alignItems: 'center' }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
          <span className="disp" style={{ fontWeight: 400, fontSize: 19 }}>Shared library</span>
          <span style={{ fontSize: 12, color: 'var(--muted)' }}>Models, column registrations and response templates shared by every API that publishes the table</span>
        </div>
        <div className="row">
          <button type="button" className="btn" onClick={() => setModelForm(modelForm ? null : {})}>{modelForm ? 'Close' : <><Plus size={13} strokeWidth={2} aria-hidden="true" /> Model</>}</button>
          <button type="button" className="btn" onClick={() => setContractForm(contractForm ? null : {})}>{contractForm ? 'Close' : <><Plus size={13} strokeWidth={2} aria-hidden="true" /> Template</>}</button>
        </div>
      </div>
      {modelForm && <ModelForm initial={modelForm.id ? modelForm : null} allowedHosts={library.allowed_hosts || []} onSaved={() => { setModelForm(null); reload(); }} onCancel={() => setModelForm(null)} />}
      {contractForm && <ContractForm base={contractForm.id ? contractForm : null} onSaved={() => { setContractForm(null); reload(); }} onCancel={() => setContractForm(null)} />}

      <span className="label">EMBEDDING MODELS</span>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(260px,1fr))', gap: 10 }}>
        {(library.models || []).map((m) => (
          <div key={m.id} className="machine">
            <div className="between" style={{ alignItems: 'center' }}>
              <code style={{ fontSize: 14 }}>{m.name}</code>
              <span style={{ fontSize: 11, color: m.key_configured ? 'var(--green)' : 'var(--red)' }}>{m.key_configured ? 'key set on server' : `${m.api_key_env} not set`}</span>
            </div>
            <span style={{ fontSize: 12, color: 'var(--muted)' }}>{m.deployment} · {m.dimensions}d · {m.endpoint || 'server endpoint'}</span>
            <span style={{ fontSize: 11.5, color: m.registrations ? 'var(--green)' : 'var(--faint)' }}>{m.registrations ? `Used by ${m.registrations} column${m.registrations === 1 ? '' : 's'}` : 'Not used'}</span>
            <div className="row" style={{ gap: 12 }}>
              <button type="button" className="link" onClick={() => test(m)} disabled={!!busy}>{busy === `test-${m.id}` ? 'Testing…' : 'Test'}</button>
              <button type="button" className="link" onClick={() => setModelForm(m)}>Edit</button>
              <button type="button" className="link" style={{ color: m.registrations ? 'var(--faint)' : 'var(--red)' }} disabled={m.registrations > 0 || !!busy} onClick={() => act(`m-${m.id}`, `/data-api/search/models/${enc(m.id)}`, `Deleted ${m.name}`)} data-tip={m.registrations ? 'In use, remove its registrations first' : undefined}>Delete</button>
            </div>
          </div>
        ))}
        {!library.models?.length && !modelForm && <span className="hint" style={{ margin: 0 }}>No models yet. Add the model that built your table&apos;s vectors.</span>}
      </div>

      <span className="label">COLUMN REGISTRATIONS</span>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        {(library.registrations || []).map((r) => (
          <div key={r.id} className="row" style={{ padding: '10px 12px', borderRadius: 10, background: 'var(--bg)', flexWrap: 'wrap' }}>
            <code style={{ fontSize: 12.5 }}>{r.schema}.{r.table}.{r.column}</code>
            <span style={{ fontSize: 12, color: 'var(--ink2)' }}>{r.source_system} · {(r.env || '').toUpperCase()} · {r.model_name}</span>
            <span style={{ flex: 1, fontSize: 11.5, color: 'var(--muted)' }}>{r.used_by.length ? `used by ${r.used_by.join(', ')}` : 'not in use'}</span>
            <button type="button" className="link" disabled={r.used_by.length > 0 || !!busy} onClick={() => act(`r-${r.id}`, `/data-api/search/registrations/${enc(r.id)}`, 'Registration removed')}>Remove</button>
          </div>
        ))}
        {!library.registrations?.length && <span className="hint" style={{ margin: 0 }}>None yet. A registration is made when an alias is saved with a model.</span>}
      </div>

      <span className="label">RESPONSE TEMPLATES</span>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(260px,1fr))', gap: 10 }}>
        {(library.contracts || []).map((c) => (
          <div key={c.id} className="machine">
            <code style={{ fontSize: 14 }}>{c.name} <span style={{ color: 'var(--muted)' }}>v{c.version}</span></code>
            <span style={{ fontSize: 12, color: 'var(--muted)' }}>
              {Object.keys(c.definition.fields || {}).length} fields · {Object.keys(c.definition.objects || {}).length} objects · {Object.keys(c.definition.lookups || {}).length} lookups
            </span>
            <span style={{ fontSize: 11.5, color: c.used_by.length ? 'var(--green)' : 'var(--faint)' }}>{c.used_by.length ? `used by ${c.used_by.join(', ')}` : 'not in use'}</span>
            <div className="row" style={{ gap: 12 }}>
              <button type="button" className="link" onClick={() => setContractForm(c)}>New version</button>
              <button type="button" className="link" style={{ color: c.used_by.length ? 'var(--faint)' : 'var(--red)' }} disabled={c.used_by.length > 0 || !!busy} onClick={() => act(`c-${c.id}`, `/data-api/search/contracts/${enc(c.id)}`, `Deleted ${c.name} v${c.version}`)}>Delete</button>
            </div>
          </div>
        ))}
        {!library.contracts?.length && !contractForm && <span className="hint" style={{ margin: 0 }}>None yet. Aliases without a template return rank, scores and the row.</span>}
      </div>
    </div>
  );
}

/* Plain-language search against the live alias, through /retrieve, exactly as
   a caller would run it. */
function AskBar({ api, tables, summary }) {
  const enabled = tables.filter((t) => summary?.aliases?.[t.id]?.settings?.enabled);
  const [alias, setAlias] = useState(enabled[0]?.alias || '');
  const t = tables.find((x) => x.alias === alias);
  const settings = t ? summary?.aliases?.[t.id]?.settings : null;
  const modes = settings?.modes || [];
  const [mode, setMode] = useState('');
  const [k, setK] = useState(5);
  const [q, setQ] = useState('');
  const [res, setRes] = useState({ status: 'idle' });
  const ctrl = useRef(null);
  useEffect(() => () => ctrl.current?.abort(), []);
  useEffect(() => { if (!enabled.some((x) => x.alias === alias)) setAlias(enabled[0]?.alias || ''); }, [enabled, alias]);

  const run = async () => {
    if (!q.trim() || !alias) return;
    ctrl.current?.abort();
    const c = new AbortController();
    ctrl.current = c;
    setRes({ status: 'loading' });
    const t0 = performance.now();
    try {
      const r = await apiFetch(`/data-api/${enc(api.slug)}/${enc(alias)}/retrieve`, {
        method: 'POST',
        signal: c.signal,
        headers: { 'Content-Type': 'application/json', 'X-API-Key': api.api_key },
        body: JSON.stringify({ retrieval_queries: [{ query: q.trim(), k, ...(mode ? { mode } : {}) }] }),
      });
      const data = await r.json().catch(() => null);
      if (!r.ok) throw new Error(typeof data?.detail === 'string' ? data.detail : data?.detail?.message || `The server answered ${r.status}.`);
      const first = data?.results?.[0] || {};
      const matches = Object.entries(first).find(([key, v]) => key !== 'k' && Array.isArray(v))?.[1] || [];
      setRes({ status: 'done', matches, ms: Math.round(performance.now() - t0), warnings: data?.warnings || [] });
    } catch (e) {
      if (e?.name !== 'AbortError') setRes({ status: 'error', error: e });
    }
  };

  const words = q.toLowerCase().split(/\s+/).filter((w) => w.length > 3).map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  const mark = (text) => {
    const s = String(text ?? '');
    if (!words.length) return s;
    const re = new RegExp(`(${words.join('|')})`, 'gi');
    return s.split(re).map((part, i) => (i % 2 ? <mark key={i}>{part}</mark> : part));
  };
  const describe = (m) => {
    const flat = { ...(m.row || {}), ...Object.fromEntries(Object.entries(m).filter(([key]) => !['rank', 'score', 'dense_score', 'sparse_score', 'row'].includes(key))) };
    const strings = Object.entries(flat).filter(([, v]) => typeof v === 'string' && v.trim());
    const title = strings.find(([key]) => /title|name|subject|heading/i.test(key)) || strings.find(([, v]) => v.length < 140) || strings[0];
    const body = strings.filter((x) => x !== title).sort((a, b) => b[1].length - a[1].length)[0];
    const objects = Object.entries(flat).filter(([, v]) => v && typeof v === 'object' && !Array.isArray(v));
    const meta = objects.flatMap(([, o]) => Object.values(o).filter((v) => typeof v === 'string')).slice(0, 2).join(' · ');
    return { title: title?.[1] || `Match ${m.rank}`, body: body?.[1] || '', meta };
  };

  if (!enabled.length) {
    return (
      <div className="qbar" style={{ filter: 'grayscale(1)', opacity: 0.6 }}>
        <div><span style={{ color: 'var(--red)', display: 'inline-flex' }}><Sparkles size={18} strokeWidth={1.8} aria-hidden="true" /></span><input disabled placeholder="Turn semantic search on for an alias below, then ask it anything" aria-label="Question" /></div>
      </div>
    );
  }

  return (
    <>
      <div className="row" style={{ flexWrap: 'wrap', gap: 6 }}>
        {enabled.map((x) => (
          <button key={x.alias} type="button" className={`chip ${x.alias === alias ? 'on' : ''}`} style={{ fontFamily: 'var(--mono)' }} onClick={() => { setAlias(x.alias); setMode(''); setRes({ status: 'idle' }); }}>/{x.alias}</button>
        ))}
      </div>
      <form className="qbar" onSubmit={(e) => { e.preventDefault(); run(); }}>
        <div>
          <span style={{ color: 'var(--red)', display: 'inline-flex' }}><Sparkles size={18} strokeWidth={1.8} aria-hidden="true" /></span>
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="e.g. sterility failures in aseptic filling" aria-label="Question" maxLength={2000} />
          {modes.length > 1 && <Seg value={mode || settings.default_mode || modes[0]} onChange={setMode} options={modes.map((x) => [x, MODE_SHORT[x] || x])} ariaLabel="Mode" />}
          <button type="submit" className="btn dark" style={{ padding: '12px 20px', borderRadius: 13, fontSize: 13 }} disabled={!q.trim() || res.status === 'loading'}>
            {res.status === 'loading' ? <><Spinner />Searching</> : 'Search'}
          </button>
        </div>
      </form>
      <div className="row" style={{ gap: 18, fontSize: 12, color: 'var(--muted)', flexWrap: 'wrap' }}>
        <span>top k <Seg value={k} onChange={setK} options={[[5, '5'], [10, '10'], [20, '20']]} style={{ display: 'inline-flex', verticalAlign: 'middle', marginLeft: 6 }} btnStyle={{ padding: '3px 9px' }} ariaLabel="Top K" /></span>
        <span>runs /{alias}/retrieve with this API&apos;s key, so it shows up in Activity</span>
        {res.status === 'done' && <span>{res.matches.length} match{res.matches.length === 1 ? '' : 'es'} in {res.ms} ms</span>}
      </div>
      {res.status === 'loading' && [0, 1, 2].map((i) => <div key={i} className="hit" style={{ animationDelay: `${i * 80}ms` }}><Sk h={28} w={60} /><div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}><Sk w="60%" h={14} /><Sk h={11} /><Sk w="80%" h={11} /></div><Sk h={6} /></div>)}
      {res.status === 'error' && <InlineError error={res.error} onRetry={run} />}
      {res.status === 'done' && res.warnings.map((w) => <div key={w} className="inline-warn">{w}</div>)}
      {res.status === 'done' && !res.matches.length && <p className="empty">Nothing matched. Try other words, or switch mode.</p>}
      {res.status === 'done' && res.matches.map((m, i) => {
        const d = describe(m);
        return (
          <div key={`${m.rank}-${i}`} className="hit" style={{ animationDelay: `${i * 80}ms` }}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
              <span className="disp num" style={{ fontWeight: 400, fontSize: 28 }}>{Number(m.score ?? 0).toFixed(3)}</span>
              <span style={{ fontSize: 10, letterSpacing: '.14em', color: 'var(--faint)' }}>#{m.rank}{m.dense_score != null ? ' · MEANING' : ''}{m.sparse_score != null ? ' · WORDS' : ''}</span>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 5, minWidth: 0 }}>
              <span style={{ fontSize: 14.5, fontWeight: 700 }}>{mark(d.title)}</span>
              {d.body && <span style={{ fontSize: 13, color: 'var(--ink2)', lineHeight: 1.55, display: '-webkit-box', WebkitLineClamp: 3, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}>{mark(d.body)}</span>}
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              <div className="meter"><i style={{ width: `${Math.min(100, (m.dense_score ?? m.sparse_score ?? m.score ?? 0) * 100)}%`, background: 'linear-gradient(90deg,var(--red),#ff6017)' }} /></div>
              <span style={{ fontSize: 11, color: 'var(--faint)', textAlign: 'right', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{d.meta}</span>
            </div>
          </div>
        );
      })}
    </>
  );
}

export default function SemanticTab({ api }) {
  const tables = api.tables || [];
  const [open, setOpen] = useState(null);
  const data = useResource(async (signal) => {
    const [library, summary] = await Promise.all([
      request('/data-api/search/library', { signal }),
      request(`/data-api/${enc(api.id)}/search`, { signal }).catch((e) => {
        if (e?.name === 'AbortError') throw e;
        return { aliases: {} };
      }),
    ]);
    return { library, summary };
  }, [api.id]);

  const library = data.data?.library;
  const summary = data.data?.summary;
  const reload = () => data.reload({ quiet: true });

  return (
    <>
      <div className="sechead" style={{ marginBottom: 0 }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          <span className="kicker">SEMANTIC SEARCH</span>
          <h2 className="h-l">Ask it in <em>plain language.</em></h2>
        </div>
        <span style={{ fontSize: 12, color: 'var(--muted)', maxWidth: 420 }}>Off until you turn it on. Each alias is set up on its own, and nothing is written back to the source.</span>
      </div>

      {data.loading && !data.data && (
        <>
          <Sk h={64} r={20} />
          <div className="panel">{[0, 1, 2].map((i) => <Sk key={i} h={44} r={12} />)}</div>
        </>
      )}
      {data.error && !data.data && <div className="panel"><ErrorBox error={data.error} onRetry={() => data.reload()} title="Couldn't load semantic search" /></div>}
      {library && !library.installed && (
        <div className="panel"><ErrorBox error={{ message: library.message }} title="Semantic search isn't set up on this server" /><span className="hint" style={{ textAlign: 'center' }}>Everything else on this API keeps working.</span></div>
      )}

      {library?.installed && (
        <>
          <AskBar api={api} tables={tables} summary={summary} />
          <div className="panel" style={{ padding: 0, gap: 0 }}>
            <div style={{ padding: '20px 24px', borderBottom: '1px solid var(--bg)', display: 'flex', flexDirection: 'column', gap: 3 }}>
              <span className="disp" style={{ fontWeight: 400, fontSize: 19 }}>Per alias</span>
              <span style={{ fontSize: 12, color: 'var(--muted)' }}>Every save is checked against the live source and rejected with a report if anything would not work.</span>
            </div>
            {tables.map((t) => {
              const s = summary?.aliases?.[t.id];
              const on = s?.settings?.enabled;
              return (
                <div key={t.id} style={{ borderBottom: '1px solid var(--bg)' }}>
                  <div className="row" style={{ padding: '14px 24px', flexWrap: 'wrap' }}>
                    <code style={{ fontSize: 14 }}>/{t.alias}</code>
                    <span style={{ fontSize: 12, color: 'var(--muted)' }}>{[t.schema, t.table].filter(Boolean).join('.')}</span>
                    <span className="kchip" style={{ background: on ? '#32871414' : 'var(--bg)', color: on ? 'var(--green)' : 'var(--muted)' }}>
                      {on ? `ON · ${(s.settings.modes || []).join(', ').toUpperCase()}` : s ? 'OFF' : 'NOT SET UP'}
                    </span>
                    {s?.model && <span className="kchip" style={{ background: 'var(--bg)', color: 'var(--ink2)' }}>{s.model}</span>}
                    {s?.contract && <span className="kchip" style={{ background: 'var(--bg)', color: 'var(--ink2)' }}>{s.contract}</span>}
                    <span style={{ flex: 1 }} />
                    <button type="button" className="btn" onClick={() => setOpen(open === t.id ? null : t.id)} aria-expanded={open === t.id}>{open === t.id ? 'Close' : 'Configure'}</button>
                  </div>
                  {open === t.id && (
                    <AliasEditor apiId={api.id} table={t} library={library} onDone={(changed) => { setOpen(null); if (changed) reload(); }} />
                  )}
                </div>
              );
            })}
          </div>
          <Library library={library} reload={reload} />
        </>
      )}
    </>
  );
}