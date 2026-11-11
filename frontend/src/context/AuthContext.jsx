import React, { createContext, useContext, useEffect, useState } from 'react';
import { useMsal, useIsAuthenticated } from '@azure/msal-react';
import { useHeartbeat } from '../hooks/useHeartbeat';

const AuthContext = createContext();

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL;

const logAuditEvent = async (action, userEmail, details = '') => {
  try {
    await fetch(`${API_BASE_URL}/api/audit/log`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-User-Email': userEmail || 'unknown',
      },
      body: JSON.stringify({ action, target: userEmail || '', details }),
    });
  } catch {
    // Audit logging is non-critical — never block the main flow
  }
};

export const AuthProvider = ({ children }) => {
  const { instance, accounts, inProgress } = useMsal();
  const isAuthenticated = useIsAuthenticated();
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);

  // Send periodic heartbeat to backend while authenticated
  useHeartbeat(user?.email || user?.username || null);

  // Fetch user's AD groups from Microsoft Graph
  const fetchUserGroups = async (accessToken) => {
    try {
      const response = await fetch('https://graph.microsoft.com/v1.0/me/memberOf', {
        headers: {
          'Authorization': `Bearer ${accessToken}`,
          'Content-Type': 'application/json'
        }
      });

      if (!response.ok) {
        console.warn('Failed to fetch user groups:', response.status);
        return [];
      }

      const data = await response.json();
      return data.value || [];
    } catch (error) {
      console.warn('Error fetching user groups:', error);
      return [];
    }
  };

  // Check authentication status
  useEffect(() => {
    const checkAuth = async () => {
      try {
        // Wait for any auth operations to complete
        if (inProgress !== "none") {
          return;
        }

        // Check if user is authenticated
        if (isAuthenticated && accounts.length > 0) {
          const account = accounts[0];

          // Get access token and fetch groups
          let groups = [];
          try {
            const tokenResponse = await instance.acquireTokenSilent({
              scopes: ["User.Read", "Group.Read.All"],
              account: account
            });

            if (tokenResponse && tokenResponse.accessToken) {
              groups = await fetchUserGroups(tokenResponse.accessToken);
            }
          } catch (tokenError) {
            console.warn('Could not fetch groups:', tokenError);
            // Continue without groups - user will get default viewer role
          }

          // Extract user information from the account
          const userData = {
            username: account.username,
            name: account.name || account.username,
            email: account.username,
            id: account.localAccountId,
            tenantId: account.tenantId,
            idTokenClaims: account.idTokenClaims,
            groups: groups
          };

          setUser(userData);
          setLoading(false);
          logAuditEvent('user_login', userData.email, 'User signed in via SSO');
        } else {
          setUser(null);
          setLoading(false);
        }
      } catch (error) {
        console.error('Error checking authentication:', error);
        setUser(null);
        setLoading(false);
      }
    };

    checkAuth();
  }, [accounts, inProgress, isAuthenticated, instance]);

  // Login function
  const login = async () => {
    try {
      setLoading(true);
      // This will be called from LoginPage component
      // The actual login is handled by MSAL
    } catch (error) {
      console.error('Login error:', error);
      throw error;
    } finally {
      setLoading(false);
    }
  };

  // Logout function
  const logout = async () => {
    try {
      setLoading(true);

      // Clear all app-specific data from localStorage
      const keysToRemove = [
        'jj_fuzzy_search_data',
      ];

      keysToRemove.forEach(key => {
        try {
          localStorage.removeItem(key);
        } catch (e) {
          console.warn(`Error removing ${key}:`, e);
        }
      });

      // Clear session storage
      try {
        sessionStorage.clear();
      } catch (e) {
        console.warn('Error clearing session storage:', e);
      }

      // Clear MSAL's local cache without redirecting to Microsoft's logout page.
      // The J&J tenant policy forces the "Pick an account" screen on logoutRedirect
      // regardless of logout_hint, so we clear locally and redirect ourselves instead.
      await logAuditEvent('user_logout', user?.email || user?.username, 'User signed out');
      await instance.clearCache();
      setUser(null);
      window.location.href = window.location.origin + "/login";
    } catch (error) {
      console.error('Logout error:', error);
      setUser(null);
      window.location.href = window.location.origin + "/login";
    } finally {
      setLoading(false);
    }
  };

  // Get access token for API calls
  const getAccessToken = async () => {
    try {
      if (accounts.length === 0) {
        throw new Error('No authenticated account found');
      }

      const account = accounts[0];
      const response = await instance.acquireTokenSilent({
        scopes: ["User.Read", "Group.Read.All"],
        account: account,
      });

      return response.accessToken;
    } catch (error) {
      console.error('Error acquiring token:', error);

      // If silent token acquisition fails, try interactive
      if (error.name === 'InteractionRequiredAuthError') {
        try {
          const response = await instance.acquireTokenPopup({
            scopes: ["User.Read", "Group.Read.All"],
          });
          return response.accessToken;
        } catch (popupError) {
          console.error('Error acquiring token via popup:', popupError);
          throw popupError;
        }
      }

      throw error;
    }
  };

  const value = {
    user,
    authenticated: isAuthenticated && !!user && accounts.length > 0,
    loading: loading || inProgress !== "none",
    login,
    logout,
    getAccessToken,
    // Keep these for backward compatibility if needed
    credentials: user ? { username: user.username, password: '' } : { username: '', password: '' },
    updateCredentials: () => {}, // No-op for SSO
  };

  return (
    <AuthContext.Provider value={value}>
      {children}
    </AuthContext.Provider>
  );
};

export const useAuth = () => {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
};