import { lazy } from 'react';

// Pages are split into their own files and fetched the first time they're
// opened. Two things can go wrong with that:
//   - a release went out while the tab was open, so the file it asks for
//     no longer exists. One quiet reload picks up the new build.
//   - the network blipped. The error boundary in App offers a retry.

const RELOAD_KEY = 'smarthub:chunk-reload';

const isChunkError = (err) => err?.name === 'ChunkLoadError'
  || /dynamically imported module|Importing a module script failed|Loading (CSS )?chunk|Unable to preload/i.test(String(err?.message || err));

// Reload at most once a minute, so a file that's really missing can't loop
function reloadOnce() {
  try {
    const last = Number(sessionStorage.getItem(RELOAD_KEY) || 0);
    if (Date.now() - last < 60000) return false;
    sessionStorage.setItem(RELOAD_KEY, String(Date.now()));
  } catch {
    return false;
  }
  window.location.reload();
  return true;
}

// Vite fires this when a page's CSS or helper files fail to preload
if (typeof window !== 'undefined') {
  window.addEventListener('vite:preloadError', (e) => {
    if (reloadOnce()) e.preventDefault();
  });
}

let waiting = 0;
const listeners = new Set();
const notify = () => listeners.forEach(fn => fn(waiting > 0));
export function onPageLoading(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

const byPath = new Map();   // "/data-studio" -> Page

export function lazyPage(loader, ...paths) {
  let pending = null;
  let done = false;
  const load = () => {
    if (!pending) {
      pending = loader().then((mod) => { done = true; return mod; }).catch((err) => {
        pending = null; // let the next try fetch again
        if (isChunkError(err) && reloadOnce()) return new Promise(() => {}); // page is reloading
        throw err;
      });
    }
    return pending;
  };
  const Page = lazy(() => {
    const p = load();
    if (!done) {
      waiting += 1;
      notify();
      const settle = () => { waiting -= 1; notify(); };
      p.then(settle, settle);
    }
    return p;
  });
  // Fetch ahead of time without rendering (used for background prefetch)
  Page.preload = () => load().catch(() => {});
  paths.forEach(path => byPath.set(path, Page));
  return Page;
}

/** Start downloading the page behind an in-app link. */
export function preloadPath(href) {
  if (!href) return;
  let path;
  try { path = new URL(href, window.location.origin).pathname; } catch { return; }
  const page = byPath.get(path) || byPath.get(`/${path.split('/')[1]}`);
  page?.preload();
}

if (typeof window !== 'undefined') {
  const intent = (e) => {
    const a = e.target?.closest?.('a[href]');
    if (a && a.origin === window.location.origin) preloadPath(a.getAttribute('href'));
  };
  document.addEventListener('pointerover', intent, { passive: true });
  document.addEventListener('pointerdown', intent, { passive: true });
  document.addEventListener('focusin', intent);
}