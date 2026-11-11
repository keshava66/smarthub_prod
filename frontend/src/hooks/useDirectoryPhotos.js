import { useCallback, useEffect, useRef, useState } from 'react';
import { useMsal } from '@azure/msal-react';

const NONE = 'NONE';

const USER_KEY = (name) => `dir_user_v2_${name}`;
const PHOTO_KEY = (id) => `dir_photo_v2_${id}`;

const GRAPH = 'https://graph.microsoft.com/v1.0';

const MIN_SCORE = 0.82;
const CLOSE = 0.06;
const MAX_TRIES = 4;

const NOISE = new Set(['dr', 'mr', 'mrs', 'ms', 'prof', 'jr', 'sr', 'ii', 'iii', 'phd', 'md']);

function cleanName(raw) {
  return String(raw || '')
    .replace(/\([^)]*\)/g, ' ')
    .replace(/\[[^\]]*\]/g, ' ')
    .replace(/[*"]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

// "Häggblom*" -> "haggblom". Diacritics out, punctuation out, titles out.
function tokens(raw) {
  return cleanName(raw)
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .replace(/[^a-z]+/g, ' ')
    .split(' ')
    .filter((t) => t && !NOISE.has(t));
}

function levenshtein(a, b) {
  if (a === b) return 0;
  if (!a.length || !b.length) return Math.max(a.length, b.length);
  let prev = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i++) {
    const row = [i];
    for (let j = 1; j <= b.length; j++) {
      row[j] = Math.min(
        prev[j] + 1,
        row[j - 1] + 1,
        prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1)
      );
    }
    prev = row;
  }
  return prev[b.length];
}

// How well two name words match. Handles "Adrian" vs "A." and typo'd spellings
// without letting "Bethany" pass as "Brittany".
function wordScore(a, b) {
  if (a === b) return 1;
  if (a.length === 1 || b.length === 1) return a[0] === b[0] ? 0.55 : 0;
  if (a.startsWith(b) || b.startsWith(a)) return 0.92;
  const dist = levenshtein(a, b);
  const ratio = 1 - dist / Math.max(a.length, b.length);
  return ratio >= 0.78 ? ratio * 0.9 : 0;
}

// Word order is free — AD flips to "Häggblom, Carina" often enough that
// position can't be trusted. Every word in the source name has to land.
function nameScore(wanted, candidate) {
  if (!wanted.length || !candidate.length) return 0;
  const pool = [...candidate];
  let total = 0;
  for (const w of wanted) {
    let best = 0;
    let bestAt = -1;
    pool.forEach((c, i) => {
      const s = wordScore(w, c);
      if (s > best) {
        best = s;
        bestAt = i;
      }
    });
    if (best === 0) return 0; // an unmatched word means it's a different person
    if (bestAt > -1) pool.splice(bestAt, 1);
    total += best;
  }
  const score = total / wanted.length;
  // Leftover words in the AD name are normal (middle names), but a pile of
  // them means we matched something much longer than what we searched for.
  return pool.length > 2 ? score * 0.9 : score;
}

const FIELDS = 'id,displayName,mail,userPrincipalName,accountEnabled,jobTitle,department';

async function searchUsers(name, token) {
  const runs = [
    // $search is token based, so this survives reordering and extra middle names
    `${GRAPH}/users?$search="displayName:${encodeURIComponent(name)}"&$top=10&$select=${FIELDS}`,
    // fall back to the surname alone for "de Haes" style multi-word names
    `${GRAPH}/users?$search="displayName:${encodeURIComponent(name.split(/\s+/).pop())}"&$top=25&$select=${FIELDS}`,
  ];

  for (const url of runs) {
    try {
      const res = await fetch(url, {
        headers: { Authorization: `Bearer ${token}`, ConsistencyLevel: 'eventual' },
      });
      if (!res.ok) continue;
      const data = await res.json();
      if (data.value?.length) return data.value;
    } catch {
      /* try the next shape */
    }
  }
  return [];
}

// Tie-breaker for accounts that score the same on the name. A real, in-use
// mailbox beats an external or disabled shell — those are the ones sitting
// there with no photo on them.
function accountWeight(u) {
  let w = 0;
  if (u.mail) w += 3;
  if (u.accountEnabled !== false) w += 2;
  if (!/#EXT#/i.test(u.userPrincipalName || '')) w += 2;
  if (u.jobTitle || u.department) w += 1;
  return w;
}

async function resolveCandidates(rawName, token) {
  const wanted = tokens(rawName);
  if (wanted.length < 2) return []; // single word is too weak to match on

  const found = await searchUsers(cleanName(rawName), token);
  if (!found.length) return [];

  const ranked = found
    .map((u) => ({ id: u.id, score: nameScore(wanted, tokens(u.displayName)), weight: accountWeight(u) }))
    .filter((c) => c.score >= MIN_SCORE)
    .sort((a, b) => b.score - a.score || b.weight - a.weight);

  if (!ranked.length) return [];

  const top = ranked[0].score;
  return ranked
    .filter((c) => top - c.score <= CLOSE)
    .slice(0, MAX_TRIES)
    .map((c) => c.id);
}

async function readBlob(res) {
  const blob = await res.blob();
  if (!blob.size) return null;
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(blob);
  });
}

// Small size first (it's all an avatar needs). /photo/$value is the fallback,
// and for mailboxes hosted a bit differently only the sized variants work.
async function fetchPhoto(idOrEmail, token) {
  const who = encodeURIComponent(idOrEmail);
  const urls = [
    `${GRAPH}/users/${who}/photos/120x120/$value`,
    `${GRAPH}/users/${who}/photo/$value`,
    `${GRAPH}/users/${who}/photos/48x48/$value`,
  ];

  for (const url of urls) {
    try {
      const res = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
      if (res.status === 401 || res.status === 403) return null; // scope problem, not a size problem
      if (!res.ok) continue;
      const data = await readBlob(res);
      if (data) return data;
    } catch {
      /* next size */
    }
  }
  return null;
}

function remember(key, value) {
  try {
    sessionStorage.setItem(key, value);
  } catch {
    /* quota — we still have it in state for this session */
  }
}

async function postBatch(token, requests) {
  try {
    const res = await fetch(`${GRAPH}/$batch`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ requests }),
    });
    if (!res.ok) return [];
    return (await res.json()).responses || [];
  } catch {
    return [];
  }
}

// Photos for many people in one go: Graph $batch, 20 per request, a few
// requests at a time. Binary bodies come back base64 encoded. Small size
// first (a few KB instead of up to 100 KB, so sessionStorage holds hundreds),
// full size only for anyone that 404s on it.
// Returns { handle: dataUrl | null } (null = no photo); anyone missing from
// the result couldn't be checked right now.
export async function fetchPhotosBatch(token, handles, size = '120x120') {
  const out = {};
  const run = async (list, path) => {
    const chunks = [];
    for (let i = 0; i < list.length; i += 20) chunks.push(list.slice(i, i + 20));
    const missing = [];
    let next = 0;
    const worker = async () => {
      while (next < chunks.length) {
        const chunk = chunks[next++];
        const responses = await postBatch(token, chunk.map((h, i) => ({
          id: String(i), method: 'GET', url: `/users/${encodeURIComponent(h)}${path}`,
        })));
        responses.forEach((r) => {
          const h = chunk[Number(r.id)];
          if (h == null) return;
          if (r.status === 200 && typeof r.body === 'string' && r.body) {
            const type = r.headers?.['Content-Type'] || r.headers?.['content-type'] || 'image/jpeg';
            out[h] = `data:${type};base64,${r.body}`;
          } else if (r.status === 404) {
            missing.push(h);
          }
        });
      }
    };
    await Promise.all(Array.from({ length: Math.min(3, chunks.length) }, worker));
    return missing;
  };
  const noSmall = await run(handles, `/photos/${size}/$value`);
  const none = noSmall.length ? await run(noSmall, '/photo/$value') : [];
  none.forEach((h) => { out[h] = null; });
  return out;
}

/*
 * people: array of { name, email } (email optional). Returns a lookup
 * function you call with the same person object.
 */
export function useDirectoryPhotos(people = []) {
  const { instance, accounts } = useMsal();
  const [photos, setPhotos] = useState({});
  const doneRef = useRef(new Set());

  const wanted = [...new Set(
    people
      .filter(Boolean)
      .map((p) => (p.email && p.email.includes('@') ? p.email.toLowerCase() : tokens(p.name).join(' ')))
      .filter(Boolean)
  )].sort();
  const key = wanted.join('|');

  useEffect(() => {
    if (!wanted.length || !accounts.length) return;

    const cached = {};
    const todo = [];
    wanted.forEach((who) => {
      if (doneRef.current.has(who)) return;
      const hit = sessionStorage.getItem(USER_KEY(who));
      if (hit === NONE) {
        doneRef.current.add(who);
      } else if (hit) {
        const photo = sessionStorage.getItem(PHOTO_KEY(hit));
        if (photo) {
          doneRef.current.add(who);
          if (photo !== NONE) cached[who] = photo;
        } else {
          todo.push({ who, id: hit });
        }
      } else {
        todo.push({ who, id: null });
      }
    });
    if (Object.keys(cached).length) setPhotos((prev) => ({ ...prev, ...cached }));
    if (!todo.length) return;

    let cancelled = false;

    (async () => {
      let token;
      try {
        const res = await instance.acquireTokenSilent({
          scopes: ['User.Read.All'],
          account: accounts[0],
        });
        token = res?.accessToken;
      } catch {
        return; // photos are decoration — never prompt for them
      }
      if (!token || cancelled) return;

      // Anything already fetched still lands even if the list moved on (paging,
      // typing): those people are marked done, so dropping it would leave
      // them without a face until the page reloads.
      const land = (updates) => {
        if (Object.keys(updates).length) setPhotos((prev) => ({ ...prev, ...updates }));
      };

      // Everyone we have an email or id for: batched
      const direct = todo.filter((t) => t.id || t.who.includes('@')).filter((t) => !doneRef.current.has(t.who));
      if (direct.length) {
        direct.forEach((t) => doneRef.current.add(t.who));
        const handleOf = (t) => t.id || t.who;
        const found = await fetchPhotosBatch(token, [...new Set(direct.map(handleOf))]);
        const updates = {};
        direct.forEach((t) => {
          const h = handleOf(t);
          if (!(h in found)) { doneRef.current.delete(t.who); return; } // couldn't check, try next time
          remember(USER_KEY(t.who), h);
          remember(PHOTO_KEY(h), found[h] || NONE);
          if (found[h]) updates[t.who] = found[h];
        });
        land(updates);
      }

      // Name only: search the directory first, a few at a time
      const byName = todo.filter((t) => !t.id && !t.who.includes('@'));
      for (let i = 0; i < byName.length && !cancelled; i += 4) {
        const batch = byName.slice(i, i + 4).filter((t) => !doneRef.current.has(t.who));
        batch.forEach((t) => doneRef.current.add(t.who));
        const results = await Promise.all(
          batch.map(async ({ who }) => {
            try {
              const targets = await resolveCandidates(who, token);
              if (!targets.length) {
                remember(USER_KEY(who), NONE);
                return null;
              }
              for (const target of targets) {
                const photo = await fetchPhoto(target, token);
                if (photo) {
                  remember(USER_KEY(who), target);
                  remember(PHOTO_KEY(target), photo);
                  return { who, photo };
                }
              }
              // Nobody in the cluster has one. Pin the best guess so the next
              // render doesn't run the whole search again.
              remember(USER_KEY(who), targets[0]);
              remember(PHOTO_KEY(targets[0]), NONE);
              return null;
            } catch {
              return null;
            }
          })
        );
        const updates = {};
        results.forEach((r) => { if (r) updates[r.who] = r.photo; });
        land(updates);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [key, accounts.length, instance]);

  // Stable until a photo arrives, so callers can memoise on it
  return useCallback((person) => {
    if (!person) return null;
    const who = person.email && person.email.includes('@')
      ? person.email.toLowerCase()
      : tokens(person.name).join(' ');
    return photos[who] || null;
  }, [photos]);
}