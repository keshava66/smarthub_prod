import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useAuth } from '../context/AuthContext';
import { useMsal } from '@azure/msal-react';

import auditIcon      from "/icons/JJ_Icon_Stationery_RGB.svg";
import searchIcon     from "/icons/JJ_Icon_Search_RGB.svg";
import closeIcon      from "/icons/JJ_Icon_Close.svg";
import chevronLeftIcon  from "/icons/JJ_Icon_Chevron_Left.svg";
import chevronRightIcon from "/icons/JJ_Icon_Chevron_Right.svg";
import downloadIcon   from "/icons/JJ_Icon_Download_PDF_RGB.svg";
import refreshIcon    from "/icons/JJ_Icon_Refresh_RGB.svg";
import filterIcon     from "/icons/JJ_Icon_Filter.svg";
import alertIcon      from "/icons/JJ_Icon_Alert_RGB.svg";
import userIcon       from "/icons/JJ_Icon_Profile_red.svg";
import communityIcon  from "/icons/JJ_Icon_Community_RGB.svg";
import activityIcon   from "/icons/JJIM_Icon_Fingernail.svg";
import shieldIcon     from "/icons/JJ_Icon_Safety.svg";
import clockIcon      from "/icons/JJ_Icon_Timer_RGB.svg";
import removeIcon     from "/icons/JJ_Icon_Remove.svg";
import recordsIcon    from "/icons/JJ_Icon_Records_Information_RGB.svg";
import userAddIcon    from "/icons/JJ_Icon_Person_RGB.svg";

// Reusable icon wrapper
const Icon = ({ src, alt = '', className = 'w-4 h-4', style = {} }) => (
  <img src={src} alt={alt} className={className} style={style} />
);

// CSS filter strings keyed by hex color — used to tint monochrome SVGs
const COLOR_FILTER = {
  '#328714': 'brightness(0) saturate(100%) invert(35%) sepia(80%) saturate(800%) hue-rotate(85deg) brightness(88%) contrast(95%)',
  '#eb1700': 'brightness(0) saturate(100%) invert(17%) sepia(100%) saturate(7426%) hue-rotate(3deg) brightness(92%) contrast(118%)',
  '#0f68b2': 'brightness(0) saturate(100%) invert(37%) sepia(93%) saturate(1535%) hue-rotate(189deg) brightness(91%) contrast(101%)',
  '#ff6017': 'brightness(0) saturate(100%) invert(48%) sepia(97%) saturate(2446%) hue-rotate(347deg) brightness(102%) contrast(105%)',
  '#8c3bbb': 'brightness(0) saturate(100%) invert(29%) sepia(60%) saturate(1500%) hue-rotate(255deg) brightness(85%) contrast(95%)',
  '#6e6259': 'brightness(0) saturate(100%) invert(42%) sepia(6%) saturate(656%) hue-rotate(346deg) brightness(92%) contrast(88%)',
  white:     'brightness(0) invert(1)',
  gray05:    'brightness(0) saturate(100%) invert(65%) sepia(6%) saturate(556%) hue-rotate(346deg) brightness(92%) contrast(88%)',
  gray06:    'brightness(0) saturate(100%) invert(50%) sepia(6%) saturate(656%) hue-rotate(346deg) brightness(92%) contrast(88%)',
  gray07:    'brightness(0) saturate(100%) invert(42%) sepia(6%) saturate(656%) hue-rotate(346deg) brightness(92%) contrast(88%)',
  gray08:    'brightness(0) saturate(100%) invert(15%) sepia(7%) saturate(806%) hue-rotate(346deg) brightness(95%) contrast(89%)',
  red:       'brightness(0) saturate(100%) invert(17%) sepia(100%) saturate(7426%) hue-rotate(3deg) brightness(92%) contrast(118%)',
};

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL;

const ACTION_META = {
  role_assigned:   { label: 'Role Assigned',    color: '#328714', bg: '#f0fdf4', icon: userAddIcon    },
  role_removed:    { label: 'Role Removed',      color: '#eb1700', bg: '#fef2f2', icon: removeIcon     },
  role_created:    { label: 'Role Created',      color: '#0f68b2', bg: '#eff6ff', icon: shieldIcon     },
  role_updated:    { label: 'Role Updated',      color: '#ff6017', bg: '#fff7ed', icon: recordsIcon    },
  role_reset:      { label: 'Role Reset',        color: '#8c3bbb', bg: '#faf5ff', icon: refreshIcon    },
  role_deleted:    { label: 'Role Deleted',      color: '#eb1700', bg: '#fef2f2', icon: removeIcon     },
  user_login:      { label: 'User Login',        color: '#328714', bg: '#f0fdf4', icon: userIcon       },
  user_logout:     { label: 'User Logout',       color: '#6e6259', bg: '#f8f7f6', icon: userIcon       },
  session_timeout: { label: 'Session Timeout',   color: '#ff6017', bg: '#fff7ed', icon: clockIcon      },
  page_access:     { label: 'Page Access',       color: '#8c3bbb', bg: '#faf5ff', icon: activityIcon   },
};

const PAGE_SIZE = 20;

const formatDate = (iso) => {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' }) +
    ' ' + d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
};

const relativeTime = (iso) => {
  if (!iso) return '';
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
};

const ActionBadge = ({ action }) => {
  const meta = ACTION_META[action] || { label: action, color: '#a39992', bg: '#f8f7f6', icon: activityIcon };
  return (
    <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-johnson-text font-semibold"
      style={{ backgroundColor: meta.bg, color: meta.color }}>
      <img src={meta.icon} alt="" className="w-3 h-3" style={{ filter: COLOR_FILTER[meta.color] }} />
      {meta.label}
    </span>
  );
};

export default function AuditPage() {
  const { user } = useAuth();
  const { instance, accounts } = useMsal();

  const [logs, setLogs]           = useState([]);
  const [total, setTotal]         = useState(0);
  const [loading, setLoading]     = useState(false);
  const [error, setError]         = useState(null);
  const [page, setPage]           = useState(1);

  // Filters
  const [search, setSearch]       = useState('');
  const [actionFilter, setAction] = useState('');
  const [userFilter, setUserF]    = useState('');
  const [dateFrom, setDateFrom]   = useState('');
  const [dateTo, setDateTo]       = useState('');
  const [showFilters, setShowFilters] = useState(false);

  const [stats, setStats]         = useState(null);
  const [activeUsers, setActiveUsers] = useState(null);
  const [activeLoading, setActiveLoading] = useState(false);
  const [userNames, setUserNames]   = useState({});  // email → displayName cache
  const [uniqueUsers, setUniqueUsers] = useState([]);
  const [showUniqueTooltip, setShowUniqueTooltip] = useState(false);
  const [userPhotos, setUserPhotos] = useState({});   // email → object URL
  const photoUrlsRef = useRef({});                     // tracks URLs for cleanup

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  // ── fetch logs ──────────────────────────────────────────────────────────────
  const fetchLogs = useCallback(async (currentPage = 1) => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({
        page: currentPage,
        page_size: PAGE_SIZE,
        ...(search      && { search }),
        ...(actionFilter && { action: actionFilter }),
        ...(userFilter   && { performed_by: userFilter }),
        ...(dateFrom     && { date_from: dateFrom }),
        ...(dateTo       && { date_to: dateTo }),
      });

      const res = await fetch(`${API_BASE_URL}/api/audit/logs?${params}`, {
        headers: { 'X-User-Email': user?.email || user?.username || '' }
      });
      if (!res.ok) throw new Error('Failed to fetch audit logs');
      const data = await res.json();
      setLogs(data.logs || []);
      setTotal(data.total || 0);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [search, actionFilter, userFilter, dateFrom, dateTo, user]);

  // ── resolve display names from Graph API ─────────────────────────────────────
  const fetchUserNames = useCallback(async (emails) => {
    if (!emails.length || !accounts.length) return;
    const missing = emails.filter(e => e && e.includes('@') && !userNames[e.toLowerCase()]);
    if (!missing.length) return;
    try {
      const tokenRes = await instance.acquireTokenSilent({
        scopes: ['User.Read.All'],
        account: accounts[0],
      });
      const results = await Promise.all(
        missing.map(async email => {
          try {
            const res = await fetch(
              `https://graph.microsoft.com/v1.0/users/${encodeURIComponent(email)}?$select=displayName`,
              { headers: { Authorization: `Bearer ${tokenRes.accessToken}` } }
            );
            if (!res.ok) return null;
            const data = await res.json();
            return { email: email.toLowerCase(), name: data.displayName };
          } catch { return null; }
        })
      );
      const updates = {};
      results.forEach(r => { if (r) updates[r.email] = r.name; });
      if (Object.keys(updates).length) {
        setUserNames(prev => ({ ...prev, ...updates }));
      }
    } catch { /* silent — names are cosmetic */ }
  }, [accounts, instance, userNames]);
  const fetchStats = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/audit/stats`, {
        headers: { 'X-User-Email': user?.email || user?.username || '' }
      });
      if (res.ok) setStats(await res.json());
    } catch { /* silent */ }
  }, [user]);

  // ── fetch active users ───────────────────────────────────────────────────────
  const fetchActiveUsers = useCallback(async () => {
    setActiveLoading(true);
    try {
      const res = await fetch(`${API_BASE_URL}/api/audit/active-users`, {
        headers: { 'X-User-Email': user?.email || user?.username || '' }
      });
      if (res.ok) setActiveUsers(await res.json());
    } catch { /* silent */ }
    finally { setActiveLoading(false); }
  }, [user]);

  const fetchUniqueUsers = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/audit/unique-users`, {
        headers: { 'X-User-Email': user?.email || user?.username || '' }
      });
      if (res.ok) {
        const data = await res.json();
        setUniqueUsers(data.users || []);
      }
    } catch { /* silent */ }
  }, [user]);

  const fetchUserPhotos = useCallback(async (users) => {
    if (!users.length || !accounts.length) return;
    const missing = users.filter(u =>
      u.email && u.email.includes('@') && !userPhotos[u.email.toLowerCase()]
    );
    if (!missing.length) return;
    try {
      const tokenRes = await instance.acquireTokenSilent({
        scopes: ['User.Read.All'],
        account: accounts[0],
      });
      const results = await Promise.all(
        missing.slice(0, 50).map(async u => {
          try {
            const res = await fetch(
              `https://graph.microsoft.com/v1.0/users/${encodeURIComponent(u.email)}/photo/$value`,
              { headers: { Authorization: `Bearer ${tokenRes.accessToken}` } }
            );
            if (!res.ok) return null;
            const blob = await res.blob();
            const url = URL.createObjectURL(blob);
            return { email: u.email.toLowerCase(), url };
          } catch { return null; }
        })
      );
      const updates = {};
      results.forEach(r => {
        if (r) { photoUrlsRef.current[r.email] = r.url; updates[r.email] = r.url; }
      });
      if (Object.keys(updates).length) setUserPhotos(prev => ({ ...prev, ...updates }));
    } catch { /* silent — photos are cosmetic */ }
  }, [accounts, instance, userPhotos]);

  // Revoke object URLs when component unmounts to avoid memory leaks
  useEffect(() => {
    return () => { Object.values(photoUrlsRef.current).forEach(url => URL.revokeObjectURL(url)); };
  }, []);

  const filtersMounted = useRef(false);

  useEffect(() => {
    fetchLogs(page);
  }, [page]);

  // Resolve display names and photos for all unique emails in the current page
  useEffect(() => {
    const emails = [...new Set(logs.map(l => l.performed_by).filter(Boolean))];
    fetchUserNames(emails);
    fetchUserPhotos(emails.map(email => ({ email })));
  }, [logs]);

  // Reset to page 1 when filters change — skip the very first render to avoid
  // double-fetching on mount (the [page] effect above already handles that)
  useEffect(() => {
    if (!filtersMounted.current) { filtersMounted.current = true; return; }
    setPage(1);
    fetchLogs(1);
  }, [search, actionFilter, userFilter, dateFrom, dateTo]);

  useEffect(() => { fetchStats(); fetchUniqueUsers(); }, []);

  useEffect(() => {
    if (uniqueUsers.length) fetchUserPhotos(uniqueUsers);
  }, [uniqueUsers]);

  // Poll active users every 30 seconds
  useEffect(() => {
    fetchActiveUsers();
    const interval = setInterval(fetchActiveUsers, 30_000);
    return () => clearInterval(interval);
  }, [fetchActiveUsers]);

  // ── export CSV ──────────────────────────────────────────────────────────────
  const handleExport = async () => {
    try {
      const params = new URLSearchParams({
        ...(search       && { search }),
        ...(actionFilter && { action: actionFilter }),
        ...(userFilter   && { performed_by: userFilter }),
        ...(dateFrom     && { date_from: dateFrom }),
        ...(dateTo       && { date_to: dateTo }),
      });
      const res = await fetch(`${API_BASE_URL}/api/audit/export?${params}`, {
        headers: { 'X-User-Email': user?.email || user?.username || '' }
      });
      if (!res.ok) throw new Error('Export failed');
      const blob = await res.blob();
      const url  = URL.createObjectURL(blob);
      const a    = document.createElement('a');
      a.href     = url;
      a.download = `audit_log_${new Date().toISOString().split('T')[0]}.csv`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(err.message);
    }
  };

  const clearFilters = () => {
    setSearch(''); setAction(''); setUserF('');
    setDateFrom(''); setDateTo('');
  };

  const hasActiveFilters = search || actionFilter || userFilter || dateFrom || dateTo;

  return (
    <div className="h-full bg-gradient-to-br from-jj-gray-01 via-white to-jj-gray-02 overflow-auto">
      <div className="flex flex-col w-full px-8 pt-5 pb-6 mx-auto">

        {/* Header */}
        <div className="mb-6">
          <div className="flex items-center gap-3 mb-2">
            <div className="w-12 h-12 bg-jj-red rounded-lg flex items-center justify-center">
              <img src={auditIcon} alt="Audit" className="w-6 h-6" style={{ filter: 'brightness(0) invert(1)' }} />
            </div>
            <div>
              <h1 className="text-3xl font-johnson-display text-jj-gray-08" style={{ lineHeight: '110%' }}>
                Audit Log
              </h1>
              <p className="text-jj-gray-06 font-johnson-text text-sm">
                Track all user actions, role changes, and system events
              </p>
            </div>
          </div>
        </div>

        {/* Stat Cards */}
        <div className="grid grid-cols-2 lg:grid-cols-5 gap-4 mb-6">
          {[
            { label: 'Total Events', value: stats?.total ?? '—',        icon: activityIcon, color: '#0f68b2' },
            { label: 'Today',        value: stats?.today ?? '—',        icon: clockIcon,    color: '#328714' },
            { label: 'Role Changes', value: stats?.role_changes ?? '—', icon: shieldIcon,   color: '#eb1700' },
          ].map(({ label, value, icon, color }) => (
            <div key={label} className="bg-white rounded-xl border border-jj-gray-02 p-5 flex items-center gap-4">
              <div className="w-10 h-10 rounded-lg flex items-center justify-center flex-shrink-0"
                style={{ backgroundColor: color + '18' }}>
                <Icon src={icon} className="w-5 h-5" style={{ filter: COLOR_FILTER[color] }} />
              </div>
              <div>
                <p className="text-2xl font-johnson-display text-jj-gray-08 leading-none">{value}</p>
                <p className="text-xs text-jj-gray-05 font-johnson-text mt-0.5">{label}</p>
              </div>
            </div>
          ))}

          {/* ── Unique Users card — hover to see who they are ── */}
          <div
            className="bg-white rounded-xl border border-jj-gray-02 p-5 flex items-center gap-4 relative cursor-default select-none"
            onMouseEnter={() => setShowUniqueTooltip(true)}
            onMouseLeave={() => setShowUniqueTooltip(false)}
          >
            <div className="w-10 h-10 rounded-lg flex items-center justify-center flex-shrink-0"
              style={{ backgroundColor: '#eb170010' }}>
              <Icon src={userAddIcon} className="w-5 h-5" style={{ filter: COLOR_FILTER.red }} />
            </div>
            <div>
              <p className="text-2xl font-johnson-display text-jj-gray-08 leading-none">
                {stats?.unique_users ?? '—'}
              </p>
              <p className="text-xs text-jj-gray-05 font-johnson-text mt-0.5">
                Unique Users
              </p>
            </div>

            {/* Tooltip */}
            <div style={{
              position: 'absolute',
              top: 'calc(100% + 14px)',
              left: '50%',
              transform: showUniqueTooltip
                ? 'translateX(-50%) translateY(0) scale(1)'
                : 'translateX(-50%) translateY(-8px) scale(0.98)',
              opacity: showUniqueTooltip ? 1 : 0,
              pointerEvents: showUniqueTooltip ? 'auto' : 'none',
              transition: 'opacity 0.2s ease, transform 0.2s ease',
              width: '380px',
              zIndex: 60,
            }}>

              {/* Arrow — border layer */}
              <div style={{
                position: 'absolute', top: '-8px', left: '50%',
                transform: 'translateX(-50%)',
                width: 0, height: 0,
                borderLeft: '8px solid transparent',
                borderRight: '8px solid transparent',
                borderBottom: '8px solid #e8e6e3',
              }} />
              {/* Arrow — fill layer */}
              <div style={{
                position: 'absolute', top: '-6px', left: '50%',
                transform: 'translateX(-50%)',
                width: 0, height: 0,
                borderLeft: '7px solid transparent',
                borderRight: '7px solid transparent',
                borderBottom: '7px solid #ffffff',
              }} />

              <div style={{
                backgroundColor: '#ffffff',
                borderRadius: '16px',
                overflow: 'hidden',
                boxShadow: '0 12px 40px rgba(49,44,42,0.13), 0 2px 8px rgba(49,44,42,0.06)',
                border: '1px solid #e8e6e3',
              }}>

                {/* ── Header ── */}
                <div style={{
                  padding: '16px 20px 14px',
                  borderBottom: '1px solid #f1efed',
                  display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                    <div style={{
                      width: '38px', height: '38px', borderRadius: '10px',
                      backgroundColor: '#eb1700',
                      display: 'flex', alignItems: 'center', justifyContent: 'center',
                      flexShrink: 0,
                    }}>
                      <Icon src={communityIcon} alt="Users" style={{ width: '18px', height: '18px', filter: COLOR_FILTER.white }} />
                    </div>
                    <div>
                      <p style={{
                        fontFamily: 'JohnsonDisplay, Arial, sans-serif',
                        fontSize: '15px', color: '#312c2a',
                        fontWeight: 600, margin: 0, lineHeight: 1.2,
                      }}>
                        {uniqueUsers.length} Unique User{uniqueUsers.length !== 1 ? 's' : ''}
                      </p>
                      <p style={{
                        fontFamily: 'JohnsonText, Arial, sans-serif',
                        fontSize: '11px', color: '#a39992', margin: 0, marginTop: '2px',
                      }}>
                        All-time contributors
                      </p>
                    </div>
                  </div>
                  <div style={{
                    backgroundColor: '#eb170010',
                    border: '1px solid #eb170025',
                    borderRadius: '20px',
                    padding: '3px 10px',
                  }}>
                    <p style={{
                      fontFamily: 'JohnsonDisplay, Arial, sans-serif',
                      fontSize: '12px', color: '#eb1700',
                      fontWeight: 700, margin: 0,
                    }}>
                      {uniqueUsers.reduce((s, u) => s + u.event_count, 0)} events
                    </p>
                  </div>
                </div>

                {/* ── User list ── */}
                <div style={{ maxHeight: '280px', overflowY: 'auto' }}
                  className="unique-users-scroll">
                  {uniqueUsers.length === 0 ? (
                    <div style={{ padding: '32px 20px', textAlign: 'center' }}>
                      <p style={{ fontFamily: 'JohnsonText, Arial, sans-serif',
                        fontSize: '13px', color: '#a39992', margin: 0 }}>
                        No users yet
                      </p>
                    </div>
                  ) : uniqueUsers.map((u, i) => {
                    const displayName = userNames[u.email?.toLowerCase()];
                    const photo = userPhotos[u.email?.toLowerCase()];
                    const initials = (displayName || u.email || '?')
                      .split(/[\s.@]/).filter(Boolean).slice(0, 2)
                      .map(w => w[0].toUpperCase()).join('');

                    return (
                      <div key={u.email}
                        style={{
                          display: 'flex', alignItems: 'center', gap: '12px',
                          padding: '10px 20px',
                          borderBottom: '1px solid #f8f7f6',
                          transition: 'background 0.15s, border-left 0.15s',
                          borderLeft: '3px solid transparent',
                          cursor: 'default',
                        }}
                        onMouseEnter={e => {
                          e.currentTarget.style.background = '#fef9f9';
                          e.currentTarget.style.borderLeft = '3px solid #eb1700';
                        }}
                        onMouseLeave={e => {
                          e.currentTarget.style.background = 'transparent';
                          e.currentTarget.style.borderLeft = '3px solid transparent';
                        }}
                      >
                        {/* Rank number */}
                        <span style={{
                          fontFamily: 'JohnsonDisplay, Arial, sans-serif',
                          fontSize: '11px', color: '#cbc4bc',
                          fontWeight: 600, width: '16px',
                          flexShrink: 0, textAlign: 'right',
                        }}>
                          {i + 1}
                        </span>

                        {/* Avatar */}
                        {photo ? (
                          <img src={photo} alt={displayName || u.email}
                            style={{
                              width: '36px', height: '36px', borderRadius: '50%',
                              objectFit: 'cover', flexShrink: 0,
                              border: '2px solid #e8e6e3',
                            }}
                          />
                        ) : (
                          <div style={{
                            width: '36px', height: '36px', borderRadius: '50%', flexShrink: 0,
                            backgroundColor: '#f1efed',
                            border: '2px solid #e8e6e3',
                            display: 'flex', alignItems: 'center', justifyContent: 'center',
                            fontSize: '12px', fontWeight: 700, color: '#81766f',
                            fontFamily: 'JohnsonDisplay, Arial, sans-serif',
                          }}>
                            {initials}
                          </div>
                        )}

                        {/* Name + email */}
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <p style={{
                            fontFamily: 'JohnsonDisplay, Arial, sans-serif',
                            fontSize: '13px', color: '#312c2a', fontWeight: 500,
                            margin: 0, lineHeight: 1.3,
                            whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                          }}>
                            {displayName || u.email}
                          </p>
                          {displayName && (
                            <p style={{
                              fontFamily: 'JohnsonText, Arial, sans-serif',
                              fontSize: '11px', color: '#a39992',
                              margin: 0, lineHeight: 1.3,
                              whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                            }}>
                              {u.email}
                            </p>
                          )}
                        </div>

                        {/* Event count + last activity */}
                        <div style={{ textAlign: 'right', flexShrink: 0 }}>
                          <p style={{
                            fontFamily: 'JohnsonDisplay, Arial, sans-serif',
                            fontSize: '14px', color: '#312c2a',
                            fontWeight: 700, margin: 0, lineHeight: 1.2,
                          }}>
                            {u.event_count.toLocaleString()}
                          </p>
                          <p style={{
                            fontFamily: 'JohnsonText, Arial, sans-serif',
                            fontSize: '10px', color: '#cbc4bc',
                            margin: 0, lineHeight: 1.3,
                          }}>
                            {relativeTime(u.last_activity)}
                          </p>
                        </div>
                      </div>
                    );
                  })}
                </div>

                {/* ── Footer ── */}
                <div style={{
                  padding: '10px 20px',
                  borderTop: '1px solid #f1efed',
                  backgroundColor: '#fafaf9',
                  display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                }}>
                  <p style={{
                    fontFamily: 'JohnsonText, Arial, sans-serif',
                    fontSize: '10px', color: '#cbc4bc', margin: 0,
                  }}>
                    Sorted by most recent activity
                  </p>
                  <p style={{
                    fontFamily: 'JohnsonText, Arial, sans-serif',
                    fontSize: '10px', color: '#cbc4bc', margin: 0,
                  }}>
                    Right column = total events
                  </p>
                </div>

              </div>
            </div>
          </div>

          {/* Active Users card — live, with tooltip listing who's online */}
          <div className="bg-white rounded-xl border border-jj-gray-02 p-5 flex items-center gap-4 relative group">
            <div className="w-10 h-10 rounded-lg flex items-center justify-center flex-shrink-0"
              style={{ backgroundColor: '#32871418' }}>
              <div className="relative">
                <Icon src={communityIcon} className="w-5 h-5" style={{ filter: COLOR_FILTER['#328714'] }} />
                <span className="absolute -top-0.5 -right-0.5 w-2.5 h-2.5 rounded-full bg-jj-green-03 border-2 border-white">
                  <span className="absolute inset-0 rounded-full bg-jj-green-03 animate-ping opacity-75" />
                </span>
              </div>
            </div>
            <div>
              <p className="text-2xl font-johnson-display text-jj-gray-08 leading-none">
                {activeLoading && activeUsers === null ? '…' : (activeUsers?.count ?? '—')}
              </p>
              <p className="text-xs text-jj-gray-05 font-johnson-text mt-0.5">Active Now</p>
            </div>

            {activeUsers?.users?.length > 0 && (
              <div className="absolute bottom-full left-0 mb-2 w-72 bg-jj-gray-08 text-white rounded-xl shadow-xl p-3 z-50
                opacity-0 group-hover:opacity-100 pointer-events-none transition-opacity duration-200">
                <p className="text-xs font-johnson-text font-semibold text-jj-gray-04 uppercase tracking-wide mb-2">
                  Online ({activeUsers.count}) — last {activeUsers.timeout_minutes} min
                </p>
                <div className="space-y-1 max-h-40 overflow-y-auto">
                  {activeUsers.users.map(u => (
                    <div key={u.email} className="flex items-center gap-2">
                      <span className="w-1.5 h-1.5 rounded-full bg-jj-green-03 flex-shrink-0" />
                      <span className="text-xs font-johnson-text text-white truncate">{u.email}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>

        <style>{`
          .unique-users-scroll::-webkit-scrollbar { width: 4px; }
          .unique-users-scroll::-webkit-scrollbar-track { background: #f1efed; }
          .unique-users-scroll::-webkit-scrollbar-thumb { background: #cbc4bc; border-radius: 4px; }
          .unique-users-scroll::-webkit-scrollbar-thumb:hover { background: #a39992; }
        `}</style>

        {/* Toolbar */}
        <div className="bg-white rounded-xl border border-jj-gray-03 p-4 mb-4">
          <div className="flex flex-wrap items-center gap-3">
            {/* Search */}
            <div className="flex-1 min-w-56 max-w-md relative">
              <Icon src={searchIcon} className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4"
                style={{ filter: COLOR_FILTER.gray05 }} />
              <input
                type="text"
                placeholder="Search by user, action or details..."
                value={search}
                onChange={e => setSearch(e.target.value)}
                className="w-full pl-9 pr-8 py-2 border border-jj-gray-03 rounded-lg text-sm font-johnson-text focus:outline-none focus:ring-2 focus:ring-jj-red"
              />
              {search && (
                <button onClick={() => setSearch('')}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-jj-gray-05 hover:text-jj-gray-08">
                  <Icon src={closeIcon} className="w-3.5 h-3.5" style={{ filter: COLOR_FILTER.gray05 }} />
                </button>
              )}
            </div>

            {/* Action filter */}
            <div className="relative">
              <select value={actionFilter} onChange={e => setAction(e.target.value)}
                className={`pl-3 pr-8 py-2 border rounded-lg text-sm font-johnson-text bg-white appearance-none cursor-pointer focus:outline-none focus:ring-2 focus:ring-jj-red transition-all ${actionFilter ? 'border-jj-red text-jj-red font-semibold' : 'border-jj-gray-03 text-jj-gray-07'}`}>
                <option value="">All Actions</option>
                {Object.entries(ACTION_META).map(([key, { label }]) => (
                  <option key={key} value={key} style={{ color: '#312c2a', fontWeight: 'normal' }}>{label}</option>
                ))}
              </select>
              <Icon src={chevronRightIcon}
                className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 w-3.5 h-3.5"
                style={{ transform: 'translateY(-50%) rotate(90deg)', filter: COLOR_FILTER.gray05 }} />
            </div>

            {/* Advanced filters toggle */}
            <button onClick={() => setShowFilters(p => !p)}
              className={`flex items-center gap-2 px-4 py-2 rounded-lg border font-johnson-text text-sm transition-all ${showFilters || userFilter || dateFrom || dateTo ? 'bg-jj-red text-white border-jj-red' : 'bg-white text-jj-gray-07 border-jj-gray-03 hover:border-jj-red hover:text-jj-red'}`}>
              <Icon src={filterIcon} className="w-4 h-4"
                style={{ filter: showFilters || userFilter || dateFrom || dateTo ? COLOR_FILTER.white : COLOR_FILTER.gray07 }} />
              Filters
              {(userFilter || dateFrom || dateTo) && (
                <span className={`inline-flex items-center justify-center w-4 h-4 rounded-full text-xs font-semibold ${showFilters ? 'bg-white text-jj-red' : 'bg-jj-red text-white'}`}>
                  {[userFilter, dateFrom, dateTo].filter(Boolean).length}
                </span>
              )}
            </button>

            {hasActiveFilters && (
              <button onClick={clearFilters}
                className="flex items-center gap-1.5 px-3 py-2 text-sm font-johnson-text text-jj-red hover:bg-red-50 rounded-lg border border-jj-red transition-colors">
                <Icon src={closeIcon} className="w-3.5 h-3.5" style={{ filter: COLOR_FILTER.red }} /> Clear all
              </button>
            )}

            <div className="ml-auto flex items-center gap-2">
              <button onClick={() => fetchLogs(page)}
                className="p-2 border border-jj-gray-03 rounded-lg hover:bg-jj-gray-01 transition-colors text-jj-gray-06 hover:text-jj-red"
                title="Refresh">
                <Icon src={refreshIcon} className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`}
                  style={{ filter: COLOR_FILTER.gray06 }} />
              </button>
              <button onClick={handleExport}
                className="flex items-center gap-2 px-4 py-2 bg-jj-gray-08 text-white rounded-lg hover:bg-jj-gray-07 transition-colors font-johnson-text text-sm">
                <Icon src={downloadIcon} className="w-4 h-4" style={{ filter: COLOR_FILTER.white }} /> Export CSV
              </button>
            </div>
          </div>

          {/* Advanced filters row */}
          {showFilters && (
            <div className="mt-3 pt-3 border-t border-jj-gray-02 flex flex-wrap gap-3">
              <div className="relative min-w-48">
                <Icon src={userIcon} className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5"
                  style={{ filter: COLOR_FILTER.gray05 }} />
                <input type="text" placeholder="Filter by user email..."
                  value={userFilter} onChange={e => setUserF(e.target.value)}
                  className="w-full pl-8 pr-3 py-2 border border-jj-gray-03 rounded-lg text-sm font-johnson-text focus:outline-none focus:ring-2 focus:ring-jj-red" />
              </div>
              <div className="flex items-center gap-2">
                <label className="text-xs text-jj-gray-05 font-johnson-text whitespace-nowrap">From</label>
                <input type="date" value={dateFrom} onChange={e => setDateFrom(e.target.value)}
                  className="px-3 py-2 border border-jj-gray-03 rounded-lg text-sm font-johnson-text focus:outline-none focus:ring-2 focus:ring-jj-red" />
              </div>
              <div className="flex items-center gap-2">
                <label className="text-xs text-jj-gray-05 font-johnson-text whitespace-nowrap">To</label>
                <input type="date" value={dateTo} onChange={e => setDateTo(e.target.value)}
                  className="px-3 py-2 border border-jj-gray-03 rounded-lg text-sm font-johnson-text focus:outline-none focus:ring-2 focus:ring-jj-red" />
              </div>
            </div>
          )}
        </div>

        {/* Table */}
        <div className="bg-white rounded-xl border border-jj-gray-03 overflow-hidden">
          {loading ? (
            <div className="text-center py-16">
              <div className="animate-spin rounded-full h-10 w-10 border-b-2 border-jj-red mx-auto mb-4"></div>
              <p className="text-jj-gray-06 font-johnson-text text-sm">Loading audit logs...</p>
            </div>
          ) : error ? (
            <div className="text-center py-16 px-6">
              <Icon src={alertIcon} className="w-12 h-12 mx-auto mb-3" style={{ filter: COLOR_FILTER.red }} />
              <p className="text-lg font-johnson-display text-jj-gray-08 mb-1">Failed to load audit logs</p>
              <p className="text-sm text-jj-gray-06 font-johnson-text mb-4">{error}</p>
              <button onClick={() => fetchLogs(page)}
                className="px-5 py-2 bg-jj-red text-white rounded-lg font-johnson-text text-sm hover:bg-jj-maroon-05 transition-colors">
                Try Again
              </button>
            </div>
          ) : logs.length === 0 ? (
            <div className="text-center py-16">
              <Icon src={activityIcon} className="w-12 h-12 mx-auto mb-3" style={{ filter: COLOR_FILTER.gray05 }} />
              <p className="text-lg font-johnson-display text-jj-gray-06">No audit events found</p>
              <p className="text-sm text-jj-gray-05 font-johnson-text mt-1">
                {hasActiveFilters ? 'Try adjusting your filters' : 'Events will appear here as actions are performed'}
              </p>
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full">
                <thead className="bg-jj-gray-02">
                  <tr>
                    <th className="px-6 py-4 text-left text-xs font-johnson-text font-semibold text-jj-gray-07 uppercase tracking-wide">Action</th>
                    <th className="px-6 py-4 text-left text-xs font-johnson-text font-semibold text-jj-gray-07 uppercase tracking-wide">Performed By</th>
                    <th className="px-6 py-4 text-left text-xs font-johnson-text font-semibold text-jj-gray-07 uppercase tracking-wide">Target</th>
                    <th className="px-6 py-4 text-left text-xs font-johnson-text font-semibold text-jj-gray-07 uppercase tracking-wide">Details</th>
                    <th className="px-6 py-4 text-left text-xs font-johnson-text font-semibold text-jj-gray-07 uppercase tracking-wide">Timestamp</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-jj-gray-02">
                  {logs.map((log) => {
                    const email = log.performed_by || '';
                    const displayName = userNames[email.toLowerCase()];
                    return (
                      <tr key={log.id} className="hover:bg-jj-gray-01 transition-colors">
                        <td className="px-4 py-4 whitespace-nowrap">
                          <ActionBadge action={log.action} />
                        </td>
                        <td className="px-6 py-4">
                          <div className="flex items-center gap-2">
                            <div className="flex-shrink-0">
                              {userPhotos[email.toLowerCase()] ? (
                                <img
                                  src={userPhotos[email.toLowerCase()]}
                                  alt={displayName || email}
                                  className="w-7 h-7 rounded-full object-cover border border-jj-gray-02"
                                />
                              ) : (
                                <div className="w-7 h-7 rounded-full bg-jj-gray-02 flex items-center justify-center">
                                  <Icon src={userIcon} className="w-4 h-4" style={{ filter: COLOR_FILTER.gray06 }} />
                                </div>
                              )}
                            </div>
                            <div className="min-w-0">
                              {displayName && (
                                <p className="text-sm font-johnson-text font-medium text-jj-gray-08 truncate max-w-[200px]" title={displayName}>
                                  {displayName}
                                </p>
                              )}
                              <p className={`font-johnson-text truncate max-w-[180px] ${displayName ? 'text-xs text-jj-gray-05' : 'text-sm text-jj-gray-08'}`} title={email}>
                                {email || '—'}
                              </p>
                            </div>
                          </div>
                        </td>
                        <td className="px-6 py-4">
                          <p className="text-sm font-johnson-text text-jj-gray-07 truncate max-w-[160px]" title={log.target}>
                            {log.target || '—'}
                          </p>
                        </td>
                        <td className="px-6 py-4 max-w-xs">
                          <p className="text-sm font-johnson-text text-jj-gray-06" title={log.details}>
                            {log.details || '—'}
                          </p>
                        </td>
                        <td className="px-6 py-4 whitespace-nowrap">
                          <p className="text-sm font-johnson-text text-jj-gray-08">{formatDate(log.timestamp)}</p>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          {/* Pagination */}
          {!loading && !error && logs.length > 0 && (
            <div className="bg-jj-gray-01 px-6 py-4 flex items-center justify-between border-t border-jj-gray-03">
              <p className="text-sm font-johnson-text text-jj-gray-06">
                Showing {(page - 1) * PAGE_SIZE + 1}–{Math.min(page * PAGE_SIZE, total)} of {total} events
              </p>
              <div className="flex items-center gap-2">
                <button onClick={() => setPage(p => Math.max(1, p - 1))} disabled={page === 1}
                  className="flex items-center gap-1.5 px-3 py-2 border border-jj-gray-03 rounded-lg text-sm font-johnson-text disabled:opacity-40 disabled:cursor-not-allowed hover:bg-white transition-colors">
                  <Icon src={chevronLeftIcon} className="w-4 h-4" style={{ filter: COLOR_FILTER.gray07 }} /> Previous
                </button>
                <span className="text-sm font-johnson-text text-jj-gray-07 px-2">
                  Page {page} of {totalPages}
                </span>
                <button onClick={() => setPage(p => Math.min(totalPages, p + 1))} disabled={page === totalPages}
                  className="flex items-center gap-1.5 px-3 py-2 border border-jj-gray-03 rounded-lg text-sm font-johnson-text disabled:opacity-40 disabled:cursor-not-allowed hover:bg-white transition-colors">
                  Next <Icon src={chevronRightIcon} className="w-4 h-4" style={{ filter: COLOR_FILTER.gray07 }} />
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}