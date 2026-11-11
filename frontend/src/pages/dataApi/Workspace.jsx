import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { ArrowDown, ArrowLeftRight, Binary, Braces, ChevronDown, ChevronRight, CircleAlert, CircleDashed, Clock, Copy, Download, Ellipsis, EyeOff, FileBox, Hash, KeyRound, Mail, Pause, Pencil, Play, RefreshCw, RotateCw, Search, ToggleLeft, Trash2, TrendingDown, TrendingUp, TriangleAlert, Type } from 'lucide-react';
import { useSearchParams } from 'react-router-dom';
import { buildConsumerDoc, downloadTextFile } from './dataApiDocs';
import EndpointsTab from './Endpoints';
import { ColumnChooser, TablePicker } from './Publish';
import SemanticTab from './SemanticTab';
import { absolute, ALIAS_COLORS, API_COLORS, baseUrl, cleanAlias, copyText, describeAgent, describeIp, emptyPick, ET, fmt, fmtDur, guideFilename, maskKey, parseTs, pctChange, pickReady, pickToSpec, plural, relative, request, shortDate, sum, tableToSpec, TABS, toolOf, TOOLS, useConfirm, useLocalSetting, usePage, usePoll, useResource, useScramble, useSlow, useToast } from './lib';
import { Busybar, CountUp, Dot, Drawer, DrawerHead, EnvTag, ErrorBox, HoldToReveal, Kbd, Orb, RowLimitField, Seg, Sk, SourceLogo, Spark, Spinner, SrcTag, StackChart, StateBox, StatusPill, highlightCode, Steps, ToolTile } from './ui';

function Sidebar({ currentSlug }) {
  const { overview, nav, range, openPalette } = usePage();
  const [q, setQ] = useState('');
  const [filter, setFilter] = useState('all');
  const apis = useMemo(() => overview.data?.apis || [], [overview.data]);

  const colorOf = useMemo(() => {
    const m = {};
    apis.forEach((a, i) => { m[a.id] = API_COLORS[i % API_COLORS.length]; });
    return m;
  }, [apis]);

  const needle = q.trim().toLowerCase();
  const visible = apis
    .filter((a) => (filter === 'all' ? true : filter === 'live' ? a.is_active : !a.is_active))
    .filter((a) => !needle || [a.name, a.description, a.created_by, a.slug, ...(a.aliases || [])]
      .filter(Boolean).join(' ').toLowerCase().includes(needle));
  const live = visible.filter((a) => a.is_active);
  const paused = visible.filter((a) => !a.is_active);

  const item = (a) => {
    const on = a.slug === currentSlug;
    return (
      <button
        key={a.id}
        type="button"
        className={`sitem ${on ? 'on' : ''}`}
        style={{ '--c': colorOf[a.id] }}
        aria-current={on ? 'page' : undefined}
        onClick={() => nav.openApi(a.slug)}
      >
        <div className="row" style={{ gap: 8 }}>
          <Dot live={a.is_active} />
          <span className="nm">{a.name}</span>
          <span className="mini-logos">{(a.sources || []).map((s) => <SourceLogo key={s} id={s} size={16} />)}</span>
        </div>
        <code style={{ fontSize: 10.5, color: 'var(--blue)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>/{a.slug}</code>
        <div className="between" style={{ fontSize: 10.5, color: 'var(--muted)' }}>
          <span>{plural(a.table_count, 'table')}</span>
          <span className="num">
            {a.calls ? `${fmt(a.calls)} calls · ${range}d` : a.last_used_at ? `last call ${relative(a.last_used_at)}` : 'never called'}
          </span>
        </div>
        <Spark values={a.series && a.series.length > 1 ? a.series : [0, 0]} color={colorOf[a.id]} h={26} />
      </button>
    );
  };

  return (
    <aside className="side2" aria-label="Your APIs">
      <div className="sh">
        <button type="button" className="row tlink" onClick={nav.home} data-tip="Back to the dashboard">
          <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minWidth: 0, gap: 4 }}>
            <span style={{ fontSize: 9, letterSpacing: '.24em', color: 'var(--red)' }}>GRA SMARTHUB</span>
            <span className="disp" style={{ fontWeight: 400, fontSize: 20, lineHeight: 1.05 }}>
              Data API <em style={{ color: 'var(--red)', fontWeight: 300 }}>Export</em>
            </span>
          </div>
          <span style={{ fontSize: 10.5, color: 'var(--ink2)', background: '#fff', border: '1px solid var(--line)', borderRadius: 999, padding: '4px 10px', whiteSpace: 'nowrap' }}>
            {overview.data ? `${apis.filter((a) => a.is_active).length} live · ${apis.length}` : '…'}
          </span>
        </button>
        <button type="button" className="btn red" style={{ justifyContent: 'center', padding: '11px 14px', fontSize: 13.5, borderRadius: 12 }} onClick={nav.create}>
          <span style={{ fontSize: 17, lineHeight: 0 }}>+</span> New API
        </button>
        <label className="searchbox">
          <Search size={14} strokeWidth={1.8} color="var(--muted)" aria-hidden="true" />
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search APIs" aria-label="Search APIs" />
          <button type="button" className="tlink" onClick={openPalette} aria-label="Open the command palette"><Kbd>Ctrl K</Kbd></button>
        </label>
        <Seg value={filter} onChange={setFilter} options={[['all', 'All'], ['live', 'Live'], ['paused', 'Inactive']]} style={{ width: '100%' }} ariaLabel="Status" />
      </div>
      <div className="sl">
        {overview.loading && !overview.data && [0, 1, 2, 3].map((i) => (
          <div key={i} style={{ padding: '13px 14px', display: 'flex', flexDirection: 'column', gap: 8 }}>
            <Sk w="70%" h={12} /><Sk w="45%" h={10} /><Sk h={22} />
          </div>
        ))}
        {overview.error && !overview.data && (
          <ErrorBox error={overview.error} onRetry={() => overview.reload()} title="List unavailable" style={{ padding: '30px 10px' }} />
        )}
        {live.length > 0 && <div className="grp"><span>LIVE</span><span>{live.length}</span></div>}
        {live.map(item)}
        {paused.length > 0 && <div className="grp"><span>INACTIVE</span><span>{paused.length}</span></div>}
        {paused.map(item)}
        {overview.data && !visible.length && (
          <p className="empty" style={{ padding: '40px 16px' }}>{apis.length ? 'Nothing matches that search.' : 'No APIs yet.'}</p>
        )}
      </div>
    </aside>
  );
}

const STATUS_TEXT = {
  200: 'OK', 400: 'bad request', 401: 'bad key', 403: 'inactive or forbidden', 404: 'unknown alias',
  429: 'rate limited', 500: 'server error', 502: 'source error', 504: 'source timed out',
};
const statusColor = (s) => (s < 400 ? 'var(--green)' : s === 401 || s === 429 ? '#ff6017' : 'var(--red)');

function KeyCard({ api, onRotate, onEmail }) {
  const toast = useToast();
  const [shown, setShown] = useState(false);
  const text = useScramble(shown ? api.api_key : maskKey(api.api_key), shown);
  useEffect(() => { setShown(false); }, [api.api_key]);

  const copy = async () => {
    const ok = await copyText(api.api_key);
    toast(ok ? 'Key copied. Treat it like a password.' : "Couldn't copy. Your browser blocked the clipboard.", ok ? 'ok' : 'error');
  };

  return (
    <div className="keycard">
      <div className="keyin">
        <div className="between" style={{ position: 'relative', zIndex: 1, gap: 20 }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8, maxWidth: 640 }}>
            <span className="kicker" style={{ color: 'var(--blue)' }}>API KEY</span>
            <span style={{ fontFamily: 'JohnsonDisplay', fontSize: 26, lineHeight: 1.15 }}>
              Anyone with this key can read <em className="em">every table</em> on this API.
            </span>
            <span style={{ fontSize: 13, color: 'var(--ink2)', lineHeight: 1.55 }}>
              No SmartHub login needed. Share it over an encrypted channel and rotate it when a consumer leaves. Rotating revokes the old key immediately.
            </span>
          </div>
          <span style={{ fontSize: 12, color: 'var(--ink2)', alignSelf: 'flex-start', whiteSpace: 'nowrap' }} data-tip={absolute(api.key_rotated_at || api.created_at, true)}>
            Rotated {relative(api.key_rotated_at || api.created_at)}
          </span>
        </div>
        <div className="keyrow" style={{ zIndex: 1 }}>
          <span style={{ opacity: 0.6, display: 'inline-flex' }}><KeyRound size={18} strokeWidth={1.8} aria-hidden="true" /></span>
          <code style={{ flex: 1, minWidth: 240, fontSize: 16, letterSpacing: '.05em', color: 'var(--ink)', wordBreak: 'break-all' }} aria-label={shown ? 'API key' : 'API key, hidden'}>
            {text}
          </code>
          {shown
            ? <button type="button" className="kbtn" onClick={() => setShown(false)}><EyeOff size={14} strokeWidth={1.8} aria-hidden="true" />Hide</button>
            : <HoldToReveal onReveal={() => setShown(true)} />}
          <button type="button" className="kbtn w" onClick={copy}><Copy size={14} strokeWidth={1.8} aria-hidden="true" />Copy key</button>
          <button type="button" className="kbtn" onClick={onEmail} data-tip="Email it under a Highly Restricted label through Communication Center"><Mail size={14} strokeWidth={1.8} aria-hidden="true" />Email key</button>
          <button type="button" className="kbtn r" onClick={onRotate}><RotateCw size={14} strokeWidth={1.8} aria-hidden="true" />Rotate key</button>
        </div>
      </div>
    </div>
  );
}

function CallersPanel({ usage, days, median }) {
  const [group, setGroup] = useState('tool');
  const [sortKey, setSortKey] = useState('calls');
  const [open, setOpen] = useState({});
  const callers = useMemo(() => usage.callers || [], [usage.callers]);

  const rows = useMemo(() => {
    let out;
    if (group === 'tool') {
      const m = {};
      callers.forEach((c) => (c.agents || []).forEach((a) => {
        const tool = toolOf(a.user_agent);
        const t = m[tool] || (m[tool] = { key: tool, tool, kids: [], calls: 0, rows: 0, time: 0, max: null, last: null, timed: 0 });
        t.kids.push({ key: `${c.ip}|${a.user_agent}`, ip: c.ip, ua: a.user_agent, calls: a.calls, rows: a.rows_served, time: a.total_ms || 0, avg: a.avg_ms, max: a.max_ms, last: a.last_call });
        t.calls += a.calls;
        t.rows += a.rows_served;
        t.time += a.total_ms || 0;
        if (a.avg_ms != null) t.timed += a.avg_ms ? Math.round((a.total_ms || 0) / a.avg_ms) : 0;
        if (a.max_ms != null && (t.max == null || a.max_ms > t.max)) t.max = a.max_ms;
        if (!t.last || a.last_call > t.last) t.last = a.last_call;
      }));
      out = Object.values(m).map((t) => ({ ...t, avg: t.timed ? t.time / t.timed : null }));
    } else {
      out = callers.map((c) => ({
        key: c.ip, ip: c.ip, calls: c.calls, rows: c.rows_served, time: c.total_ms || 0, avg: c.avg_ms, max: c.max_ms, last: c.last_call,
        kids: (c.agents || []).map((a) => ({ key: a.user_agent || '(none)', ua: a.user_agent, calls: a.calls, rows: a.rows_served, time: a.total_ms || 0, avg: a.avg_ms, max: a.max_ms, last: a.last_call })),
      }));
    }
    const val = (r) => (sortKey === 'last' ? r.last || '' : r[sortKey] ?? -1);
    return out.sort((x, y) => (sortKey === 'last' ? String(val(y)).localeCompare(String(val(x))) : val(y) - val(x)));
  }, [callers, group, sortKey]);

  const T = {
    calls: sum(rows.map((r) => r.calls)) || 1,
    rows: sum(rows.map((r) => r.rows)) || 1,
    time: sum(rows.map((r) => r.time)) || 1,
  };
  const isOpen = (k) => (open[`${group}:${k}`] ?? (rows[0]?.key === k));
  const speed = (ms) => (ms == null || !median ? '' : ms > median * 1.4 ? 'slow' : ms < median * 0.8 ? 'fast' : '');

  const nums = (r) => (
    <>
      <div className="cnum"><b>{r.calls.toLocaleString()}</b><div className="cbar"><i style={{ width: `${(r.calls / T.calls) * 100}%`, background: 'var(--blue)' }} /></div></div>
      <div className="cnum"><b>{fmt(r.rows)}</b><div className="cbar"><i style={{ width: `${(r.rows / T.rows) * 100}%`, background: 'var(--purple)' }} /></div></div>
      <div className="cnum"><b>{fmtDur(r.time)}</b><div className="cbar"><i style={{ width: `${(r.time / T.time) * 100}%`, background: '#ff6017' }} /></div></div>
      <div className="cnum"><b className={speed(r.avg)}>{fmtDur(r.avg)}</b></div>
      <div className="cnum"><b>{fmtDur(r.max)}</b></div>
      <div className="cnum" style={{ textAlign: 'right' }}><b style={{ fontFamily: 'JohnsonText', fontSize: 12, fontWeight: 400 }} data-tip={absolute(r.last)}>{relative(r.last)}</b></div>
    </>
  );

  const H = [['calls', 'Calls'], ['rows', 'Rows served'], ['time', 'Time taken'], ['avg', 'Avg / call'], ['max', 'Slowest'], ['last', 'Last call']];
  const totalTime = sum(rows.map((r) => r.time));
  const totalCalls = sum(rows.map((r) => r.calls));

  return (
    <div className="panel">
      <div className="ptl">
        <span className="t">Who is calling</span>
        <div className="row" style={{ gap: 10 }}>
          <span className="s">last {days} days · group by</span>
          <Seg value={group} onChange={setGroup} options={[['machine', 'Machine'], ['tool', 'Tool']]} btnStyle={{ padding: '6px 14px' }} ariaLabel="Group callers by" />
        </div>
      </div>
      {!rows.length ? (
        <p className="empty">No callers in the last {days} days.</p>
      ) : (
        <>
          <div className="ctable" role="table">
            <div className="crow head" role="row">
              <div>{group === 'tool' ? 'Tool' : 'Machine'}</div>
              {H.map(([k, l]) => (
                <div
                  key={k}
                  role="columnheader"
                  tabIndex={0}
                  aria-sort={k === sortKey ? 'descending' : 'none'}
                  className={k === sortKey ? 'on' : ''}
                  data-act="sort"
                  style={{ ...(k === 'last' ? { textAlign: 'right' } : {}), cursor: 'pointer' }}
                  onClick={() => setSortKey(k)}
                  onKeyDown={(e) => { if (e.key === 'Enter') setSortKey(k); }}
                >
                  {l}{k === sortKey ? <> <ArrowDown size={12} strokeWidth={1.8} aria-hidden="true" /></> : ''}
                </div>
              ))}
            </div>
            {rows.map((r) => (
              <React.Fragment key={r.key}>
                <div
                  className="crow"
                  role="row"
                  tabIndex={0}
                  aria-expanded={isOpen(r.key)}
                  onClick={() => setOpen((o) => ({ ...o, [`${group}:${r.key}`]: !isOpen(r.key) }))}
                  onKeyDown={(e) => { if (e.key === 'Enter') setOpen((o) => ({ ...o, [`${group}:${r.key}`]: !isOpen(r.key) })); }}
                >
                  <div className="cwho">
                    <span className={`chev2 ${isOpen(r.key) ? 'on' : ''}`}><ChevronRight size={14} strokeWidth={1.8} aria-hidden="true" /></span>
                    {group === 'tool' && <ToolTile tool={r.tool} />}
                    <span>
                      {group === 'tool'
                        ? <><b style={{ fontSize: 13.5 }}>{TOOLS[r.tool].name}</b><small>{[...new Set(r.kids.map((k) => describeIp(k.ip)))].join(', ')}</small></>
                        : <><code>{describeIp(r.ip)}</code><small>{[...new Set(r.kids.map((k) => TOOLS[toolOf(k.ua)].name))].join(' · ')}</small></>}
                    </span>
                  </div>
                  {nums(r)}
                </div>
                {isOpen(r.key) && r.kids.map((k) => (
                  <div className="crow sub" role="row" key={k.key}>
                    <div className="cwho">
                      {group === 'tool'
                        ? <span><code style={{ fontSize: 12.5 }}>{describeIp(k.ip)}</code><small>{describeAgent(k.ua)}</small></span>
                        : <><ToolTile ua={k.ua} size={24} /><span><b>{TOOLS[toolOf(k.ua)].name}</b><small>{describeAgent(k.ua)}</small></span></>}
                    </div>
                    {nums(k)}
                  </div>
                ))}
              </React.Fragment>
            ))}
            <div className="crow cfoot" role="row">
              <div>All callers</div>
              <div className="cnum"><b>{totalCalls.toLocaleString()}</b></div>
              <div className="cnum"><b>{fmt(sum(rows.map((r) => r.rows)))}</b></div>
              <div className="cnum"><b>{fmtDur(totalTime)}</b></div>
              <div className="cnum"><b>{fmtDur(usage.avg_duration_ms)}</b></div>
              <div className="cnum"><b>{fmtDur(Math.max(...rows.map((r) => r.max || 0)))}</b></div>
              <div />
            </div>
          </div>
          <div className="row" style={{ gap: 16, fontSize: 11, color: 'var(--muted)', flexWrap: 'wrap' }}>
            <span className="row" style={{ gap: 6 }}><i className="lg2" style={{ background: 'var(--blue)' }} />share of calls</span>
            <span className="row" style={{ gap: 6 }}><i className="lg2" style={{ background: 'var(--purple)' }} />share of rows</span>
            <span className="row" style={{ gap: 6 }}><i className="lg2" style={{ background: '#ff6017' }} />share of time on the source</span>
            <span><b className="slow" style={{ fontWeight: 700 }}>red</b> avg is noticeably slower than this API&apos;s median · click a row to see {group === 'tool' ? 'which machines use it' : 'its tools'}</span>
            {callers.length >= 25 && <span>Showing the 25 busiest machines.</span>}
          </div>
        </>
      )}
    </div>
  );
}

function OverviewTab({ api, usage, days, setDays, onRotate, onEmail }) {
  const { nav } = usePage();
  const u = usage.data;
  const first = usage.loading && !u;
  const tables = useMemo(() => api.tables || [], [api.tables]);
  const colorOf = Object.fromEntries(tables.map((t, i) => [t.alias, ALIAS_COLORS[i % ALIAS_COLORS.length]]));
  const dates = (u?.buckets || []).map((b) => b.date);

  const series = useMemo(() => {
    const s = u?.alias_series || {};
    const known = tables.map((t) => t.alias);
    const extra = Object.keys(s).filter((a) => !known.includes(a));
    return [...known, ...extra].map((alias, i) => ({
      key: alias,
      name: `/${alias}`,
      color: colorOf[alias] || ALIAS_COLORS[(known.length + i) % ALIAS_COLORS.length],
      vals: s[alias] || dates.map(() => 0),
    }));
  }, [u, tables, colorOf, dates]);

  const statuses = u?.by_status || [];
  const total = sum(statuses.map((s) => s.calls));
  const okCalls = sum(statuses.filter((s) => s.status < 400).map((s) => s.calls));
  const ok = total ? (okCalls / total) * 100 : 100;
  const C = 2 * Math.PI * 50;
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    const id = requestAnimationFrame(() => setArmed(true));
    return () => cancelAnimationFrame(id);
  }, [u]);

  const byAlias = u?.by_alias || [];
  const aliasTop = Math.max(...byAlias.map((a) => a.calls), 1);

  const kpis = [
    { l: `Requests · ${days}d`, v: u?.total_calls, acc: '#eb1700', sp: (u?.buckets || []).map((b) => b.calls), dl: u ? pctChange(u.total_calls, u.prev_total_calls) : null, note: u ? `${u.lifetime_calls.toLocaleString()} since logging began` : '' },
    { l: 'Rows served', v: u?.total_rows_served, acc: '#0f68b2', sp: (u?.buckets || []).map((b) => b.rows_served), note: 'row pulls only' },
    { l: 'Median latency', v: u?.median_duration_ms, unit: 'ms', raw: true, acc: '#328714', note: u?.avg_duration_ms != null ? `avg ${u.avg_duration_ms} ms per call` : 'per call, p50' },
    { l: 'Callers', v: u?.callers?.length, raw: true, acc: '#8c3bbb', note: u?.errors ? `${u.errors.toLocaleString()} rejected call${u.errors === 1 ? '' : 's'}` : 'distinct machines' },
    { l: 'Last call', txt: u ? relative(u.last_call || api.last_used_at) : null, acc: '#ff6017', note: !api.is_active ? 'inactive, calls return 403' : (u?.last_call || api.last_used_at) ? absolute(u?.last_call || api.last_used_at) : 'key accepting calls' },
  ];

  return (
    <>
      {usage.error && !u ? (
        <div className="panel"><ErrorBox error={usage.error} onRetry={() => usage.reload()} title="Usage history unavailable" /></div>
      ) : (
        <>
          <div className="kstrip">
            {kpis.map((k) => (
              <div className="ks" key={k.l} style={{ '--acc': k.acc }}>
                <div className="ksl">
                  <span>{k.l}</span>
                  {k.dl != null && <span className={`delta ${k.dl >= 0 ? 'up' : 'down'}`}>{k.dl >= 0 ? <TrendingUp size={13} strokeWidth={1.8} aria-hidden="true" /> : <TrendingDown size={13} strokeWidth={1.8} aria-hidden="true" />}{Math.abs(k.dl)}%</span>}
                </div>
                <div className="ksv">
                  <span className="kv3">
                    {first ? <Sk w={70} h={22} /> : k.txt != null ? k.txt : k.v == null ? '–' : <CountUp value={k.v} raw={k.raw} />}
                    {k.unit && k.v != null && !first && <small>{k.unit}</small>}
                  </span>
                  {k.sp && <Spark values={k.sp.length > 1 ? k.sp : [0, 0]} color={k.acc} h={28} w={120} className="ksp" />}
                </div>
                <div className="ksn">{first ? <Sk w={100} h={9} /> : k.note}</div>
              </div>
            ))}
          </div>

          {u && u.lifetime_calls === 0 && (
            <div className="panel" style={{ flexDirection: 'row', alignItems: 'center', gap: 14 }}>
              <CircleDashed size={22} strokeWidth={1.8} aria-hidden="true" />
              <span style={{ fontSize: 13.5, color: 'var(--ink2)' }}>
                Nothing has called this API yet. Counts start the moment a consumer hits an endpoint. Grab a snippet from{' '}
                <button type="button" className="link" style={{ fontSize: 13.5 }} onClick={() => nav.openApi(api.slug, 'endpoints')}>Endpoints</button>.
              </span>
            </div>
          )}

          <div className="cgrid" style={{ marginTop: 0 }}>
            <div className="panel">
              <Busybar on={usage.loading && !!u} />
              <div className="ptl">
                <span className="t">Requests per day, by alias</span>
                <Seg value={days} onChange={setDays} options={[[7, '7d'], [14, '14d'], [30, '30d']]} btnStyle={{ padding: '6px 12px' }} ariaLabel="Time window" />
              </div>
              <div className="legend">
                {series.map((s) => (
                  <span className="lg" key={s.key}><i style={{ background: s.color }} /><code style={{ fontSize: 11 }}>{s.name}</code></span>
                ))}
              </div>
              {first ? <Sk h={240} r={14} /> : <StackChart series={series} dates={dates} height={240} monoNames />}
            </div>
            <div className="panel">
              <div className="ptl"><span className="t">Response health</span><span className="s">last {days} days</span></div>
              {first ? <Sk h={118} r={14} /> : (
                <div className="health">
                  <div className="gauge">
                    <svg viewBox="0 0 120 120">
                      <circle cx="60" cy="60" r="50" fill="none" stroke="#f1efed" strokeWidth="12" />
                      <circle
                        className="seg"
                        cx="60" cy="60" r="50" fill="none"
                        stroke={ok > 99 ? '#328714' : ok > 95 ? '#ff6017' : '#eb1700'}
                        strokeWidth="12"
                        strokeLinecap="round"
                        strokeDasharray={`${armed && total ? (C * ok) / 100 : 0} ${C}`}
                        style={{ transition: 'stroke-dasharray 1.2s cubic-bezier(.2,.8,.2,1)' }}
                      />
                    </svg>
                    <div className="rc"><b>{total ? `${ok.toFixed(ok === 100 ? 0 : 2)}%` : '–'}</b><span>SUCCESS</span></div>
                  </div>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 10, flex: 1, minWidth: 0 }}>
                    {!statuses.length && <span style={{ fontSize: 12.5, color: 'var(--muted)' }}>No calls in this window.</span>}
                    {statuses.map((s) => (
                      <div key={s.status} className="between" style={{ fontSize: 12.5, flexWrap: 'nowrap' }}>
                        <span className="row" style={{ gap: 8, minWidth: 0 }} data-tip={`${s.status} ${STATUS_TEXT[s.status] || ''}`}>
                          <i style={{ width: 8, height: 8, borderRadius: 3, flexShrink: 0, background: statusColor(s.status) }} />
                          <span className="hl">{s.status} {STATUS_TEXT[s.status] || ''}</span>
                        </span>
                        <b className="num" style={{ fontWeight: 700 }}>{s.calls.toLocaleString()}</b>
                      </div>
                    ))}
                  </div>
                </div>
              )}
              <div style={{ height: 1, background: 'var(--line)' }} />
              <div className="ptl"><span className="t" style={{ fontSize: 16 }}>Calls by alias</span></div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                {first && [0, 1].map((i) => <Sk key={i} h={22} />)}
                {u && !byAlias.length && <span style={{ fontSize: 12.5, color: 'var(--muted)' }}>No alias has been read in this window.</span>}
                {byAlias.map((a) => (
                  <div className="aliasbar2" key={a.alias}>
                    <div className="between" style={{ fontSize: 12.5, flexWrap: 'nowrap' }}>
                      <code style={{ fontSize: 12, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>/{a.alias}</code>
                      <span style={{ whiteSpace: 'nowrap' }}>
                        <b className="num" style={{ fontWeight: 700 }}>{fmt(a.calls)}</b>
                        <span style={{ fontSize: 11, color: 'var(--muted)', marginLeft: 10 }}>{a.avg_ms != null ? `${a.avg_ms} ms` : ''}</span>
                      </span>
                    </div>
                    <div className="tr"><i style={{ width: armed ? `${(a.calls / aliasTop) * 100}%` : 0, background: colorOf[a.alias] || 'var(--faint)' }} /></div>
                  </div>
                ))}
              </div>
            </div>
          </div>

          {first ? <div className="panel"><Sk h={200} r={14} /></div> : u && <CallersPanel usage={u} days={days} median={u.median_duration_ms} />}
        </>
      )}
      <KeyCard api={api} onRotate={onRotate} onEmail={onEmail} />
    </>
  );
}

const qs = (o) => new URLSearchParams(Object.entries(o).filter(([, v]) => v)).toString();

const lineage = (t) => [
  ['ENV', (t.env || '').toUpperCase()],
  t.database && [t.source_system === 's3' ? 'BUCKET' : 'DATABASE', t.database],
  t.schema && ['SCHEMA', t.schema],
  [t.source_system === 's3' ? 'OBJECT' : 'TABLE', t.table],
].filter(Boolean);

function Lineage({ t, end, style }) {
  return (
    <div className="lin" style={style}>
      <span className="lnode"><SourceLogo id={t.source_system} size={22} /></span>
      <span className="lline" />
      {lineage(t).map(([k, v]) => (
        <React.Fragment key={k}>
          <span className="lstep"><span>{k}</span><code>{v}</code></span>
          <span className="lline" />
        </React.Fragment>
      ))}
      {end}
    </div>
  );
}

const useSourceColumns = (t, enabled = true) => useResource(
  (signal) => request(`/data-api/source-columns?${qs({ source_system: t.source_system, env: t.env, database: t.database, schema: t.schema, table: t.table })}`, { signal }).then((d) => d?.columns || []),
  [t.source_system, t.env, t.database, t.schema, t.table], { enabled, reset: true },
);

// short label + icon for a db type, so long postgres names fit the cell
function colType(raw = '') {
  let t = String(raw).toLowerCase().trim();
  let arr = false;
  if (t.startsWith('_')) { arr = true; t = t.slice(1); }
  if (t.endsWith('[]')) { arr = true; t = t.slice(0, -2); }
  const short = t
    .replace('timestamp with time zone', 'timestamptz')
    .replace('timestamp without time zone', 'timestamp')
    .replace('character varying', 'varchar')
    .replace('double precision', 'double');
  let kind = 'other';
  let icon = '•';
  if (/vector|embedding/.test(t)) { kind = 'vec'; icon = <Binary size={12} strokeWidth={1.8} aria-hidden="true" />; }
  else if (/char|text|string|uuid|citext/.test(t)) { kind = 'text'; icon = <Type size={12} strokeWidth={1.8} aria-hidden="true" />; }
  else if (/int|numeric|decimal|double|real|float|number|serial/.test(t)) { kind = 'num'; icon = <Hash size={12} strokeWidth={1.8} aria-hidden="true" />; }
  else if (/time|date/.test(t)) { kind = 'time'; icon = <Clock size={12} strokeWidth={1.8} aria-hidden="true" />; }
  else if (/bool/.test(t)) { kind = 'bool'; icon = <ToggleLeft size={12} strokeWidth={1.8} aria-hidden="true" />; }
  else if (/json|variant|object/.test(t)) { kind = 'json'; icon = <Braces size={12} strokeWidth={1.8} aria-hidden="true" />; }
  else if (/bytea|blob|binary/.test(t)) { kind = 'vec'; icon = <FileBox size={12} strokeWidth={1.8} aria-hidden="true" />; }
  return { kind, icon, short: arr ? `${short}[]` : short };
}

/* Which columns an alias exposes, read live from the source so the list shows
   what is held back as well as what is shown. */
function ColumnsPeek({ t }) {
  const cols = useSourceColumns(t);
  const exposed = t.columns?.length ? t.columns : null;
  return (
    <div style={{ padding: '4px 22px 18px', display: 'flex', flexDirection: 'column', gap: 8 }}>
      {cols.loading && <div className="row" style={{ gap: 8, fontSize: 12, color: 'var(--muted)' }}><Spinner />Reading the source…</div>}
      {cols.error && (
        <div className="inline-warn">
          Couldn&apos;t read the source columns ({cols.error.message}).{' '}
          {exposed ? <>Exposed: <code>{exposed.join(', ')}</code></> : 'This alias exposes every column the source has.'}
        </div>
      )}
      {cols.data && (
        <>
          <div className="colgrid">
            {cols.data.map((c) => {
              const on = exposed ? exposed.includes(c.name) : true;
              const ty = colType(c.type);
              return (
                <div key={c.name} className={`ccell ${on ? '' : 'off'} ${c.bulk ? 'bulk' : ''}`} title={c.type}>
                  <span className={`cico k-${ty.kind}`}>{ty.icon}</span>
                  <span className="cname">{c.name}</span>
                  {c.bulk && <span className="ctag">bulk</span>}
                  {!on && <span className="ctag">hidden</span>}
                  <span className="ctype">{ty.short}</span>
                </div>
              );
            })}
          </div>
          <span className="hint" style={{ margin: 0 }}>
            {exposed
              ? `${exposed.length} of ${cols.data.length} columns exposed. The rest can't be read or searched.`
              : 'Follows the source: every column, including any added later. Bulk columns (embeddings, blobs) only come back when a caller names them.'}
          </span>
        </>
      )}
    </div>
  );
}

function AddDrawer({ api, save, onClose }) {
  const toast = useToast();
  const [P, setP] = useState(emptyPick);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const taken = (api.tables || []).map((t) => t.alias.toLowerCase());
  const ready = pickReady(P, taken);

  const add = async () => {
    if (!ready || busy) return;
    setBusy(true);
    setErr(null);
    const spec = pickToSpec(P);
    try {
      await save({ tables: [...(api.tables || []).map(tableToSpec), spec] });
      toast(`/${spec.alias} is live on this API.`);
      onClose();
    } catch (e) {
      setErr(e);
      setBusy(false);
    }
  };

  return (
    <Drawer onClose={busy ? undefined : onClose} label="Add a table" busy={busy}>
      <DrawerHead kicker={`ADD TABLE · ${api.name.toUpperCase()}`} title={<>Publish one more <em>alias.</em></>} onClose={onClose} />
      <div className="dbd">
        <TablePicker value={P} onChange={setP} takenAliases={taken} slugPreview={api.slug} />
      </div>
      <div className="dft">
        <span style={{ fontSize: 12, color: err ? 'var(--red)' : 'var(--muted)', flex: 1 }}>
          {err ? err.message : P.table ? 'Goes live as soon as you add it. Existing callers are not affected.' : 'Pick a source, then drill down to a table.'}
        </span>
        <button type="button" className="btn" onClick={onClose} disabled={busy}>Cancel</button>
        <button type="button" className="btn red" style={{ padding: '11px 18px' }} onClick={add} disabled={!ready || busy}>
          {busy && <Spinner />}
          {busy ? 'Checking the source…' : `Add /${P.alias.trim() || 'alias'}`}
        </button>
      </div>
    </Drawer>
  );
}

function EditDrawer({ api, t, save, onClose }) {
  const toast = useToast();
  const confirm = useConfirm();
  const cols = useSourceColumns(t);
  const [alias, setAlias] = useState(t.alias);
  const [limit, setLimit] = useState(t.row_limit);
  const [picked, setPicked] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [moving, setMoving] = useState(false);
  const [P, setP] = useState(() => ({
    ...emptyPick(),
    source: t.source_system, env: t.env, database: t.database || '', schema: t.schema || '', table: t.table,
    alias: t.alias, limit: t.row_limit, picked: t.columns?.length ? t.columns.slice() : null,
  }));
  const otherAliases = (api.tables || []).filter((x) => x.id !== t.id).map((x) => x.alias.toLowerCase());
  const sameSource = P.source === t.source_system && P.env === t.env && (P.database || '') === (t.database || '')
    && (P.schema || '') === (t.schema || '') && P.table === t.table;
  const sourceLabel = (x) => [x.env?.toUpperCase(), x.database, x.schema, x.table].filter(Boolean).join(' / ');

  const all = useMemo(() => (cols.data || []).map((c) => c.name), [cols.data]);
  const before = useMemo(() => (t.columns?.length ? t.columns : all), [t.columns, all]);
  useEffect(() => { if (cols.data && picked == null) setPicked(t.columns?.length ? t.columns.slice() : all); }, [cols.data, all, picked, t.columns]);

  const sel = picked || before;
  const hidden = before.filter((c) => !sel.includes(c));
  const shown = sel.filter((c) => !before.includes(c));
  const aliasTrim = alias.trim();
  const renamed = aliasTrim !== t.alias;
  const taken = !!aliasTrim && (api.tables || []).some((x) => x.id !== t.id && x.alias.toLowerCase() === aliasTrim.toLowerCase());
  const nextColumns = !cols.data || sel.length === all.length ? [] : sel;
  const colsChanged = cols.data ? JSON.stringify(nextColumns) !== JSON.stringify(t.columns || []) : false;
  const changed = renamed || Number(limit) !== t.row_limit || colsChanged;
  const moveReady = moving && !sameSource && pickReady(P, otherAliases);
  const ok = moving ? moveReady && !busy : changed && !!aliasTrim && !taken && (!cols.data || sel.length > 0) && !busy;
  const bulkOn = (cols.data || []).filter((c) => c.bulk && sel.includes(c.name));

  const doSave = async () => {
    const spec = { ...tableToSpec(t), alias: aliasTrim, row_limit: Number(limit), columns: cols.data ? nextColumns : (t.columns || []) };
    await save({ tables: (api.tables || []).map((x) => (x.id === t.id ? spec : tableToSpec(x))) });
  };

  const submit = async () => {
    if (!ok) return;
    setErr(null);
    if (moving) {
      const spec = pickToSpec(P);
      const yes = await confirm({
        title: <>Point <code>/{t.alias}</code> at a different table?</>,
        text: `It will read from ${sourceLabel({ env: P.env, database: P.database, schema: P.schema, table: P.table })} instead of ${t.table}. ${spec.alias !== t.alias ? `The URL also moves to /${spec.alias}.` : 'The URL stays the same.'} Callers may see different columns.`,
        okLabel: 'Change table',
        busyLabel: 'Checking the source…',
        danger: true,
        icon: <ArrowLeftRight size={20} strokeWidth={1.8} aria-hidden="true" />,
        action: () => save({ tables: (api.tables || []).map((x) => (x.id === t.id ? spec : tableToSpec(x))) }),
      });
      if (!yes) return;
      toast(`/${spec.alias} now reads from ${P.table}`);
      onClose(spec.alias !== t.alias ? spec.alias : undefined);
      return;
    }
    if (renamed) {
      const yes = await confirm({
        title: <>Move <code>/{t.alias}</code> to <code>/{aliasTrim}</code>?</>,
        text: 'Anyone still calling the old URL gets 404 until they switch. Send the new URL to your callers.',
        okLabel: 'Rename alias',
        busyLabel: 'Saving…',
        danger: true,
        icon: <ArrowLeftRight size={20} strokeWidth={1.8} aria-hidden="true" />,
        action: doSave,
      });
      if (!yes) return;
      toast(`/${t.alias} is now /${aliasTrim}`);
      onClose(aliasTrim);
      return;
    }
    setBusy(true);
    try {
      await doSave();
      toast(`/${t.alias} updated`);
      onClose();
    } catch (e) {
      setErr(e);
      setBusy(false);
    }
  };

  return (
    <Drawer onClose={busy ? undefined : () => onClose()} label={`Edit /${t.alias}`} busy={busy}>
      <DrawerHead kicker={`EDIT ALIAS · ${api.name.toUpperCase()}`} title={<>Change what <code style={{ fontSize: 26 }}>/{t.alias}</code> <em>returns.</em></>} onClose={() => onClose()} />
      <div className="dbd" style={{ display: 'flex', flexDirection: 'column', gap: 24 }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          <span className="lab" style={{ margin: 0 }}>Source</span>
          <Lineage
            t={t}
            style={{ border: '1px solid var(--line)', borderRadius: 14, padding: '12px 16px' }}
            end={<span className="lend" style={{ background: '#fff', color: 'var(--red)', border: '1px solid #eb170055', boxShadow: 'none' }}>GET /{aliasTrim || '…'}/rows</span>}
          />
          <div className="row" style={{ justifyContent: 'space-between', flexWrap: 'wrap' }}>
            <span className="hint" style={{ margin: 0 }}>
              {moving ? 'Pick the new table below. The alias, and so the URL, stays the same unless you change it.' : 'Callers keep the same URL if you point this alias at another table.'}
            </span>
            <button type="button" className="btn" onClick={() => { setMoving((m) => !m); setErr(null); }} disabled={busy}>
              {moving ? 'Keep current table' : 'Edit table'}
            </button>
          </div>
        </div>
        {moving ? (
          <div className="movebox">
            <TablePicker value={P} onChange={setP} takenAliases={otherAliases} slugPreview={api.slug} keepAlias />
            {P.table && !sameSource && (
              <div className="chg">
                <b>What changes for callers</b>
                <span>• Data comes from <code>{sourceLabel({ env: P.env, database: P.database, schema: P.schema, table: P.table })}</code> instead of <code>{sourceLabel(t)}</code></span>
                {P.alias.trim() !== t.alias && <span>• URL moves from <code>/{t.alias}</code> to <code>/{P.alias.trim()}</code></span>}
                <span>• Column names and types follow the new table</span>
              </div>
            )}
          </div>
        ) : (<>
        <div className="cfg">
          <div>
            <label className="lab" htmlFor="dax-ed-alias">Alias, used in the URL</label>
            <input id="dax-ed-alias" className="field mono" value={alias} onChange={(e) => setAlias(cleanAlias(e.target.value))} style={{ fontSize: 14 }} maxLength={64} />
            {taken ? <div className="hint" style={{ color: 'var(--red)' }}>That alias is already used on this API.</div>
              : !aliasTrim ? <div className="hint" style={{ color: 'var(--red)' }}>An alias is required.</div>
                : renamed ? <div className="hint" style={{ color: '#9e3a00', display: 'flex', gap: 6, alignItems: 'baseline' }}><TriangleAlert size={13} strokeWidth={1.8} aria-hidden="true" /><span>The URL moves to <code>/{aliasTrim}</code>. Callers still using <code>/{t.alias}</code> get 404.</span></div>
                  : <div className="hint"><code>/data-api/{api.slug}/<b style={{ color: 'var(--red)', fontWeight: 400 }}>{aliasTrim}</b>/rows</code></div>}
          </div>
          <div>
            <label className="lab" htmlFor="dax-ed-limit">
              Rows per page
              {Number(limit) !== t.row_limit && <span style={{ color: 'var(--muted)', marginLeft: 6 }}>was {t.row_limit.toLocaleString()}</span>}
            </label>
            <RowLimitField id="dax-ed-limit" value={Number(limit)} onChange={setLimit} />
            <div className="hint">Caps one call. Callers page through the rest with offset.</div>
          </div>
        </div>
        <ColumnChooser cols={cols} picked={sel} onPick={setPicked} bulkOn={bulkOn} before={before} />
        {changed && (
          <div className="chg">
            <b>What changes for callers</b>
            {renamed && <span>• URL moves from <code>/{t.alias}</code> to <code>/{aliasTrim}</code></span>}
            {Number(limit) !== t.row_limit && <span>• Page size goes from {t.row_limit.toLocaleString()} to {Number(limit).toLocaleString()} rows</span>}
            {hidden.length > 0 && <span>• {plural(hidden.length, 'column')} {hidden.length === 1 ? 'disappears' : 'disappear'} from responses: <code>{hidden.join(', ')}</code></span>}
            {shown.length > 0 && <span>• {plural(shown.length, 'column')} {shown.length === 1 ? 'starts' : 'start'} appearing: <code>{shown.join(', ')}</code></span>}
          </div>
        )}
        </>)}
      </div>
      <div className="dft">
        <span style={{ fontSize: 12, color: err ? 'var(--red)' : 'var(--muted)', flex: 1 }}>
          {err ? err.message
            : moving ? (sameSource ? 'Pick a different table to continue.' : P.table ? 'Takes effect on the next call.' : 'Drill down to the new table.')
              : changed ? 'Takes effect on the next call.' : 'Nothing changed yet.'}
        </span>
        <button type="button" className="btn" onClick={() => onClose()} disabled={busy}>Cancel</button>
        <button type="button" className="btn red" style={{ padding: '11px 18px' }} onClick={submit} disabled={!ok}>
          {busy && <Spinner />}{busy ? 'Saving…' : moving ? 'Change table' : 'Save changes'}
        </button>
      </div>
    </Drawer>
  );
}

function TablesTab({ api, usage, days, save, params }) {
  const { nav } = usePage();
  const confirm = useConfirm();
  const toast = useToast();
  const [open, setOpen] = useState({});
  const tables = api.tables || [];
  const u = usage.data;
  const editing = params.edit ? tables.find((t) => t.alias === params.edit) : null;

  /* An edit link to an alias that no longer exists just closes the drawer. */
  useEffect(() => {
    if (params.edit && !editing) nav.set({ edit: null }, { replace: true });
  }, [params.edit, editing, nav]);

  const remove = async (t) => {
    if (tables.length === 1) {
      toast('An API needs at least one table. Delete the whole API instead.', 'error');
      return;
    }
    const ok = await confirm({
      title: <>Remove <code>/{t.alias}</code>?</>,
      text: 'Its URL stops working immediately. Other aliases are not affected.',
      okLabel: 'Remove alias',
      busyLabel: 'Removing…',
      danger: true,
      icon: <Trash2 size={20} strokeWidth={1.8} aria-hidden="true" />,
      action: () => save({ tables: tables.filter((x) => x.id !== t.id).map(tableToSpec) }),
    });
    if (ok) toast(`Removed /${t.alias}.`);
  };

  return (
    <>
      <div className="sechead" style={{ marginBottom: 0 }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          <span className="kicker">LINEAGE</span>
          <h2 className="h-l">What this API <em>exposes.</em></h2>
        </div>
        <div className="row" style={{ flexWrap: 'wrap' }}>
          <span style={{ fontSize: 12, color: 'var(--muted)' }}>Each alias is its own endpoint. Nothing else in the source is reachable.</span>
          <button type="button" className="cta" style={{ padding: '12px 12px 12px 18px', fontSize: 13.5 }} onClick={() => nav.set({ add: 1, edit: null })}>
            Add table <span className="arr">+</span>
          </button>
        </div>
      </div>

      {!tables.length && <p className="empty">This API has no tables.</p>}
      {tables.map((t, i) => {
        const color = ALIAS_COLORS[i % ALIAS_COLORS.length];
        const vals = u?.alias_series?.[t.alias] || [];
        const calls = u ? sum(vals) : null;
        return (
          <div className="tcard" key={t.id || t.alias} style={{ animationDelay: `${i * 70}ms` }}>
            <div className="th">
              <span style={{ width: 10, height: 10, borderRadius: 3, background: color }} />
              <code className="talias">/{t.alias}</code>
              <EnvTag env={t.env} />
              <SrcTag id={t.source_system} />
              <span style={{ flex: 1 }} />
              <span className="tstat"><b>{t.columns?.length ? t.columns.length : 'All'}</b>columns</span>
              <span className="tstat"><b>{t.row_limit.toLocaleString()}</b>rows / page</span>
              <span className="tstat"><b>{calls == null ? <Sk w={40} h={16} /> : fmt(calls)}</b>calls · {days}d</span>
              <span className="tstat tspk" style={{ border: 0, paddingRight: 0 }}>
                <Spark values={vals.length > 1 ? vals : [0, 0]} color={color} h={30} style={{ width: 110, height: 30 }} />
              </span>
              <span className="tacts">
                <button type="button" className="btn" aria-expanded={!!open[t.alias]} onClick={() => setOpen((o) => ({ ...o, [t.alias]: !o[t.alias] }))}>
                  {open[t.alias] ? <ChevronDown size={14} strokeWidth={1.8} aria-hidden="true" /> : <ChevronRight size={14} strokeWidth={1.8} aria-hidden="true" />} Columns
                </button>
                <button type="button" className="btn" onClick={() => nav.set({ edit: t.alias, add: null })}><Pencil size={13} strokeWidth={1.8} aria-hidden="true" /> Edit</button>
                <button type="button" className="btn" onClick={() => nav.openApi(api.slug, 'endpoints', { alias: t.alias })}>Try it</button>
                <button type="button" className="iconbtn" style={{ width: 34, height: 34, fontSize: 12, color: 'var(--red)' }} onClick={() => remove(t)} aria-label={`Remove /${t.alias}`} data-tip={`Remove /${t.alias}`}><Trash2 size={14} strokeWidth={1.8} aria-hidden="true" /></button>
              </span>
            </div>
            <Lineage t={t} end={<span className="lend">GET /{t.alias}/rows</span>} />
            {open[t.alias] && <ColumnsPeek t={t} />}
          </div>
        );
      })}

      {params.add && <AddDrawer api={api} save={save} onClose={() => nav.set({ add: null }, { replace: true })} />}
      {editing && (
        <EditDrawer
          key={editing.id}
          api={api}
          t={editing}
          save={save}
          onClose={() => nav.set({ edit: null }, { replace: true })}
        />
      )}
    </>
  );
}

/* The Docs tab renders the same markdown the download contains, so the screen
   and the file can never drift apart. This renderer covers what
   buildConsumerDoc emits: headings, fenced code, pipe tables, dash lists,
   rules, bold and inline code. */

const slug = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');

function inline(text, key) {
  return String(text).split(/(`[^`]+`|\*\*[^*]+\*\*)/g).filter(Boolean).map((part, i) => {
    const k = `${key}-${i}`;
    if (part.startsWith('`') && part.endsWith('`')) return <span key={k} className="ic">{part.slice(1, -1)}</span>;
    if (part.startsWith('**') && part.endsWith('**')) return <b key={k}>{part.slice(2, -2)}</b>;
    return <React.Fragment key={k}>{part}</React.Fragment>;
  });
}

function CodeBlock({ code, lang }) {
  const toast = useToast();
  const html = useMemo(() => highlightCode(code, lang), [code, lang]);
  return (
    <div style={{ position: 'relative' }}>
      {/* Every token is escaped inside highlightCode() before markup is added. */}
      <pre className="code hl" style={{ borderRadius: 12, overflowX: 'auto', whiteSpace: 'pre', wordBreak: 'normal', paddingRight: 70 }} dangerouslySetInnerHTML={{ __html: html }} />
      <button
        type="button"
        className="kbtn"
        style={{ position: 'absolute', top: 8, right: 8, width: 30, height: 30, padding: 0, display: 'inline-flex', alignItems: 'center', justifyContent: 'center', lineHeight: 0 }}
        aria-label="Copy snippet"
        data-tip="Copy"
        onClick={async () => { const ok = await copyText(code); toast(ok ? 'Snippet copied' : "Couldn't copy.", ok ? 'ok' : 'error'); }}
      >
        <Copy size={14} strokeWidth={1.8} aria-hidden="true" />
      </button>
    </div>
  );
}

/* Splits the guide into sections at each "## " heading. */
function parse(md) {
  const lines = String(md).split('\n');
  const sections = [{ id: 'intro', title: 'Overview', blocks: [] }];
  let cur = sections[0];
  let i = 0;
  let para = [];
  const flush = () => {
    if (para.length) cur.blocks.push({ t: 'p', text: para.join(' ') });
    para = [];
  };
  while (i < lines.length) {
    const line = lines[i];
    if (line.startsWith('```')) {
      flush();
      const lang = line.slice(3).trim();
      const buf = [];
      i += 1;
      while (i < lines.length && !lines[i].startsWith('```')) { buf.push(lines[i]); i += 1; }
      i += 1;
      cur.blocks.push({ t: 'code', text: buf.join('\n'), lang });
      continue;
    }
    if (line.trim().startsWith('|')) {
      flush();
      const rows = [];
      while (i < lines.length && lines[i].trim().startsWith('|')) { rows.push(lines[i].trim()); i += 1; }
      const cells = (r) => r.replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
      cur.blocks.push({ t: 'table', head: cells(rows[0]), rows: rows.slice(rows[1] && /^[\s|:-]+$/.test(rows[1]) ? 2 : 1).map(cells) });
      continue;
    }
    if (line.startsWith('- ')) {
      flush();
      const items = [];
      while (i < lines.length && lines[i].startsWith('- ')) { items.push(lines[i].slice(2)); i += 1; }
      cur.blocks.push({ t: 'ul', items });
      continue;
    }
    if (line.startsWith('## ')) {
      flush();
      const title = line.slice(3).trim();
      cur = { id: slug(title) || `s${sections.length}`, title, blocks: [] };
      sections.push(cur);
      i += 1;
      continue;
    }
    if (line.startsWith('### ')) { flush(); cur.blocks.push({ t: 'h4', text: line.slice(4) }); i += 1; continue; }
    if (line.startsWith('# ')) { flush(); i += 1; continue; }
    if (line.trim() === '---') { flush(); i += 1; continue; }
    if (!line.trim()) { flush(); i += 1; continue; }
    para.push(line);
    i += 1;
  }
  flush();
  return sections.filter((s) => s.blocks.length);
}

function DocsTab({ api, onDownload }) {
  const md = useMemo(() => buildConsumerDoc(api.name || 'Data API Export', api.slug, api.api_key, api.tables || []), [api]);
  const sections = useMemo(() => parse(md), [md]);
  const [active, setActive] = useState(sections[0]?.id);

  useEffect(() => {
    const els = sections.map((s) => document.getElementById(`dax-doc-${s.id}`)).filter(Boolean);
    if (!els.length || !('IntersectionObserver' in window)) return undefined;
    const io = new IntersectionObserver((ents) => {
      const vis = ents.filter((e) => e.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
      if (vis) setActive(vis.target.id.replace('dax-doc-', ''));
    }, { rootMargin: '-90px 0px -60% 0px' });
    els.forEach((el) => io.observe(el));
    return () => io.disconnect();
  }, [sections]);

  return (
    <>
      <div className="sechead" style={{ marginBottom: 0 }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          <span className="kicker">CONSUMER GUIDE</span>
          <h2 className="h-l">Everything a caller <em>needs.</em></h2>
        </div>
        <div className="row" style={{ flexWrap: 'wrap' }}>
          <span style={{ fontSize: 12, color: 'var(--muted)' }}>Built from this API&apos;s aliases and limits. The download is exactly this page.</span>
          <button type="button" className="btn dlbtn" onClick={onDownload}><Download size={14} strokeWidth={1.8} aria-hidden="true" /> Download .md</button>
        </div>
      </div>
      <div className="docs">
        <nav className="toc" aria-label="Guide sections">
          {sections.map((s) => (
            <a
              key={s.id}
              href={`#dax-doc-${s.id}`}
              className={active === s.id ? 'on' : ''}
              onClick={(e) => {
                e.preventDefault();
                setActive(s.id);
                document.getElementById(`dax-doc-${s.id}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
              }}
            >
              {s.title}
            </a>
          ))}
        </nav>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14, minWidth: 0 }}>
          {sections.map((s) => (
            <section key={s.id} id={`dax-doc-${s.id}`} className="dsec">
              <h3>{s.title}</h3>
              <div className="md">
                {s.blocks.map((b, i) => {
                  const k = `${s.id}-${i}`;
                  if (b.t === 'p') return <p key={k}>{inline(b.text, k)}</p>;
                  if (b.t === 'h4') return <h4 key={k}>{inline(b.text, k)}</h4>;
                  if (b.t === 'code') return <CodeBlock key={k} code={b.text} lang={b.lang} />;
                  if (b.t === 'ul') return <ul key={k}>{b.items.map((it, j) => <li key={`${k}-${j}`}>{inline(it, `${k}-${j}`)}</li>)}</ul>;
                  return (
                    <div key={k} className="ptable" style={{ '--cols': b.head.length, overflowX: 'auto' }}>
                      <div>{b.head.map((h) => <span key={h}>{h.toUpperCase()}</span>)}</div>
                      {b.rows.map((r, ri) => (
                        <div key={`${k}-${ri}`}>{r.map((c, ci) => <span key={`${k}-${ri}-${ci}`}>{inline(c, `${k}-${ri}-${ci}`)}</span>)}</div>
                      ))}
                    </div>
                  );
                })}
              </div>
            </section>
          ))}
        </div>
      </div>
    </>
  );
}

const enc = encodeURIComponent;
const LIMITS = [60, 200];

const dayKey = (d) => d.toLocaleDateString('en-CA', { timeZone: ET });

/* Today / yesterday / this week / older, by Eastern calendar day. */
const groupOf = (iso) => {
  const d = parseTs(iso);
  if (!d) return 'OLDER';
  const today = new Date();
  const k = dayKey(d);
  if (k === dayKey(today)) return 'TODAY';
  if (k === dayKey(new Date(today.getTime() - 86400000))) return 'YESTERDAY';
  if (today.getTime() - d.getTime() < 7 * 86400000) return 'EARLIER THIS WEEK';
  return 'OLDER';
};

function ActivityTab({ api }) {
  const [limit, setLimit] = useState(LIMITS[0]);
  const [filter, setFilter] = useState('all');
  const [q, setQ] = useState('');
  const feed = useResource((signal) => request(`/data-api/${enc(api.id)}/activity?limit=${limit}`, { signal }), [api.id, limit]);
  const events = useMemo(() => feed.data?.events || [], [feed.data]);

  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return events.filter((e) => {
      if (filter === 'call' && e.kind !== 'request') return false;
      if (filter === 'management' && e.kind !== 'management') return false;
      if (filter === 'errors' && !(e.status >= 400)) return false;
      if (!needle) return true;
      return [e.what, e.who, e.details, e.ip, e.user_agent, e.alias, String(e.status)].filter(Boolean).join(' ').toLowerCase().includes(needle);
    });
  }, [events, filter, q]);

  const color = (e) => (e.status >= 400 ? 'var(--red)' : e.kind === 'management' ? 'var(--blue)' : 'var(--green)');
  let last = '';

  return (
    <>
      <div className="sechead" style={{ marginBottom: 0 }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          <span className="kicker">ACTIVITY</span>
          <h2 className="h-l">Every call, every <em>change.</em></h2>
        </div>
        <div className="row" style={{ flexWrap: 'wrap' }}>
          <label className="searchbox" style={{ background: '#fff', borderColor: 'var(--line)', width: 220 }}>
            <Search size={14} strokeWidth={1.8} color="var(--muted)" aria-hidden="true" />
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search activity" aria-label="Search activity" />
          </label>
          <Seg value={filter} onChange={setFilter} options={[['all', 'All'], ['call', 'Calls'], ['management', 'Changes'], ['errors', 'Rejected']]} btnStyle={{ padding: '7px 12px' }} ariaLabel="Filter activity" />
          <button type="button" className="iconbtn" onClick={() => feed.reload()} disabled={feed.loading} aria-label="Refresh" data-tip="Refresh">{feed.loading && feed.data ? <span className="spinner" /> : <RefreshCw size={14} strokeWidth={1.8} aria-hidden="true" />}</button>
        </div>
      </div>

      <div style={{ position: 'relative' }}>
        <Busybar on={feed.loading && !!feed.data} />
        {feed.loading && !feed.data && (
          <div className="tl">
            {[0, 1, 2, 3, 4].map((i) => (
              <div key={i} className="tev" style={{ animation: 'none' }}>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}><Sk w="55%" h={13} /><Sk w="35%" h={10} /></div>
                <Sk w={70} h={12} />
              </div>
            ))}
          </div>
        )}
        {feed.error && !feed.data && <div className="panel"><ErrorBox error={feed.error} onRetry={() => feed.reload()} title="Couldn't load the activity" /></div>}
        {feed.data && (
          <div className="tl">
            {shown.map((e, i) => {
              const g = groupOf(e.ts);
              const head = g !== last ? <div className="tday">{(last = g)}</div> : null;
              const tool = e.kind === 'request' ? TOOLS[toolOf(e.user_agent)].name : null;
              return (
                <React.Fragment key={`${e.ts}-${i}`}>
                  {head}
                  <div className="tev" style={{ animationDelay: `${Math.min(i, 12) * 40}ms` }}>
                    <span className="nd" style={{ background: color(e), boxShadow: `0 0 0 4px color-mix(in srgb, ${color(e)} 18%, transparent)` }} />
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 5, minWidth: 0 }}>
                      <span style={{ fontSize: 14 }}>{e.what}</span>
                      <span className="row" style={{ gap: 8, fontSize: 11.5, color: 'var(--muted)', flexWrap: 'wrap' }}>
                        <span className="kchip" style={{ background: e.kind === 'management' ? '#0f68b214' : e.status >= 400 ? '#eb170012' : '#32871414', color: color(e) }}>
                          {e.kind === 'management' ? 'CHANGE' : e.status >= 400 ? `HTTP ${e.status}` : 'CALL'}
                        </span>
                        {e.kind === 'request'
                          ? <><code>{describeIp(e.ip)}</code><span>{tool}</span></>
                          : <span>{e.who}</span>}
                        {e.details && e.kind === 'management' && (
                          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 520 }}>{e.details.replace(/\s*\(api_id=[^)]*\)/, '')}</span>
                        )}
                      </span>
                    </div>
                    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 2 }}>
                      <span style={{ fontSize: 12.5, whiteSpace: 'nowrap' }}>{relative(e.ts)}</span>
                      <span style={{ fontSize: 11, color: 'var(--faint)', whiteSpace: 'nowrap' }}>{absolute(e.ts)}</span>
                    </div>
                  </div>
                </React.Fragment>
              );
            })}
            {!shown.length && (
              <p className="empty">
                {events.length ? 'Nothing matches that filter.' : 'Nothing recorded for this API yet. Calls and changes appear here as they happen.'}
              </p>
            )}
          </div>
        )}
        {feed.data && events.length >= limit && limit < LIMITS[LIMITS.length - 1] && (
          <div className="row" style={{ justifyContent: 'center', marginTop: 8 }}>
            <button type="button" className="btn" onClick={() => setLimit(LIMITS[LIMITS.length - 1])}>Show older activity</button>
          </div>
        )}
      </div>
    </>
  );
}


function HeaderSkeleton() {
  return (
    <div className="dh2">
      <div className="dh2l">
        <Sk w={260} h={10} />
        <Sk w={420} h={30} style={{ marginTop: 4 }} />
        <Sk w={560} h={12} />
        <Sk w={480} h={22} r={9} />
      </div>
    </div>
  );
}

function EditMeta({ api, onSave, onCancel }) {
  const [name, setName] = useState(api.name);
  const [desc, setDesc] = useState(api.description || '');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const submit = async () => {
    if (!name.trim()) {
      setErr(new Error('Give it a name.'));
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      await onSave({ name: name.trim(), description: desc.trim() });
    } catch (e) {
      setErr(e);
      setBusy(false);
    }
  };
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14, maxWidth: 640, padding: 18, borderRadius: 18, background: 'rgba(255,255,255,.94)', border: '1px solid var(--line)', boxShadow: '0 20px 40px -30px rgba(49,44,42,.6)' }}>
      <div>
        <label className="lab" htmlFor="dax-ed-name">Name</label>
        <input
          id="dax-ed-name"
          className="bigin"
          value={name}
          maxLength={120}
          style={{ fontSize: 28 }}
          autoFocus
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') submit(); if (e.key === 'Escape') onCancel(); }}
        />
      </div>
      <div>
        <label className="lab" htmlFor="dax-ed-desc">What it&apos;s for, and who gets the key</label>
        <textarea id="dax-ed-desc" className="field" rows={2} value={desc} maxLength={1000} onChange={(e) => setDesc(e.target.value)} />
      </div>
      {err && <div className="inline-err" role="alert"><span><CircleAlert size={15} strokeWidth={1.8} aria-hidden="true" /></span><span>{err.message}</span></div>}
      <div className="row" style={{ flexWrap: 'wrap' }}>
        <button type="button" className="btn dark" onClick={submit} disabled={busy}>{busy ? 'Saving…' : 'Save changes'}</button>
        <button type="button" className="btn" onClick={onCancel} disabled={busy}>Cancel</button>
        <span className="hint" style={{ margin: 0 }}>The URL stays <code>/data-api/{api.slug}</code> so nobody&apos;s integration breaks.</span>
      </div>
    </div>
  );
}

export default function Workspace({ slug, tabParam }) {
  const { overview, nav, refreshOverview, showHandoff, emailKey } = usePage();
  const toast = useToast();
  const confirm = useConfirm();
  const [sp] = useSearchParams();
  const knownTab = TABS.some(([id]) => id === tabParam);
  const tab = knownTab ? tabParam : 'overview';

  useEffect(() => {
    if (tabParam && !knownTab) nav.set({ tab: null }, { replace: true });
  }, [tabParam, knownTab, nav]);

  const detail = useResource((signal) => request(`/data-api/${enc(slug)}`, { signal }), [slug]);
  const [days, setDays] = useLocalSetting('dataApi.usageDays', 14);
  const usage = useResource((signal) => request(`/data-api/${enc(slug)}/usage?days=${days}`, { signal }), [slug, days], { enabled: !!detail.data });
  const api = detail.data;
  const summary = overview.data?.apis?.find((a) => a.slug === slug);

  const [cpm, setCpm] = useState(null);
  const readCpm = useCallback(async () => {
    const d = await request(`/data-api/recent-calls?limit=0&api_id=${enc(slug)}`);
    setCpm(d?.per_minute ?? 0);
  }, [slug]);
  useEffect(() => { if (api?.is_active) readCpm().catch(() => setCpm(null)); }, [api?.is_active, readCpm]);
  usePoll(readCpm, 10000, !!api?.is_active);

  const [editing, setEditing] = useState(false);
  const [menu, setMenu] = useState(false);
  const [busy, setBusy] = useState('');
  const menuRef = useRef(null);

  useEffect(() => {
    if (!menu) return undefined;
    const close = (e) => { if (!menuRef.current?.contains(e.target)) setMenu(false); };
    const esc = (e) => { if (e.key === 'Escape') setMenu(false); };
    document.addEventListener('mousedown', close);
    document.addEventListener('keydown', esc);
    return () => {
      document.removeEventListener('mousedown', close);
      document.removeEventListener('keydown', esc);
    };
  }, [menu]);

  /* ---- sticky tab bar and its sliding indicator ---- */
  const scroller = useRef(null);
  const tbar = useRef(null);
  const pills = useRef(null);
  const [stuck, setStuck] = useState(false);
  const [ind, setInd] = useState(null);

  useLayoutEffect(() => {
    const place = () => {
      const on = pills.current?.querySelector('.tp.on');
      if (on) setInd({ left: on.offsetLeft, width: on.offsetWidth });
    };
    place();
    window.addEventListener('resize', place);
    return () => window.removeEventListener('resize', place);
  }, [tab, api, editing]);

  useLayoutEffect(() => {
    const m = scroller.current;
    const tb = tbar.current;
    if (m && tb && m.scrollTop > tb.offsetTop) m.scrollTop = tb.offsetTop;
  }, [tab]);

  const onScroll = () => {
    const m = scroller.current;
    const tb = tbar.current;
    if (m && tb) setStuck(m.scrollTop > tb.offsetTop - 2);
  };

  /* ---- writes ---- */
  const save = useCallback(async (body) => {
    const d = await request(`/data-api/${enc(api.id)}`, { method: 'PATCH', body });
    detail.setData(d);
    refreshOverview();
    usage.reload({ quiet: true });
    return d;
  }, [api, detail, refreshOverview, usage]);

  const saveMeta = async (body) => {
    await save(body);
    setEditing(false);
    toast('Details saved.');
  };

  const togglePause = async () => {
    setMenu(false);
    if (api.is_active) {
      const ok = await confirm({
        title: 'Deactivate this API?',
        text: <>Every call to <code>/data-api/{api.slug}</code> returns 403 until you activate it again. The key stays the same.</>,
        okLabel: 'Deactivate',
        busyLabel: 'Deactivating…',
        danger: true,
        icon: <Pause size={20} strokeWidth={1.8} aria-hidden="true" />,
        action: () => save({ is_active: false }),
      });
      if (ok) toast('Inactive. Calls now return 403.');
      return;
    }
    setBusy('resume');
    try {
      await save({ is_active: true });
      toast('Active. Calls go through again.');
    } catch (e) {
      toast(`Couldn't resume: ${e.message}`, 'error');
    } finally {
      setBusy('');
    }
  };

  const rotate = async () => {
    setMenu(false);
    let fresh = null;
    const ok = await confirm({
      title: 'Rotate the key?',
      text: 'The old key stops working immediately. Anyone still using it gets 401 until they switch to the new one.',
      okLabel: 'Rotate key',
      busyLabel: 'Rotating…',
      danger: true,
      icon: <RotateCw size={20} strokeWidth={1.8} aria-hidden="true" />,
      action: async () => {
        fresh = await request(`/data-api/${enc(api.id)}/regenerate-key`, { method: 'POST' });
      },
    });
    if (!ok || !fresh) return;
    refreshOverview();
    showHandoff({ id: api.id, slug: api.slug, name: api.name, api_key: fresh.api_key, tables: api.tables || [] }, { slug: api.slug, rotated: true });
  };

  const remove = async () => {
    setMenu(false);
    const ok = await confirm({
      title: <>Delete <em className="em">{api.name}</em>?</>,
      text: "This can't be undone. Its URLs and key stop working immediately, and every caller gets 404.",
      okLabel: 'Delete API',
      busyLabel: 'Deleting…',
      danger: true,
      icon: <Trash2 size={20} strokeWidth={1.8} aria-hidden="true" />,
      action: () => request(`/data-api/${enc(api.id)}`, { method: 'DELETE' }),
    });
    if (!ok) return;
    toast(`${api.name} deleted.`);
    refreshOverview();
    nav.home();
  };

  const copyBase = async () => {
    setMenu(false);
    const ok = await copyText(baseUrl(api.slug));
    toast(ok ? 'Base URL copied' : "Couldn't copy. Your browser blocked the clipboard.", ok ? 'ok' : 'error');
  };

  const downloadGuide = () => {
    downloadTextFile(guideFilename(api.name), buildConsumerDoc(api.name || 'Data API Export', api.slug, api.api_key, api.tables || []));
    toast('Consumer guide downloaded.');
  };

  /* ---- launch overlay, only when the first load is slow enough to notice ---- */
  const slow = useSlow(detail.loading && !api, 250);
  const [step, setStep] = useState(0);
  useEffect(() => {
    if (!slow) return undefined;
    const t = setInterval(() => setStep((s) => Math.min(s + 1, 2)), 420);
    return () => clearInterval(t);
  }, [slow]);

  const notFound = detail.error?.status === 404;
  const tables = api?.tables || [];
  const sources = [...new Set(tables.map((t) => t.source_system))];

  const tabProps = {
    api, usage, days, setDays, save, onRotate: rotate, onEmail: () => emailKey(api), reloadDetail: detail.reload,
    params: { add: sp.get('add') === '1', edit: sp.get('edit') || '', alias: sp.get('alias') || '' },
  };

  return (
    <div className="ws">
      <Sidebar currentSlug={slug} />
      <div className="wsmain" ref={scroller} onScroll={onScroll}>
        <div className="texture" />
        <div className="g1" />
        <div className="g2" />

        {slow && !api && (
          <div className="launch2">
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 26, textAlign: 'center' }}>
              <Orb size={150} inset={34}><span style={{ fontFamily: 'JohnsonDisplay', fontSize: 20, color: 'var(--red)' }}>API</span></Orb>
              <span style={{ fontFamily: 'JohnsonDisplay', fontSize: 30 }}>{summary?.name || `/${slug}`}</span>
              <Steps steps={['Checking key and permissions', 'Reading tables', `Loading ${days} days of usage`]} at={step} />
            </div>
          </div>
        )}

        {notFound && (
          <StateBox
            title="No API lives here"
            text={<>Nothing is published at <code>/data-api/{slug}</code>. It may have been deleted or renamed.</>}
            style={{ minHeight: '70vh' }}
            action={(
              <div className="row">
                <button type="button" className="btn" onClick={nav.home}>Back to the dashboard</button>
                <button type="button" className="btn red" onClick={nav.create}>Publish a new API</button>
              </div>
            )}
          />
        )}
        {detail.error && !notFound && !api && (
          <ErrorBox error={detail.error} onRetry={() => detail.reload()} title="Couldn't open this API" style={{ minHeight: '70vh' }} />
        )}

        {!api && detail.loading && !slow && <HeaderSkeleton />}

        {api && (
          <>
            <div className="dh2">
              <div className="dh2l">
                <nav className="crumbs sm" aria-label="Breadcrumb">
                  <b>GRA SMARTHUB</b><span>/</span>
                  <button type="button" className="tlink c" onClick={nav.home}>DATA API EXPORT</button><span>/</span>
                  <span style={{ color: 'var(--ink)' }}>{api.slug.toUpperCase()}</span>
                </nav>
                {editing ? (
                  <EditMeta api={api} onSave={saveMeta} onCancel={() => setEditing(false)} />
                ) : (
                  <>
                    <div className="trow">
                      <h1 className="dt2">{api.name}</h1>
                      <button type="button" className="link" onClick={() => setEditing(true)} style={{ fontSize: 12 }}>Edit</button>
                      <StatusPill live={api.is_active} />
                      {api.is_active && cpm != null && (
                        <span className="cpm" data-tip="Calls in the last minute"><b>{cpm}</b> calls / min</span>
                      )}
                    </div>
                    {api.description?.trim() && <p className="desc">{api.description}</p>}
                    <div className="mrow">
                      <button type="button" className="urlchip" onClick={copyBase} data-tip={`${baseUrl(api.slug)} · click to copy`}>
                        <span className="method get" style={{ minWidth: 0, padding: '2px 6px', fontSize: 9.5 }}>GET</span>
                        <code>/data-api/{api.slug}</code>
                        <span className="cp"><Copy size={13} strokeWidth={1.8} aria-hidden="true" /></span>
                      </button>
                      <span className="mini-logos">{sources.map((s) => <SourceLogo key={s} id={s} size={18} />)}</span>
                      <span>{plural(tables.length, 'table')}</span>
                      <span className="mi"><i />{(api.created_by || '').split('@')[0] || 'unknown'}</span>
                      <span className="mi"><i />created {shortDate(api.created_at)}</span>
                      <span className="mi"><i />key rotated {relative(api.key_rotated_at || api.created_at)}</span>
                    </div>
                  </>
                )}
              </div>
              <div className="dh2r">
                <button type="button" className="btn" onClick={togglePause} disabled={!!busy}>
                  {busy === 'resume' ? 'Activating…' : api.is_active ? <><Pause size={13} strokeWidth={1.8} aria-hidden="true" /> Deactivate</> : <><Play size={13} strokeWidth={1.8} aria-hidden="true" /> Activate</>}
                </button>
                <button type="button" className="btn" onClick={() => emailKey(api)} data-tip="Email it under a Highly Restricted label through Communication Center"><Mail size={13} strokeWidth={1.8} aria-hidden="true" /> Email key</button>
                {tab !== 'docs' && <button type="button" className="btn dark" onClick={downloadGuide} data-tip="Download the consumer guide (.md)"><Download size={13} strokeWidth={1.8} aria-hidden="true" /> Download guide</button>}
                <span style={{ position: 'relative' }} ref={menuRef}>
                  <button type="button" className="iconbtn" style={{ width: 36, height: 36 }} onClick={() => setMenu((m) => !m)} aria-haspopup="menu" aria-expanded={menu} aria-label="More actions"><Ellipsis size={16} strokeWidth={1.8} aria-hidden="true" /></button>
                  {menu && (
                    <div className="menu" role="menu">
                      <button type="button" role="menuitem" onClick={() => { setMenu(false); setEditing(true); }}><Pencil size={14} strokeWidth={1.8} aria-hidden="true" /> Edit name and description</button>
                      <button type="button" role="menuitem" onClick={copyBase}><Copy size={14} strokeWidth={1.8} aria-hidden="true" /> Copy base URL</button>
                      <button type="button" role="menuitem" onClick={rotate}><RotateCw size={14} strokeWidth={1.8} aria-hidden="true" /> Rotate key</button>
                      <hr />
                      <button type="button" role="menuitem" className="dz" onClick={remove}><Trash2 size={14} strokeWidth={1.8} aria-hidden="true" /> Delete API</button>
                    </div>
                  )}
                </span>
              </div>
            </div>

            <div className={`tbar ${stuck ? 'stuck' : ''}`} ref={tbar}>
              <span className="tmini"><span className={`dot ${api.is_active ? 'live' : 'off'}`} />{api.name}</span>
              <div className="tpills" ref={pills} role="tablist" aria-label="API sections">
                {ind && <span className="tind" style={{ left: ind.left, width: ind.width }} />}
                {TABS.map(([id, label]) => (
                  <button
                    key={id}
                    type="button"
                    role="tab"
                    aria-selected={tab === id}
                    className={`tp ${tab === id ? 'on' : ''}`}
                    onClick={() => nav.openApi(api.slug, id)}
                  >
                    {label}
                    {id === 'tables' && <span className="n">{tables.length}</span>}
                  </button>
                ))}
              </div>
              <Busybar on={detail.loading && !!api} />
            </div>

            <div className="dbody" key={tab} role="tabpanel">
              {detail.error && (
                <div className="inline-warn">
                  Couldn&apos;t refresh this API ({detail.error.message}).{' '}
                  <button type="button" className="link" onClick={() => detail.reload()}>Try again</button>
                </div>
              )}
              {tab === 'overview' && <OverviewTab {...tabProps} />}
              {tab === 'tables' && <TablesTab {...tabProps} />}
              {tab === 'endpoints' && <EndpointsTab {...tabProps} />}
              {tab === 'search' && <SemanticTab {...tabProps} />}
              {tab === 'docs' && <DocsTab {...tabProps} onDownload={downloadGuide} />}
              {tab === 'activity' && <ActivityTab {...tabProps} />}
            </div>
          </>
        )}
      </div>
    </div>
  );
}