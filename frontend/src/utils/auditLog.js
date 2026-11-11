const API_BASE_URL = import.meta.env.VITE_API_BASE_URL;
export const logAuditEvent = async (action, userEmail, target = '', details = '') => {
  try {
    await fetch(`${API_BASE_URL}/api/audit/log`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-User-Email': userEmail || 'unknown',
      },
      body: JSON.stringify({ action, target: target || '', details: details || '' }),
    });
  } catch {
    // Audit logging is non-critical — never block the main flow
  }
};