import { useEffect, useRef, useCallback } from 'react';

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL;
const HEARTBEAT_INTERVAL_MS = 30_000;   // send at most every 30 s
const ACTIVITY_TIMEOUT_MS   = 60_000;   // consider user inactive after 60 s of no activity

export function useHeartbeat(userEmail) {
  const lastActivityRef  = useRef(null);   // timestamp of last real user activity
  const intervalRef      = useRef(null);
  const emailRef         = useRef(userEmail);

  // Keep emailRef current without re-running effects
  useEffect(() => { emailRef.current = userEmail; }, [userEmail]);

  const sendHeartbeat = useCallback(async () => {
    const email = emailRef.current;
    if (!email) return;
    if (!lastActivityRef.current) return;                          // no activity yet
    const idle = Date.now() - lastActivityRef.current;
    if (idle > ACTIVITY_TIMEOUT_MS) return;                        // user went idle

    try {
      await fetch(`${API_BASE_URL}/api/audit/heartbeat`, {
        method: 'POST',
        headers: { 'X-User-Email': email },
      });
    } catch { /* non-critical */ }
  }, []);

  const recordActivity = useCallback(() => {
    lastActivityRef.current = Date.now();
  }, []);

  useEffect(() => {
    if (!userEmail) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
      return;
    }

    // Listen for real activity
    const events = ['mousedown', 'mousemove', 'keydown', 'scroll', 'touchstart', 'click'];
    events.forEach(e => window.addEventListener(e, recordActivity, { passive: true }));

    // Send on an interval — but sendHeartbeat checks activity before actually sending
    intervalRef.current = setInterval(sendHeartbeat, HEARTBEAT_INTERVAL_MS);

    return () => {
      events.forEach(e => window.removeEventListener(e, recordActivity));
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    };
  }, [userEmail, sendHeartbeat, recordActivity]);
}