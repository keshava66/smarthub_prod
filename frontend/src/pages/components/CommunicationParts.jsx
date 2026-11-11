import React, { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { useMsal } from '@azure/msal-react';
import { useRBAC } from '../../context/RBACContext';
import { useAdGroupMembers } from '../../hooks/useAdGroupMembers';
import { useDirectoryPhotos } from '../../hooks/useDirectoryPhotos';

import closeIcon    from "/icons/JJ_Icon_Close.svg";
import searchIcon   from "/icons/JJ_Icon_Search_RGB.svg";
import communityIcon from "/icons/JJ_Icon_Community_RGB.svg";
import personIcon   from "/icons/JJ_Icon_Person_RGB.svg";
import linkIcon     from "/icons/JJ_Icon_Link_RGB.svg";

export const CLS = {
  card:      'bg-white border border-jj-gray-02 rounded-2xl',
  label:     'block font-johnson-text text-[12.5px] text-jj-gray-06 mb-1.5',
  hint:      'font-johnson-text text-[11.5px] text-jj-gray-05',
  input:     'w-full px-3 py-2.5 rounded-[10px] border border-jj-gray-03 bg-white font-johnson-text ' +
             'text-[13px] text-jj-gray-08 outline-none focus:border-jj-red focus:ring-[3px] focus:ring-jj-red/10',
  inputSm:   'w-full px-2.5 py-1.5 rounded-lg border border-jj-gray-02 bg-white font-johnson-text ' +
             'text-[12.5px] text-jj-gray-08 outline-none focus:border-jj-red',
  sectionH:  'font-johnson-display font-medium text-[15px] leading-tight text-jj-gray-08',
  railH:     'font-johnson-text text-[10.5px] uppercase tracking-[0.09em] text-jj-gray-05 font-medium',
  ghostBtn:  'px-3.5 py-2.5 rounded-[10px] border border-jj-gray-03 bg-white font-johnson-text ' +
             'text-[13px] text-jj-gray-08 hover:bg-jj-gray-01 disabled:opacity-50',
  primaryBtn:'px-4 py-2.5 rounded-[10px] bg-jj-red text-white font-johnson-text text-[13px] ' +
             'font-medium hover:bg-jj-maroon-05 transition-colors disabled:opacity-60',
  chipBtn:   'px-1.5 py-1 rounded-md border border-jj-gray-02 font-johnson-text text-[11px] ' +
             'text-jj-gray-07 hover:border-jj-gray-04 disabled:opacity-40',
};

/* Example text for each message type — the compose fields' placeholders and
   the welcome screen's sample banners, so the two never disagree. */
export const PLACEHOLDERS = {
  update:      { bannerTitle: 'Release Notes',          bannerSubtitle: 'GRA Data Quality Engine',        headline: "What's new this month",        subject: 'New in SmartHub: ' },
  warning:     { bannerTitle: 'Action required',        bannerSubtitle: 'SmartHub Alert!',                headline: 'Please review before Friday',  subject: 'Your SmartHub access needs re-approval' },
  maintenance: { bannerTitle: 'Scheduled maintenance',  bannerSubtitle: 'Day 01 Month, Time - Time PM ET', headline: 'SmartHub will be unavailable', subject: 'Planned downtime this Saturday' },
  custom:      { bannerTitle: 'SmartHub information session', bannerSubtitle: 'Updates to SmartHub',      headline: 'Thanks for joining us',        subject: "Recap from today's session" },
};

/* Several of the J&J SVGs ship with a baked-in white fill, which is invisible
   on a white button. Colour comes from a filter rather than currentColor,
   since the fill is in the file and not inheritable. */
export const ICON_TONES = {
  white: 'brightness(0) invert(1)',
  dark:  'brightness(0) saturate(100%) invert(15%) sepia(7%) saturate(806%) hue-rotate(346deg) brightness(95%) contrast(89%)',
  gray:  'brightness(0) saturate(100%) invert(50%) sepia(6%) saturate(656%) hue-rotate(346deg) brightness(92%) contrast(88%)',
  red:   'brightness(0) saturate(100%) invert(13%) sepia(97%) saturate(6477%) hue-rotate(359deg) brightness(91%) contrast(99%)',
  green: 'brightness(0) saturate(100%) invert(37%) sepia(87%) saturate(1158%) hue-rotate(71deg) brightness(91%) contrast(93%)',
};

export const Icon = ({ src, alt = '', className = 'w-4 h-4', style, tone }) => (
  <img
    src={src}
    alt={alt}
    className={className}
    style={tone ? { filter: ICON_TONES[tone], ...style } : style}
  />
);

/* ─── Modal shell ─────────────────────────────────────────────────────────── */

/** Escape closes, a click on the backdrop closes, and both are ignored while
 *  `busy` — a send in flight should not be abandoned by a stray keypress. */
export const Modal = ({ title, subtitle, children, footer, onClose, width = 430, busy = false }) => {
  const close = useRef(onClose);
  close.current = onClose;

  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape' && !busy) close.current?.(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [busy]);

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-jj-gray-08/35 p-5"
      onMouseDown={(e) => { if (e.target === e.currentTarget && !busy) close.current?.(); }}
    >
      {/* The height cap is an inline style, not a class: it is the one rule
          that decides whether this thing stays on the screen, and it should not
          depend on a utility being generated. */}
      <div
        className="w-full bg-white rounded-2xl shadow-[0_24px_60px_rgba(49,44,42,0.22)]
                   flex flex-col"
        style={{ maxWidth: width, maxHeight: '86vh' }}
      >
        <div className="flex-none px-6 pt-6 pb-3.5">
          <h3 className="font-johnson-display font-medium text-[19px] leading-tight text-jj-gray-08">
            {title}
          </h3>
          {subtitle && (
            <p className="font-johnson-text text-[12.5px] text-jj-gray-06 mt-1 leading-relaxed">
              {subtitle}
            </p>
          )}
        </div>
        <div className="flex-1 min-h-0 overflow-auto px-6">{children}</div>
        <div className="flex-none px-6 pt-4 pb-6 flex flex-wrap justify-end gap-2.5">
          {footer}
        </div>
      </div>
    </div>
  );
};

/** Replaces window.prompt — same job, but it looks like the rest of the portal
 *  and cannot be suppressed by a browser that has decided this tab asks too
 *  often. */
export const PromptModal = ({
  title, subtitle, label, defaultValue = '', placeholder,
  confirmLabel = 'Save', busy = false, onCancel, onConfirm,
}) => {
  const [value, setValue] = useState(defaultValue);
  const field = useRef(null);

  useEffect(() => {
    field.current?.focus();
    field.current?.select();
  }, []);

  // Guarded against a second press while the first is still in the air: the
  // save behind this is a POST, and sending it twice gets a 409 back for a
  // template that saved perfectly well the first time.
  const submit = () => {
    if (busy || !value.trim()) return;
    onConfirm(value.trim());
  };

  return (
    <Modal
      title={title}
      subtitle={subtitle}
      onClose={onCancel}
      busy={busy}
      footer={
        <>
          <button onClick={onCancel} disabled={busy} className={CLS.ghostBtn}>Cancel</button>
          <button
            onClick={submit}
            disabled={busy || !value.trim()}
            className={CLS.primaryBtn}
          >
            {busy ? 'Saving…' : confirmLabel}
          </button>
        </>
      }
    >
      {label && <label className={CLS.label}>{label}</label>}
      <input
        ref={field}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); submit(); } }}
        placeholder={placeholder}
        disabled={busy}
        className={CLS.input}
      />
    </Modal>
  );
};

/** Replaces window.confirm. `tone="danger"` for anything that destroys
 *  something — deleting a template, cancelling a queued send. */
export const ConfirmModal = ({
  title, subtitle, children, confirmLabel = 'Confirm', tone = 'default',
  busy = false, onCancel, onConfirm,
}) => (
  <Modal
    title={title}
    subtitle={subtitle}
    onClose={onCancel}
    busy={busy}
    footer={
      <>
        <button onClick={onCancel} disabled={busy} className={CLS.ghostBtn}>Cancel</button>
        <button
          onClick={onConfirm}
          disabled={busy}
          className={
            tone === 'danger'
              ? 'px-4 py-2.5 rounded-[10px] bg-jj-red text-white font-johnson-text text-[13px] ' +
                'font-medium hover:bg-jj-maroon-05 transition-colors disabled:opacity-60'
              : CLS.primaryBtn
          }
        >
          {busy ? 'Working…' : confirmLabel}
        </button>
      </>
    }
  >
    {children}
  </Modal>
);

const LIST_MARK_RE = /^(\s*)(?:[-*]\s+|\d+[.)]\s+)?/;

export const BodyEditor = ({ value, onChange, placeholder, rows = 9, hint, invalid = false }) => {
  const area = useRef(null);

  /* React owns the value, so the caret has to be put back by hand after the
   * re-render or it jumps to the end of the message on every button press. */
  const restore = (start, end) => {
    requestAnimationFrame(() => {
      const el = area.current;
      if (!el) return;
      el.focus();
      el.setSelectionRange(start, end);
    });
  };

  const surround = (marker) => {
    const el = area.current;
    if (!el) return;
    const { selectionStart: from, selectionEnd: to } = el;
    const picked = value.slice(from, to);
    const inner = picked || 'text';
    onChange(value.slice(0, from) + marker + inner + marker + value.slice(to));
    restore(from + marker.length, from + marker.length + inner.length);
  };

  /** Stamp every line the selection touches, whole lines at a time — dragging
   *  across three lines and pressing "list" should make three bullets, not one
   *  bullet in the middle of the second. */
  const stampLines = (make) => {
    const el = area.current;
    if (!el) return;
    const from = value.lastIndexOf('\n', el.selectionStart - 1) + 1;
    const nextBreak = value.indexOf('\n', el.selectionEnd);
    const to = nextBreak === -1 ? value.length : nextBreak;

    let index = 0;
    const stamped = value.slice(from, to).split('\n')
      .map((line) => {
        if (!line.trim()) return line;
        index += 1;
        return line.replace(LIST_MARK_RE, (_, indent) => indent + make(index));
      })
      .join('\n');

    onChange(value.slice(0, from) + stamped + value.slice(to));
    restore(from, from + stamped.length);
  };

  const heading = () => {
    const el = area.current;
    if (!el) return;
    const from = value.lastIndexOf('\n', el.selectionStart - 1) + 1;
    const nextBreak = value.indexOf('\n', el.selectionStart);
    const to = nextBreak === -1 ? value.length : nextBreak;
    const line = value.slice(from, to);
    // Second press takes it off again.
    const next = /^#{1,3}\s+/.test(line) ? line.replace(/^#{1,3}\s+/, '') : `## ${line}`;
    onChange(value.slice(0, from) + next + value.slice(to));
    restore(from + next.length, from + next.length);
  };

  const link = () => {
    const el = area.current;
    if (!el) return;
    const { selectionStart: from, selectionEnd: to } = el;
    const text = value.slice(from, to) || 'link text';
    const inserted = `[${text}](https://)`;
    onChange(value.slice(0, from) + inserted + value.slice(to));
    // Land the caret right after "https://" so the address is the next thing
    // typed, which is the only part the admin still has to supply.
    const caret = from + inserted.length - 1;
    restore(caret, caret);
  };

  // Grouped as they read in the toolbar: inline marks | blocks | link.
  const groups = [
    [
      { key: 'bold',   label: 'B', title: 'Bold  **text**',   run: () => surround('**'), className: 'font-bold' },
      { key: 'italic', label: 'I', title: 'Italic  *text*',   run: () => surround('*'),  className: 'italic' },
    ],
    [
      { key: 'head',   label: 'H',       title: 'Heading  ## text',       run: heading },
      { key: 'bullet', label: '• List',  title: 'Bulleted list  - item',  run: () => stampLines(() => '- ') },
      { key: 'number', label: '1. List', title: 'Numbered list  1. item', run: () => stampLines((n) => `${n}. `) },
    ],
    [
      { key: 'link', icon: linkIcon, title: 'Link  [text](url)', run: link },
    ],
  ];

  return (
    <div
      className={`rounded-xl border bg-white overflow-hidden transition-shadow ${invalid
        ? 'border-jj-red ring-[3px] ring-jj-red/10'
        : 'border-jj-gray-03 focus-within:border-jj-red focus-within:ring-[3px] focus-within:ring-jj-red/[0.08]'}`}
    >
      <div className="flex items-center gap-0.5 flex-wrap px-2 py-1.5 border-b border-jj-gray-02 bg-[#fbfaf8]"
           role="toolbar" aria-label="Formatting">
        {groups.map((group, g) => (
          <React.Fragment key={g}>
            {g > 0 && <span className="w-px h-4 bg-jj-gray-03 mx-1.5" aria-hidden="true" />}
            {group.map((tool) => (
              <button
                key={tool.key}
                type="button"
                onClick={tool.run}
                title={tool.title}
                aria-label={tool.title}
                className={`min-w-[28px] h-7 px-2 rounded-[7px] flex items-center justify-center font-johnson-text
                            text-[12.5px] text-jj-gray-07 hover:bg-jj-gray-02 hover:text-jj-gray-08 transition-colors
                            ${tool.className || ''}`}
              >
                {tool.icon ? <Icon src={tool.icon} className="w-3.5 h-3.5" tone="gray" /> : tool.label}
              </button>
            ))}
          </React.Fragment>
        ))}
        {hint && <span className={`${CLS.hint} ml-auto pl-2 text-[10.5px] ${invalid ? 'text-jj-red' : ''}`}>{hint}</span>}
      </div>
      <textarea
        ref={area}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        rows={rows}
        placeholder={placeholder}
        aria-invalid={invalid || undefined}
        className="block w-full min-h-[210px] px-3.5 py-3 border-0 outline-none resize-y bg-white font-johnson-text
                   text-[13.5px] leading-[1.65] text-jj-gray-08 placeholder:text-jj-gray-04"
      />
    </div>
  );
};

/* ─── Directory picker ────────────────────────────────────────────────────── */

const GRAPH = 'https://graph.microsoft.com/v1.0';
const SEARCH_LIMIT = 25;      // the picker's list
const SUGGEST_LIMIT = 8;      // the dropdown under a recipient box
const MIN_SEARCH = 2;
const SUGGEST_DEBOUNCE = 220;

/** Same splitting rule the backend uses: comma, semicolon or newline. */
export const splitAddresses = (raw) =>
  String(raw || '')
    .split(/[,;\n\r]+/)
    .map((s) => s.trim().replace(/^<|>$/g, ''))
    .filter(Boolean);

export const ADDR_RE = /^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$/;

/* AD display names carry a trailing affiliation tag — "Bakash, Alla [JRDUS
 * NON-J&J]". Same trim the group member list does. */
const cleanName = (raw) => String(raw || '').replace(/\s*\[[^\]]*\]\s*$/, '').trim();

const initials = (name, email) => {
  const source = (name || email || '?').replace(/[^A-Za-z, ]/g, ' ').trim();
  const parts = source.split(/[\s,]+/).filter(Boolean);
  return ((parts[0]?.[0] || '') + (parts[1]?.[0] || '')).toUpperCase() || '?';
};

// A steady colour per address, so the same person always looks the same.
const CHIP_COLORS = ['#eb1700', '#0f68b2', '#8c3bbb', '#328714', '#ff6017', '#9e0000', '#004685', '#541980'];
const chipColor = (addr) => CHIP_COLORS[
  [...String(addr).toLowerCase()].reduce((sum, ch) => sum + ch.charCodeAt(0), 0) % CHIP_COLORS.length];

const NONE = 'NONE';
const NAME_KEY = (email) => `dir_name_${email}`;

const rememberSession = (key, value) => {
  try {
    sessionStorage.setItem(key, value);
  } catch {
    /* quota or blocked storage - state still has it for this session */
  }
};

function useDirectoryNames(emails) {
  const { instance, accounts } = useMsal();
  const [names, setNames] = useState({});
  const asked = useRef(new Set());

  // A sorted, de-duplicated string so the effect re-runs when the set of
  // people changes and not merely when the array is rebuilt.
  const key = [...new Set(emails.filter(Boolean).map((e) => e.toLowerCase()))]
    .sort().join('|');

  useEffect(() => {
    const wanted = key ? key.split('|') : [];
    if (!wanted.length || !accounts.length) return undefined;

    const cached = {};
    const todo = [];
    wanted.forEach((email) => {
      if (asked.current.has(email)) return;
      let hit = null;
      try {
        hit = sessionStorage.getItem(NAME_KEY(email));
      } catch {
        /* blocked storage - just fetch it */
      }
      if (!hit) {
        todo.push(email);
        return;
      }
      asked.current.add(email);
      if (hit === NONE) return;
      try {
        cached[email] = JSON.parse(hit);
      } catch {
        todo.push(email);
      }
    });
    if (Object.keys(cached).length) setNames((prev) => ({ ...prev, ...cached }));
    if (!todo.length) return undefined;

    let cancelled = false;

    (async () => {
      let token;
      try {
        const auth = await instance.acquireTokenSilent({
          scopes: ['User.Read.All'],
          account: accounts[0],
        });
        token = auth?.accessToken;
      } catch {
        return; // names are decoration - never prompt for them
      }
      if (!token || cancelled) return;

      todo.forEach((email) => asked.current.add(email));

      // Small batches, so a role with thirty people on it does not fire thirty
      // requests at once and get throttled for its trouble.
      for (let i = 0; i < todo.length && !cancelled; i += 5) {
        const batch = todo.slice(i, i + 5);
        const found = {};
        await Promise.all(batch.map(async (email) => {
          try {
            const res = await fetch(
              `${GRAPH}/users/${encodeURIComponent(email)}?$select=displayName,jobTitle`,
              { headers: { Authorization: `Bearer ${token}` } },
            );
            if (!res.ok) {
              // A 404 is ordinary here - a distribution list or a leaver.
              rememberSession(NAME_KEY(email), NONE);
              return;
            }
            const user = await res.json();
            const entry = {
              name: cleanName(user.displayName),
              title: user.jobTitle || '',
            };
            if (!entry.name) {
              rememberSession(NAME_KEY(email), NONE);
              return;
            }
            rememberSession(NAME_KEY(email), JSON.stringify(entry));
            found[email] = entry;
          } catch {
            /* leave the address showing */
          }
        }));
        if (cancelled) return;
        if (Object.keys(found).length) setNames((prev) => ({ ...prev, ...found }));
      }
    })();

    return () => { cancelled = true; };
  }, [key, accounts.length, instance]);

  return (email) => names[String(email || '').toLowerCase()] || null;
}

async function graphToken(instance, accounts) {
  if (!accounts.length) throw new Error('Sign in to search the directory');
  const auth = await instance.acquireTokenSilent({
    scopes: ['User.Read.All'],
    account: accounts[0],
  });
  if (!auth?.accessToken) throw new Error('Could not get a directory token');
  return auth.accessToken;
}

/**
 * One directory lookup, used by the picker and by the suggestion dropdown.
 *
 * No $orderby next to $filter: Graph turns that pair down with a 400 on
 * /users unless the request opts into advanced queries. User Management hits
 * the same wall and answers it the same way - filter on the server, sort here.
 *
 * startswith, so this matches the front of the display name ("Surname, Given"
 * in this directory), the mailbox, or the UPN. A given name on its own will
 * not match a display name - the same limit User Management has.
 */
async function searchDirectory(token, query, limit) {
  // Doubling the quote is how a literal one is escaped inside an OData
  // string - an apostrophe in a surname would otherwise end the filter.
  const safe = encodeURIComponent(query.replace(/'/g, "''"));
  const filter =
    `startswith(displayName,'${safe}') or startswith(mail,'${safe}') ` +
    `or startswith(userPrincipalName,'${safe}')`;

  const res = await fetch(
    `${GRAPH}/users?$filter=${filter}&$top=${limit}` +
    `&$select=id,displayName,mail,userPrincipalName,jobTitle`,
    { headers: { Authorization: `Bearer ${token}` } },
  );
  if (!res.ok) {
    // Graph puts the actual complaint in the body. Showing only the status
    // sent us looking in the wrong place once already.
    const body = await res.json().catch(() => null);
    throw new Error(body?.error?.message
      || `Directory search returned ${res.status}`);
  }

  const data = await res.json();
  return (data.value || [])
    .map((u) => ({
      id: u.id,
      name: cleanName(u.displayName),
      email: u.mail || u.userPrincipalName || '',
      title: u.jobTitle || '',
    }))
    .filter((u) => u.email)
    .sort((a, b) => (a.name || a.email).localeCompare(b.name || b.email));
}

const PersonRow = ({ person, photo, checked, onToggle }) => (
  <label
    className={`flex items-center gap-2.5 px-2.5 py-2 rounded-[9px] cursor-pointer border
                ${checked ? 'bg-jj-red/[0.04] border-jj-red/20' : 'border-transparent hover:bg-jj-gray-01'}`}
  >
    <input
      type="checkbox"
      checked={checked}
      onChange={onToggle}
      className="w-3.5 h-3.5 flex-none accent-[#eb1700]"
    />
    {photo ? (
      <img src={photo} alt="" className="w-7 h-7 rounded-full object-cover flex-none" />
    ) : (
      <span className="w-7 h-7 rounded-full flex-none bg-jj-gray-02 flex items-center justify-center
                       font-johnson-text text-[10.5px] text-jj-gray-07">
        {initials(person.name, person.email)}
      </span>
    )}
    <span className="flex-1 min-w-0">
      <span className="block font-johnson-text text-[13px] text-jj-gray-08 truncate">
        {person.name || person.email}
      </span>
      <span className="block font-johnson-text text-[11.5px] text-jj-gray-05 truncate">
        {person.name ? person.email : person.title || ''}
      </span>
    </span>
  </label>
);

/* ─── Recipient field (chips over the raw string, with AD suggestions) ─────── */

/**
 * The To/Cc/Bcc box.
 *
 * Still chips over the raw comma-separated string the backend parses - the
 * suggestions are a way of typing, not a second source of truth, and anything
 * typed by hand and never matched still goes through exactly as before.
 *
 * Behaves the way the same box does in Outlook: type two characters, pick from
 * the directory with the arrow keys or the mouse, Enter takes the highlighted
 * one. Enter with nothing highlighted still commits what was typed, so an
 * address that is not in the directory - a distribution list, a contractor -
 * is no harder to enter than it was.
 */
export const RecipientField = ({
  label, value, onChange, placeholder, onDraftChange,
}) => {
  const { instance, accounts } = useMsal();
  const [draft, setDraft] = useState('');
  // `for` is the query these results answer. Without it there is no way to
  // tell whether what is on screen still belongs to what is in the box.
  const [suggest, setSuggest] = useState(
    { loading: false, error: '', results: [], for: '' });
  const [open, setOpen] = useState(false);
  const [highlight, setHighlight] = useState(0);
  // Whether the admin has actually moved onto a suggestion, as opposed to
  // one merely sitting there at the top of the list.
  const [navigated, setNavigated] = useState(false);
  // Every lookup takes a ticket. A slow reply for "sa" must not land on top of
  // a fast one for "sabapathy" and put the wrong names under the cursor.
  const ticket = useRef(0);
  const listId = useId();

  const parsed = useMemo(() => splitAddresses(value), [value]);
  const hasInvalid = parsed.some((a) => !ADDR_RE.test(a));
  const taken = useMemo(
    () => new Set(parsed.map((a) => a.toLowerCase())), [parsed]);

  const query = draft.trim();
  const looking = query.length >= MIN_SEARCH;

  useEffect(() => {
    if (!looking || !accounts.length) {
      setSuggest({ loading: false, error: '', results: [], for: '' });
      return undefined;
    }
    const mine = ticket.current + 1;
    ticket.current = mine;
    setSuggest((current) => ({ ...current, loading: true, error: '' }));

    const timer = setTimeout(async () => {
      try {
        const results = await searchDirectory(
          await graphToken(instance, accounts), query, SUGGEST_LIMIT);
        if (mine !== ticket.current) return;
        setSuggest({ loading: false, error: '', results, for: query });
        setHighlight(0);
      } catch (err) {
        if (mine !== ticket.current) return;
        setSuggest({
          loading: false, error: err.message || 'Lookup failed', results: [], for: query });
      }
    }, SUGGEST_DEBOUNCE);

    return () => clearTimeout(timer);
  }, [query, looking, instance, accounts.length]);

  // Somebody already on the list is not a suggestion.
  const shown = useMemo(
    () => suggest.results.filter((person) => !taken.has(person.email.toLowerCase())),
    [suggest.results, taken]);

  const listVisible = open && looking;
  // Do these suggestions answer what is in the box right now? While they do
  // not, they stay on screen so the list does not flicker on every
  // keystroke - but nothing can be taken from them.
  const fresh = !suggest.loading && suggest.for === query;
  const photoFor = useDirectoryPhotos(listVisible ? shown : []);

  const commit = () => {
    const next = splitAddresses(draft);
    if (!next.length) return;
    onChange([...parsed, ...next].join(', '));
    setDraft('');
  };

  const accept = (person) => {
    onChange([...parsed, person.email].join(', '));
    setDraft('');
    setOpen(false);
    setNavigated(false);
    setSuggest({ loading: false, error: '', results: [], for: '' });
  };

  const removeAt = (index) =>
    onChange(parsed.filter((_, i) => i !== index).join(', '));

  const report = useRef(onDraftChange);
  report.current = onDraftChange;
  useEffect(() => { report.current?.(draft); }, [draft]);

  const onKeyDown = (e) => {
    const picking = listVisible && fresh && shown.length > 0;

    if (picking && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
      e.preventDefault();
      setNavigated(true);
      setHighlight((current) => {
        const step = e.key === 'ArrowDown' ? 1 : -1;
        return (current + step + shown.length) % shown.length;
      });
      return;
    }

    if (picking && (navigated || !ADDR_RE.test(query))
        && (e.key === 'Enter' || e.key === 'Tab')) {
      const person = shown[Math.min(highlight, shown.length - 1)];
      if (person) {
        // Tab still moves focus along; Enter must not reach anything else.
        if (e.key === 'Enter') e.preventDefault();
        accept(person);
        return;
      }
    }
    if (listVisible && e.key === 'Escape') {
      // Kept off the window listener, or this would close the send dialog too.
      e.preventDefault();
      e.stopPropagation();
      setOpen(false);
      return;
    }

    if (['Enter', ',', ';'].includes(e.key) && draft.trim()) {
      e.preventDefault();
      commit();
    } else if (e.key === 'Tab' && draft.trim()) {
      // Commit what was typed but let focus move on - preventing default here
      // left keyboard users unable to tab out of the field.
      commit();
    } else if (e.key === 'Backspace' && !draft && parsed.length) {
      onChange(parsed.slice(0, -1).join(', '));
    }
  };

  return (
    <div className="flex items-start gap-2.5 py-1 border-b border-dashed border-jj-gray-02 last:border-b-0">
      <label className="w-8 flex-none pt-[11px] font-johnson-text text-[12px] font-medium text-jj-gray-06">{label}</label>
      <div className="flex-1 min-w-0 relative">
        <div
          className={`flex flex-wrap items-center gap-1.5 min-h-[40px] py-1 transition-shadow ${
            hasInvalid ? 'shadow-[inset_0_-2px_0_rgba(235,23,0,0.55)]' : ''}`}
        >
          {parsed.map((addr, index) => {
            const ok = ADDR_RE.test(addr);
            return (
              <span
                key={`${addr}-${index}`}
                title={ok ? addr : `${addr} is not a valid address`}
                className={`cc-pop inline-flex items-center gap-1.5 pl-1 pr-1.5 py-1 rounded-full font-johnson-text text-[12px] max-w-full
                            transition-colors ${ok ? 'bg-jj-gray-01 text-jj-gray-08 hover:bg-jj-gray-02'
                                 : 'bg-jj-red/[0.08] text-jj-maroon-05 shadow-[inset_0_0_0_1px_rgba(235,23,0,0.25)]'}`}
              >
                <span
                  aria-hidden="true"
                  className="w-[22px] h-[22px] rounded-full flex items-center justify-center flex-none text-white
                             font-johnson-display font-bold text-[9.5px]"
                  style={{ background: ok ? chipColor(addr) : '#9e0000' }}
                >
                  {ok ? initials('', addr.split('@')[0].replace(/[._-]+/g, ' ')) : '!'}
                </span>
                <span className="truncate max-w-[230px]">{addr}</span>
                <button
                  type="button"
                  onClick={() => removeAt(index)}
                  aria-label={`Remove ${addr}`}
                  className="w-4 h-4 rounded-full flex items-center justify-center opacity-60 hover:opacity-100 hover:bg-white"
                >
                  <Icon src={closeIcon} className="w-2 h-2" tone="gray" />
                </button>
              </span>
            );
          })}
          <input
            value={draft}
            onChange={(e) => {
              setDraft(e.target.value);
              setOpen(true);
              setNavigated(false);   // a new query - nothing chosen in it yet
            }}
            onKeyDown={onKeyDown}
            onFocus={() => setOpen(true)}
            onBlur={() => setOpen(false)}
            placeholder={parsed.length ? '' : placeholder}
            role="combobox"
            aria-expanded={listVisible}
            aria-controls={listId}
            aria-autocomplete="list"
            aria-activedescendant={
              listVisible && shown.length ? `${listId}-${highlight}` : undefined}
            autoComplete="off"
            autoCorrect="off"
            autoCapitalize="none"
            spellCheck={false}
            name={`recipients-${listId}`}
            className="flex-1 min-w-[160px] border-0 outline-none bg-transparent px-0.5 py-1.5
                       font-johnson-text text-[13px] text-jj-gray-08 placeholder:text-jj-gray-05"
          />
        </div>

        {listVisible && (
          <div
            id={listId}
            role="listbox"
            // mousedown, not click: the input would blur first and the list
            // would be gone before the click ever landed on a row.
            onMouseDown={(e) => e.preventDefault()}
            className="absolute left-0 right-0 top-full mt-1 z-40 bg-white rounded-xl
                       border border-jj-gray-02 shadow-[0_12px_32px_rgba(49,44,42,0.16)]
                       overflow-hidden"
          >
            {/* Inline, like the modal's cap: the one rule that keeps this
                list from running off the card should not depend on a
                utility class being generated. */}
            <div
              className={`overflow-auto py-1 ${
                fresh ? '' : 'opacity-50'}`}
              style={{ maxHeight: 264 }}
            >
              {suggest.loading && shown.length === 0 && (
                <p className={`${CLS.hint} px-3 py-2`}>Searching the directory…</p>
              )}
              {!suggest.loading && suggest.error && (
                <p className="px-3 py-2 font-johnson-text text-[11.5px] text-jj-maroon-05">
                  {suggest.error}
                </p>
              )}
              {fresh && !suggest.error && shown.length === 0 && (
                <p className={`${CLS.hint} px-3 py-2`}>
                  Nobody in the directory starts with “{query}”.
                  {ADDR_RE.test(query) && ' Press Enter to use it anyway.'}
                </p>
              )}
              {shown.map((person, index) => (
                <button
                  key={person.id || person.email}
                  id={`${listId}-${index}`}
                  role="option"
                  aria-selected={index === highlight}
                  onMouseEnter={() => setHighlight(index)}
                  // Guarded here, not by the dimming class above. A stale row
                  // adding the wrong address is a data problem, and a data
                  // problem must not be prevented by a stylesheet.
                  disabled={!fresh}
                  onClick={() => { if (fresh) accept(person); }}
                  className={`flex items-center gap-2.5 w-full text-left px-3 py-2 ${
                    index === highlight ? 'bg-jj-red/[0.05]' : 'hover:bg-jj-gray-01'
                  }`}
                >
                  {photoFor(person) ? (
                    <img src={photoFor(person)} alt=""
                         className="w-7 h-7 rounded-full object-cover flex-none" />
                  ) : (
                    <span className="w-7 h-7 rounded-full flex-none bg-jj-gray-02 flex items-center
                                     justify-center font-johnson-text text-[10.5px] text-jj-gray-07">
                      {initials(person.name, person.email)}
                    </span>
                  )}
                  <span className="flex-1 min-w-0">
                    <span className="block font-johnson-text text-[13px] text-jj-gray-08 truncate">
                      {person.name || person.email}
                    </span>
                    <span className="block font-johnson-text text-[11.5px] text-jj-gray-05 truncate">
                      {person.email}{person.title ? ` · ${person.title}` : ''}
                    </span>
                  </span>
                </button>
              ))}
            </div>
            {shown.length > 0 && (
              <div className="px-3 py-1.5 bg-[#f7f6f4] border-t border-jj-gray-01">
                <span className={CLS.hint}>
                  {fresh
                    ? '↑↓ to choose · Enter to add'
                    : 'Still looking · Enter adds what you typed'}
                </span>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
};

/**
 * Pick recipients out of the directory instead of typing them.
 *
 * Three kinds of source, all of them things the portal already knows:
 *   • the AD groups that grant a role, expanded through the same hook the
 *     sponsor lists use, so a group looked up once is cached for the session
 *   • anyone given a role directly in User Management — those are emails in
 *     the RBAC tables, not a group, so there is nobody to expand
 *   • a name search against the directory, for the one person who is in
 *     neither
 *
 * Whatever is ticked is handed back as plain addresses. The page still keeps
 * recipients as the raw string the backend parses, so this is a way of typing,
 * not a second source of truth.
 */
export const RecipientPicker = ({ onClose, onAdd, allowedDomains = [] }) => {
  const { AD_GROUP_ROLES, getAllRoles, getAllUserRoles, customRoles } = useRBAC();
  const { groups, load } = useAdGroupMembers();
  const { instance, accounts } = useMsal();

  const [picked, setPicked] = useState([]);
  const [term, setTerm] = useState('');
  const [people, setPeople] = useState({ loading: false, error: '', results: [] });

  // getAllRoles()/getAllUserOverrides() are rebuilt on every RBAC render, so
  // they are called inside rather than listed as dependencies; the overrides
  // are keyed on their contents instead.
  const overrides = getAllUserRoles() || {};
  const overridesKey = JSON.stringify(overrides);

  const sources = useMemo(() => {
    const roles = getAllRoles() || {};
    const out = Object.entries(AD_GROUP_ROLES || {}).map(([group, roleId]) => ({
      kind: 'group',
      id: `g:${group}`,
      group,
      name: group,
      note: roles[roleId]?.name || roleId,
      color: roles[roleId]?.color || '#a39992',
    }));

    const byRole = {};
    // Someone given several roles is listed under each of them
    Object.entries(JSON.parse(overridesKey)).forEach(([email, entry]) => {
      (entry?.roles || []).forEach((roleId) => {
        if (!byRole[roleId]) byRole[roleId] = [];
        byRole[roleId].push(email);
      });
    });
    Object.entries(byRole).forEach(([roleId, emails]) => {
      out.push({
        kind: 'role',
        id: `r:${roleId}`,
        name: roles[roleId]?.name || roleId,
        note: `${emails.length} assigned directly`,
        color: roles[roleId]?.color || '#a39992',
        people: emails.slice().sort().map((email) => ({ email, name: '', title: '' })),
      });
    });
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [AD_GROUP_ROLES, overridesKey, customRoles]);

  const [activeId, setActiveId] = useState(() => sources[0]?.id || 'search');
  const active = sources.find((s) => s.id === activeId) || null;
  const activeGroup = active?.kind === 'group' ? active.group : '';

  useEffect(() => {
    if (activeGroup) load(activeGroup);
  }, [activeGroup, load]);

  const token = useCallback(
    () => graphToken(instance, accounts), [instance, accounts]);

  // Debounced so a name typed at speed is one search, not nine.
  useEffect(() => {
    if (activeId !== 'search') return undefined;
    const query = term.trim();
    if (query.length < 2) {
      setPeople({ loading: false, error: '', results: [] });
      return undefined;
    }

    let cancelled = false;
    const timer = setTimeout(async () => {
      setPeople({ loading: true, error: '', results: [] });
      try {
        const results = await searchDirectory(await token(), query, SEARCH_LIMIT);
        if (cancelled) return;
        setPeople({ loading: false, error: '', results });
      } catch (err) {
        if (!cancelled) setPeople({ loading: false, error: err.message, results: [] });
      }
    }, 400);

    return () => { cancelled = true; clearTimeout(timer); };
  }, [activeId, term, token]);

  /* What the right-hand list is showing, whichever source is selected. */
  const listing = useMemo(() => {
    if (activeId === 'search') {
      return { loading: people.loading, error: people.error, people: people.results };
    }
    if (active?.kind === 'role') return { people: active.people };
    const state = groups[activeGroup] || {};
    return {
      loading: state.loading,
      error: state.error,
      people: (state.members || []).filter((m) => m.isUser !== false && m.email),
    };
  }, [activeId, active, activeGroup, groups, people]);

  const shown = listing.people || [];

  // Faces for the people the group hook does not cover. Group members already
  // get theirs through useAdGroupMembers, so handing those over as well would
  // just fetch every one of them twice; search results and role entries arrive
  // with nothing but an address, and this resolves them straight off it.
  const photoFor = useDirectoryPhotos(active?.kind === 'group' ? [] : shown);

  // Only the role source needs this - a group member and a search hit both
  // arrive with a name already on them.
  const nameFor = useDirectoryNames(
    active?.kind === 'role' ? shown.map((person) => person.email) : []);

  const offDomain = allowedDomains.length
    ? picked.filter((email) => {
        const domain = email.split('@')[1]?.toLowerCase() || '';
        return !allowedDomains.some(
          (d) => domain === d.toLowerCase() || domain.endsWith(`.${d.toLowerCase()}`));
      })
    : [];

  const toggle = (email) =>
    setPicked((current) =>
      current.includes(email) ? current.filter((e) => e !== email) : [...current, email]);

  const addAllShown = () =>
    setPicked((current) => {
      const merged = [...current];
      shown.forEach((p) => { if (p.email && !merged.includes(p.email)) merged.push(p.email); });
      return merged;
    });

  const hand = (field) => {
    if (!picked.length) return;
    onAdd(field, picked);
    onClose();
  };

  return (
    <Modal
      title="Add from the directory"
      subtitle="Pick a group, a role, or search for someone. Everything ticked is added as an address you can still edit."
      width={760}
      onClose={onClose}
      footer={
        <>
          <span className="mr-auto font-johnson-text text-[12px] text-jj-gray-06 self-center">
            {picked.length
              ? `${picked.length} selected${offDomain.length ? ` · ${offDomain.length} outside the allowed domains` : ''}`
              : 'Nothing selected yet'}
          </span>
          <button onClick={onClose} className={CLS.ghostBtn}>Cancel</button>
          {['To', 'Cc', 'Bcc'].map((field) => (
            <button
              key={field}
              onClick={() => hand(field.toLowerCase())}
              disabled={!picked.length}
              className={field === 'To' ? CLS.primaryBtn : CLS.ghostBtn}
            >
              Add to {field}
            </button>
          ))}
        </>
      }
    >
      {/* A real height, not a minimum. Both columns are scroll containers, and
          a scroll container whose parent is free to grow does not scroll - it
          grows, and takes the modal off the top and bottom of the screen with
          it once a search returns a couple of dozen people. */}
      <div className="flex gap-4" style={{ height: 'min(58vh, 460px)' }}>
        {/* sources */}
        <div className="w-[230px] flex-none flex flex-col gap-0.5 overflow-auto pr-0.5">
          <div className={`${CLS.railH} mb-1.5`}>Groups and roles</div>
          {sources.length === 0 && (
            <p className={`${CLS.hint} mb-2`}>
              No AD groups are mapped in this environment.
            </p>
          )}
          {sources.map((source) => (
            <button
              key={source.id}
              onClick={() => setActiveId(source.id)}
              className={`flex items-center gap-2.5 w-full text-left px-2.5 py-2 rounded-[9px]
                          border transition-colors ${
                activeId === source.id
                  ? 'bg-jj-red/[0.04] border-jj-red/20'
                  : 'border-transparent hover:bg-jj-gray-01'
              }`}
              style={{ borderLeft: `3px solid ${activeId === source.id ? source.color : 'transparent'}` }}
            >
              <Icon
                src={source.kind === 'group' ? communityIcon : personIcon}
                className="w-3.5 h-3.5 flex-none opacity-70"
              />
              <span className="flex-1 min-w-0">
                <span className="block font-johnson-text text-[12.5px] text-jj-gray-08 truncate">
                  {source.name}
                </span>
                <span className="block font-johnson-text text-[11px] text-jj-gray-05 truncate">
                  {source.note}
                </span>
              </span>
            </button>
          ))}

          <button
            onClick={() => setActiveId('search')}
            className={`flex items-center gap-2.5 w-full text-left px-2.5 py-2 mt-1.5 rounded-[9px]
                        border transition-colors ${
              activeId === 'search'
                ? 'bg-jj-red/[0.04] border-jj-red/20 border-l-[3px] border-l-jj-red'
                : 'border-transparent hover:bg-jj-gray-01'
            }`}
          >
            <Icon src={searchIcon} className="w-3.5 h-3.5 flex-none" tone="gray" />
            <span className="font-johnson-text text-[12.5px] text-jj-gray-08">
              Search the directory
            </span>
          </button>
        </div>

        {/* members */}
        <div className="flex-1 min-w-0 flex flex-col">
          {activeId === 'search' ? (
            <div className="relative mb-2.5">
              <Icon src={searchIcon} className="w-3.5 h-3.5 absolute left-2.5 top-1/2 -translate-y-1/2" tone="gray" />
              <input
                autoFocus
                value={term}
                onChange={(e) => setTerm(e.target.value)}
                placeholder="Name or email (at least two characters)"
                className={`${CLS.input} pl-8`}
              />
            </div>
          ) : (
            <div className="flex items-center justify-between mb-2.5">
              <span className="font-johnson-text text-[12.5px] text-jj-gray-06">
                {shown.length
                  ? `${shown.length} member${shown.length === 1 ? '' : 's'}`
                  : ''}
              </span>
              <button onClick={addAllShown} disabled={!shown.length} className={CLS.chipBtn}>
                Select all
              </button>
            </div>
          )}

          <div className="flex-1 min-h-0 overflow-auto flex flex-col gap-0.5 pr-0.5">
            {listing.loading && (
              <p className={`${CLS.hint} p-4 text-center`}>Looking that up…</p>
            )}
            {!listing.loading && listing.error && (
              <p className="p-4 text-center font-johnson-text text-[12.5px] text-jj-maroon-05">
                {listing.error}
              </p>
            )}
            {!listing.loading && !listing.error && shown.length === 0 && (
              <p className={`${CLS.hint} p-4 text-center`}>
                {activeId === 'search'
                  ? 'Type a name or an address to search.'
                  : 'Nobody in this one.'}
              </p>
            )}
            {shown.map((person) => {
              const resolved = person.name ? null : nameFor(person.email);
              return (
                <PersonRow
                  key={person.id || person.email}
                  person={resolved ? { ...person, ...resolved } : person}
                  photo={person.photo || photoFor(person)}
                  checked={picked.includes(person.email)}
                  onToggle={() => toggle(person.email)}
                />
              );
            })}
            {activeId === 'search' && shown.length >= SEARCH_LIMIT && (
              <p className={`${CLS.hint} px-2.5 py-2`}>
                First {SEARCH_LIMIT} matches. Keep typing to narrow it down.
              </p>
            )}
          </div>

          {picked.length > 0 && (
            <div
              className="flex-none mt-2.5 pt-2.5 border-t border-jj-gray-01
                         flex flex-wrap gap-1.5 overflow-auto"
              style={{ maxHeight: 86 }}
            >
              {picked.map((email) => (
                <span
                  key={email}
                  className="inline-flex items-center gap-1.5 px-2 py-1 rounded-md bg-jj-gray-01
                             border border-jj-gray-02 font-johnson-text text-[11.5px] text-jj-gray-08"
                >
                  <span className="truncate max-w-[200px]">{email}</span>
                  <button onClick={() => toggle(email)} aria-label={`Remove ${email}`}
                          className="opacity-50 hover:opacity-100 flex">
                    <Icon src={closeIcon} className="w-2.5 h-2.5" tone="gray" />
                  </button>
                </span>
              ))}
            </div>
          )}
        </div>
      </div>
    </Modal>
  );
};