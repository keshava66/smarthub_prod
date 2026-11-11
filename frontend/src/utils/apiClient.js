import { InteractionRequiredAuthError } from '@azure/msal-browser';
import { msalInstance } from '../config/msalInstance';
import { loginRequest } from '../config/msalConfig';
import { isSessionEnding, saveReturnPath } from './sessionKeys';

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL;

/** Thrown by apiJson (and by apiFetch only when no token can be obtained). */
export class ApiError extends Error {
  constructor(message, status, detail) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

function resolveUrl(pathOrUrl) {
  if (/^https?:\/\//i.test(pathOrUrl)) return pathOrUrl;
  if (API_BASE_URL && pathOrUrl.startsWith(API_BASE_URL)) return pathOrUrl;
  return `${API_BASE_URL}${pathOrUrl}`;
}

let interactiveSignInStarted = false;

function isInteractionRequired(error) {
  if (error instanceof InteractionRequiredAuthError) return true;
  if (error?.name === 'InteractionRequiredAuthError') return true;
  return ['login_required', 'interaction_required', 'consent_required', 'no_tokens_found']
    .includes(error?.errorCode);
}

async function startInteractiveSignIn(account) {
  if (isSessionEnding() || interactiveSignInStarted) return;
  interactiveSignInStarted = true;
  saveReturnPath();

  try {
    await msalInstance.acquireTokenRedirect({ scopes: loginRequest.scopes, account });
  } catch (error) {
    if (error?.errorCode === 'interaction_in_progress') {
      return;
    }
    interactiveSignInStarted = false;
    console.error('Could not start interactive sign-in:', error);
  }
}
const inFlight = { normal: null, forced: null };

export async function getAccessToken(forceRefresh = false) {
  const account = msalInstance.getActiveAccount() || msalInstance.getAllAccounts()[0];
  if (!account) return null;

  const slot = forceRefresh ? 'forced' : 'normal';
  if (inFlight[slot]) return inFlight[slot];

  const pending = msalInstance
    .acquireTokenSilent({ scopes: loginRequest.scopes, account, forceRefresh })
    .then((result) => result?.accessToken ?? null)
    .catch(async (error) => {
      if (isInteractionRequired(error)) {
        await startInteractiveSignIn(account);
        return null;
      }
      console.error('Token acquisition failed:', error);
      return null;
    })
    .finally(() => {
      if (inFlight[slot] === pending) inFlight[slot] = null;
    });

  inFlight[slot] = pending;
  return pending;
}

export async function apiFetch(path, options = {}, _isRetry = false) {
  const token = await getAccessToken();
  if (!token) {
    const error = new ApiError('Not authenticated', 401, 'No access token available');
    error.redirecting = interactiveSignInStarted || isSessionEnding();
    throw error;
  }

  const { body, headers: extraHeaders, ...rest } = options;

  const headers = new Headers(extraHeaders || {});
  headers.set('Authorization', `Bearer ${token}`);
  // Lets the Data API activity log tell SmartHub's own calls from outside callers.
  headers.set('X-Client-App', 'SmartHub');
  // Identity is taken from the token server-side; these headers are ignored by
  // the backend and are stripped here so no call site relies on them again.
  headers.delete('X-User-Email');
  headers.delete('X-User-Role');

  let payload = body;
  const isPlainObject =
    body !== null &&
    typeof body === 'object' &&
    !(body instanceof FormData) &&
    !(body instanceof Blob) &&
    !(body instanceof ArrayBuffer) &&
    !(body instanceof URLSearchParams) &&
    !ArrayBuffer.isView(body);

  if (isPlainObject) {
    payload = JSON.stringify(body);
    if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  }

  const url = resolveUrl(path);
  let response = await fetch(url, { ...rest, headers, body: payload });

  // A 401 here means the token was accepted by MSAL's cache but rejected by
  // the API — typically a revoked session or a token that expired in flight.
  // Retry once with a forced refresh before giving up, so a long-lived tab
  // recovers silently instead of showing the user a spurious error.
  if (response.status === 401 && !_isRetry) {
    const fresh = await getAccessToken(true);
    if (fresh) {
      headers.set('Authorization', `Bearer ${fresh}`);
      response = await fetch(url, { ...rest, headers, body: payload });
    }
    if (response.status === 401) {
      const account = msalInstance.getActiveAccount() || msalInstance.getAllAccounts()[0];
      if (account) {
        await startInteractiveSignIn(account);
      }
    }
  }

  return response;
}

/** apiFetch + JSON parsing, throwing ApiError on any non-2xx response. */
export async function apiJson(path, options = {}) {
  const response = await apiFetch(path, options);

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const parsed = await response.clone().json();
      detail = parsed.detail || parsed.message || detail;
    } catch {
      /* non-JSON error body — keep statusText */
    }
    throw new ApiError(detail, response.status, detail);
  }

  return response.json();
}

export async function downloadFile(path, fallbackName = 'download') {
  const response = await apiFetch(path);

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const parsed = await response.clone().json();
      detail = parsed.detail || detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(detail, response.status, detail);
  }

  // Prefer the server's filename from Content-Disposition.
  let filename = fallbackName;
  const disposition = response.headers.get('Content-Disposition') || '';
  const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition);
  if (match) filename = decodeURIComponent(match[1]);

  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);
  const anchor = Object.assign(document.createElement('a'), {
    href: objectUrl,
    download: filename,
  });
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  // Revoke on the next tick so the download has started.
  setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
}

export function apiEventSource(pathOrUrl, options = {}) {
  const controller = new AbortController();
  const listeners = new Map();
  let closed = false;

  const shim = {
    onmessage: null,
    onerror: null,
    onopen: null,
    onend: null,
    readyState: 0, // CONNECTING
    close() {
      closed = true;
      shim.readyState = 2; // CLOSED
      controller.abort();
    },
    addEventListener(type, handler) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(handler);
    },
    removeEventListener(type, handler) {
      listeners.get(type)?.delete(handler);
    },
  };

  const emit = (type, event) => {
    if (closed) return;
    const direct = shim[`on${type}`];
    if (typeof direct === 'function') direct(event);
    listeners.get(type)?.forEach(handler => handler(event));
  };

  (async () => {
    try {
      const response = await apiFetch(pathOrUrl, {
        ...options,
        headers: { Accept: 'text/event-stream', ...(options.headers || {}) },
        signal: controller.signal,
      });

      if (!response.ok) {
        // Pass the server's reason on (a 409 "a run is still going" is useless as just "Conflict").
        let message = response.statusText;
        try {
          const body = await response.json();
          if (typeof body?.detail === 'string') message = body.detail;
        } catch { /* not JSON */ }
        emit('error', { status: response.status, message });
        shim.readyState = 2;
        return;
      }

      shim.readyState = 1; // OPEN
      emit('open', {});

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        // SSE frames are separated by a blank line.
        const frames = buffer.split(/\r?\n\r?\n/);
        buffer = frames.pop() ?? '';

        for (const frame of frames) {
          let eventName = 'message';
          const dataLines = [];
          for (const line of frame.split(/\r?\n/)) {
            if (line.startsWith('event:')) eventName = line.slice(6).trim();
            else if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^ /, ''));
          }
          if (dataLines.length === 0) continue;
          emit(eventName, { data: dataLines.join('\n') });
        }
      }

      shim.readyState = 2;
      emit('end', {}); // server closed the stream (no-op once close() was called)
    } catch (error) {
      if (error.name !== 'AbortError') {
        emit('error', error);
      }
      shim.readyState = 2;
    }
  })();

  return shim;
}