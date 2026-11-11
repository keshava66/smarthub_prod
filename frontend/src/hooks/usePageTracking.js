import { useEffect, useRef } from 'react';
import { useLocation } from 'react-router-dom';

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL;

const ROUTE_LABELS = {
  '/dashboard':       'Dashboard',
  '/dqe':             'Data Quality Engine',
  '/user-management': 'User Management',
  '/audit':           'Audit Log',
};
export function usePageTracking(userEmail) {
  const location = useLocation();
  const lastPathRef = useRef(null);

  useEffect(() => {
    if (!userEmail) return;

    const path = location.pathname;

    // Skip if same path (e.g. query param change only)
    if (path === lastPathRef.current) return;
    lastPathRef.current = path;

    const pageName = ROUTE_LABELS[path] || path;

    const logPageAccess = async () => {
      try {
        await fetch(`${API_BASE_URL}/api/audit/log`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-User-Email': userEmail,
          },
          body: JSON.stringify({
            action: 'page_access',
            target: path,
            details: `Visited ${pageName}`,
          }),
        });
      } catch { /* non-critical */ }
    };

    logPageAccess();
  }, [location.pathname, userEmail]);
}