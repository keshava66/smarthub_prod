import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Activity, ArrowRight, CircleAlert, CircleCheck, CircleDashed, Search, TrendingDown, TrendingUp } from 'lucide-react';
import { API_COLORS, baseUrl, clock, copyText, daysOld, describeIp, fmt, fmtDur, greeting, pctChange, plural, relative, sourceLook, sum, toolOf, TOOLS, useLiveCalls, usePage, useToast } from './lib';
import { Busybar, CountUp, Dot, ErrorBox, Kbd, Seg, Sk, SourceLogo, Spark, StackChart, ToolMark, ToolTile } from './ui';
import aiIcon from '/icons/JJ_Icon_AI_RGB.svg';
import vizIcon from '/icons/JJ_Icon_Data_Visualization.svg';
import globeIcon from '/icons/JJ_Icon_Globe_RGB.svg';
import graphIcon from '/icons/JJ_Icon_Graph_Chart_RGB.svg';
import networkIcon from '/icons/JJ_Icon_Network_RGB.svg';

const TOOL_ICON = { powerbi: graphIcon, python: aiIcon, tableau: vizIcon, browser: globeIcon };
const DEFAULT_DESTS = ['powerbi', 'python', 'tableau'];
const PINNED_DESTS = ['bruno', 'curl']; // always on the map, even with no traffic yet
const SRC_SLOTS = [16, 39, 62, 85];
const DST_SLOTS = [12, 31, 50, 69, 88];

const firstName = (user) => {
  const raw = (user?.name || user?.email || '').replace(/\[.*?\]|\(.*?\)/g, '').trim();
  if (!raw) return '';
  const part = raw.includes(',') ? raw.split(',')[1] : raw.split('@')[0].replace(/[._-]+/g, ' ');
  const w = (part || '').trim().split(/\s+/)[0] || '';
  return w.charAt(0).toUpperCase() + w.slice(1).toLowerCase();
};

const methodOf = (endpoint) => (['vector-search', 'retrieve'].includes(endpoint) ? 'POST' : 'GET');
const callPath = (c) => (c.alias ? `/${c.slug}/${c.alias}/${c.endpoint}` : `/${c.slug}/${c.endpoint}`);

/* Reveals a block the first time it scrolls into view. */
function Reveal({ as: Tag = 'div', className = '', delay = 0, style, children, ...rest }) {
  const ref = useRef(null);
  const [inView, setInView] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el || inView) return undefined;
    if (!('IntersectionObserver' in window)) {
      setInView(true);
      return undefined;
    }
    const io = new IntersectionObserver((ents) => {
      if (ents.some((e) => e.isIntersecting)) {
        setInView(true);
        io.disconnect();
      }
    }, { threshold: 0.08 });
    io.observe(el);
    return () => io.disconnect();
  }, [inView]);
  return (
    <Tag ref={ref} className={`rv ${inView ? 'in' : ''} ${className}`} style={{ transitionDelay: `${delay}ms`, ...style }} {...rest}>
      {children}
    </Tag>
  );
}

/* ------------------------------------------------------------ gateway */

const bez = (a, c1, c2, b, t) => {
  const u = 1 - t;
  return u * u * u * a + 3 * u * u * t * c1 + 3 * u * t * t * c2 + t * t * t * b;
};

function Gateway({ srcNodes, dstNodes, perMinute, live, paused, apisBySource, packetRef }) {
  const box = useRef(null);
  const canvas = useRef(null);
  const layerRef = useRef(null);
  const [hot, setHot] = useState(null);
  const hotRef = useRef(null);
  const pausedRef = useRef(paused);
  const busyRef = useRef(perMinute > 0);
  hotRef.current = hot;
  pausedRef.current = paused;
  busyRef.current = perMinute > 0;

  const nS = srcNodes.length;
  const nD = dstNodes.length;

  useEffect(() => {
    const cv = canvas.current;
    const wrap = box.current;
    if (!cv || !wrap || !nS || !nD) return undefined;
    const ctx = cv.getContext('2d');
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    let W = 0;
    let H = 0;
    const fit = () => {
      W = cv.clientWidth;
      H = cv.clientHeight;
      cv.width = W * dpr;
      cv.height = H * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    fit();
    const sy = (i) => SRC_SLOTS[i] ?? 50;
    const dy = (i) => DST_SLOTS[i] ?? 50;
    const P = (p) => {
      const hx = W / 2;
      const hy = H / 2;
      if (p.t < 0.5) {
        const t = p.t * 2;
        const ax = W * 0.09 + 34;
        const ay = (H * sy(p.s)) / 100;
        return [bez(ax, ax + W * 0.16, hx - W * 0.16, hx, t), bez(ay, ay, hy, hy, t)];
      }
      const t = (p.t - 0.5) * 2;
      const bx = W * 0.91 - 34;
      const by = (H * dy(p.d)) / 100;
      return [bez(hx, hx + W * 0.16, bx - W * 0.16, bx, t), bez(hy, hy, by, by, t)];
    };
    const srcRgb = srcNodes.map((n) => {
      const c = sourceLook(n).color.replace('#', '');
      return `${parseInt(c.slice(0, 2), 16)},${parseInt(c.slice(2, 4), 16)},${parseInt(c.slice(4, 6), 16)}`;
    });
    const parts = Array.from({ length: 90 }, () => ({
      s: Math.floor(Math.random() * nS), d: Math.floor(Math.random() * nD), t: Math.random(), v: 0.002 + Math.random() * 0.004,
    }));
    const packets = [];
    let last = 0;
    let rot = 0;
    let raf = 0;

    const ping = (sel) => {
      const n = wrap.querySelector(sel);
      if (!n) return;
      n.classList.remove('ping');
      void n.offsetWidth; // restart the ring animation
      n.classList.add('ping');
    };

    packetRef.current = (call, s, d) => {
      if (!layerRef.current || packets.length > 3 || document.hidden) return;
      const el = document.createElement('div');
      el.className = 'packet';
      const label = document.createElement('span');
      label.textContent = `${methodOf(call.endpoint)} ${call.alias ? `${call.alias}/` : ''}${call.endpoint} `;
      const st = document.createElement('b');
      st.textContent = String(call.status);
      if (call.status >= 400) st.className = 'e';
      el.append(label, st, document.createTextNode(call.duration_ms != null ? ` ${call.duration_ms}ms` : ''));
      layerRef.current.appendChild(el);
      packets.push({ el, s, d, t: 0 });
      ping(`[data-gsrc="${s}"]`);
    };

    const draw = (now) => {
      raf = requestAnimationFrame(draw);
      if (document.hidden || now - last < 30) return;
      last = now;
      if (cv.clientWidth !== W || cv.clientHeight !== H) fit();
      ctx.clearRect(0, 0, W, H);
      const hx = W / 2;
      const hy = H / 2;
      const h = hotRef.current;
      const speed = pausedRef.current ? 0.2 : busyRef.current ? 1 : 0.35;
      srcNodes.forEach((_, i) => {
        const a = h == null || h === i ? 1 : 0.25;
        const ax = W * 0.09 + 34;
        const ay = (H * sy(i)) / 100;
        const g = ctx.createLinearGradient(ax, 0, hx, 0);
        g.addColorStop(0, `rgba(${srcRgb[i]},${0.35 * a})`);
        g.addColorStop(1, `rgba(235,23,0,${0.12 * a})`);
        ctx.strokeStyle = g;
        ctx.lineWidth = h === i ? 2 : 1.2;
        ctx.beginPath();
        ctx.moveTo(ax, ay);
        ctx.bezierCurveTo(ax + W * 0.16, ay, hx - W * 0.16, hy, hx, hy);
        ctx.stroke();
      });
      dstNodes.forEach((_, i) => {
        const bx = W * 0.91 - 34;
        const by = (H * dy(i)) / 100;
        ctx.strokeStyle = 'rgba(235,23,0,.2)';
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        ctx.moveTo(hx, hy);
        ctx.bezierCurveTo(hx + W * 0.16, hy, bx - W * 0.16, by, bx, by);
        ctx.stroke();
      });
      rot += 0.005 * speed;
      [[104, 1.1, 'rgba(235,23,0,.5)', 1, 2.4], [122, 1.7, 'rgba(15,104,178,.32)', -1, 1.4], [142, 0.7, 'rgba(235,23,0,.25)', 1, 1.4],
        [164, 2.3, 'rgba(15,104,178,.18)', -1, 1.2], [188, 1.2, 'rgba(140,59,187,.16)', 1, 1.2], [214, 0.5, 'rgba(235,23,0,.12)', -1, 1]]
        .forEach(([r, len, c, dir, lw], i) => {
          const rr = Math.min(r, (Math.min(W, H) * r) / 520);
          ctx.strokeStyle = c;
          ctx.lineWidth = lw;
          for (let k = 0; k < 3; k += 1) {
            const a0 = rot * dir * (1 + i * 0.25) + k * 2.1;
            ctx.beginPath();
            ctx.arc(hx, hy, rr, a0, a0 + len);
            ctx.stroke();
          }
        });
      parts.forEach((p, idx) => {
        if (!busyRef.current && idx % 3) return; // a quiet gateway shows a trickle, not a flood
        p.t += p.v * speed;
        if (p.t > 1) {
          p.t = 0;
          p.s = Math.floor(Math.random() * nS);
          p.d = Math.floor(Math.random() * nD);
        }
        const [x, y] = P(p);
        const out = p.t >= 0.5;
        const a = h == null || h === p.s ? 1 : 0.15;
        const col = out ? '235,23,0' : srcRgb[p.s];
        ctx.fillStyle = `rgba(${col},${0.16 * a})`;
        ctx.beginPath();
        ctx.arc(x, y, 5.5, 0, 6.283);
        ctx.fill();
        ctx.fillStyle = `rgba(${col},${0.95 * a})`;
        ctx.beginPath();
        ctx.arc(x, y, 2, 0, 6.283);
        ctx.fill();
      });
      for (let i = packets.length - 1; i >= 0; i -= 1) {
        const k = packets[i];
        k.t += 0.0042;
        if (k.t > 1) {
          k.el.remove();
          packets.splice(i, 1);
          continue;
        }
        const [x, y] = P(k);
        k.el.style.left = `${x}px`;
        k.el.style.top = `${y}px`;
        k.el.style.opacity = k.t < 0.06 ? k.t / 0.06 : k.t > 0.92 ? (1 - k.t) / 0.08 : 1;
        if (!k.pinged && k.t > 0.5) {
          k.pinged = true;
          ping(`[data-gdst="${k.d}"]`);
        }
      }
    };
    raf = requestAnimationFrame(draw);
    return () => {
      cancelAnimationFrame(raf);
      packets.forEach((k) => k.el.remove());
      packetRef.current = null;
    };
  }, [nS, nD, srcNodes, dstNodes, packetRef]);

  const tipFor = hot != null ? srcNodes[hot] : null;

  return (
    <div className="gw" ref={box}>
      <canvas ref={canvas} />
      <div ref={layerRef} style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }} />
      {srcNodes.map((id, i) => (
        <div
          key={id}
          className={`node ${hot != null && hot !== i ? 'dim' : ''} ${hot === i ? 'hot' : ''}`}
          data-gsrc={i}
          style={{ left: '9%', top: `${SRC_SLOTS[i]}%` }}
          onMouseEnter={() => setHot(i)}
          onMouseLeave={() => setHot(null)}
        >
          <span className="halo" />
          <span className="bub"><SourceLogo id={id} size={30} /></span>
          <span className="nl">{sourceLook(id).label.toUpperCase()}</span>
          <span className="nc">{plural((apisBySource[id] || []).length, 'API')}</span>
        </div>
      ))}
      {dstNodes.map((tool, i) => (
        <div key={tool} className="node" data-gdst={i} style={{ left: '91%', top: `${DST_SLOTS[i]}%` }}>
          <span className="halo" />
          <span className="bub">
            {ToolMark({ tool, size: 24 }) || <img src={TOOL_ICON[tool] || networkIcon} alt="" style={{ width: 24, height: 24, filter: 'brightness(0)', opacity: 0.75 }} />}
          </span>
          <span className="nl">{TOOLS[tool].name.toUpperCase()}</span>
        </div>
      ))}
      <div className="core">
        <span className="label" style={{ fontSize: 9.5, letterSpacing: '.24em' }}>GATEWAY</span>
        <span className="cv">{paused ? '–' : perMinute}</span>
        <span style={{ fontSize: 11, color: 'var(--muted)' }}>calls / min</span>
      </div>
      <div
        className="gtip"
        style={{
          opacity: tipFor ? 1 : 0,
          left: `calc(9% + 50px)`,
          top: `calc(${SRC_SLOTS[hot ?? 0]}% - 30px)`,
        }}
      >
        {tipFor && (
          <>
            <div className="row" style={{ gap: 8, marginBottom: 6 }}>
              <SourceLogo id={tipFor} size={18} />
              <b style={{ fontWeight: 700 }}>{sourceLook(tipFor).label}</b>
            </div>
            {(apisBySource[tipFor] || []).slice(0, 6).map((a) => (
              <div key={a.id} style={{ fontSize: 11.5, color: 'var(--ink2)', padding: '2px 0' }}>· {a.name}</div>
            ))}
            {!(apisBySource[tipFor] || []).length && <div style={{ fontSize: 11.5, color: 'var(--muted)' }}>No APIs use it yet</div>}
          </>
        )}
      </div>
      <div className="gwcap">
        <i style={{ background: live ? 'var(--purple)' : 'var(--line3)' }} />
        {live ? 'LIVE TRAFFIC · HOVER A SOURCE' : 'GATEWAY UNREACHABLE'}
      </div>
    </div>
  );
}

/* ---------------------------------------------------------- dashboard */

export default function Dashboard() {
  const { user, overview, sources, range, setRange, nav, openPalette } = usePage();
  const toast = useToast();
  const data = overview.data;
  const apis = useMemo(() => data?.apis || [], [data]);
  const [hidden, setHidden] = useState([]);
  const [paused, setPaused] = useState(false);
  const [tq, setTq] = useState('');
  const [tf, setTf] = useState('all');
  const [ts, setTs] = useState('calls');
  const packetRef = useRef(null);

  const colorOf = useMemo(() => {
    const m = {};
    apis.forEach((a, i) => { m[a.id] = API_COLORS[i % API_COLORS.length]; });
    return m;
  }, [apis]);

  /* Which source each alias reads from, so a live call can fly from the right node. */
  const aliasSource = useMemo(() => {
    const m = {};
    apis.forEach((a) => (a.tables || []).forEach((t) => { m[`${a.id}/${t.alias}`] = t.source_system; }));
    return m;
  }, [apis]);

  const apisBySource = useMemo(() => {
    const m = {};
    apis.forEach((a) => (a.sources || []).forEach((s) => { (m[s] = m[s] || []).push(a); }));
    return m;
  }, [apis]);

  const configured = useMemo(() => (sources.data || []).filter((s) => s.configured), [sources.data]);

  const srcNodes = useMemo(() => {
    const order = [
      ...(data?.by_source || []).map((s) => s.source_system),
      ...Object.keys(apisBySource),
      ...configured.map((s) => s.id),
    ];
    const out = [...new Set(order)].slice(0, 4);
    return out.length ? out : ['snowflake', 'postgresql'];
  }, [data, apisBySource, configured]);

  const dstNodes = useMemo(() => {
    const seen = [];
    (data?.by_agent || []).forEach((a) => {
      const t = toolOf(a.user_agent);
      if (t !== 'unknown' && t !== 'smarthub' && !PINNED_DESTS.includes(t) && !seen.includes(t)) seen.push(t);
    });
    DEFAULT_DESTS.forEach((t) => { if (!seen.includes(t)) seen.push(t); });
    return [...seen.slice(0, 3), ...PINNED_DESTS];
  }, [data]);

  const live = useLiveCalls({
    keep: 7,
    paused,
    onNew: (calls) => {
      calls.slice(-3).forEach((c, i) => setTimeout(() => {
        const src = aliasSource[`${c.api_id}/${c.alias}`];
        const s = Math.max(0, srcNodes.indexOf(src));
        const d = Math.max(0, dstNodes.indexOf(toolOf(c.user_agent)));
        packetRef.current?.(c, s, d);
      }, i * 700));
    },
  });

  const totals = data?.totals || {};
  const prev = data?.previous || {};
  const daily = data?.daily || [];
  const dates = data?.dates || [];

  const kpis = [
    { l: `Requests · ${range}d`, v: totals.calls, acc: '#eb1700', sp: daily.map((d) => d.calls), delta: pctChange(totals.calls, prev.calls), note: 'vs previous period' },
    { l: 'Rows served', v: totals.rows_served, acc: '#0f68b2', sp: daily.map((d) => d.rows_served), note: `last ${range} days` },
    {
      l: 'Median latency', v: totals.median_ms, unit: 'ms', raw: true, acc: '#328714', sp: daily.map((d) => d.median_ms || 0),
      deltaTxt: totals.median_ms != null && prev.median_ms != null ? `${totals.median_ms - prev.median_ms > 0 ? '+' : '−'}${Math.abs(totals.median_ms - prev.median_ms)} ms` : null,
      good: totals.median_ms <= prev.median_ms, note: 'p50 across APIs',
    },
    {
      l: 'Rejected calls', v: totals.errors, raw: true, acc: '#ff6017', sp: daily.map((d) => d.errors),
      deltaTxt: prev.errors != null && totals.errors !== prev.errors ? `${totals.errors > prev.errors ? '+' : '−'}${Math.abs(totals.errors - prev.errors)}` : null,
      good: totals.errors <= prev.errors,
      note: totals.calls ? `${((totals.errors / totals.calls) * 100).toFixed(2)}% of traffic` : 'no traffic yet',
    },
    {
      l: 'Distinct callers', v: totals.callers, raw: true, acc: '#8c3bbb', sp: daily.map((d) => d.callers),
      deltaTxt: prev.callers != null && totals.callers !== prev.callers ? `${totals.callers > prev.callers ? '+' : '−'}${Math.abs(totals.callers - prev.callers)}` : null,
      good: totals.callers >= prev.callers, note: 'machines this period',
    },
  ];

  const chartSeries = useMemo(() => apis
    .filter((a) => !hidden.includes(a.id))
    .map((a) => ({ key: a.id, name: a.name, color: colorOf[a.id], vals: a.series || [] })), [apis, hidden, colorOf]);

  const attention = useMemo(() => {
    const out = [];
    apis.forEach((a) => {
      if (!a.is_active && a.calls > 0) {
        out.push({ sev: 3, id: `paused-${a.id}`, icon: <CircleAlert size={15} strokeWidth={1.8} aria-hidden="true" />, c: '#9e3a00', bg: '#ff601718', t: `${plural(a.calls, 'call')} to an inactive API`, s: `/${a.slug} is inactive but callers still try it. They all get 403.`, btn: 'View', go: () => nav.openApi(a.slug, 'activity') });
      } else if (a.is_active && a.calls >= 20 && a.errors / a.calls > 0.05) {
        out.push({ sev: 2, id: `err-${a.id}`, icon: <CircleAlert size={15} strokeWidth={1.8} aria-hidden="true" />, c: '#9e3a00', bg: '#ff601718', t: `${Math.round((a.errors / a.calls) * 100)}% of calls rejected`, s: `${a.name}: ${plural(a.errors, 'rejected call')} in ${range} days.`, btn: 'View', go: () => nav.openApi(a.slug, 'activity') });
      }
      const np = (a.tables || []).find((t) => (t.env || '').toLowerCase() !== 'prod');
      if (np) {
        out.push({ sev: 1, id: `env-${a.id}`, icon: (np.env || '?').toUpperCase().slice(0, 3), c: 'var(--blue)', bg: '#0f68b214', t: 'Alias reads a non-PROD table', s: `${a.name}: /${np.alias} points at ${(np.env || '').toUpperCase()}.`, btn: 'Review', go: () => nav.openApi(a.slug, 'tables') });
      }
      if (a.calls >= 50 && a.prev_calls > 0 && a.calls / a.prev_calls >= 2) {
        out.push({ sev: 1, id: `up-${a.id}`, icon: <TrendingUp size={15} strokeWidth={1.8} aria-hidden="true" />, c: 'var(--green)', bg: '#32871414', t: `Traffic up ${(a.calls / a.prev_calls).toFixed(1)}×`, s: `${a.name}, against the ${range} days before. Check the row limits still fit.`, btn: 'Open', go: () => nav.openApi(a.slug) });
      }
      const sinceCreated = daysOld(a.created_at);
      if (a.is_active && !a.last_used_at && sinceCreated != null && sinceCreated >= 7) {
        out.push({ sev: 0, id: `idle-${a.id}`, icon: <CircleDashed size={15} strokeWidth={1.8} aria-hidden="true" />, c: 'var(--muted)', bg: 'var(--bg)', t: 'Never called', s: `${a.name} has had no calls since it was published ${relative(a.created_at)}.`, btn: 'Open', go: () => nav.openApi(a.slug) });
      }
    });
    return out.sort((x, y) => y.sev - x.sev);
  }, [apis, nav, range]);

  const bySource = data?.by_source || [];
  const sourceTotal = sum(bySource.map((s) => s.calls));

  const clients = useMemo(() => {
    const m = {};
    (data?.by_agent || []).forEach((a) => {
      const k = toolOf(a.user_agent);
      const e = m[k] || (m[k] = { tool: k, calls: 0, rows: 0, ms: 0, machines: 0 });
      e.calls += a.calls;
      e.rows += a.rows_served;
      e.ms += a.total_ms;
      e.machines += a.machines;
    });
    return Object.values(m).sort((a, b) => b.calls - a.calls).slice(0, 6);
  }, [data]);

  const [armed, setArmed] = useState(false);
  useEffect(() => {
    const id = requestAnimationFrame(() => setArmed(true));
    return () => cancelAnimationFrame(id);
  }, [data]);

  const tiles = useMemo(() => {
    const q = tq.trim().toLowerCase();
    let list = apis
      .filter((a) => (tf === 'all' ? true : tf === 'live' ? a.is_active : !a.is_active))
      .filter((a) => !q || [a.name, a.description, a.created_by, a.slug, ...(a.aliases || [])].filter(Boolean).join(' ').toLowerCase().includes(q));
    if (ts === 'calls') list = list.slice().sort((x, y) => y.calls - x.calls);
    if (ts === 'az') list = list.slice().sort((x, y) => x.name.localeCompare(y.name));
    if (ts === 'new') list = list.slice().sort((x, y) => String(y.created_at).localeCompare(String(x.created_at)));
    return list;
  }, [apis, tq, tf, ts]);


  const copyUrl = async (a) => {
    const ok = await copyText(baseUrl(a.slug));
    toast(ok ? 'Base URL copied' : "Couldn't copy. Your browser blocked the clipboard.", ok ? 'ok' : 'error');
  };

  const sourceNames = configured.slice(0, 4).map((s) => s.label);
  const name = firstName(user);
  const firstLoad = overview.loading && !data;
  const ex = apis[0];
  const exAlias = ex?.aliases?.[0] || 'customers';

  return (
    <div className="home">
      <div className="texture" />
      <div className="glow1" />
      <div className="glow2" />
      <div className="glow3" />
      <div className="hwrap">
        <div className="hhead">
          <div className="lockup"><small>J&amp;J Innovative Medicine</small><b>GRA SmartHub</b></div>
          <span className="vrule" />
          <div className="ptitle"><span>DATA API EXPORT</span><span>GLOBAL REGULATORY AFFAIRS</span></div>
          <span style={{ flex: 1 }} />
          <button type="button" className="jump" onClick={openPalette}>
            <Search size={15} strokeWidth={1.8} aria-hidden="true" />
            <span style={{ flex: 1, textAlign: 'left' }}>Jump to an API</span>
            <Kbd>Ctrl K</Kbd>
          </button>
          <div className="capsule">
            <span className="cs"><b>{totals.live_apis ?? '–'}</b> live APIs</span>
            <span className="cs"><b>{data ? fmt(totals.calls) : '–'}</b> calls · {range}d</span>
            <span className="ok">
              <span className={`dot ${overview.error && !data ? '' : 'live'}`} style={overview.error && !data ? { background: 'var(--red)' } : undefined} />
              {overview.error && !data ? 'GATEWAY UNREACHABLE' : 'GATEWAY ONLINE'}
            </span>
          </div>
        </div>

        <section className="herogrid" style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1fr) minmax(0,1.08fr)', gap: 28, alignItems: 'center', padding: '34px 0 10px' }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 26, animation: 'dax-rise .8s both' }}>
            <span className="eyebrow"><i />{greeting().toUpperCase()}{name ? `, ${name.toUpperCase()}` : ''} · PUBLISHING DESK</span>
            <h1 className="h-xl">
              <span style={{ whiteSpace: 'nowrap' }}>Governed tables in.</span>
              <br />
              <em style={{ whiteSpace: 'nowrap' }}>Live endpoints out.</em>
            </h1>
            <span className="redrule" />
            <p style={{ margin: 0, maxWidth: 540, fontSize: 16.5, lineHeight: 1.65, color: 'var(--ink2)' }}>
              Publish {sourceNames.length ? `${sourceNames.slice(0, -1).join(', ')}${sourceNames.length > 1 ? ' and ' : ''}${sourceNames[sourceNames.length - 1]}` : 'source'} tables
              as key-authenticated, read-only APIs. Hand one key to Power BI, a notebook or a service, then watch every call land right here.
            </p>
            <div className="row" style={{ gap: 12, flexWrap: 'wrap' }}>
              <button type="button" className="cta" onClick={nav.create}>Publish a new API <span className="arr"><ArrowRight size={14} strokeWidth={1.8} aria-hidden="true" /></span></button>
              <button type="button" className="ghost" onClick={() => document.getElementById('dax-apis')?.scrollIntoView({ behavior: 'smooth' })}>Browse your APIs</button>
            </div>
            <div className="conn">
              <span className="lbl">CONNECTED</span>
              {sources.loading && !sources.data && [0, 1, 2].map((i) => <Sk key={i} w={110} h={32} r={999} />)}
              {sources.error && !sources.data && <span style={{ fontSize: 12, color: 'var(--muted)' }}>Source list unavailable</span>}
              {configured.map((s) => (
                <span key={s.id} className="srcpill"><SourceLogo id={s.id} size={20} />{s.label}<span className="g" /></span>
              ))}
            </div>
          </div>
          <Gateway
            srcNodes={srcNodes}
            dstNodes={dstNodes}
            perMinute={live.perMinute}
            live={!live.error}
            paused={paused}
            apisBySource={apisBySource}
            packetRef={packetRef}
          />
        </section>

        <section style={{ paddingTop: 70, position: 'relative' }} id="dax-cc">
          <Reveal className="sechead">
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              <span className="kicker">COMMAND CENTER</span>
              <h2 className="h-l">Your traffic, <em>at a glance.</em></h2>
            </div>
            <div className="row" style={{ gap: 10 }}>
              {overview.loading && data && <span className="row" style={{ gap: 6, fontSize: 12, color: 'var(--muted)' }}><span className="spinner" />Updating</span>}
              <Seg value={range} onChange={setRange} options={[[7, '7 days'], [14, '14 days'], [30, '30 days']]} btnStyle={{ padding: '7px 16px' }} ariaLabel="Time window" />
            </div>
          </Reveal>

          {overview.error && data && (
            <div className="inline-warn" style={{ marginBottom: 14 }}>
              Couldn&apos;t refresh just now ({overview.error.message}). Showing the last figures that loaded.{' '}
              <button type="button" className="link" onClick={() => overview.reload()}>Try again</button>
            </div>
          )}

          {overview.error && !data ? (
            <div className="panel"><ErrorBox error={overview.error} onRetry={() => overview.reload()} title="Couldn't load the traffic" /></div>
          ) : (
            <>
              <div className="kpis" style={{ opacity: overview.loading && data ? 0.6 : 1, transition: 'opacity .2s' }}>
                {kpis.map((k, i) => (
                  <Reveal key={k.l} className="kpi" delay={i * 60} style={{ '--acc': k.acc }}>
                    <div className="between" style={{ alignItems: 'center' }}>
                      <span className="label">{k.l}</span>
                      {k.delta != null && <span className={`delta ${k.delta >= 0 ? 'up' : 'down'}`}>{k.delta >= 0 ? <TrendingUp size={13} strokeWidth={1.8} aria-hidden="true" /> : <TrendingDown size={13} strokeWidth={1.8} aria-hidden="true" />} {Math.abs(k.delta)}%</span>}
                      {k.delta == null && k.deltaTxt && <span className={`delta ${k.good ? 'up' : 'down'}`}>{k.deltaTxt}</span>}
                    </div>
                    <span className="kv">
                      {firstLoad ? <Sk w={90} h={34} /> : k.v == null ? '–' : <CountUp value={k.v} raw={k.raw} />}
                      {k.unit && k.v != null && !firstLoad && <small>{k.unit}</small>}
                    </span>
                    <span style={{ fontSize: 11.5, color: 'var(--muted)' }}>{firstLoad ? <Sk w={120} h={10} /> : k.note}</span>
                    <Spark values={k.sp.length ? k.sp : [0, 0]} color={k.acc} />
                  </Reveal>
                ))}
              </div>

              <div className="cgrid">
                <Reveal className="panel">
                  <Busybar on={overview.loading && !!data} />
                  <div className="ptl">
                    <span className="t">Requests per day, by API</span>
                    <span className="s">hover for the breakdown · click a chip to hide it</span>
                  </div>
                  <div className="legend">
                    {firstLoad && [0, 1, 2].map((i) => <Sk key={i} w={120} h={26} r={999} />)}
                    {apis.map((a) => (
                      <button
                        key={a.id}
                        type="button"
                        className={`lg ${hidden.includes(a.id) ? 'off' : ''}`}
                        aria-pressed={!hidden.includes(a.id)}
                        onClick={() => setHidden((h) => {
                          const next = h.includes(a.id) ? h.filter((x) => x !== a.id) : [...h, a.id];
                          return next.length >= apis.length ? [] : next;
                        })}
                      >
                        <i style={{ background: colorOf[a.id] }} />
                        <span>{a.name}</span>
                      </button>
                    ))}
                  </div>
                  {firstLoad ? <Sk h={260} r={14} style={{ marginTop: 6 }} /> : (
                    <StackChart series={chartSeries} dates={dates} height={260} style={{ margin: '6px 0 18px 28px' }} emptyText={apis.length ? 'No calls in this window' : 'Publish an API and its traffic shows up here'} />
                  )}
                </Reveal>

                <Reveal className="panel feed" delay={80}>
                  <div className="ptl">
                    <span className="t row" style={{ gap: 10 }}>
                      <span className="livedot" style={paused || live.error ? { background: '#a39992', animation: 'none' } : undefined} />
                      Live calls
                    </span>
                    <button type="button" className="kbtn" style={{ padding: '5px 10px', fontSize: 11 }} onClick={() => setPaused((p) => !p)}>
                      {paused ? 'Resume' : 'Pause'}
                    </button>
                  </div>
                  <div style={{ display: 'flex', flexDirection: 'column', marginTop: -6, flex: 1 }}>
                    {live.loading && !live.calls.length && [0, 1, 2, 3, 4].map((i) => (
                      <div key={i} className="frow" style={{ animation: 'none' }}><Sk w={44} h={10} /><Sk w={30} h={10} /><Sk h={10} /><Sk w={36} h={16} /></div>
                    ))}
                    {live.error && !live.calls.length && (
                      <div className="feed-empty"><i><CircleAlert size={16} strokeWidth={1.8} aria-hidden="true" /></i>Couldn&apos;t read the call log.<button type="button" className="link" onClick={live.retry}>Try again</button></div>
                    )}
                    {!live.loading && !live.error && !live.calls.length && (
                      <div className="feed-empty"><i><Activity size={16} strokeWidth={1.8} aria-hidden="true" /></i>No calls yet. They appear here the moment a consumer hits an endpoint.</div>
                    )}
                    {live.calls.map((c, i) => (
                      <div key={c.id} className="frow" style={i ? { animation: 'none' } : undefined}>
                        <span style={{ fontSize: 10.5, color: '#81766f' }} className="num">{clock(c.ts)}</span>
                        <span className="mth">{methodOf(c.endpoint)}</span>
                        <span style={{ minWidth: 0 }}>
                          <code style={{ display: 'block' }}>{callPath(c)}</code>
                          <span className="who">{describeIp(c.ip)} · {TOOLS[toolOf(c.user_agent)].name}</span>
                        </span>
                        <span style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 3 }}>
                          <span className={`st ${c.status >= 400 ? 'err' : 'ok'}`}>{c.status}</span>
                          <span style={{ fontSize: 10, color: '#81766f' }} className="num">{c.duration_ms != null ? `${c.duration_ms} ms` : ''}</span>
                        </span>
                      </div>
                    ))}
                  </div>
                  <span style={{ marginTop: 'auto', fontSize: 11, color: '#81766f' }}>
                    Every request is logged with caller IP, client and status. Full history lives on each API&apos;s Activity tab.
                  </span>
                </Reveal>
              </div>

              <div className="cgrid3">
                <Reveal className="panel">
                  <div className="ptl"><span className="t">Needs attention</span><span className="s">{firstLoad ? '' : plural(attention.length, 'item')}</span></div>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                    {firstLoad && [0, 1, 2].map((i) => <Sk key={i} h={56} r={14} />)}
                    {!firstLoad && !attention.length && (
                      <div className="att-empty"><CircleCheck size={16} strokeWidth={1.8} aria-hidden="true" />Nothing needs you right now. Callers are getting through.</div>
                    )}
                    {attention.slice(0, 4).map((x) => (
                      <div key={x.id} className="att">
                        <span className="ai" style={{ background: x.bg, color: x.c, fontSize: typeof x.icon === 'string' && x.icon.length > 1 ? 10 : 14 }}>{x.icon}</span>
                        <div className="ab"><b>{x.t}</b><span>{x.s}</span></div>
                        <button type="button" className="btn" style={{ padding: '6px 10px', fontSize: 11 }} onClick={x.go}>{x.btn}</button>
                      </div>
                    ))}
                    {attention.length > 4 && <span style={{ fontSize: 11.5, color: 'var(--muted)' }}>{plural(attention.length - 4, 'more item')} on the APIs below.</span>}
                  </div>
                </Reveal>

                <Reveal className="panel" delay={80}>
                  <div className="ptl"><span className="t">Traffic by source</span><span className="s">last {range} days</span></div>
                  {firstLoad ? <Sk h={170} r={14} /> : (
                    <div className="donut">
                      <div style={{ position: 'relative', width: 170, height: 170, flexShrink: 0 }}>
                        <svg viewBox="0 0 170 170">
                          <circle cx="85" cy="85" r="70" fill="none" stroke="#f1efed" strokeWidth="18" />
                          {(() => {
                            const C = 2 * Math.PI * 70;
                            let off = 0;
                            return bySource.map((s) => {
                              const len = sourceTotal ? (s.calls / sourceTotal) * C : 0;
                              const el = (
                                <circle
                                  key={s.source_system}
                                  className="seg"
                                  cx="85" cy="85" r="70" fill="none"
                                  stroke={sourceLook(s.source_system).color}
                                  strokeWidth="18"
                                  strokeDasharray={`${armed ? Math.max(0, len - 3) : 0} ${C}`}
                                  strokeDashoffset={-off}
                                />
                              );
                              off += len;
                              return el;
                            });
                          })()}
                        </svg>
                        <div className="dc" style={{ left: 0, top: 0 }}><b><CountUp value={sourceTotal} /></b><span>CALLS</span></div>
                      </div>
                      <div className="dl">
                        {!bySource.length && <span style={{ fontSize: 12.5, color: 'var(--muted)' }}>No table reads in this window yet.</span>}
                        {bySource.map((s) => (
                          <div key={s.source_system} className="dr">
                            <SourceLogo id={s.source_system} size={22} />
                            <span>{sourceLook(s.source_system).label}</span>
                            <b>{Math.round((s.calls / (sourceTotal || 1)) * 100)}%</b>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </Reveal>

                <Reveal className="panel" delay={160}>
                  <div className="ptl"><span className="t">Who&apos;s calling</span><span className="s">by client</span></div>
                  {firstLoad ? <Sk h={170} r={14} /> : (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                      {!clients.length && <span style={{ fontSize: 12.5, color: 'var(--muted)' }}>No callers in this window.</span>}
                      {clients.map((c, i) => (
                        <div key={c.tool} className="client">
                          <ToolTile tool={c.tool} size={30} />
                          <div style={{ minWidth: 0 }}>
                            <div className="between" style={{ fontSize: 12.5, flexWrap: 'nowrap' }}>
                              <span>{TOOLS[c.tool].name}</span>
                              <span className="row" style={{ gap: 10 }}>
                                <span style={{ fontSize: 11, color: 'var(--muted)' }} data-tip="Average time per call">{c.calls ? `${Math.round(c.ms / c.calls)} ms` : ''}</span>
                                <b style={{ fontWeight: 700 }} className="num">{fmt(c.calls)}</b>
                              </span>
                            </div>
                            <div className="cb"><i style={{ width: armed ? `${(c.calls / (clients[0].calls || 1)) * 100}%` : 0, background: API_COLORS[i % API_COLORS.length] }} /></div>
                          </div>
                          <span />
                        </div>
                      ))}
                      <span style={{ fontSize: 11, color: 'var(--faint)' }}>Grouped from user agents. A Power BI refresh and an Excel pull through the same gateway look alike.</span>
                    </div>
                  )}
                </Reveal>
              </div>
            </>
          )}
        </section>

        <section style={{ paddingTop: 80 }} id="dax-apis">
          <Reveal className="sechead">
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              <span className="kicker">PUBLISHED</span>
              <h2 className="h-l">Your <em>endpoints.</em></h2>
            </div>
            <div className="row" style={{ gap: 10, flexWrap: 'wrap' }}>
              <input className="field" value={tq} onChange={(e) => setTq(e.target.value)} placeholder="Filter by name, owner or alias" aria-label="Filter APIs" style={{ width: 250, borderRadius: 999, padding: '9px 16px' }} />
              <Seg value={tf} onChange={setTf} options={[['all', 'All'], ['live', 'Live'], ['paused', 'Inactive']]} btnStyle={{ padding: '7px 14px' }} ariaLabel="Status" />
              <Seg value={ts} onChange={setTs} options={[['calls', 'Most called'], ['new', 'Newest'], ['az', 'A to Z']]} btnStyle={{ padding: '7px 14px' }} ariaLabel="Sort" />
            </div>
          </Reveal>
          <div className="atiles">
            {firstLoad && [0, 1, 2].map((i) => (
              <div key={i} className="tile-sk"><Sk w={80} h={24} r={999} /><Sk w="70%" h={22} /><Sk w="50%" h={12} /><Sk h={40} style={{ marginTop: 'auto' }} /></div>
            ))}
            {overview.error && !data && (
              <div className="panel" style={{ gridColumn: '1/-1' }}><ErrorBox error={overview.error} onRetry={() => overview.reload()} title="Couldn't load your APIs" /></div>
            )}
            {tiles.map((a, i) => (
              <Reveal
                key={a.id}
                as="div"
                role="link"
                tabIndex={0}
                className="atile"
                delay={Math.min(i, 8) * 60}
                style={{ cursor: 'pointer', '--acc': colorOf[a.id] }}
                onClick={() => nav.openApi(a.slug)}
                onKeyDown={(e) => { if (e.key === 'Enter') nav.openApi(a.slug); }}
              >
                <span className="strip" style={{ background: `linear-gradient(90deg,${colorOf[a.id]},transparent)` }} />
                <div className="between" style={{ alignItems: 'center' }}>
                  <div className="row" style={{ gap: 10 }}>
                    <span className="logos">{(a.sources || []).map((s) => <SourceLogo key={s} id={s} size={24} />)}</span>
                    <span style={{ fontSize: 11, color: 'var(--muted)' }}>{plural(a.table_count, 'table')}</span>
                  </div>
                  <span className="row stat0" style={{ gap: 6, fontSize: 11, fontWeight: 700, color: a.is_active ? 'var(--green)' : 'var(--muted)' }}>
                    <Dot live={a.is_active} />{a.is_active ? 'Live' : 'Inactive'}
                  </span>
                </div>
                <div className="acts" onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
                  <button type="button" className="l" onClick={() => copyUrl(a)}>Copy URL</button>
                  <button type="button" className="l" onClick={() => nav.openApi(a.slug, 'endpoints')}>Try it</button>
                  <button type="button" onClick={() => nav.openApi(a.slug)}>Open <ArrowRight size={13} strokeWidth={1.8} aria-hidden="true" /></button>
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                  <span className="disp" style={{ fontSize: 22, lineHeight: 1.15, fontWeight: 400 }}>{a.name}</span>
                  <code style={{ fontSize: 11.5, color: 'var(--blue)' }}>/data-api/{a.slug}</code>
                </div>
                <div className="mets">
                  <span><b>{fmt(a.calls)}</b>calls · {range}d</span>
                  <span><b>{a.avg_ms != null ? (() => { const [n, u] = fmtDur(a.avg_ms).split(' '); return <>{n}<small style={{ fontSize: 11, color: 'var(--muted)', marginLeft: 3 }}>{u}</small></>; })() : '–'}</b>avg per call</span>
                  <span><b style={{ fontSize: 15 }}>{a.last_used_at ? relative(a.last_used_at) : 'Never'}</b>last call</span>
                </div>
                <Spark values={a.series && a.series.length > 1 ? a.series : [0, 0]} color={colorOf[a.id]} h={58} className="sp" />
                {!a.calls && <span className="nocalls">No calls in the last {range} days</span>}
              </Reveal>
            ))}
            {data && !tiles.length && apis.length > 0 && <p className="empty" style={{ gridColumn: '1/-1' }}>Nothing matches that filter.</p>}
            {!firstLoad && (
              <button type="button" className="atile add rv in" onClick={nav.create}>
                <span className="plus">+</span>
                <span style={{ fontSize: 15, fontWeight: 700 }}>{apis.length ? 'Publish a new API' : 'Publish your first API'}</span>
                <span style={{ fontSize: 12, color: 'var(--muted)' }}>Three steps, about a minute</span>
              </button>
            )}
          </div>
        </section>

        <section style={{ paddingTop: 80 }}>
          <Reveal className="sechead">
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              <span className="kicker">HOW IT WORKS</span>
              <h2 className="h-l">Three steps to a <em>live endpoint.</em></h2>
            </div>
          </Reveal>
          <div className="steps">
            {[
              ['01', 'Pick the tables', 'Browse a source by environment, database and schema. Choose the columns each alias exposes and the rows per page.', configured[0] ? `${configured[0].label} / PROD / …` : 'Source / PROD / schema / table'],
              ['02', 'Get one key', 'Publishing issues a single key. Rotate it any time and the old one stops working straight away.', 'api_key=sh_live_••••••••'],
              ['03', 'Call it from anywhere', 'Every alias gets rows, columns and count endpoints, plus semantic search where embeddings exist. Usage lands here per caller.', `GET /data-api/${ex?.slug || 'your-api'}/${exAlias}/rows`],
            ].map(([n, t, x, c], i) => (
              <Reveal key={n} className={`step ${i === 2 ? 'dark' : ''}`} delay={i * 90}>
                <span className="sn">{n}</span>
                <span className="disp" style={{ fontSize: 22, fontWeight: 400 }}>{t}</span>
                <span style={{ fontSize: 14, lineHeight: 1.6, color: 'var(--ink2)' }}>{x}</span>
                <code>{c}</code>
              </Reveal>
            ))}
          </div>
        </section>

        <div className="foot">
          <span>Confidential · Internal Use Only · © {new Date().getFullYear()} Regulatory Integrated Data · Johnson &amp; Johnson</span>
          <span>Data API Export · all calls are logged and audited</span>
        </div>
      </div>
    </div>
  );
}