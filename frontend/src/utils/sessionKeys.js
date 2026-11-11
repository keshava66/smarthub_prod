// Shared localStorage keys used to keep the session state in sync across
// browser tabs. Written by AuthContext (logout) and SessionTimeoutContext
// (activity tracking).
export const LAST_ACTIVITY_KEY = 'dqe_last_activity';
export const LOGOUT_BROADCAST_KEY = 'dqe_logout_broadcast';

// Where to send the user after they sign back in. Stored in sessionStorage
// (per-tab, survives the MSAL redirect round-trip) at logout time, consumed
// once after the next successful login.
export const RETURN_PATH_KEY = 'dqe_return_path';

let sessionEnding = false;

/** Called by the logout paths before they redirect. One-way latch. */
export function beginSessionTeardown() {
  sessionEnding = true;
}

/** True once this page load has committed to ending the session. */
export function isSessionEnding() {
  return sessionEnding;
}
/** Internal paths we never restore to — there is nothing there to go back to. */
function isRestorablePath(path) {
  return (
    typeof path === 'string' &&
    path.startsWith('/') &&
    !path.startsWith('//') &&
    path !== '/' &&
    !path.startsWith('/login') &&
    !path.startsWith('/logout')
  );
}
let pendingReturnPath = null;

/**
 * Remember the page the user is currently on so we can restore it after
 * re-login. Called just before a logout redirect, and before any interactive
 * token redirect. /login, /logout and / are never saved (nothing to restore).
 *
 * MUST be called synchronously, before any await, by anything that is about to
 * tear the session down — see pendingReturnPath above.
 */
export function saveReturnPath() {
  try {
    const path = window.location.pathname + window.location.search + window.location.hash;
    if (isRestorablePath(path)) {
      pendingReturnPath = path;
      sessionStorage.setItem(RETURN_PATH_KEY, path);
      // A new save invalidates any previously consumed value. Without this,
      // a logout/login cycle that completes within a single JS page load
      // (SPA navigation racing the logout redirect, silent SSO re-auth)
      // would restore the PREVIOUS page from the stale cache instead of
      // re-reading the fresh path from sessionStorage.
      consumedReturnPath = undefined;
    }
  } catch { /* storage unavailable — fall back to dashboard */ }
}

export function restoreReturnPath() {
  if (!pendingReturnPath) return;
  try {
    sessionStorage.setItem(RETURN_PATH_KEY, pendingReturnPath);
    consumedReturnPath = undefined;
  } catch { /* storage unavailable — fall back to dashboard */ }
}

// Several components resolve the post-login destination (AuthRedirectHandler,
// LoginPage, the "/" route). Cache the consumed value for the lifetime of the
// page load so they all agree instead of the second caller falling back to
// /dashboard and overriding the first navigation. A logout always does a full
// page reload, which resets this cache.
let consumedReturnPath;

/**
 * Read-and-clear the saved return path. Returns null if none was saved or the
 * value is not a safe internal path. Idempotent within a single page load.
 */
export function consumeReturnPath() {
  if (consumedReturnPath !== undefined) return consumedReturnPath;
  let path = null;
  try {
    path = sessionStorage.getItem(RETURN_PATH_KEY);
    sessionStorage.removeItem(RETURN_PATH_KEY);
  } catch { /* storage unavailable */ }
  if (!isRestorablePath(path)) {
    path = null;
  }
  consumedReturnPath = path;
  return path;
}