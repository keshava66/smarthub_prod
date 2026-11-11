import { useCallback, useRef, useState } from 'react';
import { useMsal } from '@azure/msal-react';
import { fetchPhotosBatch } from './useDirectoryPhotos';

const GRAPH = 'https://graph.microsoft.com/v1.0';
const NONE = 'NONE';
// Bump the suffix when the cached member shape changes, or a browser that
// already has the old shape keeps serving it for the rest of the session.
const CACHE_KEY = (name) => `ad_group_members_v2_${name.toLowerCase()}`;
const PHOTO_KEY = (id) => `dir_photo_${id}`;

/* The table stores a group's display name, not its object id, so every lookup
 * costs a search before the members call. Results are cached per session —
 * group membership doesn't move fast enough to justify re-asking. */
async function findGroupId(name, token) {
  const clean = name.replace(/'/g, "''");
  const urls = [
    `${GRAPH}/groups?$filter=displayName eq '${encodeURIComponent(clean)}'&$select=id&$top=1`,
    `${GRAPH}/groups?$search="displayName:${encodeURIComponent(name)}"&$select=id,displayName&$top=1`,
  ];
  for (const url of urls) {
    const res = await fetch(url, {
      headers: { Authorization: `Bearer ${token}`, ConsistencyLevel: 'eventual' },
    });
    if (!res.ok) continue;
    const data = await res.json();
    if (data.value?.length) return data.value[0].id;
  }
  return null;
}

/* AD display names carry a trailing affiliation tag — "Bakash, Alla [JRDUS
 * NON-J&J]". It's noise on a member chip, so it comes off. */
const cleanName = (raw) => String(raw || '').replace(/\s*\[[^\]]*\]\s*$/, '').trim();

async function fetchMembers(groupId, token) {
  const people = [];
  let url = `${GRAPH}/groups/${groupId}/members?$select=id,displayName,mail,userPrincipalName,jobTitle&$top=500`;

  // One page of 500 covers most groups; stop there so one click can't pull thousands.
  for (let page = 0; url && page < 1; page++) {
    const res = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
    if (!res.ok) throw new Error(`Graph returned ${res.status}`);
    const data = await res.json();
    (data.value || []).forEach((m) => {
      people.push({
        id: m.id,
        name: cleanName(m.displayName) || m.userPrincipalName || '',
        email: m.mail || m.userPrincipalName || '',
        title: m.jobTitle || '',
        // Nested groups and service principals never have a face — skip them
        // rather than spending a request finding that out.
        isUser: (m['@odata.type'] || '#microsoft.graph.user') === '#microsoft.graph.user',
      });
    });
    url = data['@odata.nextLink'] || null;
  }

  return people.sort((a, b) => a.name.localeCompare(b.name));
}

function remember(key, value) {
  try {
    sessionStorage.setItem(key, value);
  } catch {
    /* quota — state still has it for this session */
  }
}

export function useAdGroupMembers() {
  const { instance, accounts } = useMsal();
  const [state, setState] = useState({});
  const inFlight = useRef(new Set());

  // Photos come in after the names — decoration shouldn't hold up the list.
  // One batched request per 20 faces instead of one (or three) per person.
  const loadPhotos = useCallback(async (group, members, token) => {
    const targets = members.filter((m) => m.isUser && !m.photo && !m.noPhoto && (m.id || m.email));
    if (!targets.length) return;
    const handleOf = (m) => m.id || m.email;
    let found;
    try {
      found = await fetchPhotosBatch(token, [...new Set(targets.map(handleOf))]);
    } catch {
      return; /* no faces, keep the initials */
    }
    const hits = {};
    targets.forEach((m) => {
      const h = handleOf(m);
      if (!(h in found)) return;
      remember(PHOTO_KEY(h), found[h] || NONE);
      if (found[h]) hits[h] = found[h];
    });
    if (!Object.keys(hits).length) return;
    setState((s) => {
      const current = s[group];
      if (!current?.members) return s;
      return {
        ...s,
        [group]: {
          ...current,
          members: current.members.map((m) => {
            const hit = hits[handleOf(m)];
            return hit ? { ...m, photo: hit } : m;
          }),
        },
      };
    });
  }, []);

  const token = useCallback(async () => {
    if (!accounts.length) return null;
    const auth = await instance.acquireTokenSilent({
      scopes: ['GroupMember.Read.All', 'User.Read.All'],
      account: accounts[0],
    });
    return auth?.accessToken || null;
  }, [instance, accounts]);

  // knownId: the group's object id when the caller already has it, which
  // skips the name search (and can't pick up a different group with the
  // same display name).
  const load = useCallback(async (group, knownId = null) => {
    if (!group || inFlight.current.has(group)) return;
    inFlight.current.add(group);

    try {
      const cached = sessionStorage.getItem(CACHE_KEY(group));
      if (cached) {
        // Faces are cached under their own keys, so put back the ones we have.
        const members = JSON.parse(cached).map((m) => {
          const photo = sessionStorage.getItem(PHOTO_KEY(m.id || m.email));
          if (photo === NONE) return { ...m, noPhoto: true };
          return photo ? { ...m, photo } : m;
        });
        setState((s) => ({ ...s, [group]: { members } }));
        // Anyone whose face wasn't cached still needs fetching — the members
        // list outliving the photos is the normal case, not an edge one.
        if (members.some((m) => m.isUser && !m.photo && !m.noPhoto)) {
          const t = await token();
          if (t) await loadPhotos(group, members, t);
        }
        return;
      }

      if (!accounts.length) {
        setState((s) => ({ ...s, [group]: { error: 'Sign in to look up group members' } }));
        return;
      }

      setState((s) => ({ ...s, [group]: { loading: true } }));

      const t = await token();
      if (!t) throw new Error('No token');

      const id = knownId || await findGroupId(group, t);
      if (!id) {
        setState((s) => ({ ...s, [group]: { error: 'Group not found in the directory' } }));
        return;
      }

      const members = await fetchMembers(id, t);
      try {
        sessionStorage.setItem(CACHE_KEY(group), JSON.stringify(members));
      } catch {
        /* quota — state still has it for this session */
      }
      setState((s) => ({ ...s, [group]: { members } }));
      await loadPhotos(group, members, t);
    } catch (e) {
      setState((s) => ({ ...s, [group]: { error: e.message || 'Lookup failed' } }));
    } finally {
      inFlight.current.delete(group);
    }
  }, [token, loadPhotos, accounts.length]);

  return { groups: state, load };
}