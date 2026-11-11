import { apiFetch, ApiError } from './apiClient';

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL;

// The backend wants a Microsoft token on every call. The older pages still use
// plain fetch, so the token gets added here instead of in each of them.
export function installApiAuth() {
  const nativeFetch = window.fetch.bind(window);

  window.fetch = (input, init = {}) => {
    const url = typeof input === 'string' ? input : input?.url ?? String(input);
    const hasAuth = new Headers(init.headers).has('Authorization');
    if (!API_BASE_URL || !url.startsWith(API_BASE_URL) || hasAuth) {
      return nativeFetch(input, init);
    }
    // No token yet (signed out): send it as is, the backend answers 401
    return apiFetch(url, init).catch((err) => {
      if (err instanceof ApiError) return nativeFetch(input, init);
      throw err;
    });
  };
}
