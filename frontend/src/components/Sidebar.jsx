import React, { useEffect, useRef, useState } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import { useRBAC } from "../context/RBACContext";
import { useAuth } from "../context/AuthContext";
import { useProfilePhoto } from "../hooks/useProfilePhoto";
import logoutIcon  from "/icons/JJ_Icon_Log_Out_RGB.svg";        // Sign out
import chevronDown from "/icons/JJ_Icon_Chevron_Down.svg"; // User menu toggle

// Icons
import collapseicon from "/icons/JJ_Icon_Chevron_Left.svg";
import expandicon from "/icons/JJ_Icon_Chevron_Right.svg";
import dashboardIcon from "/icons/JJ_Icon_Dashboard_RGB.svg";
import dqIcon from "/icons/JJ_Icon_Analysis_RGB.svg";
import userManagementIcon from "/icons/JJ_Icon_Gear_RGB.svg";
import auditIcon from "/icons/JJ_Icon_Stationery_RGB.svg";
import dataApiIcon from "/icons/JJ_Icon_Network_RGB.svg";
import userIcon from "/icons/JJ_Icon_Profile_red.svg";
import jjLogo from "/jj-logo.png";
import jjLogoSmall from "/jj-logo-small.png";

const Icon = ({ src, alt, className = "w-4 h-4", style = {} }) => (
  <img src={src} alt={alt} className={className} style={style} />
);

const Sidebar = ({ showUserMenu = false }) => {
  const navigate = useNavigate();
  const location = useLocation();
  const { hasPermission } = useRBAC();
  const { logout, user } = useAuth();
  const { profilePhoto, photoLoading } = useProfilePhoto(user, logout);

  const [showUserDropdown, setShowUserDropdown] = useState(false);

  const currentPath = location.pathname.split('/')[1] || 'dashboard';
  const previousPath = useRef(null);

  // Sidebar is always expanded on dashboard, collapsed elsewhere.
  // previousPath starts as null so this fires on first render too,
  // overriding anything stale that may have been in localStorage before.
  const [sidebarExpanded, setSidebarExpanded] = useState(
    currentPath === 'dashboard'
  );

  const toggleSidebar = () => setSidebarExpanded(p => !p);

  // Auto-expand on dashboard, collapse elsewhere on route change
  useEffect(() => {
    if (previousPath.current !== currentPath) {
      setSidebarExpanded(currentPath === 'dashboard');
      previousPath.current = currentPath;
    }
  }, [currentPath]);

  // Close dropdown when sidebar collapses
  useEffect(() => {
    if (!sidebarExpanded) setShowUserDropdown(false);
  }, [sidebarExpanded]);

  const allMenuItems = [
    { id: "dashboard",        label: "Dashboard",        icon: dashboardIcon,      path: "/dashboard",        permission: "viewDashboard" },
    { id: "dqe",              label: "Data quality",     icon: dqIcon,             path: "/dqe",              permission: "viewDQE" },
    { id: "data-api",         label: "Data API Export",  icon: dataApiIcon,        path: "/data-api",         permission: "manageDataAPIExports" },
    { id: "user-management",  label: "User Management",  icon: userManagementIcon, path: "/user-management",  permission: "viewUserManagement" },
    { id: "audit",            label: "Audit Log",        icon: auditIcon,          path: "/audit",            permission: "viewAudit" },
  ];

  const menuItems = allMenuItems.filter(item => hasPermission(item.permission));

  const handleNavigation = (path) => navigate(path);

  const handleLogout = async () => {
    try {
      await logout();
    } catch (error) {
      console.error('Logout failed:', error);
      navigate('/login');
    }
  };

  const displayUser = {
    name: (user?.name || user?.username || "User").replace(/\s*\[.*?\]\s*/g, '').trim(),
    email: user?.email || user?.username || "email@its.jnj.com",
    avatar: profilePhoto
  };

  return (
    <div
      className={`${
        sidebarExpanded ? 'w-60' : 'w-16'
      } flex-shrink-0 bg-jj-gray-08 shadow-md transition-all duration-300 ease-in-out flex flex-col`}
    >
      {/* ── Logo + Toggle — flush to top, full width ── */}
      {sidebarExpanded ? (
        /* Expanded: full logo left, chevron right */
        <div className="flex items-center justify-between py-1 bg-white border-b-4 border-jj-red">
          <img
            src={jjLogo}
            alt="J&J Logo"
            className="h-10 object-contain"
          />
          <button
            onClick={toggleSidebar}
            className="p-1 rounded hover:bg-jj-gray-02 transition-colors duration-200 flex-shrink-0"
            aria-label="Collapse sidebar"
          >
            <Icon
              src={collapseicon}
              alt="Collapse"
              className="w-5 h-5"
              style={{ filter: 'brightness(0) saturate(100%) invert(16%) sepia(89%) saturate(6616%) hue-rotate(357deg) brightness(103%) contrast(109%)' }}
            />
          </button>
        </div>
      ) : (
        /* Collapsed: small logo centered, chevron below */
        <div className="flex flex-col items-center py-2">
          {/* Small logo with white background */}
          <div className="w-11 h-11 rounded-full bg-white flex items-center justify-center">
            <img
              src={jjLogoSmall}
              alt="J&J"
              className="w-10 h-10 object-contain"
            />
          </div>
          {/* Chevron on dark background */}
          <button
            onClick={toggleSidebar}
            className="p-1 mt-2 rounded hover:bg-jj-gray-07 transition-colors duration-200"
            aria-label="Expand sidebar"
          >
            <Icon
              src={expandicon}
              alt="Expand"
              className="w-5 h-5"
              style={{ filter: 'brightness(0) invert(1)' }}
            />
          </button>
        </div>
      )}

      {/* ── Menu Items ── */}
      <ul className="space-y-3 flex-1 p-3">
        {menuItems.map((item) => (
          <li key={item.id}>
            <div
              className={`text-white p-2 rounded font-johnson-text cursor-pointer hover:bg-jj-red transition-colors duration-200 flex items-center space-x-3 ${
                currentPath === item.id ? "bg-jj-red" : ""
              }`}
              onClick={() => handleNavigation(item.path)}
              title={!sidebarExpanded ? item.label : ""}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault();
                  handleNavigation(item.path);
                }
              }}
            >
              <Icon
                src={item.icon}
                alt={item.label}
                className="w-5 h-5 flex-shrink-0"
                style={{ filter: 'brightness(0) invert(1)' }}
              />
              <span
                className="whitespace-nowrap font-johnson-text overflow-hidden transition-all duration-300"
                style={{
                  maxWidth: sidebarExpanded ? '160px' : '0px',
                  opacity: sidebarExpanded ? 1 : 0
                }}
              >
                {item.label}
              </span>
            </div>
          </li>
        ))}
      </ul>

      {/* ── User Menu (non-dashboard pages) ── */}
      {showUserMenu && (
        <div className="px-3 pb-3 border-t border-jj-gray-07 pt-3 relative">

          {/* Dropdown panel — opens upward */}
          {showUserDropdown && sidebarExpanded && (
            <div className="absolute bottom-full left-0 right-0 mb-2 bg-white rounded-lg shadow-xl border border-jj-gray-03 overflow-hidden z-50">
              {/* User info */}
              <div className="px-4 py-3 border-b border-jj-gray-02 bg-jj-gray-01">
                <div className="flex items-center space-x-3">
                  <div className="flex-shrink-0">
                    {photoLoading ? (
                      <div className="w-9 h-9 rounded-full bg-jj-gray-02 animate-pulse" />
                    ) : displayUser.avatar ? (
                      <img
                        src={displayUser.avatar}
                        alt={displayUser.name}
                        className="w-9 h-9 rounded-full object-cover border-2 border-jj-gray-02"
                      />
                    ) : (
                      <img src={userIcon} alt="User" className="w-9 h-9" />
                    )}
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="text-sm font-johnson-display text-jj-gray-08 truncate font-semibold">
                      {displayUser.name}
                    </p>
                    <p className="text-xs text-jj-gray-05 font-johnson-text truncate">
                      {displayUser.email}
                    </p>
                  </div>
                </div>
              </div>
              {/* Sign out */}
              <button
                onClick={handleLogout}
                className="w-full flex items-center space-x-3 px-4 py-3 text-sm font-johnson-text text-jj-red hover:bg-red-50 transition-colors duration-200 text-left"
              >
                <img src={logoutIcon} alt="" className="w-4 h-4 flex-shrink-0" style={{ filter: 'brightness(0) saturate(100%) invert(17%) sepia(100%) saturate(7426%) hue-rotate(3deg) brightness(92%) contrast(118%)' }} />
                <span>Sign out</span>
              </button>
            </div>
          )}

          {/* Expanded: user trigger button */}
          {sidebarExpanded && (
            <button
              onClick={() => setShowUserDropdown(prev => !prev)}
              className="w-full flex items-center space-x-3 p-2 rounded hover:bg-jj-gray-07 transition-colors duration-200 text-left"
            >
              <div className="flex-shrink-0">
                {photoLoading ? (
                  <div className="w-7 h-7 rounded-full bg-jj-gray-06 animate-pulse" />
                ) : displayUser.avatar ? (
                  <img
                    src={displayUser.avatar}
                    alt={displayUser.name}
                    className="w-7 h-7 rounded-full object-cover border border-jj-gray-06"
                  />
                ) : (
                  <img
                    src={userIcon}
                    alt="User"
                    className="w-7 h-7"
                    style={{ filter: 'brightness(0) invert(1)' }}
                  />
                )}
              </div>
              <div className="flex items-center justify-between flex-1 min-w-0">
                <div className="min-w-0 flex-1">
                  <p className="text-white text-xs font-johnson-display font-semibold truncate leading-tight">
                    {displayUser.name}
                  </p>
                  <p className="text-jj-gray-05 text-xs font-johnson-text truncate leading-tight">
                    {displayUser.email}
                  </p>
                </div>
                <img
                  src={chevronDown}
                  alt=""
                  className={`w-3.5 h-3.5 flex-shrink-0 ml-1 transition-transform duration-200 ${
                    showUserDropdown ? 'rotate-180' : ''
                  }`}
                  style={{ filter: 'brightness(0) saturate(100%) invert(65%) sepia(6%) saturate(556%) hue-rotate(346deg) brightness(92%) contrast(88%)' }}
                />
              </div>
            </button>
          )}

          {/* Collapsed: avatar + sign-out icon stacked */}
          {!sidebarExpanded && (
            <div className="flex flex-col items-center space-y-2">
              <div title={displayUser.name}>
                {photoLoading ? (
                  <div className="w-7 h-7 rounded-full bg-jj-gray-06 animate-pulse" />
                ) : displayUser.avatar ? (
                  <img
                    src={displayUser.avatar}
                    alt={displayUser.name}
                    className="w-7 h-7 rounded-full object-cover border border-jj-gray-06"
                  />
                ) : (
                  <img
                    src={userIcon}
                    alt="User"
                    className="w-7 h-7"
                    style={{ filter: 'brightness(0) invert(1)' }}
                  />
                )}
              </div>
              <button
                onClick={handleLogout}
                title="Sign out"
                className="p-1.5 rounded hover:bg-jj-red transition-colors duration-200"
              >
                <img src={logoutIcon} alt="Sign out" className="w-4 h-4" style={{ filter: 'brightness(0) saturate(100%) invert(65%) sepia(6%) saturate(556%) hue-rotate(346deg) brightness(92%) contrast(88%)' }} />
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
};

export default Sidebar;