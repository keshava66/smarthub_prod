import React, { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { Check, CircleAlert, CircleCheck, CircleDashed, CircleHelp, Globe, TriangleAlert, X } from 'lucide-react';
import { createPortal } from 'react-dom';
import { ConfirmContext, dayLabel, fmt, KEY_PLACEHOLDER, ROW_LIMITS, smoothPath, sourceLook, sum, ToastContext, toolOf, TOOLS, useProgress } from './lib';

/* ----------------------------------------------------------------- portal */

/* Overlays (drawers, menus, toasts, tooltips) live in one layer on <body> so
   no transformed ancestor can clip a position:fixed child. The layer carries
   the page's scope class, so the page's styles still reach them. */
let layer = null;
const getLayer = () => {
  if (!layer || !document.body.contains(layer)) {
    layer = document.createElement('div');
    layer.className = 'dax dax-layer';
    document.body.appendChild(layer);
  }
  return layer;
};

export function Portal({ children }) {
  return createPortal(children, getLayer());
}

/* ------------------------------------------------------------- small bits */

export const Sk = ({ w = '100%', h = 12, r = 8, style }) => (
  <span className="sk" style={{ width: w, height: h, borderRadius: r, ...style }} />
);

export const Spinner = ({ size = 14, style }) => (
  <span className="spinner" style={{ width: size, height: size, ...style }} aria-hidden="true" />
);

export const Kbd = ({ children }) => <span className="kbd">{children}</span>;

export const Dot = ({ live }) => <span className={`dot ${live ? 'live' : 'off'}`} />;

export function StatusPill({ live, small = true }) {
  return (
    <span
      className={`stpill ${small ? 'sm' : ''}`}
      style={{ background: live ? '#32871416' : 'rgba(49,44,42,.06)', color: live ? 'var(--green)' : 'var(--ink2)' }}
    >
      <Dot live={live} />
      {live ? 'LIVE' : 'INACTIVE'}
    </span>
  );
}

export function EnvTag({ env }) {
  const prod = (env || '').toLowerCase() === 'prod';
  return (
    <span className="env" style={{ background: prod ? '#0f68b21a' : '#f1efed', color: prod ? 'var(--blue)' : 'var(--ink2)' }}>
      {(env || '–').toUpperCase()}
    </span>
  );
}

export function SrcTag({ id }) {
  const s = sourceLook(id);
  return <span className="src" style={{ background: s.bg, color: s.ink }}>{s.label}</span>;
}

export function SourceLogo({ id, size = 20, style, className = '' }) {
  const s = sourceLook(id);
  if (s.logo) {
    return <img src={s.logo} alt="" className={className} style={{ width: size, height: size, borderRadius: '50%', ...style }} />;
  }
  return (
    <span
      className={`mk ${className}`}
      aria-hidden="true"
      style={{
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center', width: size, height: size,
        borderRadius: '50%', background: s.color, color: '#fff', fontFamily: 'JohnsonDisplay',
        fontSize: Math.max(8, Math.round(size * 0.38)), flexShrink: 0, ...style,
      }}
    >
      {s.mono}
    </span>
  );
}

/* Brand marks for the clients that have one. Path data comes from Simple Icons
   (simpleicons.org, CC0). Web gets a plain globe. */
const BRAND_MARKS = {
  python: { hex: '#3776AB', path: 'M14.25.18l.9.2.73.26.59.3.45.32.34.34.25.34.16.33.1.3.04.26.02.2-.01.13V8.5l-.05.63-.13.55-.21.46-.26.38-.3.31-.33.25-.35.19-.35.14-.33.1-.3.07-.26.04-.21.02H8.77l-.69.05-.59.14-.5.22-.41.27-.33.32-.27.35-.2.36-.15.37-.1.35-.07.32-.04.27-.02.21v3.06H3.17l-.21-.03-.28-.07-.32-.12-.35-.18-.36-.26-.36-.36-.35-.46-.32-.59-.28-.73-.21-.88-.14-1.05-.05-1.23.06-1.22.16-1.04.24-.87.32-.71.36-.57.4-.44.42-.33.42-.24.4-.16.36-.1.32-.05.24-.01h.16l.06.01h8.16v-.83H6.18l-.01-2.75-.02-.37.05-.34.11-.31.17-.28.25-.26.31-.23.38-.2.44-.18.51-.15.58-.12.64-.1.71-.06.77-.04.84-.02 1.27.05zm-6.3 1.98l-.23.33-.08.41.08.41.23.34.33.22.41.09.41-.09.33-.22.23-.34.08-.41-.08-.41-.23-.33-.33-.22-.41-.09-.41.09zm13.09 3.95l.28.06.32.12.35.18.36.27.36.35.35.47.32.59.28.73.21.88.14 1.04.05 1.23-.06 1.23-.16 1.04-.24.86-.32.71-.36.57-.4.45-.42.33-.42.24-.4.16-.36.09-.32.05-.24.02-.16-.01h-8.22v.82h5.84l.01 2.76.02.36-.05.34-.11.31-.17.29-.25.25-.31.24-.38.2-.44.17-.51.15-.58.13-.64.09-.71.07-.77.04-.84.01-1.27-.04-1.07-.14-.9-.2-.73-.25-.59-.3-.45-.33-.34-.34-.25-.34-.16-.33-.1-.3-.04-.25-.02-.2.01-.13v-5.34l.05-.64.13-.54.21-.46.26-.38.3-.32.33-.24.35-.2.35-.14.33-.1.3-.06.26-.04.21-.02.13-.01h5.84l.69-.05.59-.14.5-.21.41-.28.33-.32.27-.35.2-.36.15-.36.1-.35.07-.32.04-.28.02-.21V6.07h2.09l.14.01zm-6.47 14.25l-.23.33-.08.41.08.41.23.33.33.23.41.08.41-.08.33-.23.23-.33.08-.41-.08-.41-.23-.33-.33-.23-.41-.08-.41.08z' },
  postman: { hex: '#FF6C37', path: 'M13.527.099C6.955-.744.942 3.9.099 10.473c-.843 6.572 3.8 12.584 10.373 13.428 6.573.843 12.587-3.801 13.428-10.374C24.744 6.955 20.101.943 13.527.099zm2.471 7.485a.855.855 0 0 0-.593.25l-4.453 4.453-.307-.307-.643-.643c4.389-4.376 5.18-4.418 5.996-3.753zm-4.863 4.861l4.44-4.44a.62.62 0 1 1 .847.903l-4.699 4.125-.588-.588zm.33.694l-1.1.238a.06.06 0 0 1-.067-.032.06.06 0 0 1 .01-.073l.645-.645.512.512zm-2.803-.459l1.172-1.172.879.878-1.979.426a.074.074 0 0 1-.085-.039.072.072 0 0 1 .013-.093zm-3.646 6.058a.076.076 0 0 1-.069-.083.077.077 0 0 1 .022-.046h.002l.946-.946 1.222 1.222-2.123-.147zm2.425-1.256a.228.228 0 0 0-.117.256l.203.865a.125.125 0 0 1-.211.117h-.003l-.934-.934-.294-.295 3.762-3.758 1.82-.393.874.874c-1.255 1.102-2.971 2.201-5.1 3.268zm5.279-3.428h-.002l-.839-.839 4.699-4.125a.952.952 0 0 0 .119-.127c-.148 1.345-2.029 3.245-3.977 5.091zm3.657-6.46l-.003-.002a1.822 1.822 0 0 1 2.459-2.684l-1.61 1.613a.119.119 0 0 0 0 .169l1.247 1.247a1.817 1.817 0 0 1-2.093-.343zm2.578 0a1.714 1.714 0 0 1-.271.218h-.001l-1.207-1.207 1.533-1.533c.661.72.637 1.832-.054 2.522zM18.855 6.05a.143.143 0 0 0-.053.157.416.416 0 0 1-.053.45.14.14 0 0 0 .023.197.141.141 0 0 0 .084.03.14.14 0 0 0 .106-.05.691.691 0 0 0 .087-.751.138.138 0 0 0-.194-.033z' },
  bruno: { hex: '#F4AA41', path: 'M9.394 9.583s-.742.511-1.13.275c-.387-.236-.51-.742-.274-1.129.236-.388.742-.511 1.129-.275.388.236.275 1.129.275 1.129m1.48 3.245-.308.82.582.24c.04-.224.254-.372.477-.332.224.041.372.254.332.478-.093.515-.638.813-1.122.613l-.95-.391a.41.41 0 0 1-.228-.524l.448-1.193c.12-.32.426-.533.769-.533h2.525l.239.036c.076.023.148.057.214.101a.817.817 0 0 1 .303.363l.476 1.12c.083.197.002.425-.187.524l-.896.471c-.505.266-1.121-.053-1.196-.62a.411.411 0 0 1 .353-.461.411.411 0 0 1 .461.353l.56-.294-.327-.771zM9.353 17.98c-.959-.514-2.183-1.54-1.961-3.22.029-.225.236-.383.461-.354.225.03.383.236.353.461-.167 1.272.822 2.009 1.546 2.395.348.185.771.151 1.081-.092l.903-.708v-1.045c0-.226.184-.41.411-.41s.411.184.411.41v1.045l.903.708c.19.15.423.22.655.209a.45.45 0 0 1 .074-.019.42.42 0 0 1 .093-.003c.089-.02.176-.051.259-.095.724-.386 1.713-1.123 1.546-2.395a.411.411 0 0 1 .353-.461c.225-.029.432.129.461.354.223 1.688-1.013 2.716-1.974 3.227-.072.038-.145.071-.22.099.148 1.424-.14 2.562-.449 3.24-.169.37-.355.617-.49.732-.194.167-.517.305-.933.366-.531.078-1.227.039-1.945-.182-.576-.177-.973-.686-1.208-1.343-.362-1.009-.352-2.368-.33-2.919m4.54.213a1.81 1.81 0 0 1-.939-.376l-.807-.633-.807.633c-.341.268-.758.397-1.173.384-.012.453-.001 1.219.15 1.922.072.333.174.652.329.908.122.2.275.361.486.425.586.18 1.152.218 1.585.155.227-.033.412-.086.518-.176.115-.099.259-.363.395-.744.213-.597.367-1.46.263-2.498m-8.683-.955-.191.402c-.384.81-1.475.955-2.053.27-.81-.961-1.948-2.505-2.548-4.068-.427-1.115-.576-2.241-.213-3.198C2.894 3.559 6.247 2.969 6.98 2.928c1.34-.865 5.583-2.807 10.424.342.795-.051 4.042.203 6.424 7.252.325.961.162 2.081-.263 3.186-.599 1.557-1.709 3.088-2.5 4.046-.574.695-1.675.553-2.061-.262l-.133-.28c-.219.369-.524.832-.901 1.281-.57.679-1.306 1.32-2.154 1.598a.412.412 0 0 1-.519-.263.4114.4114 0 0 1 .263-.518c.705-.231 1.306-.78 1.781-1.345.556-.662.938-1.355 1.097-1.666l-.245-.518c-.126-.264-.154-.56-.078-.842.246-.924.981-4.015.071-6.043a.4105.4105 0 1 1 .749-.336c.993 2.212.243 5.583-.026 6.591a.405.405 0 0 0 .026.278l.811 1.712c.129.27.494.32.685.089.748-.904 1.801-2.346 2.367-3.817.351-.912.52-1.834.252-2.628-2.447-7.241-5.667-6.687-5.667-6.687a.416.416 0 0 1-.309-.061C12.48.972 8.456 2.917 7.339 3.676a.41.41 0 0 1-.252.07S3.739 3.649.973 10.935c-.297.783-.137 1.701.213 2.612.565 1.475 1.644 2.927 2.408 3.833.192.228.555.177.683-.092l.816-1.723a.4.4 0 0 0 .027-.278c-.269-1.008-1.02-4.38-.027-6.592a.4113.4113 0 0 1 .543-.206c.207.093.3.336.207.543-.911 2.028-.176 5.119.071 6.043.075.282.047.578-.078.842l-.203.427c.117.226 1.249 2.351 2.958 2.911.216.071.334.303.263.519a.4115.4115 0 0 1-.519.262c-1.484-.487-2.606-1.983-3.125-2.798m10.247-7.655s-.114-.893.274-1.129a.821.821 0 0 1 1.129.275c.236.387.113.893-.274 1.129-.388.236-1.129-.275-1.129-.275' },
};

/* SmartHub calls show the app's own mark, the same one in the browser tab. */
const IMAGE_MARKS = { smarthub: '/favicon.png' };

/* Just the logo for a client, or null when it has none. */
export function ToolMark({ tool, size = 24 }) {
  const mark = BRAND_MARKS[tool];
  const name = (TOOLS[tool] || TOOLS.unknown).name;
  if (IMAGE_MARKS[tool]) return <img src={IMAGE_MARKS[tool]} alt={name} className="tmark-img" style={{ width: size * 1.5, height: size }} />;
  if (tool === 'bruno') return <img src="/bruno.png" alt={name} style={{ width: size * 1.15, height: size * 1.15 }} />;
  if (mark) return <svg viewBox="0 0 24 24" width={size} height={size} fill={mark.hex} role="img" aria-label={name}><path d={mark.path} /></svg>;
  if (tool === 'browser') return <Globe size={size} strokeWidth={1.8} color={TOOLS.browser.color} aria-label={name} />;
  if (tool === 'curl') return (
    <svg viewBox="0 0 24 24" width={size} height={size} role="img" aria-label={name}>
      <rect x="1.5" y="1.5" width="21" height="21" rx="5" fill="#2b2b2b" />
      <path d="M7 8.5l3.5 3.5L7 15.5M12.5 16h4.5" fill="none" stroke="#fff" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
  return null;
}

export function ToolTile({ ua, tool, size = 30 }) {
  const key = tool || toolOf(ua);
  const t = TOOLS[key] || TOOLS.unknown;
  const mark = BRAND_MARKS[key];
  const glyph = Math.round(size * 0.58);
  if (IMAGE_MARKS[key]) {
    return (
      <span className="ttile logo" style={{ width: size, height: size }} title={t.name}>
        <img src={IMAGE_MARKS[key]} alt={t.name} className="tmark-img" style={{ width: size * 1.1, height: size * 0.72 }} />
      </span>
    );
  }
  if (mark) {
    return (
      <span className="ttile logo" style={{ width: size, height: size }} title={t.name}>
        <svg viewBox="0 0 24 24" width={glyph} height={glyph} fill={mark.hex} role="img" aria-label={t.name}><path d={mark.path} /></svg>
      </span>
    );
  }
  if (key === 'browser') {
    return (
      <span className="ttile logo" style={{ width: size, height: size, color: t.color }} title={t.name}>
        <Globe size={glyph} strokeWidth={1.8} aria-label={t.name} />
      </span>
    );
  }
  return (
    <span className="ttile" style={{ width: size, height: size, background: t.color, fontSize: size < 26 ? 9 : 10.5 }}>
      {t.tile}
    </span>
  );
}

export function Seg({ value, onChange, options, style, btnStyle, ariaLabel }) {
  return (
    <div className="seg" style={{ width: 'auto', ...style }} role="radiogroup" aria-label={ariaLabel}>
      {options.map(([v, label, title, disabled]) => (
        <button
          key={String(v)}
          type="button"
          role="radio"
          aria-checked={value === v}
          className={value === v ? 'on' : ''}
          style={btnStyle}
          data-tip={title || undefined}
          disabled={disabled}
          onClick={() => onChange(v)}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

export function Toggle({ on, onChange, label, disabled }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={!!on}
      aria-label={label}
      disabled={disabled}
      className={`toggle ${on ? 'on' : ''}`}
      onClick={() => onChange(!on)}
    />
  );
}

export const MethodTag = ({ m = 'GET', style }) => (
  <span className={`method ${m === 'GET' ? 'get' : 'post'}`} style={style}>{m}</span>
);

export function StateBox({ kind = 'empty', icon, title, text, detail, action, style }) {
  return (
    <div className={`state ${kind === 'error' ? 'err' : ''}`} style={style} role={kind === 'error' ? 'alert' : undefined}>
      <span className="si">{icon || (kind === 'error' ? <CircleAlert size={22} strokeWidth={1.8} aria-hidden="true" /> : <CircleDashed size={22} strokeWidth={1.8} aria-hidden="true" />)}</span>
      {title && <b>{title}</b>}
      {text && <span>{text}</span>}
      {detail && <code>{detail}</code>}
      {action}
    </div>
  );
}

export function ErrorBox({ error, onRetry, title = "That didn't load", style }) {
  return (
    <StateBox
      kind="error"
      title={title}
      text={error?.message || String(error || 'Something went wrong.')}
      style={style}
      action={onRetry && <button type="button" className="btn" onClick={() => onRetry()}>Try again</button>}
    />
  );
}

export function InlineError({ error, onRetry }) {
  if (!error) return null;
  return (
    <div className="inline-err" role="alert">
      <span><CircleAlert size={15} strokeWidth={1.8} aria-hidden="true" /></span>
      <span style={{ flex: 1 }}>{error.message || String(error)}</span>
      {onRetry && <button type="button" className="link" onClick={() => onRetry()}>Retry</button>}
    </div>
  );
}

export const Busybar = ({ on }) => (on ? <span className="busybar" aria-hidden="true"><i /></span> : null);

export function Orb({ size = 150, children, inset }) {
  return (
    <div className="orb" style={{ width: size, height: size }}>
      <span className="r" />
      <span className="r r2" />
      <span className="r r3" />
      <div className="c" style={inset != null ? { inset } : undefined}>{children}</div>
    </div>
  );
}

export function Steps({ steps, at }) {
  return (
    <div className="lsteps">
      {steps.map((s, i) => (
        <div key={s} className={`lst ${i < at ? 'done' : i === at ? 'now' : ''}`}>
          <i>{i < at ? <Check size={11} strokeWidth={2.6} aria-hidden="true" /> : ''}</i>
          {s}
        </div>
      ))}
    </div>
  );
}

/* -------------------------------------------------------------- numbers */

export function CountUp({ value, raw, progressKey, format }) {
  const p = useProgress(progressKey ?? value);
  const v = (Number(value) || 0) * p;
  if (format) return <>{format(v)}</>;
  return <>{raw ? Math.round(v).toLocaleString() : fmt(v)}</>;
}

/* --------------------------------------------------------------- charts */

export function Spark({ values, color: tint = '#eb1700', h = 54, w = 300, className, style }) {
  const id = useId().replace(/:/g, '');
  const vals = values && values.length > 1 ? values : [0, ...(values || [0])];
  // no traffic at all, so draw a quiet grey baseline instead of a bright stripe
  const color = vals.some((v) => Number(v) > 0) ? tint : '#d5cfc9';
  const mx = Math.max(...vals, 1);
  const pts = vals.map((v, i) => [(i / (vals.length - 1)) * w, h - 6 - (v / mx) * (h - 14)]);
  const line = smoothPath(pts);
  return (
    <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" className={className} style={style} aria-hidden="true">
      <defs>
        <linearGradient id={`g${id}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor={color} stopOpacity=".22" />
          <stop offset="1" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={`${line} L${w},${h} L0,${h} Z`} fill={`url(#g${id})`} />
      <path d={line} fill="none" stroke={color} strokeWidth="1.8" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

/* Stacked areas, one per series, over Eastern calendar days. Hover shows the
   day's breakdown. */
export function StackChart({ series, dates, height = 240, monoNames = false, emptyText = 'No calls in this window', style }) {
  const id = useId().replace(/:/g, '');
  const box = useRef(null);
  const [hover, setHover] = useState(null);
  const d = dates.length;
  const W = 1000;
  const H = height;

  const { stacks, max } = useMemo(() => {
    const base = Array(d).fill(0);
    const out = series.map((s) => {
      const lo = base.slice();
      s.vals.forEach((v, i) => { base[i] += v || 0; });
      return { s, lo, hi: base.slice() };
    });
    return { stacks: out, max: Math.max(...base, 1) * 1.15 };
  }, [series, d]);

  const X = (i) => (i / Math.max(1, d - 1)) * W;
  const Y = (v) => H - (v / max) * H;
  const total = sum(series.map((s) => sum(s.vals)));

  const onMove = (e) => {
    const r = box.current.getBoundingClientRect();
    const f = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
    setHover({ i: Math.round(f * (d - 1)), w: r.width });
  };

  const rows = hover ? series.map((s) => [s, s.vals[hover.i] || 0]).sort((a, b) => b[1] - a[1]) : [];
  const left = hover ? (hover.i / Math.max(1, d - 1)) * hover.w : 0;
  const xTicks = d > 2 ? [0, Math.floor((d - 1) / 2), d - 1] : [0, d - 1];

  return (
    <div
      className="chart"
      ref={box}
      style={{ height, margin: '4px 0 18px 32px', ...style }}
      onMouseMove={total ? onMove : undefined}
      onMouseLeave={() => setHover(null)}
    >
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" key={`${d}-${series.map((s) => s.key).join(',')}`}>
        <defs>
          <clipPath id={`rv${id}`}>
            <rect x="0" y="-20" width="0" height={H + 40}>
              <animate attributeName="width" from="0" to={W} dur="1.3s" fill="freeze" calcMode="spline" keySplines=".2 .8 .2 1" keyTimes="0;1" />
            </rect>
          </clipPath>
        </defs>
        {[0, 0.25, 0.5, 0.75, 1].map((f) => (
          <line key={f} x1="0" x2={W} y1={H * f} y2={H * f} stroke="#e8e6e3" strokeDasharray="3 5" vectorEffect="non-scaling-stroke" />
        ))}
        <g clipPath={`url(#rv${id})`}>
          {!!total && stacks.slice().reverse().map(({ s, lo, hi }) => {
            const top = hi.map((v, i) => [X(i), Y(v)]);
            const bot = lo.map((v, i) => [X(i), Y(v)]).reverse();
            const line = smoothPath(top);
            return (
              <g key={s.key}>
                <path d={`${line} L${bot.map((p) => p.join(',')).join(' L')} Z`} fill={s.color} fillOpacity=".14" />
                <path d={line} fill="none" stroke={s.color} strokeWidth="2" vectorEffect="non-scaling-stroke" />
              </g>
            );
          })}
        </g>
      </svg>
      {!!total && [1, 0.5, 0].map((f) => (
        <span key={f} className="yl" style={{ top: `${(1 - f) * 100}%`, left: -32 }}>{fmt(max * f)}</span>
      ))}
      {xTicks.map((i) => (
        <span key={i} className="xl" style={{ left: `${(i / Math.max(1, d - 1)) * 100}%` }}>
          {i === d - 1 ? 'today' : dates[i] ? dayLabel(dates[i]) : ''}
        </span>
      ))}
      {!total && (
        <span style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 12.5, color: 'var(--muted)' }}>
          {emptyText}
        </span>
      )}
      <div className="xhair" style={{ left: `${hover ? (hover.i / Math.max(1, d - 1)) * 100 : 0}%`, opacity: hover ? 0.25 : 0 }} />
      <div className="ctip" style={{ opacity: hover ? 1 : 0, left: hover ? (left > hover.w - 240 ? left - 230 : left + 14) : 0 }}>
        {hover && (
          <>
            <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 6 }}>
              {hover.i === d - 1 ? 'Today' : dates[hover.i] ? dayLabel(dates[hover.i], true) : ''}
            </div>
            {rows.map(([s, v]) => (
              <div className="r" key={s.key}>
                <i style={{ width: 8, height: 8, borderRadius: 3, background: s.color }} />
                <span style={monoNames ? { fontFamily: 'var(--mono)', fontSize: 11.5 } : undefined}>{s.name}</span>
                <b className="num">{v.toLocaleString()}</b>
              </div>
            ))}
            <div className="r" style={{ borderTop: '1px solid var(--line)', marginTop: 6, paddingTop: 8 }}>
              <span />
              <span>Total</span>
              <b className="num">{sum(rows.map((r) => r[1])).toLocaleString()}</b>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

/* --------------------------------------------------------------- select */

const Chevron = () => (
  <svg className="ddc" viewBox="0 0 16 16" aria-hidden="true">
    <path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);

/* A styled replacement for <select>. Options are {value, label, sub, disabled}.
   Opens on click or arrow keys, filters when the list is long, and keeps the
   menu on screen by flipping above the button when there is no room below. */
export function Select({
  value, onChange, options, placeholder = 'Choose…', disabled, mono, small, style, ariaLabel, searchable,
}) {
  const btn = useRef(null);
  const menu = useRef(null);
  const filterRef = useRef(null);
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState('');
  const [idx, setIdx] = useState(0);
  const [pos, setPos] = useState(null);

  const current = options.find((o) => o.value === value);
  const list = useMemo(
    () => options.filter((o) => !q || `${o.label} ${o.sub || ''}`.toLowerCase().includes(q.toLowerCase())),
    [options, q],
  );
  const showFilter = searchable ?? options.length > 7;

  const close = useCallback((focus = false) => {
    setOpen(false);
    setQ('');
    if (focus) btn.current?.focus();
  }, []);

  const openMenu = () => {
    if (disabled) return;
    const at = Math.max(0, options.findIndex((o) => o.value === value));
    setIdx(at);
    setOpen(true);
  };

  useLayoutEffect(() => {
    if (!open || !btn.current || !menu.current) return;
    const r = btn.current.getBoundingClientRect();
    const h = menu.current.offsetHeight;
    const w = Math.max(r.width, 180);
    const below = window.innerHeight - r.bottom;
    const up = below < h + 12 && r.top > below;
    setPos({
      up,
      minWidth: w,
      left: Math.max(8, Math.min(r.left, window.innerWidth - menu.current.offsetWidth - 8)),
      top: up ? r.top - h - 6 : r.bottom + 6,
    });
  }, [open, list.length]);

  useEffect(() => {
    if (!open) return undefined;
    if (showFilter) filterRef.current?.focus();
    const onDown = (e) => {
      if (menu.current?.contains(e.target) || btn.current?.contains(e.target)) return;
      close();
    };
    const onScroll = (e) => { if (!menu.current?.contains(e.target)) close(); };
    const onResize = () => close();
    document.addEventListener('mousedown', onDown, true);
    document.addEventListener('scroll', onScroll, true);
    window.addEventListener('resize', onResize);
    return () => {
      document.removeEventListener('mousedown', onDown, true);
      document.removeEventListener('scroll', onScroll, true);
      window.removeEventListener('resize', onResize);
    };
  }, [open, showFilter, close]);

  useEffect(() => {
    if (!open) return;
    menu.current?.querySelector('.ddo.act')?.scrollIntoView({ block: 'nearest' });
  }, [idx, open]);

  const pick = (o) => {
    if (!o || o.disabled) return;
    close(true);
    if (o.value !== value) onChange(o.value);
  };

  const onKey = (e) => {
    if (!open) {
      if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(e.key)) {
        e.preventDefault();
        openMenu();
      }
      return;
    }
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      close(true);
    } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (!list.length) return;
      setIdx((i) => (i + (e.key === 'ArrowDown' ? 1 : -1) + list.length) % list.length);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      pick(list[idx]);
    } else if (e.key === 'Tab') {
      close();
    }
  };

  return (
    <>
      <button
        ref={btn}
        type="button"
        className={`dd ${small ? 'sm' : ''} ${open ? 'open' : ''} ${current ? '' : 'ph'} ${disabled ? 'dis' : ''}`}
        style={style}
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={ariaLabel}
        onClick={() => (open ? close() : openMenu())}
        onKeyDown={onKey}
      >
        <span className="ddv">
          <span className={mono && current?.value ? 'mn' : ''}>{current ? current.label : placeholder}</span>
        </span>
        <Chevron />
      </button>
      {open && (
        <Portal>
          <div
            ref={menu}
            className={`ddm ${pos?.up ? 'up' : ''}`}
            role="listbox"
            style={{ left: pos?.left ?? -9999, top: pos?.top ?? -9999, minWidth: pos?.minWidth ?? 180 }}
            onMouseDown={(e) => { if (e.target.tagName !== 'INPUT') e.preventDefault(); }}
            onKeyDown={onKey}
          >
            {showFilter && (
              <input
                ref={filterRef}
                className="dds"
                placeholder="Filter…"
                value={q}
                onChange={(e) => { setQ(e.target.value); setIdx(0); }}
              />
            )}
            <div className="ddl">
              {list.map((o, k) => (
                <div
                  key={String(o.value)}
                  role="option"
                  aria-selected={o.value === value}
                  aria-disabled={o.disabled || undefined}
                  className={`ddo ${o.value === value ? 'sel' : ''} ${k === idx ? 'act' : ''} ${o.value === '' ? 'ph' : ''} ${o.disabled ? 'dis' : ''}`}
                  onMouseEnter={() => setIdx(k)}
                  onClick={() => pick(o)}
                >
                  <span className={mono && o.value ? 'mn' : ''}>{o.label}</span>
                  {o.sub && <span className="sub">{o.sub}</span>}
                  <span className="ck"><Check size={13} strokeWidth={1.8} aria-hidden="true" /></span>
                </div>
              ))}
              {!list.length && <div className="dde">No match</div>}
            </div>
          </div>
        </Portal>
      )}
    </>
  );
}

/* --------------------------------------------------------------- drawer */

export function Drawer({ onClose, wide, className = '', children, label, busy }) {
  const panel = useRef(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === 'Escape' && !e.defaultPrevented && !document.querySelector('.dax .mbg')) closeRef.current?.();
    };
    document.addEventListener('keydown', onKey);
    const prev = document.activeElement;
    const t = setTimeout(() => {
      const first = panel.current?.querySelector('input:not([disabled]),textarea:not([disabled]),[data-autofocus]');
      (first || panel.current)?.focus?.();
    }, 60);
    return () => {
      clearTimeout(t);
      document.removeEventListener('keydown', onKey);
      prev?.focus?.();
    };
  }, []);
  return (
    <Portal>
      <div className="drawer-bg" onClick={() => onClose?.()} />
      <aside
        ref={panel}
        className={`drawer ${className}`}
        style={wide ? { width: 'min(1240px,97vw)' } : undefined}
        role="dialog"
        aria-modal="true"
        aria-label={label}
        tabIndex={-1}
      >
        <Busybar on={busy} />
        {children}
      </aside>
    </Portal>
  );
}

export function DrawerHead({ kicker, title, onClose }) {
  return (
    <div className="dhd">
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, minWidth: 0 }}>
        <span className="kicker" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{kicker}</span>
        <span className="h-l" style={{ fontSize: 30 }}>{title}</span>
      </div>
      <button type="button" className="iconbtn" onClick={onClose} aria-label="Close"><X size={16} strokeWidth={1.8} aria-hidden="true" /></button>
    </div>
  );
}

/* ---------------------------------------------------------- hold to reveal */

export function HoldToReveal({ onReveal, ms = 900, disabled }) {
  const [holding, setHolding] = useState(false);
  const [done, setDone] = useState(false);
  const timer = useRef(null);

  const start = () => {
    if (disabled || timer.current) return;
    setHolding(true);
    timer.current = setTimeout(() => {
      timer.current = null;
      setDone(true);
      setHolding(false);
      onReveal();
    }, ms);
  };
  const stop = () => {
    clearTimeout(timer.current);
    timer.current = null;
    setHolding(false);
  };
  useEffect(() => () => clearTimeout(timer.current), []);

  return (
    <button
      type="button"
      className={`kbtn hold ${holding ? 'holding' : ''} ${done ? 'done' : ''}`}
      data-tip="Press and hold to show the full key"
      disabled={disabled}
      onPointerDown={(e) => { if (e.button === 0) { e.preventDefault(); start(); } }}
      onPointerUp={stop}
      onPointerLeave={stop}
      onPointerCancel={stop}
      onKeyDown={(e) => { if ((e.key === ' ' || e.key === 'Enter') && !e.repeat) { e.preventDefault(); start(); } }}
      onKeyUp={(e) => { if (e.key === ' ' || e.key === 'Enter') stop(); }}
      onContextMenu={(e) => e.preventDefault()}
    >
      <svg className="hring" viewBox="0 0 20 20" aria-hidden="true">
        <circle cx="10" cy="10" r="8" className="hb" />
        <circle cx="10" cy="10" r="8" className="hf" />
      </svg>
      <span className="hl">{holding ? 'Keep holding…' : 'Hold to reveal'}</span>
    </button>
  );
}

/* ------------------------------------------------------- toasts + confirm */

export function ToastProvider({ children }) {
  const [t, setT] = useState(null);
  const timer = useRef(null);
  const toast = useCallback((msg, kind = 'ok') => {
    clearTimeout(timer.current);
    setT({ msg, err: kind === 'error', id: Date.now() });
    timer.current = setTimeout(() => setT(null), kind === 'error' ? 6500 : 3200);
  }, []);
  useEffect(() => () => clearTimeout(timer.current), []);
  const value = useMemo(() => ({ toast }), [toast]);
  return (
    <ToastContext.Provider value={value}>
      {children}
      {t && (
        <Portal>
          <div key={t.id} className={`toastx ${t.err ? 'err' : ''}`} role={t.err ? 'alert' : 'status'}>
            <span style={{
              width: 20, height: 20, borderRadius: '50%', flexShrink: 0, display: 'flex', alignItems: 'center', justifyContent: 'center',
              fontSize: 11, background: t.err ? 'rgba(255,255,255,.25)' : '#53ce76', color: '#fff',
            }}
            >
              {t.err ? <CircleAlert size={15} strokeWidth={1.8} aria-hidden="true" /> : <CircleCheck size={15} strokeWidth={1.8} aria-hidden="true" />}
            </span>
            <span style={{ maxWidth: 520 }}>{t.msg}</span>
            <button type="button" className="link" style={{ color: 'inherit', opacity: 0.7, marginLeft: 4 }} onClick={() => setT(null)} aria-label="Dismiss"><X size={13} strokeWidth={1.8} aria-hidden="true" /></button>
          </div>
        </Portal>
      )}
    </ToastContext.Provider>
  );
}

/* confirm({ title, text, okLabel, danger, icon, action }) resolves true once
   confirmed. With an `action`, the dialog stays up while it runs and shows its
   error in place, so a failed delete doesn't vanish behind a toast. */
export function ConfirmProvider({ children }) {
  const [m, setM] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const resolver = useRef(null);
  const okRef = useRef(null);

  const confirm = useCallback((opts) => new Promise((resolve) => {
    resolver.current = resolve;
    setErr('');
    setBusy(false);
    setM(opts);
  }), []);

  const finish = useCallback((result) => {
    resolver.current?.(result);
    resolver.current = null;
    setM(null);
    setBusy(false);
  }, []);

  const ok = async () => {
    if (!m?.action) return finish(true);
    setBusy(true);
    setErr('');
    try {
      await m.action();
      finish(true);
    } catch (e) {
      setBusy(false);
      setErr(e?.message || 'That did not work.');
    }
    return undefined;
  };

  useEffect(() => {
    if (!m) return undefined;
    okRef.current?.focus();
    const onKey = (e) => {
      if (e.key === 'Escape' && !busy) {
        e.preventDefault();
        finish(false);
      }
    };
    document.addEventListener('keydown', onKey, true);
    return () => document.removeEventListener('keydown', onKey, true);
  }, [m, busy, finish]);

  const value = useMemo(() => ({ confirm }), [confirm]);
  return (
    <ConfirmContext.Provider value={value}>
      {children}
      {m && (
        <Portal>
          <div className="mbg" onMouseDown={(e) => { if (e.target === e.currentTarget && !busy) finish(false); }}>
            <div className="modal2" role="alertdialog" aria-modal="true" aria-label={typeof m.title === 'string' ? m.title : undefined}>
              <span className="mi" style={{ background: m.danger ? '#eb170012' : 'var(--bg)', color: m.danger ? 'var(--red)' : 'var(--ink)' }}>
                {m.icon || (m.danger ? <TriangleAlert size={20} strokeWidth={1.8} aria-hidden="true" /> : <CircleHelp size={20} strokeWidth={1.8} aria-hidden="true" />)}
              </span>
              <span style={{ fontFamily: 'JohnsonDisplay', fontSize: 24, lineHeight: 1.15 }}>{m.title}</span>
              <span style={{ fontSize: 14, color: 'var(--ink2)', lineHeight: 1.6 }}>{m.text}</span>
              {err && <div className="inline-err" role="alert"><span><CircleAlert size={15} strokeWidth={1.8} aria-hidden="true" /></span><span>{err}</span></div>}
              <div className="row" style={{ justifyContent: 'flex-end', marginTop: 8 }}>
                <button type="button" className="btn" style={{ padding: '10px 16px' }} onClick={() => finish(false)} disabled={busy}>Cancel</button>
                <button
                  ref={okRef}
                  type="button"
                  className={`btn ${m.danger ? 'red' : 'dark'}`}
                  style={{ padding: '10px 16px' }}
                  onClick={ok}
                  disabled={busy}
                >
                  {busy && <Spinner />}
                  {busy ? (m.busyLabel || 'Working…') : m.okLabel}
                </button>
              </div>
            </div>
          </div>
        </Portal>
      )}
    </ConfirmContext.Provider>
  );
}

/* --------------------------------------------------------------- tooltips */

/* One tooltip for the whole page: anything with data-tip (or a title, which is
   moved to data-tip so the browser's own doesn't double up), and anything whose
   text is cut off with an ellipsis, shows its full text on hover. */
export function TooltipLayer() {
  const tipRef = useRef(null);
  useEffect(() => {
    const tip = tipRef.current;
    if (!tip) return undefined;
    let cur = null;
    let timer = null;

    const truncated = (el) => {
      const cs = getComputedStyle(el);
      if (cs.textOverflow === 'ellipsis' && el.scrollWidth > el.clientWidth + 1) return true;
      if (cs.webkitLineClamp && cs.webkitLineClamp !== 'none' && el.scrollHeight > el.clientHeight + 1) return true;
      return false;
    };
    const find = (start) => {
      if (!start?.closest || !start.closest('.dax') || start.closest('.tipx,.ddm')) return null;
      for (let n = start, depth = 0; n && n.nodeType === 1 && depth < 4; n = n.parentElement, depth += 1) {
        if (n.classList?.contains('dax')) break;
        const title = n.getAttribute('title');
        if (title) {
          n.dataset.tip = title;
          n.removeAttribute('title');
        }
        if (n.dataset?.tip) return { el: n, text: n.dataset.tip };
        if (truncated(n)) return { el: n, text: n.textContent.trim() };
      }
      return null;
    };
    const hide = () => {
      clearTimeout(timer);
      cur = null;
      tip.classList.remove('on');
    };
    const show = (hit) => {
      cur = hit.el;
      const mono = /mono|Menlo|Consolas/.test(getComputedStyle(hit.el).fontFamily);
      tip.className = `tipx${mono ? ' mono' : ''}`;
      tip.textContent = hit.text;
      const r = hit.el.getBoundingClientRect();
      tip.style.left = '0px';
      tip.style.top = '0px';
      const w = tip.offsetWidth;
      const h = tip.offsetHeight;
      const left = Math.max(8, Math.min(r.left + r.width / 2 - w / 2, window.innerWidth - w - 8));
      let top = r.top - h - 8;
      if (top < 8) top = r.bottom + 8;
      tip.style.left = `${left}px`;
      tip.style.top = `${top}px`;
      requestAnimationFrame(() => tip.classList.add('on'));
    };
    const over = (e) => {
      const hit = find(e.target);
      if (!hit) {
        if (cur && !cur.contains(e.target)) hide();
        return;
      }
      if (hit.el === cur) return;
      hide();
      timer = setTimeout(() => { if (document.body.contains(hit.el)) show(hit); }, 280);
    };
    const out = (e) => { if (cur && !cur.contains(e.relatedTarget)) hide(); };
    document.addEventListener('mouseover', over);
    document.addEventListener('mouseout', out);
    document.addEventListener('scroll', hide, true);
    document.addEventListener('mousedown', hide, true);
    return () => {
      hide();
      document.removeEventListener('mouseover', over);
      document.removeEventListener('mouseout', out);
      document.removeEventListener('scroll', hide, true);
      document.removeEventListener('mousedown', hide, true);
    };
  }, []);
  return (
    <Portal>
      <div ref={tipRef} className="tipx" role="tooltip" />
    </Portal>
  );
}

/* ------------------------------------------------------- rows per page */

/* Slider for the usual steps, plus a box to type any value from 1 to the
   server's cap. The typed value is applied on Enter or when the box loses focus. */
export function RowLimitField({ id, value, onChange, min = 1, max = ROW_LIMITS[ROW_LIMITS.length - 1] }) {
  const [draft, setDraft] = useState(String(value));
  useEffect(() => { setDraft(String(value)); }, [value]);

  const commit = () => {
    const n = Math.round(Number(draft));
    if (!draft.trim() || !Number.isFinite(n)) { setDraft(String(value)); return; }
    const clamped = Math.min(max, Math.max(min, n));
    setDraft(String(clamped));
    if (clamped !== Number(value)) onChange(clamped);
  };

  const step = ROW_LIMITS.findIndex((x) => x >= Number(value));
  const pos = step === -1 ? ROW_LIMITS.length - 1 : step;

  return (
    <div className="slider">
      <input
        type="range" min="0" max={ROW_LIMITS.length - 1} step="1"
        value={pos}
        onChange={(e) => onChange(ROW_LIMITS[Number(e.target.value)])}
        aria-label="Rows per page slider"
        aria-valuetext={`${value} rows`}
      />
      <input
        id={id}
        className="field rl-num"
        inputMode="numeric"
        value={draft}
        onChange={(e) => setDraft(e.target.value.replace(/\D/g, ''))}
        onBlur={commit}
        onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); commit(); } }}
        aria-describedby={id ? `${id}-range` : undefined}
      />
      {id && <span id={`${id}-range`} className="rl-range">{min.toLocaleString()} to {max.toLocaleString()}</span>}
    </div>
  );
}

/* ------------------------------------------------------- code colouring */

/* Light syntax colouring for code snippets and the guide. Each token is
   escaped on its own, so nothing from the code ends up as markup. */
const escCode = (s) => s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const STR = String.raw`"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'`;
const NUM = ['n', String.raw`-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b`];
const CODE_TOKENS = {
  python: [
    ['c', String.raw`#[^\n]*`],
    ['s', STR],
    ['k', String.raw`\b(?:import|from|as|def|return|None|True|False|for|in|if|elif|else|while|with|try|except|raise|not|and|or|is|lambda|class|break|continue|pass)\b`],
    ['f', String.raw`\b[A-Za-z_][\w.]*(?=\()`],
    ['a', String.raw`\b[A-Za-z_]\w*(?==[^=])`],
    NUM,
  ],
  pq: [
    ['c', String.raw`//[^\n]*`],
    ['s', STR],
    ['k', String.raw`\b(?:let|in|each|if|then|else)\b`],
    ['f', String.raw`\b[A-Z]\w*\.[A-Za-z]\w*(?=\()`],
    ['a', String.raw`\b[A-Za-z_]\w*(?=\s*=)`],
    NUM,
  ],
  curl: [
    ['c', String.raw`(?:^|(?<=\s))#[^\n]*`],
    ['s', STR],
    ['k', String.raw`^\s*(?:curl|export|set|echo|pip|python3?)\b`],
    ['a', String.raw`(?<=\s)--?[A-Za-z][\w-]*`],
    ['v', String.raw`\$\{?[A-Za-z_]\w*\}?`],
    ['p', String.raw`\\$`],
  ],
  http: [
    ['key', KEY_PLACEHOLDER],
    ['k', String.raw`^(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b`],
    ['a', String.raw`^[A-Za-z][\w-]*(?=:\s)`],
    ['s', String.raw`(?<=[?&])[^=&\s]+`],
    ['j', String.raw`[?&=]`],
    ['f', String.raw`(?<=^(?:GET|POST|PUT|PATCH|DELETE)\s+)\S+?(?=[?\s]|$)`],
  ],
  json: [
    ['f', String.raw`"(?:\\.|[^"\\\n])*"(?=\s*:)`],
    ['s', STR],
    ['b', String.raw`\b(?:true|false|null)\b`],
    NUM,
  ],
};
CODE_TOKENS.bash = CODE_TOKENS.curl;
CODE_TOKENS.sh = CODE_TOKENS.curl;
CODE_TOKENS.shell = CODE_TOKENS.curl;
CODE_TOKENS.py = CODE_TOKENS.python;

/* A fence with no language still gets coloured by what it looks like. */
function guessLang(code) {
  const s = code.trimStart();
  if (/^(GET|POST|PUT|PATCH|DELETE)\s/.test(s)) return 'http';
  if (/^curl\b/.test(s)) return 'curl';
  if (/^[[{]/.test(s)) return 'json';
  if (/^(import |from \w+ import |def )/m.test(s)) return 'python';
  if (/^let\b/.test(s)) return 'pq';
  return '';
}

export function highlightCode(code, lang) {
  const key = (lang || '').toLowerCase() || guessLang(code);
  const rules = CODE_TOKENS[key];
  if (!rules) return escCode(code);
  const re = new RegExp(rules.map(([, r]) => `(${r})`).join('|'), 'gm');
  let html = '';
  let last = 0;
  code.replace(re, (m, ...groups) => {
    if (!m) return m;
    const at = groups[rules.length];
    const kind = rules[groups.slice(0, rules.length).findIndex((g) => g !== undefined)][0];
    let piece = escCode(m);
    if (kind === 's' || kind === 'a') piece = piece.split(KEY_PLACEHOLDER).join(`<span class="tk-key">${KEY_PLACEHOLDER}</span>`);
    html += escCode(code.slice(last, at)) + `<span class="tk-${kind}">${piece}</span>`;
    last = at + m.length;
    return m;
  });
  html += escCode(code.slice(last));
  // A placeholder key outside a string (say in an http line) is flagged too.
  return html.replace(new RegExp(`(?<![">])${KEY_PLACEHOLDER}`, 'g'), `<span class="tk-key">${KEY_PLACEHOLDER}</span>`);
}

/* --------------------------------------------------------------- json view */

const escHtml = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);

export function JsonView({ text, style }) {
  const html = useMemo(() => escHtml(text).replace(
    /(&quot;(?:[^&]|&(?!quot;))*?&quot;)(\s*:)?|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g,
    (m, str, colon, bool, num) => {
      if (str) return colon ? `<span class="k">${str}</span>${colon}` : `<span class="s">${str}</span>`;
      if (bool) return `<span class="b">${bool}</span>`;
      if (num) return `<span class="n">${num}</span>`;
      return m;
    },
  ), [text]);
  // The text is escaped above before any markup is added.
  return <pre className="json" style={style} dangerouslySetInnerHTML={{ __html: html }} />;
}