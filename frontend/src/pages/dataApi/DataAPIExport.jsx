import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ArrowDown, ArrowRight, ArrowUp } from 'lucide-react';
import { useSearchParams } from 'react-router-dom';
import { useAuth } from '../../context/AuthContext';
import { useRBAC } from '../../context/RBACContext';
import Dashboard from './Dashboard';
import CreateWizard, { EmailKeyDrawer, KeyHandoff } from './Publish';
import Workspace from './Workspace';
import { PageContext, rememberSourceLabels, request, requestStream, useLocalSetting, usePage, useResource } from './lib';
import { ConfirmProvider, Kbd, Portal, ToastProvider, TooltipLayer } from './ui';
import './dataApi.css';

/* What the URL says is what is on screen, so every view can be linked,
   bookmarked and walked with the browser's back and forward buttons:

     /data-api                              dashboard
     /data-api?api=<slug>                   that API, Overview tab
     /data-api?api=<slug>&tab=<tab>         a tab (tables, endpoints, search, docs, activity)
     /data-api?api=<slug>&tab=tables&add=1  add-a-table drawer
     /data-api?api=<slug>&tab=tables&edit=<alias>
     /data-api?api=<slug>&tab=endpoints&alias=<alias>
     /data-api?new=1&step=<1-3>             publish wizard
     /data-api?tab=<tab>                    older assistant links: first API, that tab

   The key hand-off, the email drawer and the command palette are not
   addressable on purpose. The hand-off shows a freshly issued key and must
   not survive a reload or a shared link. */

function Page() {
  const { user } = useAuth();
  const { hasPermission } = useRBAC();
  const [sp, setSp] = useSearchParams();
  const [range, setRange] = useLocalSetting('dataApi.dashboardDays', 14);
  const [handoff, setHandoff] = useState(null);
  const [mailFor, setMailFor] = useState(null);
  const [palette, setPalette] = useState(false);

  const overview = useResource(
    (signal) => request(`/data-api/overview?days=${range}`, { signal }),
    [range],
  );
  // Streamed: the full list shows right away, slow connection checks fill in as they finish.
  const sources = useResource(async (signal, push) => {
    let list = [];
    await requestStream('/data-api/sources/stream', {
      signal,
      onLine: (m) => {
        if (m.sources) list = m.sources;
        else if (m.source) list = list.map((s) => (s.id === m.source.id ? m.source : s));
        else return;
        rememberSourceLabels(list);
        push(list);
      },
    });
    return list;
  }, []);

  const apiParam = sp.get('api') || '';
  const tabParam = sp.get('tab') || '';
  const isNew = sp.get('new') === '1';
  const search = sp.toString();

  /* Older links (the assistant's among them) say ?tab=<tab> with no API.
     They meant "this tab on the API you were looking at", which here is the
     first one. */
  useEffect(() => {
    if (apiParam || isNew || !tabParam) return;
    const apis = overview.data?.apis;
    if (!apis) return;
    if (!apis.length) setSp({}, { replace: true });
    else setSp({ api: apis[0].slug, tab: tabParam }, { replace: true });
  }, [apiParam, isNew, tabParam, overview.data, setSp]);

  /* The hand-off belongs to the moment it was opened. Back, forward or any
     other navigation away from that URL closes it. */
  const armed = useRef(false);
  useEffect(() => {
    if (!handoff) return;
    if (handoff.search === search) armed.current = true;
    else if (armed.current) setHandoff(null);
  }, [search, handoff]);

  const nav = useMemo(() => ({
    home: () => setSp({}),
    create: () => setSp({ new: '1' }),
    openApi: (slug, tab = 'overview', extra = {}) =>
      setSp({ api: slug, ...(tab && tab !== 'overview' ? { tab } : {}), ...extra }),
    set: (patch, opts) => {
      const next = new URLSearchParams(sp);
      Object.entries(patch).forEach(([k, v]) => {
        if (v == null || v === '' || v === false) next.delete(k);
        else next.set(k, String(v));
      });
      setSp(next, opts);
    },
  }), [sp, setSp]);

  const refreshOverview = useCallback(() => overview.reload({ quiet: true }), [overview]);

  const showHandoff = useCallback((payload, { slug, rotated }) => {
    const target = new URLSearchParams({ api: slug });
    armed.current = false;
    setHandoff({ ...payload, rotated: !!rotated, search: target.toString() });
    setSp(target, { replace: true });
  }, [setSp]);

  useEffect(() => {
    const onKey = (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setPalette((p) => !p);
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, []);

  const ctx = useMemo(() => ({
    user,
    can: hasPermission,
    overview,
    sources,
    range,
    setRange,
    nav,
    refreshOverview,
    showHandoff,
    openPalette: () => setPalette(true),
    emailKey: (api) => setMailFor(api),
  }), [user, hasPermission, overview, sources, range, setRange, nav, refreshOverview, showHandoff]);

  let view = 'dashboard';
  if (handoff) view = 'handoff';
  else if (isNew) view = 'create';
  else if (apiParam) view = 'workspace';

  return (
    <PageContext.Provider value={ctx}>
      <div className="dax dax-root">
        {view === 'dashboard' && <Dashboard />}
        {view === 'workspace' && <Workspace key={apiParam} slug={apiParam} tabParam={tabParam} />}
        {view === 'create' && <CreateWizard />}
        {view === 'handoff' && (
          <KeyHandoff
            payload={handoff}
            onDone={() => { const slug = handoff.slug; setHandoff(null); nav.openApi(slug); }}
          />
        )}
      </div>
      {palette && <CommandPalette onClose={() => setPalette(false)} />}
      {mailFor && <EmailKeyDrawer api={mailFor} onClose={() => setMailFor(null)} />}
      <TooltipLayer />
    </PageContext.Provider>
  );
}

export default function DataAPIExport() {
  return (
    <ToastProvider>
      <ConfirmProvider>
        <Page />
      </ConfirmProvider>
    </ToastProvider>
  );
}

function CommandPalette({ onClose }) {
  const { overview, nav } = usePage();
  const [q, setQ] = useState('');
  const [sel, setSel] = useState(0);
  const input = useRef(null);
  const list = useRef(null);

  useEffect(() => { input.current?.focus(); }, []);

  const items = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const apis = (overview.data?.apis || [])
      .filter((a) => !needle || [a.name, a.slug, ...(a.aliases || [])].join(' ').toLowerCase().includes(needle))
      .map((a) => ({ g: 'APIS', id: a.id, t: a.name, code: `/data-api/${a.slug}`, live: a.is_active, run: () => nav.openApi(a.slug) }));
    const acts = [
      { g: 'ACTIONS', id: 'new', t: 'Publish a new API', run: nav.create },
      { g: 'ACTIONS', id: 'home', t: 'Go to the dashboard', run: nav.home },
    ].filter((x) => !needle || x.t.toLowerCase().includes(needle));
    return [...apis, ...acts];
  }, [q, overview.data, nav]);

  useEffect(() => { list.current?.querySelector('.pi.on')?.scrollIntoView({ block: 'nearest' }); }, [sel]);

  const go = (it) => { if (!it) return; onClose(); it.run(); };
  const onKey = (e) => {
    if (e.key === 'Escape') { e.preventDefault(); onClose(); }
    if (e.key === 'ArrowDown') { e.preventDefault(); setSel((s) => Math.min(items.length - 1, s + 1)); }
    if (e.key === 'ArrowUp') { e.preventDefault(); setSel((s) => Math.max(0, s - 1)); }
    if (e.key === 'Enter') { e.preventDefault(); go(items[sel]); }
  };

  let group = '';
  return (
    <Portal>
      <div className="pal" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
        <div className="palbox" role="dialog" aria-modal="true" aria-label="Jump to">
          <input
            ref={input}
            value={q}
            onChange={(e) => { setQ(e.target.value); setSel(0); }}
            onKeyDown={onKey}
            placeholder="Search APIs, aliases or actions"
            aria-label="Search"
            autoComplete="off"
          />
          <div className="pl" ref={list} role="listbox">
            {overview.loading && !overview.data && <p className="empty">Loading your APIs…</p>}
            {items.map((it, i) => {
              const head = it.g !== group ? <div className="pg">{(group = it.g)}</div> : null;
              return (
                <React.Fragment key={it.id}>
                  {head}
                  <div
                    role="option"
                    aria-selected={i === sel}
                    className={`pi ${i === sel ? 'on' : ''}`}
                    onMouseEnter={() => setSel(i)}
                    onClick={() => go(it)}
                  >
                    {it.live != null ? <span className={`dot ${it.live ? 'live' : 'off'}`} /> : <span style={{ color: 'var(--red)', display: 'inline-flex' }}><ArrowRight size={14} strokeWidth={1.8} aria-hidden="true" /></span>}
                    {it.t}
                    {it.code && <code>{it.code}</code>}
                  </div>
                </React.Fragment>
              );
            })}
            {!items.length && <p className="empty">No matches.</p>}
          </div>
          <div className="pf"><span><Kbd><ArrowUp size={11} strokeWidth={1.8} aria-hidden="true" /></Kbd> <Kbd><ArrowDown size={11} strokeWidth={1.8} aria-hidden="true" /></Kbd> move</span><span><Kbd>Enter</Kbd> open</span><span><Kbd>Esc</Kbd> close</span></div>
        </div>
      </div>
    </Portal>
  );
}