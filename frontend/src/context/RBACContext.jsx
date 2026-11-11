import React, { createContext, useContext, useState, useEffect, useMemo } from 'react';
import { useAuth } from './AuthContext';

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL;

// AD Group to Role Mapping — group names loaded from .env (VITE_AD_GROUP_*)
// Each entry is only included if the corresponding env var is set.
const AD_GROUP_ROLES = {
  ...(import.meta.env.VITE_AD_GROUP_ADMIN         && { [import.meta.env.VITE_AD_GROUP_ADMIN]:         'admin'         }),
  ...(import.meta.env.VITE_AD_GROUP_DQE_RIGHTTIME_SUPERUSER && { [import.meta.env.VITE_AD_GROUP_DQE_RIGHTTIME_SUPERUSER]: 'dqe_righttime_superuser' }),
  ...(import.meta.env.VITE_AD_GROUP_IT_VIEWER  && { [import.meta.env.VITE_AD_GROUP_IT_VIEWER]:  'it_viewer'  }),
};

// Role Definitions with permissions
const ROLE_DEFINITIONS = {
  admin: {
    name: 'Admin',
    description: 'Full access to all features including user management',
    color: '#eb1700',
    permissions: {
      viewDashboard: true,
      viewDQE: true,
      viewITChecks: true,
      runITChecks: true,
      viewBusinessChecks: true,
      viewAudit: true,
      viewUserManagement: true,
      manageUsers: true,
      manageRoles: true,
      runBDCOperations: true,
      viewBDCSuperuser: true
    }
  },
  dqe_righttime_superuser: {
    name: 'DQE - RightTime Super User',
    description: 'Full Access to data quality engine business defined checks',
    color: '#328714',
    permissions: {
      viewDashboard: true,
      viewDQE: true,
      viewITChecks: false,
      runITChecks: false,
      viewBusinessChecks: true,
      runBDCOperations: true,
      viewBDCSuperuser: true,
      viewAudit: false,
      viewUserManagement: false,
      manageUsers: false,
      manageRoles: false
    }
  },
  it_viewer: {
    name: 'DQE - IT User',
    description: 'Read-only access to data quality engine business defined checks and IT checks',
    color: '#ff6017',
    permissions: {
      viewDashboard: true,
      viewDQE: true,
      viewITChecks: true,
      runITChecks: false,
      viewBusinessChecks: true,
      runBDCOperations: false,
      viewBDCSuperuser: false,
      viewAudit: false,
      viewUserManagement: false,
      manageUsers: false,
      manageRoles: false
    }
  },
  viewer: {
    name: 'Viewer',
    description: 'Read-only access to dashboard',
    color: '#a39992',
    permissions: {
      viewDashboard: true,
      viewDQE: false,
      viewITChecks: false,
      runITChecks: false,
      viewBusinessChecks: false,
      viewAudit: false,
      viewUserManagement: false,
      manageUsers: false,
      manageRoles: false,
      runBDCOperations: false,
      viewBDCSuperuser: false
    }
  }
};

const RBACContext = createContext();

export const RBACProvider = ({ children }) => {
  const { user, loading: authLoading } = useAuth();
  const [customRoles, setCustomRoles] = useState({});
  const [userRoleOverrides, setUserRoleOverrides] = useState({});
  const [overridesLoaded, setOverridesLoaded] = useState(false);
  // What the backend says this user can do. It enforces the same list.
  const [serverPermissions, setServerPermissions] = useState(null);

  // Fetch RBAC config from backend on mount
  useEffect(() => {
    const fetchRBACConfig = async () => {
      try {
        const response = await fetch(`${API_BASE_URL}/api/rbac/config`);
        if (response.ok) {
          const data = await response.json();
          if (data.success && data.config) {
            setCustomRoles(data.config.custom_roles || {});
            setUserRoleOverrides(data.config.user_role_overrides || {});
          }
          if (data.caller_permissions) setServerPermissions(data.caller_permissions);
        }
      } catch (error) {
        console.error('Error fetching RBAC config:', error);
      } finally {
        setOverridesLoaded(true);
      }
    };
    fetchRBACConfig();
  }, []);

  // Compute role inline as a derived value — never stale, always in sync with the
  // current render. This eliminates the gap where overrides are loaded but userRole
  // hasn't updated yet (which caused the premature redirect to /dashboard).
  const userRole = useMemo(() => {
    if (!user) return 'viewer';

    const userEmail = user.email || user.username;

    // 1. Manual override takes highest priority
    if (userRoleOverrides[userEmail?.toLowerCase()]) {
      return userRoleOverrides[userEmail.toLowerCase()];
    }

    // 2. AD group membership
    if (user.groups && user.groups.length > 0) {
      const rolesPriority = ['admin', 'dqe_righttime_superuser', 'it_viewer'];
      for (const priority of rolesPriority) {
        const adGroup = Object.keys(AD_GROUP_ROLES).find(
          group => AD_GROUP_ROLES[group] === priority
        );
        if (adGroup && user.groups.some(g =>
          g.displayName === adGroup || g.id === adGroup
        )) {
          return priority;
        }
      }
    }

    // 3. Default
    return 'viewer';
  }, [user, userRoleOverrides]);

  // loading is true until:
  //   - auth has resolved (user object available)
  //   - RBAC config has been fetched (overrides populated)
  // Once both are true, userRole above is guaranteed to be correct.
  const loading = authLoading || !overridesLoaded;

  // Get all available roles (built-in + custom)
  const getAllRoles = () => {
    return {
      ...ROLE_DEFINITIONS,
      ...customRoles
    };
  };

  // Get current user's role definition
  const getRoleDefinition = (roleName = null) => {
    const role = roleName || userRole;
    return getAllRoles()[role] || ROLE_DEFINITIONS.viewer;
  };

  // Check if user has specific permission
  const hasPermission = (permission) => {
    if (serverPermissions) return serverPermissions[permission] === true;
    const roleDefinition = getRoleDefinition();
    return roleDefinition.permissions[permission] === true;
  };

  // Check if user has any of the specified permissions
  const hasAnyPermission = (permissions = []) => {
    return permissions.some(permission => hasPermission(permission));
  };

  // Check if user has all of the specified permissions
  const hasAllPermissions = (permissions = []) => {
    return permissions.every(permission => hasPermission(permission));
  };

  // Check if user has specific role
  const hasRole = (role) => {
    if (Array.isArray(role)) {
      return role.includes(userRole);
    }
    return userRole === role;
  };

  // Create custom role (admin only) - calls backend
  const createCustomRole = async (roleId, roleDefinition) => {
    if (!hasPermission('manageRoles')) {
      throw new Error('Insufficient permissions to create roles');
    }

    try {
      const response = await fetch(`${API_BASE_URL}/api/rbac/roles`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-User-Email': user?.email || user?.username || 'unknown'
        },
        body: JSON.stringify({
          role_id: roleId,
          role_definition: roleDefinition
        })
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.detail || 'Failed to create role');
      }

      // Update local state
      setCustomRoles(prev => ({
        ...prev,
        [roleId]: roleDefinition
      }));

      return data;
    } catch (error) {
      console.error('Error creating custom role:', error);
      throw error;
    }
  };

  // Update custom role (admin only) - calls backend
  const updateCustomRole = async (roleId, updates) => {
    if (!hasPermission('manageRoles')) {
      throw new Error('Insufficient permissions to update roles');
    }

    try {
      const response = await fetch(`${API_BASE_URL}/api/rbac/roles/${roleId}`, {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json',
          'X-User-Email': user?.email || user?.username || 'unknown'
        },
        body: JSON.stringify(updates)
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.detail || 'Failed to update role');
      }

      // Update local state
      setCustomRoles(prev => ({
        ...prev,
        [roleId]: updates
      }));

      return data;
    } catch (error) {
      console.error('Error updating custom role:', error);
      throw error;
    }
  };

  // Delete custom role (admin only) - calls backend
  const deleteCustomRole = async (roleId) => {
    if (!hasPermission('manageRoles')) {
      throw new Error('Insufficient permissions to delete roles');
    }

    try {
      const response = await fetch(`${API_BASE_URL}/api/rbac/roles/${roleId}`, {
        method: 'DELETE',
        headers: {
          'X-User-Email': user?.email || user?.username || 'unknown'
        }
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.detail || 'Failed to delete role');
      }

      // Update local state
      setCustomRoles(prev => {
        const updated = { ...prev };
        delete updated[roleId];
        return updated;
      });

      // Remove any user overrides using this role
      setUserRoleOverrides(prev => {
        const updated = {};
        Object.keys(prev).forEach(email => {
          if (prev[email] !== roleId) {
            updated[email] = prev[email];
          }
        });
        return updated;
      });

      return data;
    } catch (error) {
      console.error('Error deleting custom role:', error);
      throw error;
    }
  };

  // Assign role to user (admin only) - calls backend
  const assignRoleToUser = async (userEmail, roleName) => {
    if (!hasPermission('manageUsers')) {
      throw new Error('Insufficient permissions to assign roles');
    }

    try {
      const response = await fetch(`${API_BASE_URL}/api/rbac/user-assignments`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-User-Email': user?.email || user?.username || 'unknown'
        },
        body: JSON.stringify({
          user_email: userEmail,
          role_name: roleName
        })
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.detail || 'Failed to assign role');
      }

      // Update local state
      setUserRoleOverrides(prev => ({
        ...prev,
        [userEmail.toLowerCase()]: roleName
      }));

      return data;
    } catch (error) {
      console.error('Error assigning role:', error);
      throw error;
    }
  };

  // Remove role override from user (admin only) - calls backend
  const removeUserRoleOverride = async (userEmail) => {
    if (!hasPermission('manageUsers')) {
      throw new Error('Insufficient permissions to remove role assignments');
    }

    try {
      const response = await fetch(`${API_BASE_URL}/api/rbac/user-assignments/${encodeURIComponent(userEmail)}`, {
        method: 'DELETE',
        headers: {
          'X-User-Email': user?.email || user?.username || 'unknown'
        }
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.detail || 'Failed to remove role assignment');
      }

      // Update local state
      setUserRoleOverrides(prev => {
        const updated = { ...prev };
        delete updated[userEmail.toLowerCase()];
        return updated;
      });

      return data;
    } catch (error) {
      console.error('Error removing role assignment:', error);
      throw error;
    }
  };

  // Get user's assigned role (including overrides)
  const getUserAssignedRole = (userEmail) => {
    return userRoleOverrides[userEmail.toLowerCase()] || null;
  };

  // Get all users with role overrides
  const getAllUserOverrides = () => {
    return userRoleOverrides;
  };

  const value = {
    userRole,
    roleDefinition: getRoleDefinition(),
    loading,
    hasPermission,
    hasAnyPermission,
    hasAllPermissions,
    hasRole,
    getAllRoles,
    getRoleDefinition,
    createCustomRole,
    updateCustomRole,
    deleteCustomRole,
    assignRoleToUser,
    removeUserRoleOverride,
    getUserAssignedRole,
    getAllUserOverrides,
    AD_GROUP_ROLES,
    customRoles
  };

  return (
    <RBACContext.Provider value={value}>
      {children}
    </RBACContext.Provider>
  );
};

export const useRBAC = () => {
  const context = useContext(RBACContext);
  if (!context) {
    throw new Error('useRBAC must be used within an RBACProvider');
  }
  return context;
};

// Permission Gate Component
export const PermissionGate = ({ permission, permissions, requireAll = false, fallback = null, children }) => {
  const { hasPermission, hasAnyPermission, hasAllPermissions } = useRBAC();

  let hasAccess = false;

  if (permission) {
    hasAccess = hasPermission(permission);
  } else if (permissions) {
    hasAccess = requireAll
      ? hasAllPermissions(permissions)
      : hasAnyPermission(permissions);
  }

  if (!hasAccess) {
    return fallback;
  }

  return <>{children}</>;
};

// Role Gate Component
export const RoleGate = ({ role, roles, fallback = null, children }) => {
  const { hasRole } = useRBAC();

  const allowedRoles = roles || (role ? [role] : []);
  const hasAccess = hasRole(allowedRoles);

  if (!hasAccess) {
    return fallback;
  }

  return <>{children}</>;
};