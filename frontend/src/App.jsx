import React, { lazy, Suspense, useEffect } from "react";
import { BrowserRouter, Routes, Route, Navigate, useNavigate, useLocation } from "react-router-dom";
import { useIsAuthenticated } from "@azure/msal-react";
import { AuthProvider } from "./context/AuthContext";
import { RBACProvider, PermissionGate, useRBAC } from "./context/RBACContext";
import { SessionTimeoutProvider } from "./context/SessionTimeoutContext";
import { useAuth } from "./context/AuthContext";
import { usePageTracking } from "./hooks/usePageTracking";
import ProtectedRoute from "./components/ProtectedRoute";
import LoginPage from "./pages/LoginPage";
import Sidebar from "./components/Sidebar";
import Header from "./components/Header";
import Dashboard from "./pages/Dashboard";
import DQEngine from "./pages/DQEngine";
import UserManagement from "./pages/UserManagement";
import AuditPage from "./pages/AuditPage";

// Big pages, loaded the first time someone opens them
const DataAPIExport = lazy(() => import("./pages/dataApi/DataAPIExport"));
const DataApiTableView = lazy(() => import("./pages/dataApi/DataApiTableView"));

// Wraps a route element with a permission check.
// Waits for both auth AND RBAC to finish loading before deciding — prevents a refresh
// from incorrectly redirecting to /dashboard while the role is still resolving.
const PermissionRoute = ({ permission, children }) => {
  const { loading: rbacLoading, hasPermission } = useRBAC();
  const { loading: authLoading } = useAuth();
  if (authLoading || rbacLoading) return null;
  if (!hasPermission(permission)) return <Navigate to="/dashboard" replace />;
  return <>{children}</>;
};

// Browsable API table opens in its own tab, outside the sidebar layout
const DataApiTableViewGate = () => (
  <SessionTimeoutProvider>
    <RBACProvider>
      <PermissionRoute permission="manageDataAPIExports">
        <Suspense fallback={null}>
          <DataApiTableView />
        </Suspense>
      </PermissionRoute>
    </RBACProvider>
  </SessionTimeoutProvider>
);

// Component to handle authentication redirects
const AuthRedirectHandler = () => {
  const isAuthenticated = useIsAuthenticated();
  const navigate = useNavigate();
  const location = useLocation();

  useEffect(() => {
    if (isAuthenticated && location.pathname === '/login') {
      navigate('/dashboard', { replace: true });
    }
  }, [isAuthenticated, location.pathname, navigate]);

  return null;
};

// Inner layout that has access to location
const AppLayoutInner = () => {
  const location = useLocation();
  const isDashboard = location.pathname === '/dashboard' || location.pathname === '/';
  const { user } = useAuth();

  // Track page navigation in audit log
  usePageTracking(user?.email || user?.username || null);

  return (
    <div className="flex h-screen bg-jj-gray-01">
      {/* Sidebar spans full height — logo panel reaches the very top */}
      <Sidebar showUserMenu={!isDashboard} />

      {/* Right side: header (dashboard only) + main content */}
      <div className="flex flex-col flex-grow overflow-hidden">
        {isDashboard && <Header />}
        <main className="flex-grow overflow-auto">
          <Routes>
            <Route path="/dashboard" element={<Dashboard />} />
            <Route
              path="/dqe"
              element={
                <PermissionRoute permission="viewDQE">
                  <DQEngine />
                </PermissionRoute>
              }
            />
            <Route
              path="/user-management"
              element={
                <PermissionRoute permission="viewUserManagement">
                  <UserManagement />
                </PermissionRoute>
              }
            />
            <Route
              path="/audit"
              element={
                <PermissionRoute permission="viewAudit">
                  <AuditPage />
                </PermissionRoute>
              }
            />
            <Route
              path="/data-api"
              element={
                <PermissionRoute permission="manageDataAPIExports">
                  <Suspense fallback={null}>
                    <DataAPIExport />
                  </Suspense>
                </PermissionRoute>
              }
            />
            <Route path="/" element={<Navigate to="/dashboard" replace />} />
          </Routes>
        </main>
      </div>
    </div>
  );
};

// Main app content component (protected routes)
const AppLayout = () => {
  return (
    <SessionTimeoutProvider>
      <RBACProvider>
        <AppLayoutInner />
      </RBACProvider>
    </SessionTimeoutProvider>
  );
};

// Root app component with authentication and routing
export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <AuthRedirectHandler />
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route
            path="/data-api-view/:apiId/:alias"
            element={
              <ProtectedRoute>
                <DataApiTableViewGate />
              </ProtectedRoute>
            }
          />
          <Route
            path="/*"
            element={
              <ProtectedRoute>
                <AppLayout />
              </ProtectedRoute>
            }
          />
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  );
}