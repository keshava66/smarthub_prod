import { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import ReactDOM from 'react-dom';
import { useSearchParams } from 'react-router-dom';
import { BarChart, Bar, Cell, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, ReferenceLine } from 'recharts';
// ─── J&J Custom Icons ────────────────────────────────────────────────────────
import chevronRightSrc  from "/icons/JJ_Icon_Chevron_Right.svg";
import chevronLeftSrc   from "/icons/JJ_Icon_Chevron_Left.svg";
import runSrc           from "/icons/JJ_Icon_Video_Play_RGB.svg";
import refreshSrc       from "/icons/JJ_Icon_Refresh_RGB.svg";
import downloadSrc      from "/icons/JJ_Icon_Download_PDF_RGB.svg";
import alertSrc         from "/icons/JJ_Icon_Alert_RGB.svg";
import chartSrc         from "/icons/JJ_Icon_Graph_Chart_RGB.svg";
import deltaSrc         from "/icons/JJ_Icon_Outcomes.svg";
import checkSrc         from "/icons/JJ_Icon_Check_RGB.svg";
import closeSrc         from "/icons/JJ_Icon_Close.svg";
import recordsSrc       from "/icons/JJ_Icon_Unfold_All_More.svg";
import infoSrc          from "/icons/JJ_Icon_Info_RGB.svg";
import gearSrc          from "/icons/JJ_Icon_Gear_RGB.svg";
import addSrc           from "/icons/JJ_Icon_Add.svg";
import editSrc          from "/icons/JJ_Icon_Edit_RGB.svg";
import saveSrc          from "/icons/JJ_Icon_Save_RGB.svg";
import databaseSrc      from "/icons/JJ_Icon_Database_RGB.svg";
import overviewSrc      from "/icons/JJ_Icon_Data_Visualization.svg";
import safetySrc        from "/icons/JJ_Icon_Safety.svg";
import sortSrc          from "/icons/JJ_Icon_Sort.svg";
import dashboardSrc     from "/icons/JJ_Icon_Dashboard_RGB.svg";
import activitySrc      from "/icons/JJ_Icon_Loading.svg";
import review           from "/icons/JJ_Icon_Vision_Accessibility_RGB.svg";

// ─── Icon color filters ───────────────────────────────────────────────────────
const F = {
  red:     'brightness(0) saturate(100%) invert(17%) sepia(100%) saturate(7426%) hue-rotate(3deg) brightness(92%) contrast(118%)',
  maroon:  'brightness(0) saturate(100%) invert(13%) sepia(97%) saturate(6477%) hue-rotate(359deg) brightness(91%) contrast(99%)',
  green:   'brightness(0) saturate(100%) invert(35%) sepia(80%) saturate(800%) hue-rotate(85deg) brightness(88%) contrast(95%)',
  blue:    'brightness(0) saturate(100%) invert(37%) sepia(93%) saturate(1535%) hue-rotate(189deg) brightness(91%) contrast(101%)',
  orange:  'brightness(0) saturate(100%) invert(48%) sepia(97%) saturate(2446%) hue-rotate(347deg) brightness(102%) contrast(105%)',
  violet:  'brightness(0) saturate(100%) invert(29%) sepia(60%) saturate(1500%) hue-rotate(255deg) brightness(85%) contrast(95%)',
  gray04:  'brightness(0) saturate(100%) invert(82%) sepia(6%) saturate(356%) hue-rotate(346deg) brightness(92%) contrast(88%)',
  gray05:  'brightness(0) saturate(100%) invert(65%) sepia(6%) saturate(556%) hue-rotate(346deg) brightness(92%) contrast(88%)',
  gray06:  'brightness(0) saturate(100%) invert(50%) sepia(6%) saturate(656%) hue-rotate(346deg) brightness(92%) contrast(88%)',
  gray07:  'brightness(0) saturate(100%) invert(42%) sepia(6%) saturate(656%) hue-rotate(346deg) brightness(92%) contrast(88%)',
  gray08:  'brightness(0) saturate(100%) invert(15%) sepia(7%) saturate(806%) hue-rotate(346deg) brightness(95%) contrast(89%)',
  white:   'brightness(0) invert(1)',
};

// Drop-in icon component — mirrors the lucide API (className, style) but renders an <img>
const Ic = ({ src, className = 'w-4 h-4', filter = F.gray07, style = {}, alt = '' }) => (
  <img src={src} alt={alt} className={className} style={{ filter, flexShrink: 0, ...style }} />
);

const INPUT_BASE_CLS   = 'w-full px-3 py-2 border rounded-lg font-johnson-text text-sm outline-none transition-all';
const INPUT_EDIT_CLS   = `${INPUT_BASE_CLS} border-jj-gray-03 text-jj-gray-08 bg-white focus:border-jj-red focus:ring-2 focus:ring-jj-red focus:ring-opacity-10`;
const INPUT_LOCKED_CLS = `${INPUT_BASE_CLS} border-jj-gray-02 text-jj-gray-05 bg-jj-gray-01 cursor-default select-all`;

import LoadingSpinner from './components/LoadingSpinner';
import { useRBAC } from '../context/RBACContext';
import { useAuth } from '../context/AuthContext';
import { apiEventSource } from '../utils/apiClient';

// ─── Audit logging helper ─────────────────────────────────────────────────────
const logAuditEvent = async (apiBaseUrl, userEmail, action, target = '', details = '') => {
  try {
    await fetch(`${apiBaseUrl}/api/audit/log`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-User-Email': userEmail || 'unknown' },
      body: JSON.stringify({ action, target, details }),
    });
  } catch { /* audit logging is non-critical */ }
};

// ─── API Service ──────────────────────────────────────────────────────────────
const createAPI = (baseURL) => ({
  baseURL,
  async get(endpoint) {
    const response = await fetch(`${baseURL}${endpoint}`);
    if (!response.ok) throw new Error(`API Error: ${response.statusText}`);
    return response.json();
  },
  async post(endpoint, data) {
    const response = await fetch(`${baseURL}${endpoint}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
    if (!response.ok) throw new Error(`API Error: ${response.statusText}`);
    return response.json();
  },
  getKPI:           function(deltaMode = false) { return this.get(`/api/overview/kpi?delta_mode=${deltaMode}`); },
  getRules:         function(deltaMode = false) { return this.get(`/api/rules?delta_mode=${deltaMode}`); },
  getDatasetHealth: function(deltaMode = false) { return this.get(`/api/dataset-health?delta_mode=${deltaMode}`); },
  getRuleTrends:    function(id, deltaMode = false) { return this.get(`/api/trends/${id}?delta_mode=${deltaMode}`); },
  getRuleRegistry:  function(id) { return this.get(`/api/rules/${id}/registry`); },
  getDownloadUrl:   function(id, deltaMode = false) { return `${baseURL}/api/rules/${id}/download-failed-rows?delta_mode=${deltaMode}`; },
  getFailedRows:    function(id, page, pageSize, colorFilter = 'ALL', deltaMode = false) {
    return fetch(`${baseURL}/api/rules/${id}/failed-rows?page=${page}&page_size=${pageSize}&color_filter=${colorFilter}&delta_mode=${deltaMode}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}),
    }).then(r => { if (!r.ok) throw new Error(`API Error: ${r.statusText}`); return r.json(); });
  },
  getFailedRowsCount: function(id, deltaMode = false) {
    return fetch(`${baseURL}/api/rules/${id}/failed-rows-count?delta_mode=${deltaMode}`)
      .then(r => { if (!r.ok) throw new Error(`API Error: ${r.statusText}`); return r.json(); });
  },
  getDeltaRows:     function(id, sheet, page, pageSize, stgTable = null, toStgTable = null) {
    const stgParam   = stgTable   ? `&stg_table=${encodeURIComponent(stgTable)}`     : '';
    const toStgParam = toStgTable ? `&to_stg_table=${encodeURIComponent(toStgTable)}` : '';
    return fetch(`${baseURL}/api/rules/${id}/delta-rows?sheet=${sheet}&page=${page}&page_size=${pageSize}${stgParam}${toStgParam}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}),
    }).then(async r => {
      if (!r.ok) {
        const b = await r.json().catch(() => null);
        throw new Error(b?.detail || r.statusText);
      }
      return r.json();
    });
  },
  getStgTables:     function(id, deltaMode = false) {
    return fetch(`${baseURL}/api/rules/${id}/stg-tables?delta_mode=${deltaMode}`)
      .then(async r => {
        if (!r.ok) {
          const b = await r.json().catch(() => null);
          throw new Error(b?.detail || r.statusText);
        }
        return r.json();
      });
  },
  // ── Admin endpoints ──────────────────────────────────────────────────────
  getDatasetMappings: function() {
    return fetch(`${baseURL}/api/admin/dataset-mappings`)
      .then(r => { if (!r.ok) throw new Error(`API Error: ${r.statusText}`); return r.json(); });
  },
  createDatasetMapping: function(data) {
    return fetch(`${baseURL}/api/admin/dataset-mappings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    }).then(r => { if (!r.ok) throw new Error(`API Error: ${r.statusText}`); return r.json(); });
  },
  updateDatasetMapping: function(id, data) {
    return fetch(`${baseURL}/api/admin/dataset-mappings/${id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    }).then(r => { if (!r.ok) throw new Error(`API Error: ${r.statusText}`); return r.json(); });
  },
  deleteDatasetMapping: function(id) {
    return fetch(`${baseURL}/api/admin/dataset-mappings/${id}`, { method: 'DELETE' })
      .then(r => { if (!r.ok) throw new Error(`API Error: ${r.statusText}`); return r.json(); });
  },
  getDQTableData: function(tableType, page, pageSize) {
    return fetch(`${baseURL}/api/admin/dq-tables/${tableType}?page=${page}&page_size=${pageSize}`)
      .then(r => { if (!r.ok) throw new Error(`API Error: ${r.statusText}`); return r.json(); });
  },
  toggleIsActive: function(ruleId, isActive) {
    return fetch(`${baseURL}/api/admin/dq-tables/rule-input/toggle-active`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rule_id: ruleId, is_active: isActive }),
    }).then(async r => {
      if (!r.ok) {
        let detail = r.statusText;
        const b = await r.json().catch(() => null); if (b?.detail) detail = b.detail;
        throw new Error(detail);
      }
      return r.json();
    });
  },
  createDQRow: function(row) {
    return fetch(`${baseURL}/api/admin/dq-tables/rule-input/row`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ row }),
    }).then(async r => {
      if (!r.ok) {
        let detail = r.statusText;
        const b = await r.json().catch(() => null); if (b?.detail) detail = b.detail;
        throw new Error(detail);
      }
      return r.json();
    });
  },
  updateDQRow: function(pkCol, pkVal, updates) {
    return fetch(`${baseURL}/api/admin/dq-tables/rule-input/row`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ primary_key_col: pkCol, primary_key_val: pkVal, updates }),
    }).then(async r => {
      if (!r.ok) {
        let detail = r.statusText;
        const body = await r.json().catch(() => null); if (body?.detail) detail = body.detail;
        throw new Error(detail);
      }
      return r.json();
    });
  },
  // Multi-source: delegates to Snowflake / Denodo / PostgreSQL based on connection_type
  fetchDatabases: function(connectionType) {
    return fetch(`${baseURL}/api/admin/tables/databases?connection_type=${encodeURIComponent(connectionType)}`)
      .then(async r => {
        if (!r.ok) {
          let detail = r.statusText;
          const b = await r.json().catch(() => null); if (b?.detail) detail = b.detail;
          throw new Error(detail);
        }
        return r.json();
      });
  },
  fetchSchemas: function(connectionType, database) {
    return fetch(`${baseURL}/api/admin/tables/schemas?connection_type=${encodeURIComponent(connectionType)}&database=${encodeURIComponent(database)}`)
      .then(async r => {
        if (!r.ok) {
          let detail = r.statusText;
          const b = await r.json().catch(() => null); if (b?.detail) detail = b.detail;
          throw new Error(detail);
        }
        return r.json();
      });
  },
  fetchTableNames: function(connectionType, database, schema, search = '') {
    const q = search ? `&search=${encodeURIComponent(search)}` : '';
    return fetch(`${baseURL}/api/admin/tables/table-names?connection_type=${encodeURIComponent(connectionType)}&database=${encodeURIComponent(database)}&schema=${encodeURIComponent(schema)}${q}`)
      .then(async r => {
        if (!r.ok) {
          let detail = r.statusText;
          const b = await r.json().catch(() => null); if (b?.detail) detail = b.detail;
          throw new Error(detail);
        }
        return r.json();
      });
  },
  fetchTableColumns: function(connectionType, database, schema, table) {
    return fetch(`${baseURL}/api/admin/tables/columns?connection_type=${encodeURIComponent(connectionType)}&database=${encodeURIComponent(database)}&schema=${encodeURIComponent(schema)}&table=${encodeURIComponent(table)}`)
      .then(async r => {
        if (!r.ok) {
          let detail = r.statusText;
          const b = await r.json().catch(() => null); if (b?.detail) detail = b.detail;
          throw new Error(detail);
        }
        return r.json();
      });
  },
  fetchNextVersion: function(ruleId) {
    return fetch(`${baseURL}/api/admin/dq-tables/rule-input/next-version?rule_id=${encodeURIComponent(ruleId)}`)
      .then(r => { if (!r.ok) throw new Error(`API Error: ${r.statusText}`); return r.json(); });
  },
  // ── Date Input table CRUD ─────────────────────────────────────────────────
  createDateInputRow: function(row) {
    return fetch(`${baseURL}/api/admin/dq-tables/date-input/row`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ row }),
    }).then(async r => {
      if (!r.ok) { const b = await r.json().catch(() => null); throw new Error(b?.detail || r.statusText); }
      return r.json();
    });
  },
  updateDateInputRow: function(id, updates) {
    return fetch(`${baseURL}/api/admin/dq-tables/date-input/row`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, updates }),
    }).then(async r => {
      if (!r.ok) { const b = await r.json().catch(() => null); throw new Error(b?.detail || r.statusText); }
      return r.json();
    });
  },
  toggleDateInputActive: function(id, isActive) {
    return fetch(`${baseURL}/api/admin/dq-tables/date-input/toggle-active`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, is_active: isActive }),
    }).then(async r => {
      if (!r.ok) { const b = await r.json().catch(() => null); throw new Error(b?.detail || r.statusText); }
      return r.json();
    });
  },
  // ── Active date ranges — for the top bar context banner ──────────────────
  getActiveDateRanges: function() {
    return fetch(`${baseURL}/api/admin/date-input/active-ranges`)
      .then(async r => { if (!r.ok) throw new Error(`API Error: ${r.statusText}`); return r.json(); });
  },
  // ── Consolidated Report — Snowflake persistence ───────────────────────────
  getConsolidatedLatest: function(deltaMode = false) {
    return fetch(`${baseURL}/api/consolidated-report/latest?delta_mode=${deltaMode}`)
      .then(async r => { if (!r.ok) throw new Error(`API Error: ${r.statusText}`); return r.json(); });
  },
  getConsolidatedHistory: function(deltaMode = false) {
    return fetch(`${baseURL}/api/consolidated-report/history?delta_mode=${deltaMode}`)
      .then(async r => { if (!r.ok) throw new Error(`API Error: ${r.statusText}`); return r.json(); });
  },
  fetchConsolidatedReport: function(reportName, deltaMode = false) {
    return fetch(`${baseURL}/api/consolidated-report/fetch?report_name=${encodeURIComponent(reportName)}&delta_mode=${deltaMode}`)
      .then(async r => {
        if (!r.ok) {
          const b = await r.json().catch(() => null);
          throw new Error(b?.detail || r.statusText);
        }
        return r.json();
      });
  },
  prepareConsolidatedExcelUrl: function(reportName, deltaMode = false) {
    return `${baseURL}/api/consolidated-report/prepare-excel?report_name=${encodeURIComponent(reportName)}&delta_mode=${deltaMode}`;
  },
});

// ─── Helpers ──────────────────────────────────────────────────────────────────
const calculateDateDifference = (date1, date2) => {
  if (!date1 || !date2) return null;
  try {
    const d1 = new Date(date1), d2 = new Date(date2);
    if (isNaN(d1.getTime()) || isNaN(d2.getTime())) return null;
    return Math.abs(Math.ceil(Math.abs(d1 - d2) / (1000 * 60 * 60 * 24)));
  } catch { return null; }
};

// Parse COLOR_COLUMNS string from DQ_RULE_INPUT (e.g. "ACTUAL_SUBMISSION_DATE, EVENT_CREATED_DATE")
// Returns [col1, col2] trimmed + uppercased, or [null, null] if absent/invalid.
const parseColorColumns = (str) => {
  if (!str) return [null, null];
  const parts = str.split(',').map(s => s.trim().toUpperCase()).filter(Boolean);
  return [parts[0] || null, parts[1] || null];
};

// Converts a UTC datetime string from Snowflake (no offset suffix) to Eastern time display
const fmtEastern = (raw) => {
  if (!raw) return '';
  const iso = /[Zz]|[+-]\d{2}:?\d{2}$/.test(raw) ? raw : raw.replace(' ', 'T') + 'Z';
  return new Date(iso).toLocaleString('en-US', {
    timeZone: 'America/New_York',
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false
  }).replace(',', '');
};

// ─── Shared: Pass Rate Bar ────────────────────────────────────────────────────
const PassBar = ({ pct }) => {
  const color = pct >= 95 ? 'bg-jj-green-03' : pct >= 80 ? 'bg-jj-blue-03' : 'bg-jj-red';
  const text  = pct >= 95 ? 'text-jj-green-03' : pct >= 80 ? 'text-jj-blue-03' : 'text-jj-red';
  return (
    <div className="flex items-center gap-3">
      <div className="flex-1 bg-jj-gray-02 rounded-full h-2 overflow-hidden">
        <div className={`h-2 rounded-full transition-all duration-500 ${color}`} style={{ width: `${pct}%` }} />
      </div>
      <span className={`font-johnson-text text-sm font-bold w-12 text-right ${text}`}>{pct}%</span>
    </div>
  );
};

// ─── Shared: Severity Badge ───────────────────────────────────────────────────
const SeverityBadge = ({ severity }) => {
  const cfg = {
    CRITICAL: { cls: 'bg-red-50 text-jj-maroon-05 border-jj-maroon-05', icon: closeSrc,  filter: F.maroon },
    HIGH:     { cls: 'bg-orange-50 text-jj-orange-03 border-jj-orange-03', icon: alertSrc, filter: F.orange },
  }[severity] || { cls: 'bg-yellow-50 text-jj-gray-08 border-jj-yellow-01', icon: alertSrc, filter: F.gray08 };
  return (
    <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-johnson-text font-bold border ${cfg.cls}`}>
      <Ic src={cfg.icon} className="w-3 h-3" filter={cfg.filter} />
      {severity}
    </span>
  );
};

// ─── Shared: Status Badge ─────────────────────────────────────────────────────
const StatusBadge = ({ status }) => {
  const cfg = {
    ACTIVE:   { cls: 'bg-green-50 text-jj-green-03 border-jj-green-03', dot: 'bg-jj-green-03 animate-pulse' },
    HEALTHY:  { cls: 'bg-green-50 text-jj-green-03 border-jj-green-03', icon: checkSrc,  filter: F.green  },
    PASS:     { cls: 'bg-green-50 text-jj-green-03 border-jj-green-03', icon: checkSrc,  filter: F.green  },
    WARNING:  { cls: 'bg-yellow-50 text-yellow-700 border-jj-yellow-01', icon: alertSrc, filter: F.orange },
    CRITICAL: { cls: 'bg-red-50 text-jj-maroon-05 border-jj-maroon-05', icon: closeSrc, filter: F.maroon },
    FAIL:     { cls: 'bg-red-50 text-jj-red border-jj-red',             icon: closeSrc, filter: F.red    },
  }[status] || { cls: 'bg-jj-gray-01 text-jj-gray-06 border-jj-gray-03', dot: 'bg-jj-gray-04' };

  return (
    <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-johnson-text font-bold border ${cfg.cls}`}>
      {cfg.dot && <span className={`w-2 h-2 rounded-full ${cfg.dot}`} />}
      {cfg.icon && <Ic src={cfg.icon} className="w-3 h-3" filter={cfg.filter} />}
      {status}
    </span>
  );
};

// ─── Sorting utilities ────────────────────────────────────────────────────────
const SEVERITY_ORDER = { CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3 };
const HEALTH_ORDER   = { CRITICAL: 0, WARNING: 1, HEALTHY: 2 };
const REVIEW_ORDER   = { NOT_REVIEWED: 0, REJECTED: 1, ACCEPTED: 2 };

/** Minimal sort hook: returns { sortKey, sortDir, toggle } */
function useSortable(defaultKey = null, defaultDir = 'asc') {
  const [sortKey, setSortKey] = useState(defaultKey);
  const [sortDir, setSortDir] = useState(defaultDir);
  const toggle = (key) => {
    if (sortKey === key) setSortDir(d => d === 'asc' ? 'desc' : 'asc');
    else { setSortKey(key); setSortDir('asc'); }
  };
  return { sortKey, sortDir, toggle };
}

/**
 * Sortable <th> cell.
 * Shows a neutral sort icon when inactive; red chevron (↑/↓) when active.
 * The entire header text + icon is clickable.
 */
const SortableTh = ({ label, colKey, sortKey, sortDir, onSort, className = '' }) => {
  const active = sortKey === colKey;
  return (
    <th
      className={`px-5 py-3 text-left text-xs font-bold bg-jj-gray-01 uppercase tracking-wide whitespace-nowrap cursor-pointer select-none group ${className}`}
      onClick={() => onSort(colKey)}
    >
      <div className="flex items-center gap-1.5">
        <span className={`transition-colors ${active ? 'text-jj-red' : 'text-jj-gray-08 group-hover:text-jj-gray-06'}`}>
          {label}
        </span>
        <span className={`flex-shrink-0 transition-opacity ${active ? 'opacity-100' : 'opacity-20 group-hover:opacity-55'}`}>
          {active && sortDir === 'asc'
            ? <Ic src={chevronRightSrc} className="w-3 h-3 -rotate-90" filter={F.red} />
            : active && sortDir === 'desc'
              ? <Ic src={chevronRightSrc} className="w-3 h-3 rotate-90" filter={F.red} />
              : <Ic src={sortSrc} className="w-3 h-3" filter={F.gray06} />
          }
        </span>
      </div>
    </th>
  );
};

// ─── Empty State ──────────────────────────────────────────────────────────────
const EmptyState = ({ icon: iconSrc, title, message }) => (
  <div className="flex flex-col items-center justify-center py-16 px-6">
    <div className="w-16 h-16 rounded-full bg-jj-gray-01 border border-jj-gray-02 flex items-center justify-center mb-4">
      <Ic src={iconSrc} className="w-8 h-8" filter={F.gray05} />
    </div>
    <h3 className="font-johnson-display text-lg text-jj-gray-08 mb-1" style={{ lineHeight: '110%' }}>{title}</h3>
    <p className="font-johnson-text text-sm text-jj-gray-06 text-center max-w-sm">{message}</p>
  </div>
);

// ─── Section Card ─────────────────────────────────────────────────────────────
const ACCENT_FILTER = {
  'text-jj-red':      F.red,
  'text-jj-blue-03':  F.blue,
  'text-jj-violet-03': F.violet,
  'text-jj-green-03': F.green,
  'text-jj-gray-07':  F.gray07,
};

const SectionCard = ({ title, icon: iconSrc, children, accentColor = 'text-jj-red', headerRight, extraStyle }) => (
  <div className="bg-white rounded-xl border border-jj-gray-03 shadow-sm overflow-hidden" style={extraStyle}>
    <div className="px-5 py-3 border-b border-jj-gray-03 bg-jj-gray-01 flex items-center justify-between gap-2">
      <div className="flex items-center gap-2">
        <Ic src={iconSrc} className="w-4 h-4" filter={ACCENT_FILTER[accentColor] ?? F.red} />
        <h3 className="font-johnson-display text-base text-jj-gray-08" style={{ lineHeight: '110%' }}>{title}</h3>
      </div>
      {headerRight}
    </div>
    {children}
  </div>
);

// ─── Rule Select ──────────────────────────────────────────────────────────────
const RuleSelect = ({ rules, value, onChange, label = 'Select a Rule', hint }) => (
  <div className="bg-white rounded-xl border border-jj-gray-03 shadow-sm overflow-hidden">
    {/* Card header */}
    <div className="px-5 py-3 border-b border-jj-gray-02 bg-jj-gray-01 flex items-center gap-2">
      <Ic src={recordsSrc} className="w-4 h-4 flex-shrink-0" filter={F.red} />
      <div className="flex-1 min-w-0">
        <h3 className="font-johnson-display text-sm text-jj-gray-08" style={{ lineHeight: '110%' }}>{label}</h3>
        {hint && <p className="font-johnson-text text-xs text-jj-gray-05 mt-0.5">{hint}</p>}
      </div>
      <span className="flex-shrink-0 inline-flex items-center gap-1 px-2 py-1 rounded-full font-johnson-text text-[10px] font-bold text-jj-gray-06 bg-white border border-jj-gray-02">
        {rules.length} rule{rules.length !== 1 ? 's' : ''}
      </span>
    </div>

    <div className="px-5 py-4">
      <label className="font-johnson-text text-[10px] font-bold text-jj-gray-05 uppercase tracking-widest mb-1.5 flex items-center gap-1.5">
        <Ic src={chevronRightSrc} className="w-3 h-3" filter={F.gray05} />
        Choose a rule from the below dropdown to analyse
      </label>
      <div className="relative">
        <select
          value={value || ''}
          onChange={e => onChange(rules.find(r => r.rule_id === e.target.value))}
          className="w-full px-4 py-2.5 pr-9 border border-jj-gray-03 rounded-lg font-johnson-text text-sm text-jj-gray-08 bg-white outline-none focus:border-jj-red focus:ring-2 focus:ring-jj-red focus:ring-opacity-10 appearance-none transition-all"
        >
          {rules.map(rule => (
            <option key={rule.rule_id} value={rule.rule_id}>
              {rule.rule_id} | {rule.rule_title}
            </option>
          ))}
        </select>
        <Ic src={chevronRightSrc} className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 pointer-events-none rotate-90" filter={F.gray05} />
      </div>
    </div>
  </div>
);

// ═══════════════════════════════════════════════════════════════════════════════
// KPI Card
// ═══════════════════════════════════════════════════════════════════════════════
const KPI_COLOR_FILTER = {
  'text-jj-blue-03':   F.blue,
  'text-jj-violet-03': F.violet,
  'text-jj-green-03':  F.green,
  'text-jj-red':       F.red,
  'text-jj-gray-07':   F.gray07,
};

const KPICard = ({ title, value, icon: iconSrc, color, borderColor, compact = false }) => (
  <div
    className={`bg-white rounded-xl border border-jj-gray-02 border-l-4 shadow-sm group ${compact ? 'px-4 py-2.5' : 'p-4'}`}
    style={{ borderLeftColor: borderColor }}
  >
    <div className={`flex items-center justify-between ${compact ? 'gap-3' : 'mb-3'}`}>
      <p className="font-johnson-text text-xs font-bold text-jj-gray-06 uppercase tracking-wide">{title}</p>
      <div className={`rounded-lg bg-jj-gray-01 flex items-center justify-center group-hover:bg-jj-gray-02 transition-colors flex-shrink-0 ${compact ? 'w-6 h-6' : 'w-8 h-8'}`}>
        <Ic src={iconSrc} className={compact ? 'w-3 h-3' : 'w-4 h-4'} filter={KPI_COLOR_FILTER[color] ?? F.gray07} />
      </div>
    </div>
    {!compact && <p className={`font-johnson-display text-3xl font-bold ${color}`} style={{ lineHeight: '110%' }}>{value}</p>}
    {compact && (
      <p className={`font-johnson-display text-xl font-bold ${color}`} style={{ lineHeight: '110%' }}>{value}</p>
    )}
  </div>
);

// ═══════════════════════════════════════════════════════════════════════════════
// Admin — Inline editable table row
// ═══════════════════════════════════════════════════════════════════════════════

const DQ_TABLE_TABS = [
  { id: 'rule-input',    label: 'DQ Rule Governance',    icon: dashboardSrc },
  { id: 'rule-registry', label: 'DQ Rules AI Registry',  icon: dashboardSrc },
  { id: 'date-input',    label: 'DQ Date Input',          icon: dashboardSrc },
];


// Column sizing: content-fit with max cap, user-draggable override
const COL_MIN_WIDTH   = 60;
const COL_MAX_WIDTH   = 280;
const COL_CHAR_PX     = 7.5;  // approx px per char at text-xs monospace
const COL_H_PADDING   = 32;   // left + right cell padding
const COL_SAMPLE_ROWS = 50;   // rows sampled for width calc

function fitColumnWidths(columns, rows) {
  const widths = {};
  columns.forEach((col, ci) => {
    let maxChars = col.length; // seed with header length
    const sample = rows.slice(0, COL_SAMPLE_ROWS);
    for (const row of sample) {
      const cell = row[ci];
      if (cell === null || cell === undefined) continue;
      // For multi-line values use the longest line only
      const longest = cell.toString().split('\n')
        .reduce((a, b) => a.length > b.length ? a : b, '');
      if (longest.length > maxChars) maxChars = longest.length;
    }
    const raw = Math.round(maxChars * COL_CHAR_PX + COL_H_PADDING);
    widths[ci] = Math.min(COL_MAX_WIDTH, Math.max(COL_MIN_WIDTH, raw));
  });
  return widths;
}

const makeResizeHandler = (colWidths, setColWidths, dragRef) => (e, ci) => {
  e.preventDefault();
  const startX     = e.clientX;
  const startWidth = colWidths[ci] ?? COL_MAX_WIDTH;
  dragRef.current  = { ci, startX, startWidth };
  const onMove = ev => {
    if (!dragRef.current) return;
    const { ci, startX, startWidth } = dragRef.current;
    setColWidths(prev => ({ ...prev, [ci]: Math.max(COL_MIN_WIDTH, startWidth + (ev.clientX - startX)) }));
  };
  const onUp = () => {
    dragRef.current = null;
    window.removeEventListener('mousemove', onMove);
    window.removeEventListener('mouseup', onUp);
  };
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup', onUp);
};

function DQTableViewer({ api, tableType, isFullscreen, containerRef }) {
  const PAGE_SIZE = 50;
  const { user } = useAuth();
  const userEmail = user?.email || user?.username || 'unknown';
  const [data, setData]               = useState(null);
  const [loading, setLoading]         = useState(false);
  const [pageLoading, setPageLoading] = useState(false);
  const [error, setError]             = useState(null);
  const [currentPage, setCurrentPage] = useState(1);
  const [search, setSearch]           = useState('');
  const [wordWrap, setWordWrap]       = useState(false);
  const [expandedRows, setExpandedRows] = useState(new Set());
  const [colWidths, setColWidths]     = useState({});   // { colIndex: px }
  const [cellModal, setCellModal]     = useState(null);  // { col, value }
  const [editRowModal, setEditRowModal] = useState(null); // { rowIndex, draft }  — rule-input only
  const [createRuleModal, setCreateRuleModal] = useState(false); // rule-input only
  const [editMode, setEditMode]               = useState(false);  // rule-input edit toggle
  const [createDateModal, setCreateDateModal] = useState(false);  // date-input only
  const [pendingToggles, setPendingToggles]   = useState({});     // { [rowId]: 'pending'|'success'|'error' }
  const [sortColIdx, setSortColIdx]           = useState(null);   // column index being sorted, or null
  const [sortDir,    setSortDir]              = useState('asc');

  // Drag-to-resize state (stored in a ref to avoid stale closures in event handlers)
  const dragRef = useRef(null);

  // ── Column resize handler — shared factory (see makeResizeHandler above) ───
  const onResizeMouseDown = makeResizeHandler(colWidths, setColWidths, dragRef);

  // ── Data fetching ───────────────────────────────────────────────────────────
  const fetchPage = (page, isFirst = false, preserveOrder = null) => {
    isFirst ? setLoading(true) : setPageLoading(true);
    setError(null);
    return api.getDQTableData(tableType, page, PAGE_SIZE)
      .then(d => {
        let rows = d.rows || [];
        if (isFirst && tableType === 'date-input') {
          const idx = (d.columns || []).findIndex(c => String(c).toUpperCase() === 'IS_ACTIVE');
          if (idx !== -1) {
            const isActiveVal = v => {
              if (v === true) return true;
              if (v === false) return false;
              return String(v ?? '').trim().toUpperCase() === 'TRUE';
            };
            rows = [...rows].sort((a, b) => Number(isActiveVal(b[idx])) - Number(isActiveVal(a[idx])));
          }
        } else if (preserveOrder && tableType === 'date-input') {
          const idIdx = (d.columns || []).findIndex(c => String(c).toUpperCase() === 'ID');
          if (idIdx !== -1) {
            const orderRank = new Map(preserveOrder.map((id, idx) => [id, idx]));
            rows = [...rows].sort((a, b) => {
              const ai = orderRank.has(a[idIdx]) ? orderRank.get(a[idIdx]) : Infinity;
              const bi = orderRank.has(b[idIdx]) ? orderRank.get(b[idIdx]) : Infinity;
              return ai - bi;
            });
          }
        }
        setData({ ...d, rows }); setCurrentPage(d.page); setExpandedRows(new Set());
        setColWidths(fitColumnWidths(d.columns || [], rows));
      })
      .catch(() => setError('Failed to load table data. Check that this table is configured.'))
      .finally(() => { setLoading(false); setPageLoading(false); });
  };

  useEffect(() => {
    setData(null); setError(null);
    setCurrentPage(1); setSearch(''); setColWidths({}); setExpandedRows(new Set());
    setSortColIdx(null); setSortDir('asc');
    fetchPage(1, true);
  }, [tableType]);

  // Cleanup drag listeners if component unmounts mid-drag
  useEffect(() => () => { dragRef.current = null; }, []);

  const pageNums = () => {
    if (!data) return [];
    const total = data.total_pages;
    const start = Math.max(1, Math.min(currentPage - 2, total - 4));
    const end   = Math.min(total, start + 4);
    return Array.from({ length: end - start + 1 }, (_, i) => start + i);
  };

  const filteredRows = data?.rows?.filter(row =>
    !search.trim() || row.some(cell => cell && cell.toString().toLowerCase().includes(search.toLowerCase()))
  ) ?? [];

  const btnBase = 'px-3 py-1.5 rounded font-johnson-text text-xs font-bold transition-colors disabled:opacity-40 disabled:cursor-not-allowed';
  const btnGray = `${btnBase} bg-white text-jj-gray-07 hover:bg-jj-gray-01 border border-jj-gray-03`;
  const btnRed  = `${btnBase} bg-jj-red text-white border border-jj-red`;

  if (loading || !data) return <div className="flex items-center justify-center min-h-[300px]"><LoadingSpinner /></div>;
  if (error)            return <EmptyState icon={closeSrc} title="Error Loading Table" message={error} />;

  // ── Sort current page client-side ─────────────────────────────────────────
  const compareRaw = (a, b) => {
    if (a === null || a === undefined) return 1;
    if (b === null || b === undefined) return -1;
    const sa = String(a).trim(), sb = String(b).trim();
    const na = parseFloat(sa), nb = parseFloat(sb);
    if (!isNaN(na) && !isNaN(nb) && sa !== '' && sb !== '') return na - nb;
    return sa.localeCompare(sb, undefined, { numeric: true, sensitivity: 'base' });
  };
  const sortedRows = sortColIdx !== null
    ? [...filteredRows].sort((a, b) => {
        const cmp = compareRaw(a[sortColIdx], b[sortColIdx]);
        return sortDir === 'asc' ? cmp : -cmp;
      })
    : filteredRows;

  return (
    <div className="flex flex-col" style={{ minHeight: isFullscreen ? 0 : 'clamp(280px, 45vh, 600px)', ...(isFullscreen && { flex: 1, minHeight: 0 }) }}>

      {/* ── Toolbar ── */}
      <div className="flex-shrink-0 flex items-center gap-3 px-4 py-2.5 bg-jj-gray-01 border-b border-jj-gray-02 flex-wrap">

        {/* Stats pills */}
        <div className="flex items-center gap-2">
          <span className="inline-flex items-center gap-1 px-2 py-1 rounded bg-white border border-jj-gray-02 font-johnson-text text-xs text-jj-gray-06">
            <Ic src={dashboardSrc} className="w-3 h-3" filter={F.gray05} />{data?.columns?.length ?? 0} columns
          </span>
          <span className="inline-flex items-center gap-1 px-2 py-1 rounded bg-white border border-jj-gray-02 font-johnson-text text-xs text-jj-gray-06">
            <Ic src={recordsSrc} className="w-3 h-3" filter={F.gray05} />{data?.total_count?.toLocaleString()} rows
          </span>
        </div>

        {/* Search */}
        <div className="relative ml-auto">
          <input
            type="text"
            placeholder="Search this page…"
            value={search}
            onChange={e => setSearch(e.target.value)}
            className="w-52 pl-3 pr-8 py-1.5 rounded-lg bg-white text-jj-gray-08 placeholder-jj-gray-04 font-johnson-text text-xs border border-jj-gray-03 focus:outline-none focus:border-jj-red focus:ring-2 focus:ring-jj-red focus:ring-opacity-10 transition-colors"
          />
          {search
            ? <button onClick={() => setSearch('')} className="absolute right-2 top-1/2 -translate-y-1/2 text-jj-gray-04 hover:text-jj-gray-07">
                <Ic src={closeSrc} className="w-3.5 h-3.5" filter={F.gray07} />
              </button>
            : <Ic src={infoSrc} className="absolute right-2 top-1/2 -translate-y-1/2 w-3.5 h-3.5 pointer-events-none" filter={F.gray05} />
          }
        </div>

        {/* Rule-input actions */}
        {tableType === 'rule-input' && (
          <div className="flex items-center gap-2">

            {/* ── Edit Rules pill toggle ── */}
            <button
              onClick={() => {
                const next = !editMode;
                setEditMode(next);
                if (next) {
                  logAuditEvent(api.baseURL, userEmail, 'bdc_edit_mode_entered', tableType,
                    `Entered edit mode for "${tableType}" table`);
                }
              }}
              title={editMode ? 'Exit edit mode' : 'Click any row to edit it'}
              className="relative flex items-center gap-0 rounded-full border border-jj-gray-03 bg-jj-gray-01 overflow-hidden shadow-inner"
              style={{ padding: 0, height: '28px' }}
            >
              {/* Sliding pill */}
              <span
                className="absolute top-0.5 bottom-0.5 rounded-full transition-all duration-300 ease-in-out"
                style={{
                  left:  editMode ? 'calc(50% + 1px)' : '2px',
                  right: editMode ? '2px'              : 'calc(50% + 1px)',
                  background: editMode
                    ? 'linear-gradient(135deg, #0f68b2, #004685)'
                    : '#ffffff',
                  boxShadow: editMode
                    ? '0 1px 4px rgba(15,104,178,0.5)'
                    : '0 1px 3px rgba(0,0,0,0.12)',
                }}
              />
              {/* View label */}
              <span
                className="relative z-10 flex items-center gap-1 font-johnson-text text-[11px] font-bold transition-colors duration-300 px-3"
                style={{ color: editMode ? '#a39992' : '#312c2a' }}
              >
                <Ic src={infoSrc} className="w-3 h-3" filter={F.gray05} />
                View
              </span>
              {/* Edit label */}
              <span
                className="relative z-10 flex items-center gap-1 font-johnson-text text-[11px] font-bold transition-colors duration-300 px-3"
                style={{ color: editMode ? '#ffffff' : '#a39992' }}
              >
                <Ic src={editSrc} className="w-3 h-3" filter={F.white} />
                Edit
                {editMode && (
                  <span className="w-1.5 h-1.5 rounded-full bg-white opacity-80 animate-pulse" />
                )}
              </span>
            </button>

            {/* New Rule */}
            <button
              onClick={() => setCreateRuleModal(true)}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg font-johnson-text text-xs font-bold text-white bg-jj-red hover:bg-jj-maroon-05 transition-colors shadow-sm"
            >
              <Ic src={addSrc} className="w-3.5 h-3.5" filter={F.white} />
              New Rule
            </button>
          </div>
        )}

        {/* Date-input actions */}
        {tableType === 'date-input' && (
          <div className="flex items-center gap-2">
            {/* New Date Range */}
            <button
              onClick={() => setCreateDateModal(true)}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg font-johnson-text text-xs font-bold text-white bg-jj-red hover:bg-jj-maroon-05 transition-colors shadow-sm"
            >
              <Ic src={addSrc} className="w-3.5 h-3.5" filter={F.white} />
              New Date Range
            </button>
          </div>
        )}

        {/* Refresh */}
        <button
          onClick={() => fetchPage(currentPage, false)}
          disabled={pageLoading}
          className="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg bg-white hover:bg-jj-gray-02 text-jj-gray-07 font-johnson-text text-xs transition-colors disabled:opacity-50 border border-jj-gray-03"
        >
          <Ic src={refreshSrc} className={`w-3 h-3 ${pageLoading ? 'animate-spin' : ''}`} filter={F.gray07} />
          Refresh
        </button>
        {/* Fullscreen toggle */}
        <button
          onClick={() => {
            if (!document.fullscreenElement) {
              containerRef?.current?.requestFullscreen?.();
            } else {
              document.exitFullscreen?.();
            }
          }}
          title={isFullscreen ? 'Exit fullscreen (Esc)' : 'Enter fullscreen'}
          className="w-8 h-8 flex items-center justify-center rounded-lg bg-white border border-jj-gray-02 hover:bg-jj-gray-01 transition-colors flex-shrink-0"
        >
          {isFullscreen ? (
            <svg xmlns="http://www.w3.org/2000/svg" className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ color: '#6b6b6b' }}>
              <path d="M8 3v3a2 2 0 0 1-2 2H3m18 0h-3a2 2 0 0 1-2-2V3m0 18v-3a2 2 0 0 1 2-2h3M3 16h3a2 2 0 0 1 2 2v3" />
            </svg>
          ) : (
            <svg xmlns="http://www.w3.org/2000/svg" className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ color: '#6b6b6b' }}>
              <path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3" />
            </svg>
          )}
        </button>
      </div>

      {/* ── Table ── */}
      <div className="flex-1 relative bg-white flex flex-col" style={{ maxHeight: isFullscreen ? 'none' : 'clamp(320px, 55vh, 640px)', ...(isFullscreen && { overflow: 'hidden' }) }}>
        {pageLoading && (
          <div className="absolute inset-0 bg-white bg-opacity-70 flex items-center justify-center z-10">
            <LoadingSpinner />
          </div>
        )}
        <div className="flex-1 overflow-auto">
        <table style={{ tableLayout: 'fixed', width: 'max-content', borderCollapse: 'separate', borderSpacing: 0, minWidth: '100%' }}>
          <colgroup>
            {/* Row number column */}
            <col style={{ width: '3rem', minWidth: '3rem' }} />
            {(data?.columns || []).map((_, ci) => (
              <col key={ci} style={{ width: `${colWidths[ci] ?? COL_MAX_WIDTH}px` }} />
            ))}
          </colgroup>

          <thead>
            <tr style={{ position: 'sticky', top: 0, zIndex: 2 }}>

              {/* Row # + word-wrap toggle header */}
              <th
                style={{ position: 'sticky', left: 0, zIndex: 3, borderBottom: '2px solid #eb1700' }}
                className="bg-jj-gray-01 border-r border-jj-gray-03 w-12 p-0"
              >
                <div className="flex flex-col items-center justify-center h-full py-1 gap-0.5">
                  <span className="font-mono text-[10px] text-jj-gray-04 leading-none">#</span>
                  <button
                    onClick={() => setWordWrap(w => !w)}
                    title={wordWrap ? 'Collapse to single line' : 'Wrap cell text'}
                    className={`w-6 h-6 flex items-center justify-center rounded transition-colors ${
                      wordWrap
                        ? 'bg-jj-red text-white'
                        : 'text-jj-gray-05 hover:bg-jj-gray-02 hover:text-jj-gray-08'
                    }`}
                  >
                    <Ic src={recordsSrc} className="w-3.5 h-3.5" filter={wordWrap ? F.white : F.gray05} />
                  </button>
                </div>
              </th>


              {/* Column headers with resize handles */}
              {(data?.columns || []).map((col, ci) => (
                <th
                  key={col}
                  className="bg-jj-gray-01 border-r border-jj-gray-03 px-0 py-0 relative select-none group/col"
                  style={{ borderBottom: '2px solid #eb1700', width: `${colWidths[ci] ?? COL_MAX_WIDTH}px` }}
                >
                  {/* Click-to-sort label — stops before the 8px resize handle */}
                  <div
                    className="px-3 py-2.5 cursor-pointer hover:bg-jj-gray-02 transition-colors overflow-hidden"
                    style={{ paddingRight: '1.25rem' }}
                    onClick={() => {
                      if (sortColIdx === ci) setSortDir(d => d === 'asc' ? 'desc' : 'asc');
                      else { setSortColIdx(ci); setSortDir('asc'); }
                    }}
                  >
                    <div className="flex items-center gap-1 min-w-0">
                      <span
                        className={`font-mono text-xs font-bold uppercase tracking-wide block truncate flex-1 ${sortColIdx === ci ? 'text-jj-red' : 'text-jj-gray-08'}`}
                        title={col}
                      >
                        {col}
                      </span>
                      <span className={`flex-shrink-0 transition-opacity ${sortColIdx === ci ? 'opacity-100' : 'opacity-0 group-hover/col:opacity-40'}`}>
                        {sortColIdx === ci && sortDir === 'asc'
                          ? <Ic src={chevronRightSrc} className="w-2.5 h-2.5 -rotate-90" filter={F.red} />
                          : sortColIdx === ci && sortDir === 'desc'
                            ? <Ic src={chevronRightSrc} className="w-2.5 h-2.5 rotate-90" filter={F.red} />
                            : <Ic src={sortSrc} className="w-2.5 h-2.5" filter={F.gray06} />
                        }
                      </span>
                    </div>
                  </div>

                  {/* Resize handle — right edge, separate from sort click */}
                  <div
                    onMouseDown={e => onResizeMouseDown(e, ci)}
                    className="absolute top-0 right-0 h-full w-2 cursor-col-resize group/handle flex items-center justify-center z-10"
                    title="Drag to resize"
                  >
                    <div className="w-0.5 h-4 bg-jj-gray-03 rounded-full group-hover/handle:bg-jj-red group-hover/handle:h-full transition-all duration-150" />
                  </div>
                </th>
              ))}
            </tr>
          </thead>

          <tbody>
            {filteredRows.length === 0 ? (
              <tr>
                <td
                  colSpan={(data?.columns?.length || 0) + 1}
                  className="px-4 py-16 text-center font-johnson-text text-sm text-jj-gray-04 italic"
                >
                  {search ? `No rows matching "${search}" on this page.` : 'No rows found in this table.'}
                </td>
              </tr>
            ) : (
              sortedRows.map((row, i) => {
                const globalRow = (currentPage - 1) * PAGE_SIZE + i + 1;
                const isEven    = i % 2 === 0;
                return (
                  <tr
                    key={i}
                    onClick={() => {
                      if (tableType === 'rule-input') {
                        if (!editMode) return;
                        const draft = {};
                        data.columns.forEach((col, ci) => { draft[col] = row[ci] ?? ''; });
                        setEditRowModal({ rowIndex: i, originalRow: row, draft });
                        const pkCol = data.columns.find(c => c.toUpperCase() === 'RULE_ID');
                        const pkVal = pkCol ? row[data.columns.indexOf(pkCol)] : null;
                        logAuditEvent(api.baseURL, userEmail, 'bdc_rule_edit_attempted', String(pkVal ?? i),
                          `Opened edit modal for rule "${pkVal ?? `row ${i}`}" in ${tableType}`);
                      }
                    }}
                    className={`group/row transition-colors ${
                      tableType === 'rule-input' && editMode
                        ? 'cursor-pointer hover:bg-blue-50 hover:ring-1 hover:ring-jj-blue-03 hover:ring-inset'
                        : 'hover:bg-red-50'
                    }`}
                    style={{ backgroundColor: isEven ? '#ffffff' : '#fafaf9' }}
                  >
                    {/* Sticky row number + per-row expand toggle */}
                    <td
                      style={{ position: 'sticky', left: 0, zIndex: 1, backgroundColor: isEven ? '#f1efed' : '#e8e6e3' }}
                      className="border-b border-r border-jj-gray-02 select-none p-0"
                    >
                      <div className="flex flex-col items-center justify-start pt-1.5 pb-1 px-1 gap-0.5 h-full">
                        <span className="font-mono text-[10px] text-jj-gray-05 leading-none">{globalRow}</span>
                        <button
                          onClick={() => setExpandedRows(prev => {
                            const next = new Set(prev);
                            next.has(i) ? next.delete(i) : next.add(i);
                            return next;
                          })}
                          title={expandedRows.has(i) ? 'Collapse row' : 'Expand row'}
                          className={`w-5 h-5 flex items-center justify-center rounded transition-colors ${
                            expandedRows.has(i)
                              ? 'bg-jj-red text-white'
                              : 'text-jj-gray-04 hover:bg-jj-gray-02 hover:text-jj-gray-07'
                          }`}
                        >
                          <Ic src={recordsSrc} className={`w-3 h-3 transition-transform duration-150 ${expandedRows.has(i) ? 'rotate-90' : ''}`} filter={expandedRows.has(i) ? F.white : F.gray07} />
                        </button>
                      </div>
                    </td>


                    {/* Data cells */}
                    {row.map((cell, j) => {
                      const isNull    = cell === null || cell === undefined;
                      const cellText  = isNull ? '' : cell.toString();
                      const colW      = colWidths[j] ?? COL_MAX_WIDTH;
                      const colName   = data.columns[j]?.toUpperCase();
                      const isActiveCol = colName === 'IS_ACTIVE';
                      const pkCol     = data.columns.find(c => c.toUpperCase() === 'RULE_ID');
                      const pkVal     = pkCol ? row[data.columns.indexOf(pkCol)] : null;
                      const isActive  = cellText === 'True' || cellText === 'true' || cellText === 'TRUE';

                      // IS_ACTIVE cell — render toggle for rule-input (needs pkVal) and always for date-input
                      const isDateInput    = tableType === 'date-input';
                      const idCol          = isDateInput ? data.columns.find(c => c.toUpperCase() === 'ID') : null;
                      const rowId          = idCol ? row[data.columns.indexOf(idCol)] : null;
                      const showToggle     = isActiveCol && (
                        (tableType === 'rule-input' && pkVal) ||
                        (isDateInput && rowId)
                      );

                      if (showToggle) {
                        // date-input: toggle always clickable. rule-input: requires edit mode.
                        const activeEditMode = isDateInput ? true : editMode;
                        // Optimistic display: use pending state if mid-toggle
                        const toggleKey      = isDateInput ? rowId : pkVal;
                        const pendingState   = pendingToggles[toggleKey];
                        const displayActive  = (pendingState === 'pending' || pendingState === 'success') ? !isActive : isActive;
                        const isSuccess      = pendingState === 'success';
                        const isError        = pendingState === 'error';
                        return (
                          <td
                            key={j}
                            className="border-b border-r border-jj-gray-02 px-3 py-2.5 align-middle"
                            style={{ width: `${colW}px`, maxWidth: `${colW}px` }}
                          >
                            <button
                              onClick={async e => {
                                e.stopPropagation();
                                if (!activeEditMode || pendingToggles[toggleKey] === 'pending') return;
                                // Optimistic flip
                                setPendingToggles(p => ({ ...p, [toggleKey]: 'pending' }));
                                try {
                                  if (isDateInput) {
                                    await api.toggleDateInputActive(rowId, !isActive);
                                  } else {
                                    await api.toggleIsActive(pkVal, !isActive);
                                  }
                                  // Show success flash briefly, then refresh
                                  setPendingToggles(p => ({ ...p, [toggleKey]: 'success' }));
                                  // Build a human-readable details string for the audit log
                                  let auditDetails = `Rule "${pkVal}" ${!isActive ? 'activated' : 'deactivated'} in ${tableType}`;
                                  if (isDateInput) {
                                    const reportSetCol = data.columns.find(c => c.toUpperCase() === 'REPORT_SET');
                                    const fromCol      = data.columns.find(c => c.toUpperCase() === 'FROM_DATE');
                                    const toCol        = data.columns.find(c => c.toUpperCase() === 'TO_DATE');
                                    const reportSet    = reportSetCol ? row[data.columns.indexOf(reportSetCol)] : null;
                                    const fromDate     = fromCol ? row[data.columns.indexOf(fromCol)] : null;
                                    const toDate       = toCol   ? row[data.columns.indexOf(toCol)]   : null;
                                    if (reportSet || fromDate || toDate) {
                                      const period = [reportSet, fromDate && toDate ? `${String(fromDate).slice(0,10)} → ${String(toDate).slice(0,10)}` : null]
                                        .filter(Boolean).join(': ');
                                      auditDetails = `${!isActive ? 'Activated' : 'Deactivated'} — ${period}`;
                                    }
                                  }
                                  logAuditEvent(api.baseURL, userEmail, 'bdc_rule_toggled',
                                    String(toggleKey),
                                    auditDetails);
                                  setTimeout(() => {
                                    if (isDateInput) {
                                      // Refetch (another row may have been auto-deactivated server-side
                                      // for the same REPORT_SET) but keep the rows in their current order.
                                      const idIdx = data.columns.indexOf(idCol);
                                      const orderSnapshot = data.rows.map(r => r[idIdx]);
                                      fetchPage(currentPage, false, orderSnapshot).then(() => {
                                        setPendingToggles(p => { const n = { ...p }; delete n[toggleKey]; return n; });
                                      });
                                    } else {
                                      fetchPage(currentPage, false).then(() => {
                                        setPendingToggles(p => { const n = { ...p }; delete n[toggleKey]; return n; });
                                      });
                                    }
                                  }, 900);
                                } catch (err) {
                                  console.error('Toggle failed:', err.message);
                                  // Revert optimistic update, show error flash
                                  setPendingToggles(p => ({ ...p, [toggleKey]: 'error' }));
                                  setTimeout(() => {
                                    setPendingToggles(p => { const n = { ...p }; delete n[toggleKey]; return n; });
                                  }, 1500);
                                }
                              }}
                              title={!activeEditMode ? 'Switch to Edit mode to change status' : displayActive ? 'Click to deactivate' : 'Click to activate'}
                              className={`flex items-center gap-1.5 transition-opacity ${
                                !activeEditMode ? 'cursor-default opacity-60' :
                                pendingToggles[toggleKey] === 'pending' ? 'cursor-wait opacity-80' :
                                'cursor-pointer hover:opacity-80'
                              }`}
                            >
                              {/* Track */}
                              <span
                                className="relative inline-flex flex-shrink-0 rounded-full transition-all duration-300"
                                style={{
                                  width: '32px', height: '18px',
                                  background: isError ? '#c0392b' : displayActive ? '#328714' : '#d5cfc9',
                                  boxShadow: isSuccess ? '0 0 0 3px rgba(50,135,20,0.35)' :
                                             isError   ? '0 0 0 3px rgba(192,57,43,0.35)' : 'none',
                                  transition: 'background 0.3s, box-shadow 0.3s',
                                }}
                              >
                                {/* Thumb */}
                                <span
                                  className="absolute top-0.5 rounded-full bg-white shadow transition-all duration-300"
                                  style={{ width: '14px', height: '14px', left: displayActive ? '16px' : '2px' }}
                                />
                                {/* Pending spinner ring */}
                                {pendingToggles[toggleKey] === 'pending' && (
                                  <span className="absolute inset-0 rounded-full border-2 border-white border-opacity-60 animate-spin"
                                    style={{ borderTopColor: 'transparent' }} />
                                )}
                              </span>
                              <span className={`font-johnson-text text-[11px] font-bold transition-colors ${
                                isError        ? 'text-jj-red' :
                                isSuccess      ? (displayActive ? 'text-jj-green-03' : 'text-jj-gray-05') :
                                displayActive  ? 'text-jj-green-03' : 'text-jj-gray-05'
                              }`}>
                                {isError   ? 'Failed!' :
                                 pendingToggles[toggleKey] === 'pending' ? '…' :
                                 isSuccess  ? (displayActive ? '✓ Active' : '✓ Inactive') :
                                 displayActive ? 'Active' : 'Inactive'}
                              </span>
                            </button>
                          </td>
                        );
                      }

                      return (
                        <td
                          key={j}
                          className="border-b border-r border-jj-gray-02 px-3 py-2.5 align-top group/cell relative"
                          style={{ width: `${colW}px`, maxWidth: `${colW}px` }}
                          title={isNull ? undefined : cellText}
                          onClick={() => { if (editMode) return; if (!isNull) setCellModal({ col: data.columns[j], value: cellText }); }}
                        >
                          {isNull ? (
                            <span className="font-mono text-[10px] text-jj-gray-04 bg-jj-gray-01 px-1.5 py-0.5 rounded border border-jj-gray-02 italic">
                              NULL
                            </span>
                          ) : (
                            <span
                              className="font-johnson-text text-xs text-jj-gray-08 block cursor-pointer group-hover/cell:text-jj-red transition-colors"
                              style={(wordWrap || expandedRows.has(i))
                                ? { whiteSpace: 'pre-wrap', wordBreak: 'break-word' }
                                : { whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }
                              }
                            >
                              {cellText}
                            </span>
                          )}
                        </td>
                      );
                    })}
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
        </div>{/* end inner overflow-auto */}
      </div>

      {/* ── Footer / pagination ── */}
      <div className="flex-shrink-0 flex items-center justify-between gap-4 px-4 py-2.5 bg-jj-gray-01 border-t border-jj-gray-02 flex-wrap">
        <div className="flex items-center gap-3 font-johnson-text text-xs text-jj-gray-06">
          {search && (
            <span><b className="text-jj-gray-08">{filteredRows.length}</b> match{filteredRows.length !== 1 ? 'es' : ''} · </span>
          )}
          <span>
            Rows{' '}
            <b className="text-jj-gray-08">{((currentPage - 1) * PAGE_SIZE + 1).toLocaleString()}</b>
            {' – '}
            <b className="text-jj-gray-08">{Math.min(currentPage * PAGE_SIZE, data.total_count).toLocaleString()}</b>
            {' of '}
            <b className="text-jj-gray-08">{data.total_count.toLocaleString()}</b>
          </span>
          <span className="text-jj-gray-03">·</span>
          <span>Page <b className="text-jj-gray-08">{currentPage}</b> / <b className="text-jj-gray-08">{data.total_pages}</b></span>
          {wordWrap && (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-jj-red bg-opacity-10 text-jj-red text-[10px] font-bold border border-jj-red border-opacity-20">
              <Ic src={recordsSrc} className="w-3 h-3" filter={F.red} />
              Wrap on
            </span>
          )}
        </div>

        <div className="flex items-center gap-1">
          <button className={btnGray} disabled={currentPage === 1 || pageLoading} onClick={() => fetchPage(1, false)}>« First</button>
          <button className={btnGray} disabled={currentPage === 1 || pageLoading} onClick={() => fetchPage(currentPage - 1, false)}>
            <Ic src={chevronLeftSrc} className="w-3.5 h-3.5" filter={F.gray07} />
          </button>
          {pageNums().map(p => (
            <button key={p} className={p === currentPage ? btnRed : btnGray} disabled={pageLoading} onClick={() => fetchPage(p, false)}>{p}</button>
          ))}
          <button className={btnGray} disabled={currentPage === data.total_pages || pageLoading} onClick={() => fetchPage(currentPage + 1, false)}>
            <Ic src={chevronRightSrc} className="w-3.5 h-3.5" filter={F.gray07} />
          </button>
          <button className={btnGray} disabled={currentPage === data.total_pages || pageLoading} onClick={() => fetchPage(data.total_pages, false)}>Last »</button>
        </div>
      </div>

      {/* ── Cell content modal ── */}
      {cellModal && ReactDOM.createPortal(
        <div
          className="fixed inset-0 bg-black bg-opacity-40 flex items-center justify-center z-50 p-4"
          style={{ backdropFilter: 'blur(3px)' }}
          onClick={() => setCellModal(null)}
        >
          <div
            className="bg-white rounded-2xl shadow-2xl border border-jj-gray-02 w-full flex flex-col overflow-hidden"
            style={{ maxWidth: '640px', maxHeight: '70vh', animation: 'slideUp 0.2s ease-out' }}
            onClick={e => e.stopPropagation()}
          >
            {/* Header */}
            <div className="flex-shrink-0 px-5 py-3 bg-white border-b-4 border-jj-red flex items-center justify-between gap-3">
              <div className="flex items-center gap-2 min-w-0">
                <Ic src={recordsSrc} className="w-4 h-4 flex-shrink-0" filter={F.red} />
                <span className="font-mono text-xs font-bold text-jj-gray-08 uppercase tracking-wide truncate">
                  {cellModal.col}
                </span>
              </div>
              <button
                onClick={() => setCellModal(null)}
                className="w-8 h-8 flex items-center justify-center rounded-lg bg-jj-gray-01 hover:bg-jj-gray-02 transition-colors flex-shrink-0"
              >
                <Ic src={closeSrc} className="w-4 h-4" filter={F.gray07} />
              </button>
            </div>

            {/* Content */}
            <div className="flex-1 overflow-y-auto p-5">
              <pre className="font-johnson-text text-sm text-jj-gray-08 whitespace-pre-wrap break-words leading-relaxed">
                {cellModal.value}
              </pre>
            </div>

            {/* Footer */}
            <div className="flex-shrink-0 px-5 py-3 bg-jj-gray-01 border-t border-jj-gray-02 flex items-center justify-between gap-3">
              <span className="font-johnson-text text-xs text-jj-gray-05">
                {cellModal.value.length.toLocaleString()} character{cellModal.value.length !== 1 ? 's' : ''}
              </span>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => { navigator.clipboard?.writeText(cellModal.value); }}
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg font-johnson-text text-xs font-bold text-jj-gray-07 bg-white hover:bg-jj-gray-02 transition-colors border border-jj-gray-03"
                >
                  Copy
                </button>
                <button
                  onClick={() => setCellModal(null)}
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg font-johnson-text text-xs font-bold text-white bg-jj-red hover:bg-jj-maroon-05 transition-colors"
                >
                  Close
                </button>
              </div>
            </div>
          </div>
        </div>,
        document.body
      )}

      {/* ── Edit Row Modal — rule-input only ── */}
      {editRowModal && ReactDOM.createPortal(
        <EditRowModal
          columns={data.columns}
          draft={editRowModal.draft}
          api={api}
          onDraftChange={newDraft => setEditRowModal(prev => ({ ...prev, draft: newDraft }))}
          onSave={async () => {
            // Determine primary key — prefer RULE_ID, fall back to first column
            const pkCol = data.columns.find(c => c.toUpperCase() === 'RULE_ID') || data.columns[0];
            const pkVal = editRowModal.originalRow[data.columns.indexOf(pkCol)];
            // Build updates — only changed fields
            const updates = {};
            data.columns.forEach((col, ci) => {
              const original = editRowModal.originalRow[ci] ?? '';
              if (editRowModal.draft[col] !== original && col.toUpperCase() !== pkCol.toUpperCase()) {
                updates[col] = editRowModal.draft[col];
              }
            });
            if (!Object.keys(updates).length) {
              setEditRowModal(null);
              return;
            }
            await api.updateDQRow(pkCol, pkVal, updates);
            logAuditEvent(api.baseURL, userEmail, 'bdc_rule_row_updated', String(pkVal),
              `Updated rule row "${pkVal}" in ${tableType}: changed fields [${Object.keys(updates).join(', ')}]`);
            // Refresh current page to reflect saved changes
            fetchPage(currentPage, false);
            setEditRowModal(null);
          }}
          onClose={() => setEditRowModal(null)}
        />,
        document.body
      )}
      {createRuleModal && ReactDOM.createPortal(
        <CreateRuleModal
          columns={data.columns}
          api={api}
          onSaved={() => { fetchPage(currentPage, false); setCreateRuleModal(false); }}
          onClose={() => setCreateRuleModal(false)}
        />,
        document.body
      )}
      {createDateModal && ReactDOM.createPortal(
        <DateInputCreateModal
          api={api}
          onSaved={() => { fetchPage(currentPage, false); setCreateDateModal(false); }}
          onClose={() => setCreateDateModal(false)}
        />,
        document.body
      )}
    </div>
  );
}


// ═══════════════════════════════════════════════════════════════════════════════
// Create Rule Modal — DQ Rule Input table
// ═══════════════════════════════════════════════════════════════════════════════

// Derives a RULE_ID slug from a free-text rule title (e.g. "Timeliness - Tiger" → "TIMELINESS_TIGER")
const slugifyRuleId = (title) => (title || '')
  .toUpperCase()
  .replace(/[^A-Z0-9]+/g, '_')
  .replace(/^_+|_+$/g, '');

const CREATE_AUTO_COLS = new Set(['CREATED_TS','CREATED_AT','UPDATED_TS','UPDATED_AT','IS_ACTIVE','RULE_TYPE']);
const CREATE_LONG_COLS = new Set(['RULE_DESCRIPTION','DESCRIPTION','SQL_PREDICATE','SQL','NOTES','PREDICATE']);

const FIELD_DROPDOWNS = {
  CONNECTION_TYPE: ['Snowflake', 'Denodo - Coming Soon', 'PostgreSQL'],
  DATASET_TITLE:   ['RightTime', 'Tiger', 'Custom'],
  SEVERITY:        ['HIGH', 'MEDIUM', 'LOW', 'NONE'],
};

function TableBrowserField({ api, value, onChange, connectionType }) {
  const [databases, setDatabases]                 = useState([]);
  const [dbLoading, setDbLoading]                 = useState(false);
  const [dbError, setDbError]                     = useState(null);
  const [schemas, setSchemas]                     = useState([]);
  const [schemaLoading, setSchemaLoading]         = useState(false);
  const [tableNames, setTableNames]               = useState([]);
  const [tableNamesLoading, setTableNamesLoading] = useState(false);
  const [tableSearch, setTableSearch]             = useState('');
  const [tableDropdownOpen, setTableDropdownOpen] = useState(false);
  const [selectedDb, setSelectedDb]               = useState(null);
  const [selectedSchema, setSelectedSchema]       = useState(null);
  const tableSearchRef = useRef(null);
  const dropdownRef    = useRef(null);

  // Reset state when connectionType changes (but don't fetch yet)
  useEffect(() => {
    setSelectedDb(null); setSelectedSchema(null); setDatabases([]); setSchemas([]); setTableNames([]); setDbError(null);
  }, [connectionType]);

  // Fetch databases lazily — only when the user opens the table dropdown
  useEffect(() => {
    if (!tableDropdownOpen || databases.length > 0 || dbLoading || dbError) return;
    setDbLoading(true);
    const connType = connectionType || 'Snowflake';
    api.fetchDatabases(connType)
      .then(d => setDatabases(d.databases || []))
      .catch(err => {
        setDatabases([]);
        setDbError(err.message || 'Failed to connect');
      })
      .finally(() => setDbLoading(false));
  }, [tableDropdownOpen, connectionType]);

  // Load schemas when a database is selected
  useEffect(() => {
    if (!selectedDb) return;
    setSchemas([]); setTableNames([]); setSelectedSchema(null);
    setSchemaLoading(true);
    const connType = connectionType || 'Snowflake';
    api.fetchSchemas(connType, selectedDb)
      .then(d => setSchemas(d.schemas || []))
      .catch(() => setSchemas([]))
      .finally(() => setSchemaLoading(false));
  }, [selectedDb, connectionType]);

  // Load tables when a schema is selected
  useEffect(() => {
    if (!selectedDb || !selectedSchema) return;
    setTableNames([]);
    setTableNamesLoading(true);
    const connType = connectionType || 'Snowflake';
    api.fetchTableNames(connType, selectedDb, selectedSchema)
      .then(d => setTableNames(d.tables || []))
      .catch(() => setTableNames([]))
      .finally(() => setTableNamesLoading(false));
  }, [selectedDb, selectedSchema, connectionType]);

  // Close dropdown when clicking outside
  useEffect(() => {
    if (!tableDropdownOpen) return;
    const handler = e => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target)) setTableDropdownOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [tableDropdownOpen]);

  const isSearching = tableSearch.trim().length > 0;
  const flatFiltered = isSearching && tableNames.length > 0
    ? tableNames.filter(t =>
        t.table_name.toLowerCase().includes(tableSearch.toLowerCase()) ||
        t.full_name.toLowerCase().includes(tableSearch.toLowerCase())
      )
    : tableNames;

  return (
    <div className="relative" ref={dropdownRef}>
      <button
        type="button"
        onClick={() => { setTableDropdownOpen(o => !o); setTableSearch(''); setTimeout(() => tableSearchRef.current?.focus(), 60); }}
        className={`${INPUT_EDIT_CLS} text-left flex items-center gap-2 pr-3`}
      >
        {value ? (
          <div className="flex items-center gap-1.5 min-w-0 flex-1">
            {value.split('.').map((part, pi, arr) => (
              <span key={pi} className="flex items-center gap-1.5 min-w-0">
                <span className={`font-mono text-xs truncate ${pi === 0 ? 'text-jj-gray-05' : pi === 1 ? 'text-jj-gray-06' : 'font-bold text-jj-gray-08'}`}>{part}</span>
                {pi < arr.length - 1 && <Ic src={chevronRightSrc} className="w-3 h-3 flex-shrink-0" filter={F.gray07} />}
              </span>
            ))}
          </div>
        ) : (
          <span className="font-johnson-text text-sm text-jj-gray-04 italic flex-1">Select a table…</span>
        )}
        <Ic src={chevronRightSrc} className={`w-4 h-4 flex-shrink-0 transition-transform duration-200 ${tableDropdownOpen ? 'rotate-90' : ''}`} filter={F.gray05} />
      </button>
      {tableDropdownOpen && (
        <div className="absolute left-0 right-0 mt-1 bg-white border border-jj-gray-03 rounded-xl shadow-2xl z-50 flex flex-col overflow-hidden" style={{ maxHeight: 'min(340px, 40vh)' }}>
          {/* Search */}
          <div className="flex-shrink-0 px-3 py-2 border-b border-jj-gray-02 bg-jj-gray-01">
            <div className="relative">
              <input
                ref={tableSearchRef} type="text"
                placeholder={!selectedDb ? 'Select a database first…' : !selectedSchema ? 'Select a schema first…' : `Search ${tableNames.length} tables…`}
                value={tableSearch} onChange={e => setTableSearch(e.target.value)} disabled={!selectedSchema}
                className="w-full pl-8 pr-8 py-1.5 text-xs font-johnson-text border border-jj-gray-02 rounded-lg bg-white text-jj-gray-08 placeholder-jj-gray-04 outline-none focus:border-jj-red focus:ring-2 focus:ring-jj-red focus:ring-opacity-10 transition-all disabled:bg-jj-gray-01 disabled:cursor-not-allowed"
              />
              <Ic src={infoSrc} className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 pointer-events-none" filter={F.gray05} />
              {tableSearch && (
                <button onClick={() => setTableSearch('')} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-jj-gray-04 hover:text-jj-gray-07">
                  <Ic src={closeSrc} className="w-3 h-3" filter={F.gray07} />
                </button>
              )}
            </div>
          </div>

          {/* 3-column browser */}
          <div className="flex flex-1 min-h-0 overflow-hidden" style={{ height: 'min(260px, 30vh)' }}>

            {/* Col 1: Database */}
            <div className="w-40 flex-shrink-0 border-r border-jj-gray-02 flex flex-col">
              <div className="flex-shrink-0 px-3 py-1.5 bg-jj-gray-01 border-b border-jj-gray-02 flex items-center justify-between">
                <span className="font-johnson-text text-[10px] font-bold text-jj-gray-05 uppercase tracking-wide">Database</span>
                {connectionType && connectionType !== 'Snowflake' && (
                  <span className="font-mono text-[9px] font-bold px-1.5 py-0.5 rounded border bg-jj-gray-01 text-jj-gray-06 border-jj-gray-02 ml-auto">
                    {connectionType.toUpperCase()}
                  </span>
                )}
                {dbLoading && <Ic src={activitySrc} className="w-3 h-3 animate-spin" filter={F.gray05} />}
              </div>
              <div className="flex-1 overflow-y-auto">
                {dbLoading ? (
                  <div className="flex items-center justify-center py-4"><LoadingSpinner /></div>
                ) : dbError ? (
                  <div className="px-3 py-3 font-johnson-text text-xs text-jj-maroon-05 leading-tight">{dbError}</div>
                ) : databases.length === 0 ? (
                  <p className="px-3 py-3 font-johnson-text text-xs text-jj-gray-04 italic">None found</p>
                ) : databases.map(db => {
                  const isActive = db === selectedDb;
                  return (
                    <button key={db} type="button"
                      onClick={() => setSelectedDb(db)}
                      className={`w-full text-left px-3 py-2 flex items-center gap-2 border-b border-jj-gray-02 transition-colors ${
                        isActive ? 'bg-blue-50 border-l-2 border-l-jj-blue-03' : 'hover:bg-jj-gray-01'
                      }`}
                    >
                      <Ic src={databaseSrc} className="w-3 h-3 flex-shrink-0" filter={F.blue} />
                      <span className={`font-mono text-xs truncate ${isActive ? 'text-jj-blue-03 font-bold' : 'text-jj-gray-07'}`}>{db}</span>
                    </button>
                  );
                })}
              </div>
            </div>

            {/* Col 2: Schema */}
            <div className="w-44 flex-shrink-0 border-r border-jj-gray-02 flex flex-col">
              <div className="flex-shrink-0 px-3 py-1.5 bg-jj-gray-01 border-b border-jj-gray-02 flex items-center justify-between">
                <span className="font-johnson-text text-[10px] font-bold text-jj-gray-05 uppercase tracking-wide">Schema</span>
                {schemaLoading && <Ic src={activitySrc} className="w-3 h-3 animate-spin" filter={F.gray05} />}
              </div>
              <div className="flex-1 overflow-y-auto">
                {!selectedDb ? (
                  <p className="px-3 py-3 font-johnson-text text-xs text-jj-gray-04 italic">← Pick a database</p>
                ) : schemaLoading ? (
                  <div className="flex items-center justify-center py-4"><LoadingSpinner /></div>
                ) : schemas.length === 0 ? (
                  <p className="px-3 py-3 font-johnson-text text-xs text-jj-gray-04 italic">No schemas</p>
                ) : schemas.map(schema => {
                  const isActive = schema === selectedSchema;
                  return (
                    <button key={schema} type="button"
                      onClick={() => setSelectedSchema(schema)}
                      className={`w-full text-left px-3 py-2 flex items-center gap-2 border-b border-jj-gray-02 transition-colors ${
                        isActive ? 'bg-red-50 border-l-2 border-l-jj-red' : 'hover:bg-jj-gray-01'
                      }`}
                    >
                      <Ic src={dashboardSrc} className="w-3 h-3 flex-shrink-0" filter={F.red} />
                      <span className={`font-mono text-xs truncate ${isActive ? 'text-jj-red font-bold' : 'text-jj-gray-07'}`}>{schema}</span>
                    </button>
                  );
                })}
              </div>
            </div>

            {/* Col 3: Tables */}
            <div className="flex-1 min-w-0 flex flex-col">
              <div className="flex-shrink-0 px-3 py-1.5 bg-jj-gray-01 border-b border-jj-gray-02 flex items-center justify-between">
                <span className="font-johnson-text text-[10px] font-bold text-jj-gray-05 uppercase tracking-wide">Table</span>
                {tableNamesLoading
                  ? <Ic src={activitySrc} className="w-3 h-3 animate-spin" filter={F.gray05} />
                  : tableNames.length > 0 && <span className="font-johnson-text text-[10px] text-jj-gray-04">{flatFiltered.length}</span>
                }
              </div>
              <div className="flex-1 overflow-y-auto">
                {!selectedSchema ? (
                  <p className="px-3 py-3 font-johnson-text text-xs text-jj-gray-04 italic">
                    {!selectedDb ? '← Pick a database' : '← Pick a schema'}
                  </p>
                ) : tableNamesLoading ? (
                  <div className="flex items-center justify-center py-4"><LoadingSpinner /></div>
                ) : flatFiltered.length === 0 ? (
                  <p className="px-3 py-3 font-johnson-text text-xs text-jj-gray-04 italic">
                    {isSearching ? `No match for "${tableSearch}"` : 'No tables'}
                  </p>
                ) : flatFiltered.map(t => {
                  const isSelected = value === t.full_name;
                  return (
                    <button key={t.full_name} type="button"
                      onClick={() => { onChange(t.full_name); setTableDropdownOpen(false); setTableSearch(''); }}
                      className={`w-full text-left px-3 py-2 flex items-center justify-between gap-2 border-b border-jj-gray-02 transition-colors ${
                        isSelected ? 'bg-red-50' : 'hover:bg-jj-gray-01'
                      }`}
                    >
                      <span className={`font-mono text-xs truncate ${isSelected ? 'text-jj-red font-bold' : 'text-jj-gray-08'}`}>
                        {t.table_name}
                      </span>
                      {isSelected && <Ic src={checkSrc} className="w-3.5 h-3.5 flex-shrink-0" filter={F.red} />}
                    </button>
                  );
                })}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
function ColorColumnsField({ api, value, onChange, connectionType, targetTable }) {
  const [columns, setColumns] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError]     = useState(null);
  const [openIdx, setOpenIdx] = useState(null); // which picker (0/1) is open
  const [dropUp, setDropUp]   = useState(false);
  const [panelMax, setPanelMax] = useState(220);
  const wrapRef = useRef(null);
  const btnRefs = useRef([null, null]);

  const toggleOpen = (idx) => {
    if (openIdx === idx) { setOpenIdx(null); return; }
    const btn = btnRefs.current[idx];
    if (btn) {
      const rect = btn.getBoundingClientRect();
      const spaceBelow = window.innerHeight - rect.bottom - 16;
      const spaceAbove = rect.top - 16;
      const flip = spaceBelow < 160 && spaceAbove > spaceBelow;
      setDropUp(flip);
      setPanelMax(Math.max(120, Math.min(220, flip ? spaceAbove : spaceBelow)));
    }
    setOpenIdx(idx);
  };

  // TARGET_TABLE is stored as "DATABASE.SCHEMA.TABLE_NAME"
  const parts = (targetTable || '').split('.').filter(Boolean);
  const [db, schema, table] = parts.length >= 3 ? parts.slice(-3) : [null, null, null];
  const tableKey = (db && schema && table) ? `${db}.${schema}.${table}` : null;
  const prevTableKey = useRef(tableKey);

  useEffect(() => {
    setColumns([]); setError(null);
    if (!db || !schema || !table) return;
    setLoading(true);
    const connType = connectionType || 'Snowflake';
    api.fetchTableColumns(connType, db, schema, table)
      .then(d => {
        const cols = d.columns || [];
        setColumns(cols);
        if (cols.length === 0) onChange(''); // nothing to pick from — clear any stale selection
      })
      .catch(err => setError(err.message || 'Failed to load columns'))
      .finally(() => setLoading(false));
  }, [db, schema, table, connectionType]);

  // Clear the selection whenever the target table actually changes
  // (but not on first mount, so an existing saved value is preserved when opening Edit)
  useEffect(() => {
    if (prevTableKey.current !== tableKey) {
      prevTableKey.current = tableKey;
      onChange('');
    }
  }, [tableKey]);

  // Close on outside click
  useEffect(() => {
    if (openIdx === null) return;
    const handler = e => { if (wrapRef.current && !wrapRef.current.contains(e.target)) setOpenIdx(null); };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [openIdx]);

  // Date/timestamp columns are the common case, but don't hard-block other types
  // (some tables store dates as NUMBER/VARCHAR) — just sort them to the top.
  const isDateType = (type) => /DATE|TIME/i.test(type || '');
  const sortedColumns = [...columns].sort((a, b) => {
    const aDate = isDateType(a.type);
    const bDate = isDateType(b.type);
    if (aDate === bDate) return a.name.localeCompare(b.name);
    return aDate ? -1 : 1;
  });

  // Resolve the stored value against the fetched column list case-insensitively,
  // so the picker's selection always lines up exactly with a fetched column
  // (column names aren't guaranteed to be uppercase in the source table).
  const resolveCol = (raw) => {
    if (!raw) return null;
    const match = sortedColumns.find(c => c.name.toUpperCase() === raw.toUpperCase());
    return match || { name: raw, type: null };
  };
  const rawParts = (value || '').split(',').map(s => s.trim()).filter(Boolean);
  const col1 = resolveCol(rawParts[0]);
  const col2 = resolveCol(rawParts[1]);

  const setCol = (idx, colName) => {
    const next = [col1?.name || '', col2?.name || ''];
    next[idx] = colName;
    onChange(next.filter(Boolean).join(', '));
    setOpenIdx(null);
  };

  if (!db || !schema || !table) {
    return (
      <p className="font-johnson-text text-xs text-jj-gray-04 italic px-1 py-2">
        Select a target table first to choose color columns.
      </p>
    );
  }

  return (
    <div ref={wrapRef}>
      <div className="grid grid-cols-2 gap-3">
        {[0, 1].map(idx => {
          const current = idx === 0 ? col1 : col2;
          const other   = idx === 0 ? col2?.name : col1?.name;
          const isOpen  = openIdx === idx;
          return (
            <div key={idx} className="relative">
              <span className="block font-johnson-text text-[10px] font-bold text-jj-gray-05 uppercase tracking-wide mb-1">
                Column {idx + 1}
              </span>
              <button
                type="button"
                ref={el => { btnRefs.current[idx] = el; }}
                disabled={loading || !!error}
                onClick={() => toggleOpen(idx)}
                className={`${INPUT_EDIT_CLS} text-left flex items-center justify-between gap-2 pr-2 ${loading || error ? 'cursor-not-allowed opacity-60' : ''}`}
              >
                {current ? (
                  <span className="flex items-center gap-1.5 min-w-0 flex-1">
                    <span className="font-mono text-xs font-bold text-jj-gray-08 truncate">{current.name}</span>
                    {current.type && (
                      <span className="flex-shrink-0 font-mono text-[9px] font-bold uppercase tracking-wide px-1.5 py-0.5 rounded border bg-jj-gray-01 text-jj-gray-05 border-jj-gray-02">
                        {current.type}
                      </span>
                    )}
                  </span>
                ) : (
                  <span className="font-johnson-text text-sm text-jj-gray-04 italic flex-1">
                    {loading ? 'Loading…' : '— Select —'}
                  </span>
                )}
                <Ic src={chevronRightSrc} className={`w-4 h-4 flex-shrink-0 transition-transform duration-200 ${isOpen ? 'rotate-90' : ''}`} filter={F.gray05} />
              </button>
              {isOpen && (
                <div className={`absolute left-0 right-0 bg-white border border-jj-gray-03 rounded-xl shadow-2xl z-50 overflow-y-auto ${dropUp ? 'bottom-full mb-1' : 'top-full mt-1'}`} style={{ maxHeight: `${panelMax}px` }}>
                  {sortedColumns.length === 0 ? (
                    <p className="px-3 py-3 font-johnson-text text-xs text-jj-gray-04 italic">No columns found</p>
                  ) : sortedColumns.map(c => {
                    const isSelected = current?.name === c.name;
                    const isDisabled = c.name === other;
                    return (
                      <button key={c.name} type="button"
                        disabled={isDisabled}
                        onClick={() => setCol(idx, c.name)}
                        title={`${c.name}${c.type ? ` (${c.type})` : ''}`}
                        className={`w-full text-left px-3 py-2 flex items-center justify-between gap-2 border-b border-jj-gray-02 transition-colors ${
                          isDisabled ? 'opacity-40 cursor-not-allowed' : isSelected ? 'bg-red-50' : 'hover:bg-jj-gray-01'
                        }`}
                      >
                        <span className={`font-mono text-xs truncate ${isSelected ? 'text-jj-red font-bold' : 'text-jj-gray-08'}`}>
                          {c.name}
                        </span>
                        <span className="flex items-center gap-1.5 flex-shrink-0">
                          <span className="font-mono text-[9px] font-bold uppercase tracking-wide px-1.5 py-0.5 rounded border bg-jj-gray-01 text-jj-gray-05 border-jj-gray-02">
                            {c.type}
                          </span>
                          {isSelected && <Ic src={checkSrc} className="w-3.5 h-3.5" filter={F.red} />}
                        </span>
                      </button>
                    );
                  })}
                </div>
              )}
              {current && current.type && !isDateType(current.type) && (
                <p className="flex items-center gap-1 font-johnson-text text-[10px] text-jj-orange-03 mt-1">
                  <Ic src={alertSrc} className="w-3 h-3 flex-shrink-0" filter={F.orange} />
                  Not a date/timestamp column — day-diff coloring may not work as expected.
                </p>
              )}
            </div>
          );
        })}
      </div>
      {error && <p className="font-johnson-text text-[11px] text-jj-maroon-05 mt-1.5">{error}</p>}
      {!loading && !error && sortedColumns.length === 0 && (
        <p className="font-johnson-text text-[11px] text-jj-gray-04 italic mt-1.5">No columns found for this table.</p>
      )}
    </div>
  );
}
function CreateRuleModal({ columns, api, onSaved, onClose }) {
  const { user } = useAuth();
  const userEmail = user?.email || user?.username || 'unknown';
  const editableCols = columns.filter(c => !CREATE_AUTO_COLS.has(c.toUpperCase()));
  const initialDraft = Object.fromEntries(editableCols.map(c => [c, '']));

  const [draft, setDraft]         = useState(initialDraft);
  const [saving, setSaving]       = useState(false);
  const [error, setError]         = useState(null);
  const [confirmOpen, setConfirmOpen] = useState(false);

  useEffect(() => {
    const onKey = e => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onKey);
    document.body.style.overflow = 'hidden';
    return () => { document.removeEventListener('keydown', onKey); document.body.style.overflow = 'unset'; };
  }, []);

  // RULE_ID is derived from RULE_TITLE and not directly editable
  useEffect(() => {
    setDraft(d => ({ ...d, RULE_ID: slugifyRuleId(d.RULE_TITLE) }));
  }, [draft.RULE_TITLE]);

  const handleSave = async () => {
    const rawId = (draft['RULE_ID'] ?? '').trim();
    if (!rawId) { setError('RULE_ID is required.'); return; }
    const versionedId = `${rawId}_v1`;
    setSaving(true); setError(null);
    try {
      await api.createDQRow({ ...draft, RULE_ID: versionedId, RULE_TYPE: 'CUSTOM' });
      logAuditEvent(api.baseURL, userEmail, 'bdc_rule_created', versionedId,
        `Created DQ rule "${versionedId}"${draft.RULE_DESCRIPTION ? `: ${draft.RULE_DESCRIPTION}` : ''}`);
      onSaved();
    } catch (e) {
      setError(e.message || 'Failed to create rule.');
      setSaving(false);
    }
  };

  const inputEdit = INPUT_EDIT_CLS;
  const isLong    = col => CREATE_LONG_COLS.has(col.toUpperCase()) || (draft[col] || '').length > 80;


  return (
    <div
      className="fixed inset-0 bg-black bg-opacity-40 flex items-center justify-center z-50 p-4"
      style={{ backdropFilter: 'blur(3px)' }}
      onClick={onClose}
    >
      <div
        className="relative bg-white rounded-2xl shadow-2xl border border-jj-gray-02 w-full flex flex-col overflow-hidden"
        style={{ maxWidth: '760px', maxHeight: '88vh', animation: 'slideUp 0.2s ease-out' }}
        onClick={e => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex-shrink-0 px-6 py-4 border-b-4 border-jj-red bg-white flex items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-red-50 border border-jj-red border-opacity-20 flex items-center justify-center flex-shrink-0">
              <Ic src={addSrc} className="w-5 h-5" filter={F.red} />
            </div>
            <div>
              <h2 className="font-johnson-display text-lg text-jj-gray-08" style={{ lineHeight: '110%' }}>Create New Rule</h2>
              <p className="font-johnson-text text-xs text-jj-gray-04 mt-0.5">
                IS_ACTIVE set to <span className="font-bold text-jj-green-03">TRUE</span> automatically · Timestamps set server-side
              </p>
            </div>
          </div>
          <button onClick={onClose} className="w-9 h-9 flex items-center justify-center rounded-xl bg-jj-gray-01 hover:bg-jj-gray-02 transition-colors">
            <Ic src={closeSrc} className="w-4 h-4" filter={F.gray07} />
          </button>
        </div>

        {/* Form body */}
        <div className="flex-1 overflow-y-auto px-6 py-5">
          {error && (
            <div className="flex items-center gap-2 px-4 py-3 mb-4 bg-red-50 border border-jj-red border-opacity-30 rounded-xl text-jj-maroon-05">
              <Ic src={closeSrc} className="w-4 h-4 flex-shrink-0" filter={F.red} />
              <span className="font-johnson-text text-sm">{error}</span>
            </div>
          )}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-5 gap-y-4">
            {editableCols.map(col => {
              const isRuleId   = col.toUpperCase() === 'RULE_ID';
              const isRequired = isRuleId;
              const label = (
                <label className="flex items-center gap-1.5 mb-1.5">
                  <span className="font-mono text-[11px] font-bold text-jj-gray-06 uppercase tracking-wide">{col}</span>
                  {isRequired && (
                    <span className="px-1.5 py-0.5 rounded text-[9px] font-bold border leading-none bg-red-50 text-jj-red border-jj-red border-opacity-30">REQUIRED</span>
                  )}
                </label>
              );
              if (col.toUpperCase() === 'TARGET_TABLE' || col.toUpperCase() === 'DELTA_TABLE') {
                return (
                  <div key={col} className="sm:col-span-2">
                    {label}
                    <TableBrowserField
                      api={api}
                      value={draft[col]}
                      connectionType={draft['CONNECTION_TYPE']}
                      onChange={val => setDraft(d => ({ ...d, [col]: val }))}
                    />
                  </div>
                );
              }
              if (col.toUpperCase() === 'COLOR_COLUMNS') {
                return (
                  <div key={col} className="sm:col-span-2">
                    {label}
                    <ColorColumnsField
                      api={api}
                      value={draft[col]}
                      connectionType={draft['CONNECTION_TYPE']}
                      targetTable={draft['TARGET_TABLE']}
                      onChange={val => setDraft(d => ({ ...d, [col]: val }))}
                    />
                  </div>
                );
              }
              if (isRuleId) {
                const rawId    = draft[col] ?? '';
                const savedAs  = rawId.trim() ? `${rawId.trim()}_v1` : '';
                return (
                  <div key={col}>
                    {label}
                    <input
                      type="text"
                      className={`${INPUT_LOCKED_CLS} font-mono`}
                      value={rawId}
                      disabled
                      placeholder="Type the rule title to generate an ID…"
                    />
                    <p className="font-johnson-text text-[10px] mt-1">
                      {savedAs
                        ? <span className="text-jj-gray-06">Will be saved as: <span className="font-mono font-bold text-jj-red">{savedAs}</span></span>
                        : <span className="text-jj-gray-04">Auto-generated from Rule Title — <span className="font-mono">_v1</span> will be appended automatically</span>
                      }
                    </p>
                  </div>
                );
              }
              if (FIELD_DROPDOWNS[col.toUpperCase()]) {
                const opts = FIELD_DROPDOWNS[col.toUpperCase()];
                const rawVal = draft[col] ?? '';
                const selectVal = opts.find(o => o.toUpperCase() === String(rawVal).toUpperCase()) || rawVal;
                return (
                  <div key={col}>
                    {label}
                    <div className="relative">
                      <select
                        className={`${inputEdit} appearance-none pr-8`}
                        value={selectVal}
                        onChange={e => setDraft(d => ({ ...d, [col]: e.target.value }))}
                      >
                        <option value="">— Select —</option>
                        {opts.map(o => <option key={o} value={o}>{o}</option>)}
                      </select>
                      <Ic src={chevronRightSrc} className="absolute right-2.5 top-1/2 -translate-y-1/2 w-4 h-4 pointer-events-none rotate-90" filter={F.gray05} />
                    </div>
                  </div>
                );
              }
              return (
                <div key={col} className={isLong(col) ? 'sm:col-span-2' : ''}>
                  {label}
                  {isLong(col) ? (
                    <textarea rows={3} className={`${inputEdit} resize-y min-h-[72px]`}
                      value={draft[col] ?? ''} onChange={e => setDraft(d => ({ ...d, [col]: e.target.value }))} />
                  ) : (
                    <input type="text" className={inputEdit}
                      value={draft[col] ?? ''} onChange={e => setDraft(d => ({ ...d, [col]: e.target.value }))} />
                  )}
                </div>
              );
            })}
          </div>
        </div>

        {/* Footer */}
        <div className="flex-shrink-0 px-6 py-3.5 bg-jj-gray-01 border-t border-jj-gray-02 flex items-center justify-between gap-3">
          <div className="flex items-center gap-1.5 font-johnson-text text-xs text-jj-gray-05">
            <Ic src={activitySrc} className="w-3 h-3" filter={F.gray07} />
            New row inserted into Snowflake with IS_ACTIVE = TRUE
          </div>
          <div className="flex items-center gap-2">
            <button onClick={onClose} disabled={saving}
              className="px-4 py-2 rounded-lg font-johnson-text text-sm font-bold text-jj-gray-07 bg-white hover:bg-jj-gray-02 transition-colors border border-jj-gray-03 disabled:opacity-50">
              Cancel
            </button>
            <button onClick={() => setConfirmOpen(true)} disabled={saving}
              className="inline-flex items-center gap-2 px-5 py-2 rounded-lg font-johnson-text text-sm font-bold text-white bg-jj-red hover:bg-jj-maroon-05 transition-colors shadow-sm disabled:opacity-50">
              {saving
                ? <><Ic src={activitySrc} className="w-4 h-4 animate-spin" filter={F.white} />Creating…</>
                : <><Ic src={addSrc} className="w-4 h-4" filter={F.white} />Create Rule</>
              }
            </button>
          </div>
        </div>

        {/* ── Confirmation overlay ── */}
        {confirmOpen && (
          <div className="absolute inset-0 bg-white bg-opacity-80 flex items-center justify-center rounded-2xl" style={{ backdropFilter: 'blur(4px)', zIndex: 10 }}>
            <div className="bg-white rounded-2xl shadow-2xl border border-jj-gray-02 mx-6 p-8 flex flex-col items-center gap-5 text-center" style={{ maxWidth: '360px', animation: 'slideUp 0.15s ease-out' }}>
              <div className="w-14 h-14 rounded-2xl bg-red-50 border border-jj-red border-opacity-20 flex items-center justify-center">
                <Ic src={addSrc} className="w-7 h-7" filter={F.red} />
              </div>
              <div>
                <h3 className="font-johnson-display text-lg text-jj-gray-08 mb-1">Create this rule?</h3>
                <p className="font-johnson-text text-sm text-jj-gray-05">A new row will be inserted into Snowflake with IS_ACTIVE set to TRUE.</p>
              </div>
              <div className="flex items-center gap-3 w-full">
                <button
                  onClick={() => setConfirmOpen(false)}
                  className="flex-1 px-4 py-2.5 rounded-xl font-johnson-text text-sm font-bold text-jj-gray-07 bg-white hover:bg-jj-gray-02 transition-colors border border-jj-gray-03"
                >
                  Cancel
                </button>
                <button
                  onClick={() => { setConfirmOpen(false); handleSave(); }}
                  className="flex-1 inline-flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl font-johnson-text text-sm font-bold text-white bg-jj-red hover:bg-jj-maroon-05 transition-colors shadow-sm"
                >
                  <Ic src={checkSrc} className="w-4 h-4" filter={F.white} />
                  Yes, Create
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}


// ═══════════════════════════════════════════════════════════════════════════════
function EditRowModal({ columns, draft, onDraftChange, onSave, onClose, api }) {
  const [saving, setSaving]         = useState(false);
  const [error, setError]           = useState(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [nextVersion, setNextVersion]         = useState(null);
  const [nextVersionLoading, setNextVersionLoading] = useState(false);

  useEffect(() => {
    const onKey = e => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onKey);
    document.body.style.overflow = 'hidden';
    return () => { document.removeEventListener('keydown', onKey); document.body.style.overflow = 'unset'; };
  }, []);

  // Fetch next version ID preview
  useEffect(() => {
    const pkCol = columns.find(c => c.toUpperCase() === 'RULE_ID') || columns[0];
    const pkVal = draft[pkCol];
    if (!pkVal) return;
    setNextVersionLoading(true);
    api.fetchNextVersion(pkVal)
      .then(d => setNextVersion(d))
      .catch(() => setNextVersion(null))
      .finally(() => setNextVersionLoading(false));
  }, []);

  const pkCol = columns.find(c => c.toUpperCase() === 'RULE_ID') || columns[0];

  // Classify columns into groups
  const TS_COLS      = new Set(['CREATED_TS', 'UPDATED_TS', 'CREATED_AT', 'UPDATED_AT']);
  const READ_ONLY    = new Set([pkCol.toUpperCase(), 'RULE_TYPE']);
  const isTimestamp  = col => TS_COLS.has(col.toUpperCase());
  const isReadOnly   = col => READ_ONLY.has(col.toUpperCase()) || isTimestamp(col);

  // Split into editable vs metadata/timestamp
  const editableCols   = columns.filter(c => !isReadOnly(c));

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    try {
      await onSave();
    } catch (e) {
      setError(e.message || 'Failed to save. Please try again.');
      setSaving(false);
    }
  };

  // Decide if a field gets a textarea (long content) or input
  const isLong = col => {
    const v = draft[col] ?? '';
    return v.length > 60 || ['DESCRIPTION', 'RULE_DESCRIPTION', 'SQL', 'PREDICATE', 'NOTES'].some(k => col.toUpperCase().includes(k));
  };

  const inputEdit   = INPUT_EDIT_CLS;

  const FieldLabel = ({ col, tag }) => (
    <label className="flex items-center gap-1.5 mb-1.5">
      <span className="font-mono text-[11px] font-bold text-jj-gray-06 uppercase tracking-wide">{col}</span>
      {tag && (
        <span className={`px-1.5 py-0.5 rounded text-[9px] font-bold border leading-none ${
          tag === 'PK'        ? 'bg-red-50 text-jj-red border-jj-red border-opacity-30'
        : tag === 'TIMESTAMP' ? 'bg-violet-50 text-jj-violet-03 border-jj-violet-03 border-opacity-30'
        :                       'bg-jj-gray-01 text-jj-gray-05 border-jj-gray-02'
        }`}>
          {tag}
        </span>
      )}
    </label>
  );

  return (
    <div
      className="fixed inset-0 bg-black bg-opacity-40 flex items-center justify-center z-50 p-4"
      style={{ backdropFilter: 'blur(3px)' }}
      onClick={onClose}
    >
      <div
        className="relative bg-white rounded-2xl shadow-2xl border border-jj-gray-02 w-full flex flex-col overflow-hidden"
        style={{ maxWidth: '760px', maxHeight: '88vh', animation: 'slideUp 0.2s ease-out' }}
        onClick={e => e.stopPropagation()}
      >
        {/* ── Header ── */}
        <div className="flex-shrink-0 px-6 py-4 border-b-4 border-jj-red bg-white flex items-center justify-between gap-4">
          <div className="flex items-center gap-3 min-w-0">
            <div className="w-10 h-10 rounded-xl bg-red-50 border border-jj-red border-opacity-20 flex items-center justify-center flex-shrink-0">
              <Ic src={editSrc} className="w-5 h-5" filter={F.red} />
            </div>
            <div className="min-w-0">
              <h2 className="font-johnson-display text-lg text-jj-gray-08 truncate" style={{ lineHeight: '110%' }}>
                Edit Rule Input Row
              </h2>
              <div className="flex items-center gap-2 mt-0.5 flex-wrap">
                <span className="inline-flex items-center gap-1 font-mono text-xs font-bold text-jj-red bg-red-50 px-2 py-0.5 rounded border border-jj-red border-opacity-20">
                  {pkCol}: {draft[pkCol]}
                </span>
                <span className="font-johnson-text text-xs text-jj-gray-04">Primary key &amp; timestamps are read-only</span>
              </div>
            </div>
          </div>
          <button
            onClick={onClose}
            className="w-9 h-9 flex-shrink-0 flex items-center justify-center rounded-xl bg-jj-gray-01 hover:bg-jj-gray-02 transition-colors"
          >
            <Ic src={closeSrc} className="w-4 h-4" filter={F.gray07} />
          </button>
        </div>

        {/* ── Body ── */}
        <div className="flex-1 overflow-y-auto">

          {/* Versioning banner */}
          <div className={`mx-6 mt-4 flex items-start gap-3 px-4 py-3 rounded-xl border ${
            nextVersionLoading
              ? 'bg-jj-gray-01 border-jj-gray-02'
              : 'bg-blue-50 border-jj-blue-03 border-opacity-30'
          }`}>
            <Ic src={activitySrc} className={`w-4 h-4 flex-shrink-0 mt-0.5 ${nextVersionLoading ? ' animate-spin' : ''}`} filter={F.blue} />
            <div className="min-w-0">
              {nextVersionLoading ? (
                <p className="font-johnson-text text-xs text-jj-gray-05">Computing next version…</p>
              ) : nextVersion ? (
                <>
                  <p className="font-johnson-text text-xs font-bold text-jj-blue-03 mb-1">Versioning behaviour</p>
                  <p className="font-johnson-text text-xs text-jj-gray-07 leading-relaxed">
                    Saving will <span className="font-bold text-jj-gray-08">deactivate</span> the original rule{' '}
                    <span className="font-mono font-bold text-jj-gray-08 bg-white px-1.5 py-0.5 rounded border border-jj-gray-02">{nextVersion.original_rule_id}</span>
                    {' '}and insert a new active row with ID{' '}
                    <span className="font-mono font-bold text-jj-blue-03 bg-white px-1.5 py-0.5 rounded border border-jj-blue-03 border-opacity-30">{nextVersion.next_version_id}</span>.
                  </p>
                </>
              ) : (
                <p className="font-johnson-text text-xs text-jj-gray-05">Version preview unavailable.</p>
              )}
            </div>
          </div>

          {/* Error banner */}
          {error && (
            <div className="mx-6 mt-4 flex items-center gap-2 px-4 py-3 bg-red-50 border border-jj-red border-opacity-30 rounded-xl text-jj-maroon-05">
              <Ic src={closeSrc} className="w-4 h-4 flex-shrink-0" filter={F.red} />
              <span className="font-johnson-text text-sm">{error}</span>
            </div>
          )}

          {/* Editable fields section */}
          <div className="px-6 pt-5 pb-4">
            <div className="flex items-center gap-2 mb-4">
              <Ic src={editSrc} className="w-3.5 h-3.5" filter={F.black} />
              <span className="font-johnson-text text-xs font-bold text-jj-gray-06 uppercase tracking-wide">
                Editable Fields
              </span>
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-5 gap-y-4">
              {editableCols.map(col => {
                if (col.toUpperCase() === 'IS_ACTIVE') {
                  const isOn = draft[col] === true || draft[col] === 'true' || draft[col] === 'True' || draft[col] === 'TRUE';
                  return (
                    <div key={col}>
                      <FieldLabel col={col} />
                      <button type="button"
                        onClick={() => onDraftChange({ ...draft, [col]: !isOn })}
                        className="flex items-center gap-2.5 px-3 py-2 border border-jj-gray-03 rounded-lg bg-white hover:bg-jj-gray-01 transition-colors">
                        <span className="relative inline-flex flex-shrink-0 rounded-full transition-all duration-300"
                          style={{ width: '36px', height: '20px', background: isOn ? '#328714' : '#d5cfc9' }}>
                          <span className="absolute top-0.5 rounded-full bg-white shadow transition-all duration-300"
                            style={{ width: '16px', height: '16px', left: isOn ? '18px' : '2px' }} />
                        </span>
                        <span className={`font-johnson-text text-sm font-bold transition-colors ${isOn ? 'text-jj-green-03' : 'text-jj-gray-05'}`}>
                          {isOn ? 'Active' : 'Inactive'}
                        </span>
                      </button>
                    </div>
                  );
                }

                if (col.toUpperCase() === 'TARGET_TABLE' || col.toUpperCase() === 'DELTA_TABLE') {
                  return (
                    <div key={col} className="sm:col-span-2">
                      <FieldLabel col={col} />
                      <TableBrowserField
                        api={api}
                        value={draft[col]}
                        connectionType={draft['CONNECTION_TYPE']}
                        onChange={val => onDraftChange({ ...draft, [col]: val })}
                      />
                    </div>
                  );
                }

                if (col.toUpperCase() === 'COLOR_COLUMNS') {
                  return (
                    <div key={col} className="sm:col-span-2">
                      <FieldLabel col={col} />
                      <ColorColumnsField
                        api={api}
                        value={draft[col]}
                        connectionType={draft['CONNECTION_TYPE']}
                        targetTable={draft['TARGET_TABLE']}
                        onChange={val => onDraftChange({ ...draft, [col]: val })}
                      />
                    </div>
                  );
                }

                if (FIELD_DROPDOWNS[col.toUpperCase()]) {
                  const opts = FIELD_DROPDOWNS[col.toUpperCase()];
                  const rawVal = draft[col] ?? '';
                  const selectVal = opts.find(o => o.toUpperCase() === String(rawVal).toUpperCase()) || rawVal;
                  return (
                    <div key={col}>
                      <FieldLabel col={col} />
                      <div className="relative">
                        <select
                          className={`${inputEdit} appearance-none pr-8`}
                          value={selectVal}
                          onChange={e => onDraftChange({ ...draft, [col]: e.target.value })}
                        >
                          <option value="">— Select —</option>
                          {opts.map(o => <option key={o} value={o}>{o}</option>)}
                        </select>
                        <Ic src={chevronRightSrc} className="absolute right-2.5 top-1/2 -translate-y-1/2 w-4 h-4 pointer-events-none rotate-90" filter={F.gray05} />
                      </div>
                    </div>
                  );
                }
                return (
                  <div key={col} className={isLong(col) ? 'sm:col-span-2' : ''}>
                    <FieldLabel col={col} />
                    {isLong(col) ? (
                      <textarea
                        rows={3}
                        className={`${inputEdit} resize-y min-h-[72px]`}
                        value={draft[col] ?? ''}
                        onChange={e => onDraftChange({ ...draft, [col]: e.target.value })}
                      />
                    ) : (
                      <input
                        type="text"
                        className={inputEdit}
                        value={draft[col] ?? ''}
                        onChange={e => onDraftChange({ ...draft, [col]: e.target.value })}
                      />
                    )}
                  </div>
                );
              })}
            </div>
          </div>

        </div>

        {/* ── Footer ── */}
        <div className="flex-shrink-0 px-6 py-3.5 bg-jj-gray-01 border-t border-jj-gray-02 flex items-center justify-between gap-3">
          <div className="flex items-center gap-1.5 font-johnson-text text-xs text-jj-gray-05">
            <Ic src={alertSrc} className="w-3 h-3" filter={F.gray07} />
            Changes are written directly to Snowflake
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={onClose}
              disabled={saving}
              className="px-4 py-2 rounded-lg font-johnson-text text-sm font-bold text-jj-gray-07 bg-white hover:bg-jj-gray-02 transition-colors border border-jj-gray-03 disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              onClick={() => setConfirmOpen(true)}
              disabled={saving}
              className="inline-flex items-center gap-2 px-5 py-2 rounded-lg font-johnson-text text-sm font-bold text-white bg-jj-red hover:bg-jj-maroon-05 transition-colors shadow-sm disabled:opacity-50"
            >
              {saving
                ? <><Ic src={activitySrc} className="w-4 h-4 animate-spin" filter={F.white} />Saving…</>
                : nextVersion
                  ? <><Ic src={saveSrc} className="w-4 h-4" filter={F.white} />Save as {nextVersion.next_version_id}</>
                  : <><Ic src={saveSrc} className="w-4 h-4" filter={F.white} />Save to Snowflake</>
              }
            </button>
          </div>
        </div>

        {/* ── Confirmation overlay ── */}
        {confirmOpen && (
          <div className="absolute inset-0 bg-white bg-opacity-80 flex items-center justify-center rounded-2xl" style={{ backdropFilter: 'blur(4px)', zIndex: 10 }}>
            <div className="bg-white rounded-2xl shadow-2xl border border-jj-gray-02 mx-6 p-8 flex flex-col items-center gap-5 text-center" style={{ maxWidth: '360px', animation: 'slideUp 0.15s ease-out' }}>
              <div className="w-14 h-14 rounded-2xl bg-red-50 border border-jj-red border-opacity-20 flex items-center justify-center">
                <Ic src={saveSrc} className="w-7 h-7" filter={F.red} />
              </div>
              <div>
                <h3 className="font-johnson-display text-lg text-jj-gray-08 mb-1">Save these changes?</h3>
                <p className="font-johnson-text text-sm text-jj-gray-05">This will write your edits directly to Snowflake.</p>
              </div>
              <div className="flex items-center gap-3 w-full">
                <button
                  onClick={() => setConfirmOpen(false)}
                  className="flex-1 px-4 py-2.5 rounded-xl font-johnson-text text-sm font-bold text-jj-gray-07 bg-white hover:bg-jj-gray-02 transition-colors border border-jj-gray-03"
                >
                  Cancel
                </button>
                <button
                  onClick={() => { setConfirmOpen(false); handleSave(); }}
                  className="flex-1 inline-flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl font-johnson-text text-sm font-bold text-white bg-jj-red hover:bg-jj-maroon-05 transition-colors shadow-sm"
                >
                  <Ic src={checkSrc} className="w-4 h-4" filter={F.white} />
                  Yes, Save
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}


// ═══════════════════════════════════════════════════════════════════════════════
// Date Input — shared constants
// ═══════════════════════════════════════════════════════════════════════════════
const DATE_INPUT_LOCKED   = new Set(['ID','CREATED_DATE','UPDATED_DATE','CREATED_TS','UPDATED_TS','CREATED_AT','UPDATED_AT']);
const DATE_INPUT_DATE_COLS = new Set(['FROM_DATE','TO_DATE']);
const DATE_INPUT_BOOL_COLS = new Set(['IS_ACTIVE']);
const REPORT_SET_OPTIONS = ['RTE_REPORT_BASELINE', 'RTE_REPORT_BIWEEKLY', 'RTE_REPORT_QUARTERLY'];

// Strip time from "YYYY-MM-DD HH:MM:SS.ffffff" → "YYYY-MM-DD" for date inputs
function toDateValue(val) {
  if (!val) return '';
  return String(val).slice(0, 10);
}

// ── Shared field renderer used by both create and edit modals ─────────────────
function DateInputField({ col, value, onChange, locked }) {
  const cu = col.toUpperCase();
  const inputEdit   = INPUT_EDIT_CLS;
  const inputLocked = INPUT_LOCKED_CLS;

  if (locked) {
    return (
      <div>
        <label className="flex items-center gap-1.5 mb-1.5">
          <span className="font-mono text-[11px] font-bold text-jj-gray-06 uppercase tracking-wide">{col}</span>
          <span className="px-1.5 py-0.5 rounded text-[9px] font-bold border leading-none bg-violet-50 text-jj-violet-03 border-jj-violet-03 border-opacity-30">AUTO</span>
        </label>
        <input type="text" readOnly className={inputLocked} value={value || '(auto-set by server)'} />
      </div>
    );
  }

  if (DATE_INPUT_DATE_COLS.has(cu)) {
    return (
      <div>
        <label className="flex items-center gap-1.5 mb-1.5">
          <span className="font-mono text-[11px] font-bold text-jj-gray-06 uppercase tracking-wide">{col}</span>
        </label>
        <input
          type="date"
          className={inputEdit}
          value={toDateValue(value)}
          onChange={e => onChange(e.target.value)}
        />
      </div>
    );
  }

  if (DATE_INPUT_BOOL_COLS.has(cu)) {
    const isOn = value === true || value === 'True' || value === 'true' || value === 'TRUE';
    return (
      <div>
        <label className="flex items-center gap-1.5 mb-1.5">
          <span className="font-mono text-[11px] font-bold text-jj-gray-06 uppercase tracking-wide">{col}</span>
        </label>
        <button
          type="button"
          onClick={() => onChange(!isOn)}
          className="flex items-center gap-2.5 px-3 py-2 border border-jj-gray-03 rounded-lg bg-white hover:bg-jj-gray-01 transition-colors"
        >
          <span className="relative inline-flex flex-shrink-0 rounded-full transition-all duration-300"
            style={{ width: '36px', height: '20px', background: isOn ? '#328714' : '#d5cfc9' }}>
            <span className="absolute top-0.5 rounded-full bg-white shadow transition-all duration-300"
              style={{ width: '16px', height: '16px', left: isOn ? '18px' : '2px' }} />
          </span>
          <span className={`font-johnson-text text-sm font-bold transition-colors ${isOn ? 'text-jj-green-03' : 'text-jj-gray-05'}`}>
            {isOn ? 'Active' : 'Inactive'}
          </span>
        </button>
      </div>
    );
  }

  // Default: plain text
  return (
    <div>
      <label className="flex items-center gap-1.5 mb-1.5">
        <span className="font-mono text-[11px] font-bold text-jj-gray-06 uppercase tracking-wide">{col}</span>
      </label>
      <input
        type="text"
        className={inputEdit}
        value={value ?? ''}
        onChange={e => onChange(e.target.value)}
      />
    </div>
  );
}


// ── Edit Modal ────────────────────────────────────────────────────────────────
function DateInputEditModal({ columns, draft, originalRow, onDraftChange, onSave, onClose, api }) {
  const [saving, setSaving]   = useState(false);
  const [error, setError]     = useState(null);
  const [confirmOpen, setConfirmOpen] = useState(false);

  useEffect(() => {
    const onKey = e => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onKey);
    document.body.style.overflow = 'hidden';
    return () => { document.removeEventListener('keydown', onKey); document.body.style.overflow = 'unset'; };
  }, []);

  const handleSave = async () => {
    setSaving(true); setError(null);
    try { await onSave(); }
    catch (e) { setError(e.message || 'Failed to save.'); setSaving(false); }
  };

  return (
    <div className="fixed inset-0 bg-black bg-opacity-40 flex items-center justify-center z-50 p-4"
      style={{ backdropFilter: 'blur(3px)' }} onClick={onClose}>
      <div className="relative bg-white rounded-2xl shadow-2xl border border-jj-gray-02 w-full flex flex-col overflow-hidden"
        style={{ maxWidth: '680px', maxHeight: '88vh', animation: 'slideUp 0.2s ease-out' }}
        onClick={e => e.stopPropagation()}>

        {/* Header */}
        <div className="flex-shrink-0 px-6 py-4 border-b-4 border-jj-red bg-white flex items-center justify-between gap-4">
          <div className="flex items-center gap-3 min-w-0">
            <div className="w-10 h-10 rounded-xl bg-red-50 border border-jj-red border-opacity-20 flex items-center justify-center flex-shrink-0">
              <Ic src={editSrc} className="w-5 h-5" filter={F.red} />
            </div>
            <div className="min-w-0">
              <h2 className="font-johnson-display text-lg text-jj-gray-08" style={{ lineHeight: '110%' }}>Edit Date Range</h2>
              <p className="font-johnson-text text-xs text-jj-gray-04 mt-0.5">
                Timestamps updated automatically · Dates use calendar picker
              </p>
            </div>
          </div>
          <button onClick={onClose} className="w-9 h-9 flex-shrink-0 flex items-center justify-center rounded-xl bg-jj-gray-01 hover:bg-jj-gray-02 transition-colors">
            <Ic src={closeSrc} className="w-4 h-4" filter={F.gray07} />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-6 py-5">
          {error && (
            <div className="flex items-center gap-2 px-4 py-3 mb-4 bg-red-50 border border-jj-red border-opacity-30 rounded-xl text-jj-maroon-05">
              <Ic src={closeSrc} className="w-4 h-4 flex-shrink-0" filter={F.red} />
              <span className="font-johnson-text text-sm">{error}</span>
            </div>
          )}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-5 gap-y-4">
            {columns.map(col => (
              <DateInputField
                key={col}
                col={col}
                value={draft[col]}
                locked={DATE_INPUT_LOCKED.has(col.toUpperCase())}
                onChange={val => onDraftChange({ ...draft, [col]: val })}
              />
            ))}
          </div>
        </div>

        {/* Footer */}
        <div className="flex-shrink-0 px-6 py-3.5 bg-jj-gray-01 border-t border-jj-gray-02 flex items-center justify-between gap-3">
          <div className="flex items-center gap-1.5 font-johnson-text text-xs text-jj-gray-05">
            <Ic src={activitySrc} className="w-3 h-3" filter={F.gray07} />
            UPDATED_DATE set server-side on save
          </div>
          <div className="flex items-center gap-2">
            <button onClick={onClose} disabled={saving}
              className="px-4 py-2 rounded-lg font-johnson-text text-sm font-bold text-jj-gray-07 bg-white hover:bg-jj-gray-02 transition-colors border border-jj-gray-03 disabled:opacity-50">
              Cancel
            </button>
            <button onClick={() => setConfirmOpen(true)} disabled={saving}
              className="inline-flex items-center gap-2 px-5 py-2 rounded-lg font-johnson-text text-sm font-bold text-white bg-jj-red hover:bg-jj-maroon-05 transition-colors shadow-sm disabled:opacity-50">
              {saving
                ? <><Ic src={activitySrc} className="w-4 h-4 animate-spin" filter={F.white} />Saving…</>
                : <><Ic src={checkSrc} className="w-4 h-4" filter={F.white} />Save Changes</>
              }
            </button>
          </div>
        </div>

        {confirmOpen && (
          <div className="absolute inset-0 bg-white bg-opacity-80 flex items-center justify-center rounded-2xl" style={{ backdropFilter: 'blur(4px)', zIndex: 10 }}>
            <div className="bg-white rounded-2xl shadow-2xl border border-jj-gray-02 mx-6 p-8 flex flex-col items-center gap-5 text-center" style={{ maxWidth: '360px', animation: 'slideUp 0.15s ease-out' }}>
              <div className="w-14 h-14 rounded-2xl bg-red-50 border border-jj-red border-opacity-20 flex items-center justify-center">
                <Ic src={editSrc} className="w-7 h-7" filter={F.red} />
              </div>
              <div>
                <h3 className="font-johnson-display text-lg text-jj-gray-08 mb-1">Save changes?</h3>
                <p className="font-johnson-text text-sm text-jj-gray-05">The row will be updated in Snowflake and UPDATED_DATE refreshed.</p>
              </div>
              <div className="flex items-center gap-3 w-full">
                <button onClick={() => setConfirmOpen(false)}
                  className="flex-1 px-4 py-2.5 rounded-xl font-johnson-text text-sm font-bold text-jj-gray-07 bg-white hover:bg-jj-gray-02 transition-colors border border-jj-gray-03">
                  Cancel
                </button>
                <button onClick={() => { setConfirmOpen(false); handleSave(); }}
                  className="flex-1 inline-flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl font-johnson-text text-sm font-bold text-white bg-jj-red hover:bg-jj-maroon-05 transition-colors shadow-sm">
                  <Ic src={checkSrc} className="w-4 h-4" filter={F.white} />Yes, Save
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}


// ── Create Modal ──────────────────────────────────────────────────────────────
function DateInputCreateModal({ api, onSaved, onClose }) {
  const { user } = useAuth();
  const userEmail = user?.email || user?.username || 'unknown';

  const [draft, setDraft]         = useState({ REPORT_SET: '', FROM_DATE: '', TO_DATE: '', IS_ACTIVE: true, NOTES: '' });
  const [saving, setSaving]       = useState(false);
  const [error, setError]         = useState(null);
  const [confirmOpen, setConfirmOpen] = useState(false);

  useEffect(() => {
    const onKey = e => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onKey);
    document.body.style.overflow = 'hidden';
    return () => { document.removeEventListener('keydown', onKey); document.body.style.overflow = 'unset'; };
  }, []);

  const handleSave = async () => {
    if (!(draft.REPORT_SET || '').trim()) { setError('REPORT_SET is required.'); return; }
    if (!draft.FROM_DATE)                 { setError('FROM_DATE is required.');   return; }
    if (!draft.TO_DATE)                   { setError('TO_DATE is required.');     return; }
    setSaving(true); setError(null);
    try {
      // Auto-generate a UUID for the ID column
      const newId = 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
        const r = Math.random() * 16 | 0;
        return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
      });
      await api.createDateInputRow({ ...draft, ID: newId });
      logAuditEvent(api.baseURL, userEmail, 'bdc_date_row_created', draft.REPORT_SET,
        `Created date-input row: ${draft.REPORT_SET} ${draft.FROM_DATE}→${draft.TO_DATE}`);
      onSaved();
    } catch (e) {
      setError(e.message || 'Failed to create row.');
      setSaving(false);
    }
  };

  const inputEdit = INPUT_EDIT_CLS;

  // IS_ACTIVE toggle
  const isOn = draft.IS_ACTIVE === true || draft.IS_ACTIVE === 'true';

  return (
    <div className="fixed inset-0 bg-black bg-opacity-40 flex items-center justify-center z-50 p-4"
      style={{ backdropFilter: 'blur(3px)' }} onClick={onClose}>
      <div className="relative bg-white rounded-2xl shadow-2xl border border-jj-gray-02 w-full flex flex-col overflow-hidden"
        style={{ maxWidth: '640px', maxHeight: '88vh', animation: 'slideUp 0.2s ease-out' }}
        onClick={e => e.stopPropagation()}>

        {/* Header */}
        <div className="flex-shrink-0 px-6 py-4 border-b-4 border-jj-red bg-white flex items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-red-50 border border-jj-red border-opacity-20 flex items-center justify-center flex-shrink-0">
              <Ic src={addSrc} className="w-5 h-5" filter={F.red} />
            </div>
            <div>
              <h2 className="font-johnson-display text-lg text-jj-gray-08" style={{ lineHeight: '110%' }}>New Date Range</h2>
              <p className="font-johnson-text text-xs text-jj-gray-04 mt-0.5">
                CREATED_DATE &amp; UPDATED_DATE set automatically by server
              </p>
            </div>
          </div>
          <button onClick={onClose} className="w-9 h-9 flex items-center justify-center rounded-xl bg-jj-gray-01 hover:bg-jj-gray-02 transition-colors">
            <Ic src={closeSrc} className="w-4 h-4" filter={F.gray07} />
          </button>
        </div>

        {/* Form */}
        <div className="flex-1 overflow-y-auto px-6 py-5">
          {error && (
            <div className="flex items-center gap-2 px-4 py-3 mb-4 bg-red-50 border border-jj-red border-opacity-30 rounded-xl text-jj-maroon-05">
              <Ic src={closeSrc} className="w-4 h-4 flex-shrink-0" filter={F.red} />
              <span className="font-johnson-text text-sm">{error}</span>
            </div>
          )}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-5 gap-y-4">
            {/* REPORT_SET */}
            <div className="sm:col-span-2">
              <label className="flex items-center gap-1.5 mb-1.5">
                <span className="font-mono text-[11px] font-bold text-jj-gray-06 uppercase tracking-wide">REPORT_SET</span>
                <span className="px-1.5 py-0.5 rounded text-[9px] font-bold border leading-none bg-red-50 text-jj-red border-jj-red border-opacity-30">REQUIRED</span>
              </label>
              <div className="relative">
                <select
                  className={`${inputEdit} appearance-none pr-8`}
                  value={draft.REPORT_SET}
                  onChange={e => setDraft(d => ({ ...d, REPORT_SET: e.target.value }))}
                >
                  <option value="">— Select —</option>
                  {REPORT_SET_OPTIONS.map(o => <option key={o} value={o}>{o}</option>)}
                </select>
                <Ic src={chevronRightSrc} className="absolute right-2.5 top-1/2 -translate-y-1/2 w-4 h-4 pointer-events-none rotate-90" filter={F.gray05} />
              </div>
            </div>

            {/* FROM_DATE */}
            <div>
              <label className="flex items-center gap-1.5 mb-1.5">
                <span className="font-mono text-[11px] font-bold text-jj-gray-06 uppercase tracking-wide">FROM_DATE</span>
                <span className="px-1.5 py-0.5 rounded text-[9px] font-bold border leading-none bg-red-50 text-jj-red border-jj-red border-opacity-30">REQUIRED</span>
              </label>
              <input type="date" className={inputEdit}
                value={draft.FROM_DATE} onChange={e => setDraft(d => ({ ...d, FROM_DATE: e.target.value }))} />
            </div>

            {/* TO_DATE */}
            <div>
              <label className="flex items-center gap-1.5 mb-1.5">
                <span className="font-mono text-[11px] font-bold text-jj-gray-06 uppercase tracking-wide">TO_DATE</span>
                <span className="px-1.5 py-0.5 rounded text-[9px] font-bold border leading-none bg-red-50 text-jj-red border-jj-red border-opacity-30">REQUIRED</span>
              </label>
              <input type="date" className={inputEdit}
                value={draft.TO_DATE} onChange={e => setDraft(d => ({ ...d, TO_DATE: e.target.value }))} />
            </div>

            {/* IS_ACTIVE */}
            <div>
              <label className="flex items-center gap-1.5 mb-1.5">
                <span className="font-mono text-[11px] font-bold text-jj-gray-06 uppercase tracking-wide">IS_ACTIVE</span>
              </label>
              <button type="button"
                onClick={() => setDraft(d => ({ ...d, IS_ACTIVE: !isOn }))}
                className="flex items-center gap-2.5 px-3 py-2 border border-jj-gray-03 rounded-lg bg-white hover:bg-jj-gray-01 transition-colors">
                <span className="relative inline-flex flex-shrink-0 rounded-full transition-all duration-300"
                  style={{ width: '36px', height: '20px', background: isOn ? '#328714' : '#d5cfc9' }}>
                  <span className="absolute top-0.5 rounded-full bg-white shadow transition-all duration-300"
                    style={{ width: '16px', height: '16px', left: isOn ? '18px' : '2px' }} />
                </span>
                <span className={`font-johnson-text text-sm font-bold transition-colors ${isOn ? 'text-jj-green-03' : 'text-jj-gray-05'}`}>
                  {isOn ? 'Active' : 'Inactive'}
                </span>
              </button>
            </div>

            {/* NOTES */}
            <div className="sm:col-span-2">
              <label className="flex items-center gap-1.5 mb-1.5">
                <span className="font-mono text-[11px] font-bold text-jj-gray-06 uppercase tracking-wide">NOTES</span>
              </label>
              <textarea rows={3} className={`${inputEdit} resize-y min-h-[72px]`}
                placeholder="Optional notes…"
                value={draft.NOTES ?? ''} onChange={e => setDraft(d => ({ ...d, NOTES: e.target.value }))} />
            </div>
          </div>
        </div>

        {/* Footer */}
        <div className="flex-shrink-0 px-6 py-3.5 bg-jj-gray-01 border-t border-jj-gray-02 flex items-center justify-between gap-3">
          <div className="flex items-center gap-1.5 font-johnson-text text-xs text-jj-gray-05">
            <Ic src={activitySrc} className="w-3 h-3" filter={F.gray07} />
            CREATED_DATE &amp; UPDATED_DATE set server-side
          </div>
          <div className="flex items-center gap-2">
            <button onClick={onClose} disabled={saving}
              className="px-4 py-2 rounded-lg font-johnson-text text-sm font-bold text-jj-gray-07 bg-white hover:bg-jj-gray-02 transition-colors border border-jj-gray-03 disabled:opacity-50">
              Cancel
            </button>
            <button onClick={() => setConfirmOpen(true)} disabled={saving}
              className="inline-flex items-center gap-2 px-5 py-2 rounded-lg font-johnson-text text-sm font-bold text-white bg-jj-red hover:bg-jj-maroon-05 transition-colors shadow-sm disabled:opacity-50">
              {saving
                ? <><Ic src={activitySrc} className="w-4 h-4 animate-spin" filter={F.white} />Creating…</>
                : <><Ic src={addSrc} className="w-4 h-4" filter={F.white} />Create Row</>
              }
            </button>
          </div>
        </div>

        {confirmOpen && (
          <div className="absolute inset-0 bg-white bg-opacity-80 flex items-center justify-center rounded-2xl" style={{ backdropFilter: 'blur(4px)', zIndex: 10 }}>
            <div className="bg-white rounded-2xl shadow-2xl border border-jj-gray-02 mx-6 p-8 flex flex-col items-center gap-5 text-center" style={{ maxWidth: '360px', animation: 'slideUp 0.15s ease-out' }}>
              <div className="w-14 h-14 rounded-2xl bg-red-50 border border-jj-red border-opacity-20 flex items-center justify-center">
                <Ic src={addSrc} className="w-7 h-7" filter={F.red} />
              </div>
              <div>
                <h3 className="font-johnson-display text-lg text-jj-gray-08 mb-1">Create this row?</h3>
                <p className="font-johnson-text text-sm text-jj-gray-05">
                  A new row will be inserted for <span className="font-bold text-jj-gray-08">{draft.REPORT_SET || '—'}</span>{' '}
                  ({draft.FROM_DATE || '?'} → {draft.TO_DATE || '?'}).
                </p>
              </div>
              <div className="flex items-center gap-3 w-full">
                <button onClick={() => setConfirmOpen(false)}
                  className="flex-1 px-4 py-2.5 rounded-xl font-johnson-text text-sm font-bold text-jj-gray-07 bg-white hover:bg-jj-gray-02 transition-colors border border-jj-gray-03">
                  Cancel
                </button>
                <button onClick={() => { setConfirmOpen(false); handleSave(); }}
                  className="flex-1 inline-flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl font-johnson-text text-sm font-bold text-white bg-jj-red hover:bg-jj-maroon-05 transition-colors shadow-sm">
                  <Ic src={checkSrc} className="w-4 h-4" filter={F.white} />Yes, Create
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}


function DQTablesSection({ api }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const VALID_DQ_TABS = DQ_TABLE_TABS.map(t => t.id);

  const [activeTable, setActiveTableState] = useState(() => {
    const t = searchParams.get('dqtab') ?? '';
    return VALID_DQ_TABS.includes(t) ? t : 'rule-input';
  });
  const [isFullscreen, setIsFullscreen] = useState(false);
  const containerRef = useRef(null);

  // Sync sub-tab → URL (replace, this is a sibling-view flip within the Admin tab,
  // not a navigation step worth its own history entry).
  const setActiveTable = (id) => {
    setActiveTableState(id);
    setSearchParams(prev => {
      const next = new URLSearchParams(prev);
      next.set('dqtab', id);
      return next;
    }, { replace: true });
  };

  // React to back/forward navigation changing the URL dqtab param.
  useEffect(() => {
    const t = searchParams.get('dqtab') ?? '';
    const resolved = VALID_DQ_TABS.includes(t) ? t : 'rule-input';
    if (resolved !== activeTable) setActiveTableState(resolved);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  useEffect(() => {
    const onFsChange = () => setIsFullscreen(!!document.fullscreenElement);
    document.addEventListener('fullscreenchange', onFsChange);
    return () => document.removeEventListener('fullscreenchange', onFsChange);
  }, []);

  return (
    <div
      ref={containerRef}
      className={isFullscreen ? 'fixed inset-0 z-[200] flex flex-col bg-white overflow-hidden' : ''}
    >
    <SectionCard title="DQ Tables — Full Visibility" icon={dashboardSrc} accentColor="text-jj-violet-03" extraStyle={isFullscreen ? { flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0, borderRadius: 0 } : {}}>
      {/* Sub-tab selector */}
      <div className="flex border-b border-jj-gray-02 px-5 bg-white">
        {DQ_TABLE_TABS.map(t => {
          const active = activeTable === t.id;
          return (
            <button
              key={t.id}
              onClick={() => setActiveTable(t.id)}
              className={`relative flex items-center gap-1.5 px-4 py-2.5 font-johnson-text text-sm whitespace-nowrap transition-colors ${
                active ? 'text-jj-violet-03 font-bold' : 'text-jj-gray-06 hover:text-jj-gray-08 hover:bg-jj-gray-01'
              }`}
            >
              <Ic src={t.icon} className="w-4 h-4" filter={active ? F.violet : F.gray06} />
              {t.label}
              {active && <span className="absolute bottom-0 left-0 right-0 h-0.5 bg-jj-violet-03 rounded-t" />}
            </button>
          );
        })}
      </div>
      <DQTableViewer api={api} tableType={activeTable} isFullscreen={isFullscreen} containerRef={containerRef} />
    </SectionCard>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// Admin Tab — top-level container
// ═══════════════════════════════════════════════════════════════════════════════
function AdminTab({ api }) {
  return (
    <div className="space-y-6">
      {/* Header banner */}
      <div className="flex items-center gap-4 px-5 py-4 bg-white rounded-xl border border-jj-gray-02 shadow-sm flex-wrap">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-jj-red flex items-center justify-center flex-shrink-0">
            <Ic src={safetySrc} className="w-5 h-5" filter={F.white} />
          </div>
          <div>
            <h2 className="font-johnson-display text-base text-jj-gray-08" style={{ lineHeight: '110%' }}>
              Superuser Configuration
            </h2>
            <p className="font-johnson-text text-xs text-jj-gray-06 mt-0.5">
              Admin-only controls for dataset mapping and full DQ table visibility.
            </p>
          </div>
        </div>
      </div>

      <div data-tour="bdc-admin-section">
        <DQTablesSection api={api} />
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// Guided Tour
// ═══════════════════════════════════════════════════════════════════════════════

const buildTourSteps = (isAdmin, canRunBDC) => [
  {
    tab: 'overview', target: null,
    title: 'Welcome to Business Defined Checks',
    body: 'This guided tour walks you through every feature on this page. Use Next / Back to navigate between steps, click any dot to jump directly to a step, or press Escape to exit at any time.',
  },
  {
    tab: 'overview', target: '[data-tour="bdc-env"]',
    title: 'Rule Count',
    body: 'Shows the total number of active rules loaded.',
  },
  ...(canRunBDC ? [{
    tab: 'overview', target: '[data-tour="bdc-actions"]',
    title: 'Action Buttons',
    body: 'Run Compilation builds the DQ rule logic in Snowflake. Run Validation executes all rules against live data and streams the output in real-time. Refresh reloads rule metadata.',
  }] : []),
  {
    tab: 'overview', target: '[data-tour="bdc-tabs"]',
    title: 'Navigation Tabs',
    body: 'Five views are available. Overview shows a high-level summary. Dataset Info groups rules by business dataset. Trends plots pass rates over time. Failed Rows lets you drill into individual failures. Superuser (admins only) gives full table access and edit capabilities.',
  },
  {
    tab: 'overview', target: '[data-tour="bdc-kpi-cards"]',
    title: 'KPI Summary Cards',
    body: 'Three headline numbers from the latest execution: how many tables are being monitored, how many rules ran, and the average pass rate across all rules. A healthy system should trend toward 100%.',
  },
  {
    tab: 'overview', target: '[data-tour="bdc-rules-table"]',
    title: 'Active Rules Table',
    body: 'Every active rule listed in one place. Each row shows the Rule ID, title, severity (Critical or High), the Snowflake table it monitors, a colour-coded pass-rate progress bar (green ≥95%, blue ≥80%, red below), and its current status.',
  },
  {
    tab: 'dataset-health', target: '[data-tour="bdc-dataset-table"]',
    title: 'Dataset Health Overview',
    body: 'Rules are grouped by business dataset so you can see aggregate health at a glance. The pass rate and status badge reflect the average across all rules in that dataset. Click any row to open a detailed modal showing every individual rule within it.',
  },
  {
    tab: 'trends', target: '[data-tour="bdc-rule-select"]',
    title: 'Rule Selector',
    body: 'Use this dropdown to choose which rule to analyse. Options are labelled with the Rule ID followed by the rule title. The card header shows the total number of available rules and a hint about what the view does.',
  },
  {
    tab: 'trends', target: '[data-tour="bdc-trend-chart"]',
    title: 'Historical Trend Chart',
    body: 'Plots the selected rule\'s pass rate over every historical execution. A steady line at 100% is ideal. Dips indicate when failures appeared. Hover any data point to see the exact percentage and date.',
  },
  {
    tab: 'failed-rows', target: '[data-tour="bdc-rule-select"]',
    title: 'Rule Selector — Failed Rows',
    body: 'Pick a rule to investigate. Options include the rule ID, title, and target table together so you can quickly identify the right rule without needing to look it up separately.',
  },
  {
    tab: 'failed-rows', target: '[data-tour="bdc-sql-section"]',
    title: 'SQL Predicate',
    body: 'The exact SQL condition that defines what a failure looks like for this rule. Any row that satisfies this condition is counted as failed. Expand the section to read the full SQL and use the Copy button to share it.',
  },
  {
    tab: 'failed-rows', target: '[data-tour="bdc-show-failed"]',
    title: 'Show Failed Rows',
    body: 'Loads every row that currently fails this rule into a paginated table (100 rows per page). If the rule has COLOR_COLUMNS configured in DQ_RULE_INPUT, rows are colour-coded by the day difference between those two date columns: green ≤7 days, yellow 8–15 days, red ≥16 days. You can also filter by colour band.',
  },
  {
    tab: 'failed-rows', target: '[data-tour="bdc-delta"]',
    title: 'Delta Report',
    body: 'Compares the current run against the previous run. The result is split into two sheets: New Failures (rows that appeared since last time) and Resolved Rows (rows that were failing before but are no longer in the current run). Download as a formatted Excel report.',
  },
  {
    tab: 'failed-rows', target: '[data-tour="bdc-adv-delta"]',
    title: 'Advanced Delta Report',
    body: 'If you need to compare against a specific historical snapshot rather than the most recent run, use this. A picker lets you browse every archived run stored in the DQ_STG schema, with the default highlighted. Useful for investigating exactly when a batch of failures first appeared.',
  },
  ...(isAdmin ? [{
    tab: 'admin', target: '[data-tour="bdc-admin-section"]',
    title: 'Superuser — DQ Tables',
    body: 'Admin-only area. The DQ Rule Governance table is the source of truth for rule configuration — you can create new rules, edit existing ones (with full versioning: edits create a new versioned row and deactivate the old one), and toggle rules active or inactive. The AI Rules Registry shows validated SQL predicates.',
  }, {
    tab: 'review', target: null,
    title: 'Review — Rule Sign-off',
    body: 'Admin-only area. Every active rule appears here with its latest execution result. You can Accept or Reject each rule and leave a note explaining the decision. A progress bar tracks how many rules have been reviewed. Pending rules are highlighted at the top, and the tab badge shows the count of rules still awaiting a decision.',
  }] : []),
];

function TourGuide({ isAdmin, canRunBDC, setActiveTab, onClose }) {
  const steps = buildTourSteps(isAdmin, canRunBDC);
  const [step, setStep]   = useState(0);
  const [rect, setRect]   = useState(null);
  const [ready, setReady] = useState(false);
  const current = steps[step];
  const PAD = 10;

  useEffect(() => {
    setReady(false);
    if (current.tab) setActiveTab(current.tab);
    const delay = current.tab ? 320 : 60;
    const t = setTimeout(() => {
      if (!current.target) { setRect(null); setReady(true); return; }
      const el = document.querySelector(current.target);
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        setTimeout(() => {
          const r = el.getBoundingClientRect();
          setRect({ top: r.top, left: r.left, width: r.width, height: r.height });
          setReady(true);
        }, 120);
      } else { setRect(null); setReady(true); }
    }, delay);
    return () => clearTimeout(t);
  }, [step]);

  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onKey);
    document.body.style.overflow = 'hidden';
    return () => { document.removeEventListener('keydown', onKey); document.body.style.overflow = 'unset'; };
  }, []);

  const W = typeof window !== 'undefined' ? window.innerWidth  : 1200;
  const H = typeof window !== 'undefined' ? window.innerHeight : 800;
  const TW = 400;

  const tooltipPos = () => {
    if (!rect) return { top: '50%', left: '50%', transform: 'translate(-50%,-50%)' };
    let top  = rect.top + rect.height + PAD + 14;
    let left = rect.left + rect.width / 2 - TW / 2;
    const isBelow = top + 260 > H - 16;
    if (isBelow) top = rect.top - 260 - PAD - 14;
    left = Math.max(12, Math.min(left, W - TW - 12));
    return { top, left };
  };

  const arrowOnTop = rect && (tooltipPos().top < rect.top);
  const tPos = tooltipPos();
  const arrowLeft = rect ? Math.min(Math.max(rect.left + rect.width / 2 - (typeof tPos.left === 'number' ? tPos.left : 0) - 8, 16), TW - 32) : 40;

  if (!ready) return null;

  return ReactDOM.createPortal(
    <div style={{ position: 'fixed', inset: 0, zIndex: 99998, pointerEvents: 'none' }}>
      {/* Backdrop with spotlight cutout */}
      <svg
        style={{ position: 'fixed', inset: 0, width: '100%', height: '100%', pointerEvents: 'all' }}
        onClick={onClose}
      >
        <defs>
          <mask id="tour-mask">
            <rect width="100%" height="100%" fill="white" />
            {rect && (
              <rect
                x={rect.left - PAD} y={rect.top - PAD}
                width={rect.width + PAD * 2} height={rect.height + PAD * 2}
                rx="8" fill="black"
              />
            )}
          </mask>
        </defs>
        <rect width="100%" height="100%" fill="rgba(0,0,0,0.70)" mask="url(#tour-mask)" />
      </svg>

      {/* Pulsing highlight border */}
      {rect && (
        <div style={{
          position: 'fixed', pointerEvents: 'none', zIndex: 99999,
          top: rect.top - PAD, left: rect.left - PAD,
          width: rect.width + PAD * 2, height: rect.height + PAD * 2,
          borderRadius: 8, border: '2px solid #eb1700',
          animation: 'tourPulse 2s ease-in-out infinite',
        }} />
      )}

      {/* Tooltip card */}
      <div
        style={{ position: 'fixed', width: TW, zIndex: 100000, pointerEvents: 'all', ...tPos }}
        onClick={e => e.stopPropagation()}
      >
        {/* Arrow — top */}
        {rect && !arrowOnTop && (
          <div style={{ position: 'absolute', top: -9, left: arrowLeft, width: 18, height: 9 }}>
            <svg width="18" height="9" viewBox="0 0 18 9"><path d="M0 9 L9 0 L18 9Z" fill="white" /></svg>
          </div>
        )}

        <div style={{ borderRadius: 14, overflow: 'hidden', boxShadow: '0 24px 64px rgba(0,0,0,0.36), 0 0 0 1px rgba(0,0,0,0.06)' }}>
          {/* Header */}
          <div style={{ background: '#eb1700', padding: '12px 16px', display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
              <Ic src={infoSrc} className="w-4 h-4" filter={F.gray05} style={{ flexShrink: 0 }} />
              <span style={{ fontFamily: 'JohnsonDisplay, Arial, sans-serif', fontWeight: 700, fontSize: 13, color: 'white', lineHeight: '120%' }}>
                {current.title}
              </span>
            </div>
            <button
              onClick={onClose}
              style={{ flexShrink: 0, width: 22, height: 22, borderRadius: 6, background: 'rgba(255,255,255,0.18)', border: 'none', cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center' }}
            >
              <svg width="9" height="9" viewBox="0 0 9 9" fill="none">
                <path d="M1 1l7 7M8 1l-7 7" stroke="white" strokeWidth="1.8" strokeLinecap="round" />
              </svg>
            </button>
          </div>

          {/* Body */}
          <div style={{ background: 'white', padding: '14px 16px 10px' }}>
            <p style={{ fontFamily: 'JohnsonText, Arial, sans-serif', fontSize: 13, color: '#312c2a', lineHeight: '1.65', margin: 0 }}>
              {current.body}
            </p>
          </div>

          {/* Footer */}
          <div style={{ background: 'white', padding: '8px 16px 14px', display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8, borderTop: '1px solid #f1efed' }}>
            {/* Step dots */}
            <div style={{ display: 'flex', gap: 4, alignItems: 'center', flexWrap: 'wrap', maxWidth: 160 }}>
              {steps.map((_, i) => (
                <button key={i} onClick={() => setStep(i)} style={{
                  width: i === step ? 18 : 6, height: 6, borderRadius: 99, padding: 0, border: 'none',
                  background: i === step ? '#eb1700' : '#d5cfc9', cursor: 'pointer',
                  transition: 'all 0.22s ease',
                }} />
              ))}
            </div>

            {/* Nav */}
            <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexShrink: 0 }}>
              <span style={{ fontFamily: 'JohnsonText, Arial, sans-serif', fontSize: 10, color: '#a39992' }}>
                {step + 1} / {steps.length}
              </span>
              {step > 0 && (
                <button onClick={() => setStep(s => s - 1)} style={{
                  padding: '5px 11px', borderRadius: 7, border: '1px solid #d5cfc9', background: 'white',
                  fontFamily: 'JohnsonText, Arial, sans-serif', fontSize: 11, fontWeight: 700, color: '#6e6259', cursor: 'pointer',
                }}>← Back</button>
              )}
              {step < steps.length - 1 ? (
                <button onClick={() => setStep(s => s + 1)} style={{
                  padding: '5px 13px', borderRadius: 7, border: 'none', background: '#eb1700',
                  fontFamily: 'JohnsonText, Arial, sans-serif', fontSize: 11, fontWeight: 700, color: 'white', cursor: 'pointer',
                }}>Next →</button>
              ) : (
                <button onClick={onClose} style={{
                  padding: '5px 13px', borderRadius: 7, border: 'none', background: '#328714',
                  fontFamily: 'JohnsonText, Arial, sans-serif', fontSize: 11, fontWeight: 700, color: 'white', cursor: 'pointer',
                }}>✓ Done</button>
              )}
            </div>
          </div>
        </div>

        {/* Arrow — bottom */}
        {rect && arrowOnTop && (
          <div style={{ position: 'absolute', bottom: -9, left: arrowLeft, width: 18, height: 9 }}>
            <svg width="18" height="9" viewBox="0 0 18 9"><path d="M0 0 L9 9 L18 0Z" fill="white" /></svg>
          </div>
        )}
      </div>

      <style>{`
        @keyframes tourPulse {
          0%, 100% { box-shadow: 0 0 0 3px rgba(235,23,0,0.20); }
          50%       { box-shadow: 0 0 0 7px rgba(235,23,0,0.06); }
        }
      `}</style>
    </div>,
    document.body
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// Main Component
// ═══════════════════════════════════════════════════════════════════════════════
// ═══════════════════════════════════════════════════════════════════════════════
// ReviewTab — Admin review queue for BDC rules
// Design: J&J brand-compliant. Table lists all rules; "Start Review" opens a
// right-side detail panel with full rule info, SQL predicate, and a
// "View Failed Rows" button that opens a full-screen modal with color coding.
// ═══════════════════════════════════════════════════════════════════════════════
function ReviewTab({ api, userEmail, onBadgeChange, isDeltaMode = false, setIsDeltaMode = () => {} }) {
  const API_BASE_URL = api.baseURL;
  const PAGE_SIZE = 50;

  // ── State ──────────────────────────────────────────────────────────────────
  const [rules,         setRules]         = useState([]);
  const [isLoading,     setIsLoading]     = useState(true);
  const [filterStatus,  setFilterStatus]  = useState('ALL');
  const [reviewingRule, setReviewingRule] = useState(null);   // rule open in detail panel
  const [notes,         setNotes]         = useState({});
  const [isSaving,      setIsSaving]      = useState({});
  const [history,          setHistory]          = useState([]);
  const [histLoading,      setHistLoading]      = useState(false);
  const [ruleHistoryModal, setRuleHistoryModal] = useState(null); // rule_id string, or null
  const [toasts,           setToasts]           = useState([]);
  const { sortKey: rvSortKey, sortDir: rvSortDir, toggle: rvToggle } = useSortable();

  // Failed rows modal state
  const [frModalOpen,        setFrModalOpen]        = useState(false);
  const [frData,             setFrData]             = useState(null);   // { cols, rows, total, totalPages, page }
  const [frPage,             setFrPage]             = useState(1);
  const [frLoading,          setFrLoading]          = useState(false);
  const [frColWidths,        setFrColWidths]        = useState({});
  const [frWordWrap,         setFrWordWrap]         = useState(false);
  const [frColorFilter,      setFrColorFilter]      = useState('ALL');
  const [frDownloading,      setFrDownloading]      = useState(false);
  const [frDownloadProgress, setFrDownloadProgress] = useState(null);
  const [frFullscreen,       setFrFullscreen]       = useState(false);
  const frModalRef = useRef(null);
  const frDragRef  = useRef(null);

  // Rejection reason dialog
  const [rejectDialogOpen, setRejectDialogOpen] = useState(false);
  const [rejectReason,     setRejectReason]     = useState('');
  const [sqlCopied,        setSqlCopied]        = useState(false);

  const FR_COLOR_KEYS = [
    { key: 'green',  label: '\u2264 7 days',  cls: 'bg-green-50 text-jj-green-03 border-jj-green-03',  activeCls: 'bg-jj-green-03 text-white border-jj-green-03' },
    { key: 'yellow', label: '8\u201315 days', cls: 'bg-yellow-50 text-yellow-700 border-jj-yellow-01', activeCls: 'bg-jj-yellow-01 text-jj-gray-08 border-jj-yellow-01' },
    { key: 'red',    label: '\u2265 16 days', cls: 'bg-red-50 text-jj-maroon-05 border-jj-maroon-05',  activeCls: 'bg-jj-red text-white border-jj-red' },
  ];

  // Close panel when filter changes
  useEffect(() => { setReviewingRule(null); }, [filterStatus]);

  // ── Helpers ────────────────────────────────────────────────────────────────
  const addToast = (msg, type = 'success') => {
    const id = Date.now();
    setToasts(t => [...t, { id, msg, type }]);
    setTimeout(() => setToasts(t => t.filter(x => x.id !== id)), 4000);
  };

  const reviewStatus = r => r.review_status || 'NOT_REVIEWED';

  // Deduplicate by rule_id — backend JOINs can produce multiple rows per rule;
  // keep the first occurrence (API already orders by review status priority).
  const deduped = rules.filter((r, i, arr) => arr.findIndex(x => x.rule_id === r.rule_id) === i);

  const counts = {
    ALL:          deduped.length,
    NOT_REVIEWED: deduped.filter(r => reviewStatus(r) === 'NOT_REVIEWED').length,
    ACCEPTED:     deduped.filter(r => reviewStatus(r) === 'ACCEPTED').length,
    REJECTED:     deduped.filter(r => reviewStatus(r) === 'REJECTED').length,
  };

  const filteredRules = filterStatus === 'ALL'
    ? deduped
    : deduped.filter(r => reviewStatus(r) === filterStatus);

  const sortedFilteredRules = rvSortKey ? [...filteredRules].sort((a, b) => {
    let av = rvSortKey === 'review_status'  ? (REVIEW_ORDER[reviewStatus(a)] ?? 99)
           : rvSortKey === 'pass_percentage'? (a.pass_percentage ?? 0)
           : String(a[rvSortKey] ?? '').toLowerCase();
    let bv = rvSortKey === 'review_status'  ? (REVIEW_ORDER[reviewStatus(b)] ?? 99)
           : rvSortKey === 'pass_percentage'? (b.pass_percentage ?? 0)
           : String(b[rvSortKey] ?? '').toLowerCase();
    const cmp = typeof av === 'number' ? av - bv : av.localeCompare(bv);
    return rvSortDir === 'asc' ? cmp : -cmp;
  }) : filteredRules;

  const reviewedCount = counts.ACCEPTED + counts.REJECTED;
  const progressPct   = counts.ALL > 0 ? Math.round((reviewedCount / counts.ALL) * 100) : 0;

  // Group full history by rule_id for the inline reviewer display and modal
  const historyByRule = history.reduce((acc, item) => {
    const id = item.RULE_ID;
    if (!acc[id]) acc[id] = [];
    acc[id].push(item);
    return acc;
  }, {});

  // ── Data loading ───────────────────────────────────────────────────────────
  const loadRules = async () => {
    setIsLoading(true);
    try {
      const r = await fetch(`${API_BASE_URL}/api/review/rules?delta_mode=${isDeltaMode}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      setRules(d.rules ?? []);
      const b  = await fetch(`${API_BASE_URL}/api/review/badge?delta_mode=${isDeltaMode}`);
      const bd = await b.json();
      onBadgeChange(bd.pending ?? 0);
    } catch (e) { addToast(`Could not load rules: ${e.message}`, 'error'); }
    finally { setIsLoading(false); }
  };

  useEffect(() => { loadRules(); }, [isDeltaMode]);

  // ── Failed rows for modal ──────────────────────────────────────────────────
  const loadFailedRows = async (ruleId, page = 1, filter = frColorFilter) => {
    setFrLoading(true);
    try {
      const r = await fetch(
        `${API_BASE_URL}/api/rules/${ruleId}/failed-rows?page=${page}&page_size=${PAGE_SIZE}&color_filter=${filter}`,
        { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) }
      );
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      const totalPages = d.total_pages ?? Math.ceil((d.total_count || 0) / PAGE_SIZE);
      setFrData({ cols: d.columns, rows: d.rows, total: d.total_count, totalPages, page });
      setFrPage(page);
      setFrColWidths(fitColumnWidths(d.columns || [], d.rows || []));
    } catch (e) { addToast(`Could not load failed rows: ${e.message}`, 'error'); }
    finally { setFrLoading(false); }
  };

  // Re-fetch when color filter changes (while modal is open)
  useEffect(() => {
    if (frModalOpen && reviewingRule) loadFailedRows(reviewingRule.rule_id, 1, frColorFilter);
  }, [frColorFilter]);

  // ── Download failed rows Excel ─────────────────────────────────────────────
  const downloadFailedRows = async () => {
    if (!reviewingRule || frDownloading) return;
    setFrDownloading(true);
    setFrDownloadProgress({ pct: 0, label: 'Starting…' });
    try {
      const prepareUrl = `${API_BASE_URL}/api/rules/${reviewingRule.rule_id}/prepare-download`;
      await new Promise((resolve, reject) => {
        const es = apiEventSource(prepareUrl);
        es.onmessage = async (event) => {
          try {
            const data = JSON.parse(event.data);
            if (data.type === 'error') { es.close(); reject(new Error(data.message)); return; }
            if (data.type === 'ready') {
              es.close();
              setFrDownloadProgress({ pct: 100, label: 'Downloading file…' });
              const dlUrl = `${API_BASE_URL}/api/rules/${reviewingRule.rule_id}/download-ready?token=${data.token}`;
              const resp  = await fetch(dlUrl);
              if (!resp.ok) throw new Error('Download failed');
              const contentLength = resp.headers.get('Content-Length');
              const total  = contentLength ? parseInt(contentLength, 10) : null;
              const reader = resp.body.getReader();
              const chunks = []; let received = 0;
              while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                chunks.push(value); received += value.length;
                if (total) setFrDownloadProgress({ pct: Math.min(99, Math.round((received / total) * 100)), label: 'Downloading…' });
              }
              setFrDownloadProgress({ pct: 100, label: 'Saving file…' });
              const blob = new Blob(chunks);
              const a = Object.assign(document.createElement('a'), {
                href: URL.createObjectURL(blob),
                download: data.filename || `${reviewingRule.rule_id}_failed_rows.xlsx`,
              });
              a.click(); URL.revokeObjectURL(a.href);
              logAuditEvent(API_BASE_URL, userEmail, 'bdc_review_failed_rows_downloaded', reviewingRule.rule_id,
                `Downloaded failed rows for rule "${reviewingRule.rule_id}" (under review)`);
              resolve();
              return;
            }
            setFrDownloadProgress({ pct: data.pct, label: data.label });
          } catch (e) { es.close(); reject(e); }
        };
        es.onerror = () => { es.close(); reject(new Error('Connection lost during preparation')); };
      });
    } catch (e) { addToast(`Download failed: ${e.message}`, 'error'); }
    finally { setFrDownloading(false); setFrDownloadProgress(null); }
  };

  const onFrResize = (e, ci) => {
    e.preventDefault();
    const startX     = e.clientX;
    const startWidth = frColWidths[ci] ?? COL_MAX_WIDTH;
    frDragRef.current = { ci, startX, startWidth };
    const onMove = ev => {
      if (!frDragRef.current) return;
      const { ci: _ci, startX: _sx, startWidth: _sw } = frDragRef.current;
      setFrColWidths(prev => ({ ...prev, [_ci]: Math.max(COL_MIN_WIDTH, _sw + (ev.clientX - _sx)) }));
    };
    const onUp = () => {
      frDragRef.current = null;
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  };

  const openReview = (rule) => {
    setReviewingRule(rule);
    setFrModalOpen(false);
    setFrData(null);
    setFrPage(1);
    setFrColWidths({});
    setFrWordWrap(false);
    setFrColorFilter('ALL');
    setFrDownloading(false);
    setFrDownloadProgress(null);
    setSqlCopied(false);
    setFrFullscreen(false);
    document.body.style.overflow = 'hidden';
  };

  const closeReview = () => {
    setReviewingRule(null);
    setFrModalOpen(false);
    document.body.style.overflow = 'unset';
  };

  const openFailedRowsModal = () => {
    setFrModalOpen(true);
    if (!frData) {
      loadFailedRows(reviewingRule.rule_id, 1);
      logAuditEvent(API_BASE_URL, userEmail, 'bdc_review_failed_rows_viewed', reviewingRule.rule_id,
        `Viewed failed rows for rule "${reviewingRule.rule_id}" (under review)`);
    }
  };

  const closeFailedRowsModal = () => setFrModalOpen(false);

  // Keyboard Escape support
  useEffect(() => {
    const onKey = e => {
      if (e.key !== 'Escape') return;
      if (frModalOpen) { closeFailedRowsModal(); return; }
      if (reviewingRule) { closeReview(); }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [frModalOpen, reviewingRule]);

  // Clean up body scroll lock on unmount
  useEffect(() => () => { document.body.style.overflow = 'unset'; }, []);

  const submitReview = async (rule, status, noteOverride = null) => {
    setIsSaving(s => ({ ...s, [rule.rule_id]: status }));
    try {
      const r = await fetch(
        `${API_BASE_URL}/api/review/submit?delta_mode=${isDeltaMode}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-User-Email': userEmail },
          body: JSON.stringify({
            rule_id:                rule.rule_id,
            rule_title:             rule.rule_title,
            status,
            note:                   noteOverride ?? notes[rule.rule_id] ?? null,
            sql_predicate_snapshot: rule.sql_predicate  || null,

          }),
        }
      );
      if (!r.ok) throw new Error((await r.json()).detail || `HTTP ${r.status}`);
      addToast(
        status === 'ACCEPTED'
          ? `${rule.rule_id} approved — now visible to users`
          : `${rule.rule_id} rejected`,
        status === 'ACCEPTED' ? 'success' : 'info'
      );
      logAuditEvent(API_BASE_URL, userEmail,
        status === 'ACCEPTED' ? 'bdc_review_accepted' : 'bdc_review_rejected',
        rule.rule_id,
        status === 'ACCEPTED'
          ? `Approved rule "${rule.rule_title || rule.rule_id}" — now visible to users`
          : `Rejected rule "${rule.rule_title || rule.rule_id}"${(noteOverride ?? notes[rule.rule_id]) ? `: ${noteOverride ?? notes[rule.rule_id]}` : ''}`);
      setNotes(n => { const c = { ...n }; delete c[rule.rule_id]; return c; });
      closeReview();
      await loadRules();
    } catch (e) { addToast(e.message, 'error'); }
    finally { setIsSaving(s => ({ ...s, [rule.rule_id]: null })); }
  };

  const loadHistory = async () => {
    setHistLoading(true);
    try {
      const r = await fetch(`${API_BASE_URL}/api/review/history?limit=1000&delta_mode=${isDeltaMode}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      setHistory(d.history ?? []);
    } catch (e) { /* non-critical — history is supplemental */ }
    finally { setHistLoading(false); }
  };

  // Load history automatically whenever rules load, and again on mode switch
  useEffect(() => { loadHistory(); }, [isDeltaMode]);

  // ── Sub-components ─────────────────────────────────────────────────────────

  const ReviewPill = ({ status }) => {
    const cfg = {
      NOT_REVIEWED: { bg: 'bg-jj-gray-02',  text: 'text-jj-gray-07',  border: 'border-jj-gray-03',  label: 'Awaiting Review' },
      ACCEPTED:     { bg: 'bg-green-50',     text: 'text-jj-green-03', border: 'border-jj-green-03', label: 'Approved' },
      REJECTED:     { bg: 'bg-red-50',       text: 'text-jj-red',      border: 'border-jj-red',      label: 'Rejected' },
    }[status] || { bg: 'bg-jj-gray-02', text: 'text-jj-gray-07', border: 'border-jj-gray-03', label: status };
    return (
      <span className={`inline-flex items-center px-2.5 py-1 rounded-full font-johnson-text text-xs font-bold border ${cfg.bg} ${cfg.text} ${cfg.border}`}>
        {cfg.label}
      </span>
    );
  };

  const borderAccent = status => ({
    NOT_REVIEWED: '#cbc4bc',
    ACCEPTED:     '#328714',
    REJECTED:     '#eb1700',
  }[status] || '#cbc4bc');

  // ── Render ─────────────────────────────────────────────────────────────────
  return (
    <div className="space-y-5">

      {/* Toast notifications */}
      {toasts.length > 0 && (
        <div className="fixed top-5 right-5 z-[200] flex flex-col gap-2 pointer-events-none">
          {toasts.map(t => (
            <div key={t.id}
              className="animate-slide-in flex items-center gap-2 px-4 py-3 rounded-lg shadow-lg font-johnson-text text-sm text-white"
              style={{ backgroundColor: t.type === 'error' ? '#eb1700' : t.type === 'info' ? '#0f68b2' : '#328714' }}>
              <Ic src={t.type === 'error' ? closeSrc : checkSrc} className="w-4 h-4 flex-shrink-0" filter={F.white} />
              {t.msg}
            </div>
          ))}
        </div>
      )}

      {/* ── Page header ─────────────────────────────────────────────────── */}
      <div className="flex items-start justify-between flex-wrap gap-3">
        <div>
          <h2 className="font-johnson-display text-xl text-jj-gray-08 flex items-center gap-2" style={{ lineHeight: '110%' }}>
            Rule Review
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full font-johnson-text text-[10px] font-bold border border-opacity-30"
              style={{ color: isDeltaMode ? '#541980' : '#8c3bbb', backgroundColor: isDeltaMode ? '#ede5f5' : '#f5f0fb', borderColor: isDeltaMode ? '#541980' : '#8c3bbb' }}>
              {isDeltaMode ? 'Semi-Monthly' : 'Quarterly'}
            </span>
          </h2>
          <p className="font-johnson-text text-sm text-jj-gray-06 mt-0.5">
            Click <strong>Start Review</strong> on any rule to inspect its details, SQL predicate, and failed rows before approving or rejecting it.
            Only approved rules are visible to users. Sign-off is tracked separately for Quarterly and Semi-Monthly — use the toggle above to switch modes.
          </p>
        </div>
        <div className="flex items-center gap-2">

          <button
            onClick={loadRules}
            className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg font-johnson-text text-xs font-bold bg-white border border-jj-gray-03 text-jj-gray-07 hover:border-jj-gray-05 transition-colors"
          >
            <Ic src={refreshSrc} className="w-3.5 h-3.5" filter={F.gray07} />
            Refresh
          </button>
        </div>
      </div>

      {/* ── KPI strip ───────────────────────────────────────────────────── */}
      {!isLoading && (
        <>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            {[
              { filter: 'NOT_REVIEWED', title: 'Awaiting Review', value: counts.NOT_REVIEWED, icon: alertSrc,  color: 'text-jj-gray-07',  borderColor: '#cbc4bc', ringColor: 'ring-jj-gray-05'  },
              { filter: 'ACCEPTED',     title: 'Approved',        value: counts.ACCEPTED,     icon: checkSrc,  color: 'text-jj-green-03', borderColor: '#328714', ringColor: 'ring-jj-green-03' },
              { filter: 'REJECTED',     title: 'Rejected',        value: counts.REJECTED,     icon: closeSrc,  color: 'text-jj-red',      borderColor: '#eb1700', ringColor: 'ring-jj-red'      },
            ].map(({ filter, title, value, icon, color, borderColor, ringColor }) => {
              const active = filterStatus === filter;
              return (
                <button
                  key={filter}
                  onClick={() => setFilterStatus(active ? 'ALL' : filter)}
                  className={`text-left rounded-xl transition-all focus:outline-none ${
                    active ? `ring-2 ${ringColor} ring-offset-1 shadow-md` : 'hover:shadow-md hover:scale-[1.01]'
                  }`}
                  title={active ? 'Click to show all rules' : `Filter by ${title}`}
                >
                  <KPICard title={title} value={value} icon={icon} color={color} borderColor={borderColor} compact />
                </button>
              );
            })}
          </div>

          {/* Progress bar */}
          <div className="bg-white rounded-xl border border-jj-gray-02 px-5 py-3">
            <div className="flex items-center justify-between mb-2">
              <span className="font-johnson-text text-xs font-bold text-jj-gray-06 uppercase tracking-wide">
                Review Progress
              </span>
              <span className="font-johnson-display text-sm text-jj-gray-08 font-bold">
                {reviewedCount} <span className="font-normal text-jj-gray-05">of</span> {counts.ALL} rules reviewed
              </span>
            </div>
            <div className="h-2 bg-jj-gray-02 rounded-full overflow-hidden">
              <div
                className="h-2 rounded-full transition-all duration-700"
                style={{ width: `${progressPct}%`, backgroundColor: progressPct === 100 ? '#328714' : '#eb1700' }}
              />
            </div>
          </div>
        </>
      )}

      {/* ── Rule table ──────────────────────────────────────────────────── */}
      <SectionCard
        title={`${filteredRules.length} Rule${filteredRules.length !== 1 ? 's' : ''}`}
        icon={recordsSrc}
        accentColor="text-jj-red"
        headerRight={
          !isLoading && (
            <div className="relative inline-flex items-center">
              <select
                value={filterStatus}
                onChange={e => setFilterStatus(e.target.value)}
                className="appearance-none pl-3 pr-8 py-1.5 rounded-lg font-johnson-text text-xs font-bold text-jj-gray-07 bg-white border border-jj-gray-03 hover:border-jj-gray-05 focus:outline-none focus:border-jj-red transition-colors cursor-pointer"
              >
                {[
                  { value: 'ALL',          label: 'All'             },
                  { value: 'NOT_REVIEWED', label: 'Awaiting Review' },
                  { value: 'ACCEPTED',     label: 'Approved'        },
                  { value: 'REJECTED',     label: 'Rejected'        },
                ].map(opt => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label} ({counts[opt.value]})
                  </option>
                ))}
              </select>
              <Ic src={chevronRightSrc} className="absolute right-2 top-1/2 -translate-y-1/2 w-3 h-3 pointer-events-none rotate-90" filter={F.gray05} />
            </div>
          )
        }
      >
        {isLoading ? (
          <div className="flex items-center justify-center py-16">
            <LoadingSpinner />
          </div>
        ) : filteredRules.length === 0 ? (
          <EmptyState
            icon={checkSrc}
            title="Nothing to show"
            message={filterStatus === 'ALL' ? 'No rules found.' : `No ${filterStatus.toLowerCase().replace('_', ' ')} rules.`}
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full font-johnson-text text-sm">
              <thead>
                <tr className="border-b-2 border-jj-gray-02">
                  {[
                    { label: 'Rule ID',          key: 'rule_id',          sortable: true  },
                    { label: 'Title',            key: 'rule_title',       sortable: true  },
                    { label: 'Pass Rate',        key: 'pass_percentage',  sortable: true  },
                    { label: 'Review Status',    key: 'review_status',    sortable: true  },
                    { label: 'Last Reviewed By', key: 'last_reviewed_by', sortable: true  },
                    { label: 'Action',           key: null,               sortable: false },
                  ].map(({ label, key, sortable }) => sortable
                    ? <SortableTh key={label} label={label} colKey={key} sortKey={rvSortKey} sortDir={rvSortDir} onSort={rvToggle} />
                    : <th key={label} className="px-5 py-3 text-left text-xs font-bold text-jj-gray-08 bg-jj-gray-01 uppercase tracking-wide whitespace-nowrap">{label}</th>
                  )}
                </tr>
              </thead>
              <tbody>
                {sortedFilteredRules.map((rule, idx) => {
                  const rs = reviewStatus(rule);
                  return (
                    <tr
                      key={`${rule.rule_id}_${idx}`}
                      className="border-b border-jj-gray-02 hover:bg-jj-gray-01 transition-colors"
                      style={{ borderLeft: `3px solid ${borderAccent(rs)}` }}
                    >
                      {/* Rule ID */}
                      <td className="px-5 py-3.5 whitespace-nowrap">
                        <span className="font-johnson-text text-xs font-bold text-jj-blue-03 bg-blue-50 px-2.5 py-1 rounded border border-jj-blue-03 border-opacity-20">
                          {rule.rule_id}
                        </span>
                      </td>

                      {/* Title */}
                      <td className="px-5 py-3.5 max-w-[220px]">
                        <span
                          className="font-johnson-text text-sm text-jj-gray-08 block truncate"
                          title={rule.rule_title || undefined}
                        >
                          {rule.rule_title || <span className="text-jj-gray-04 italic">—</span>}
                        </span>
                        {rule.target_table && (
                          <p
                            className="font-johnson-text text-xs text-jj-gray-05 mt-0.5 font-mono truncate"
                            title={rule.target_table}
                          >{rule.target_table}</p>
                        )}
                      </td>

                      {/* Pass rate */}
                      <td className="px-5 py-3.5 min-w-[160px]">
                        <PassBar pct={rule.pass_percentage ?? 0} />
                      </td>

                      {/* Review status */}
                      <td className="px-5 py-3.5 whitespace-nowrap">
                        <ReviewPill status={rs} />
                      </td>

                      {/* Last reviewed by */}
                      <td className="px-5 py-3.5">
                        {rule.last_reviewed_by ? (() => {
                          const ruleHist = historyByRule[rule.rule_id] || [];
                          const extraCount = ruleHist.length > 1 ? ruleHist.length - 1 : 0;
                          return (
                            <div className="space-y-1">
                              <div className="flex items-center gap-1.5 flex-wrap">
                                <p className="font-johnson-text text-xs text-jj-gray-07 truncate max-w-[140px]">{rule.last_reviewed_by}</p>
                                {extraCount > 0 && (
                                  <span className="inline-flex items-center px-1.5 py-0.5 rounded-full font-johnson-text text-[10px] font-bold bg-jj-gray-02 text-jj-gray-06 border border-jj-gray-03 flex-shrink-0">
                                    +{extraCount} more
                                  </span>
                                )}
                              </div>
                              {rule.last_reviewed_at && (
                                <p className="font-johnson-text text-[10px] text-jj-gray-05">{rule.last_reviewed_at}</p>
                              )}
                              <button
                                onClick={e => { e.stopPropagation(); setRuleHistoryModal(rule.rule_id); }}
                                className="inline-flex items-center gap-1 font-johnson-text text-[10px] font-bold text-jj-blue-03 hover:underline transition-colors mt-0.5"
                              >
                                <Ic src={recordsSrc} className="w-2.5 h-2.5 flex-shrink-0" filter={F.blue} />
                                Show Details
                              </button>
                            </div>
                          );
                        })() : (
                          <span className="text-jj-gray-04 text-xs">Not reviewed yet</span>
                        )}
                      </td>

                      {/* Action — varies by review status */}
                      <td className="px-5 py-3.5 whitespace-nowrap">
                        {rs === 'NOT_REVIEWED' && (
                          <button
                            onClick={() => openReview(rule)}
                            className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg font-johnson-text text-xs font-bold border border-jj-red bg-jj-red text-white hover:bg-jj-maroon-05 hover:border-jj-maroon-05 active:opacity-80 transition-colors"
                          >
                            <Ic src={recordsSrc} className="w-3.5 h-3.5" filter={F.white} />
                            Start Review
                          </button>
                        )}
                        {rs === 'ACCEPTED' && (
                          <button
                            onClick={() => openReview(rule)}
                            className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg font-johnson-text text-xs font-bold border border-jj-green-03 text-jj-green-03 bg-white hover:bg-green-50 active:opacity-80 transition-colors"
                          >
                            <Ic src={checkSrc} className="w-3.5 h-3.5" filter={F.green} />
                            Re-review
                          </button>
                        )}
                        {rs === 'REJECTED' && (
                          <button
                            onClick={() => openReview(rule)}
                            className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg font-johnson-text text-xs font-bold border border-jj-gray-04 text-jj-gray-07 bg-white hover:bg-jj-gray-01 active:opacity-80 transition-colors"
                          >
                            <Ic src={closeSrc} className="w-3.5 h-3.5" filter={F.gray07} />
                            Re-review
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </SectionCard>



      {/* ── Per-Rule Review History Modal ────────────────────────────────── */}
      {ruleHistoryModal && ReactDOM.createPortal(
        <div className="fixed inset-0 z-[120] flex items-center justify-center">
          <div className="absolute inset-0 bg-black bg-opacity-40" onClick={() => setRuleHistoryModal(null)} />
          <div className="relative bg-white rounded-2xl shadow-2xl overflow-hidden flex flex-col" style={{ width: 'min(720px, 94vw)', maxHeight: 'min(80vh, calc(100vh - 2rem))' }}>

            {/* Header */}
            <div className="flex-shrink-0 flex items-center justify-between px-6 py-4 border-b border-jj-gray-02 bg-jj-gray-01">
              <div className="flex items-center gap-3">
                <Ic src={recordsSrc} className="w-4 h-4 flex-shrink-0" filter={F.gray07} />
                <span className="font-johnson-display text-base font-bold text-jj-gray-08">Review History</span>
                <span className="font-johnson-text text-xs font-bold text-jj-blue-03 bg-blue-50 px-2.5 py-1 rounded border border-jj-blue-03 border-opacity-20">
                  {ruleHistoryModal}
                </span>
                {historyByRule[ruleHistoryModal]?.length > 0 && (
                  <span className="font-johnson-text text-xs text-jj-gray-05">
                    {historyByRule[ruleHistoryModal].length} review{historyByRule[ruleHistoryModal].length !== 1 ? 's' : ''}
                  </span>
                )}
              </div>
              <button
                onClick={() => setRuleHistoryModal(null)}
                className="w-8 h-8 flex items-center justify-center rounded-lg hover:bg-jj-gray-02 transition-colors"
              >
                <Ic src={closeSrc} className="w-4 h-4" filter={F.gray07} />
              </button>
            </div>

            {/* Body */}
            <div className="flex-1 overflow-y-auto">
              {histLoading ? (
                <div className="flex items-center justify-center py-16"><LoadingSpinner /></div>
              ) : !historyByRule[ruleHistoryModal]?.length ? (
                <EmptyState icon={recordsSrc} title="No History Yet" message="No review actions have been recorded for this rule." />
              ) : (
                <table className="w-full font-johnson-text text-sm">
                  <thead>
                    <tr className="border-b-2 border-jj-gray-02 sticky top-0 z-10">
                      {['Decision', 'Reviewed By', 'Date & Time', 'Note'].map(h => (
                        <th key={h} className="px-5 py-3 text-left text-xs font-bold text-jj-gray-08 bg-jj-gray-01 uppercase tracking-wide whitespace-nowrap">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-jj-gray-02">
                    {historyByRule[ruleHistoryModal].map((item, i) => (
                      <tr key={item.REVIEW_ID ?? i} className={`transition-colors ${i === 0 ? 'bg-blue-50 bg-opacity-30' : 'hover:bg-jj-gray-01'}`}>
                        <td className="px-5 py-3 whitespace-nowrap">
                          <div className="flex items-center gap-2">
                            <ReviewPill status={item.STATUS} />
                            {i === 0 && (
                              <span className="font-johnson-text text-[10px] font-bold text-jj-blue-03 bg-blue-50 px-1.5 py-0.5 rounded border border-jj-blue-03 border-opacity-20">
                                Latest
                              </span>
                            )}
                          </div>
                        </td>
                        <td className="px-5 py-3 font-johnson-text text-xs text-jj-gray-07 max-w-[180px] truncate">
                          {item.REVIEWED_BY || '—'}
                        </td>
                        <td className="px-5 py-3 whitespace-nowrap font-johnson-text text-xs text-jj-gray-05">
                          {item.REVIEWED_AT ? fmtEastern(item.REVIEWED_AT) : '—'}
                        </td>

                        <td className="px-5 py-3 font-johnson-text text-xs text-jj-gray-05 italic max-w-[220px]">
                          <span className="block truncate" title={item.REVIEW_NOTE || undefined}>
                            {item.REVIEW_NOTE || '—'}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>

          </div>
        </div>,
        document.body
      )}

      {/* ── Review Detail Panel (slide-in from right) ────────────────────── */}
      {reviewingRule && ReactDOM.createPortal(
        <div className="fixed inset-0 z-[100] flex">
          {/* Backdrop */}
          <div className="absolute inset-0 bg-black bg-opacity-40" onClick={closeReview} />

          {/* Slide-in panel */}
          <div
            className="absolute right-0 top-0 h-full bg-white shadow-2xl flex flex-col"
            style={{ width: 'min(760px, 96vw)' }}
          >
            {/* Panel header */}
            <div className="flex-shrink-0 flex items-center justify-between px-6 py-4 border-b border-jj-gray-02 bg-jj-gray-01">
              <div className="flex items-center gap-3 min-w-0">
                <span className="font-johnson-text text-sm font-bold text-jj-blue-03 bg-blue-50 px-3 py-1.5 rounded-lg border border-jj-blue-03 border-opacity-30 flex-shrink-0">
                  {reviewingRule.rule_id}
                </span>
                <ReviewPill status={reviewStatus(reviewingRule)} />
              </div>
              <button
                onClick={closeReview}
                title="Close (Esc)"
                className="flex-shrink-0 w-8 h-8 flex items-center justify-center rounded-lg hover:bg-jj-gray-02 transition-colors ml-3"
              >
                <Ic src={closeSrc} className="w-4 h-4" filter={F.gray07} />
              </button>
            </div>

            {/* Scrollable content */}
            <div className="flex-1 overflow-y-auto px-6 py-5 space-y-6">

              {/* ── Rule Metadata ── */}
              <div>
                <p className="font-johnson-text text-[10px] font-bold text-jj-gray-05 uppercase tracking-widest mb-3">Rule Details</p>
                <div className="grid grid-cols-2 gap-x-6 gap-y-4">
                  <div>
                    <p className="font-johnson-text text-[10px] font-bold text-jj-gray-05 uppercase tracking-wider mb-1">Title</p>
                    <p className="font-johnson-text text-sm font-bold text-jj-gray-08">
                      {reviewingRule.rule_title || <span className="italic font-normal text-jj-gray-04">Untitled</span>}
                    </p>
                  </div>
                  <div>
                    <p className="font-johnson-text text-[10px] font-bold text-jj-gray-05 uppercase tracking-wider mb-1">Rule ID</p>
                    <p className="font-mono text-xs text-jj-blue-03">{reviewingRule.rule_id}</p>
                  </div>
                  <div>
                    <p className="font-johnson-text text-[10px] font-bold text-jj-gray-05 uppercase tracking-wider mb-1">Target Table</p>
                    <p className="font-mono text-xs text-jj-gray-07 bg-jj-gray-01 px-2.5 py-1.5 rounded border border-jj-gray-02 break-all">
                      {reviewingRule.target_table || '—'}
                    </p>
                  </div>
                  <div>
                    <p className="font-johnson-text text-[10px] font-bold text-jj-gray-05 uppercase tracking-wider mb-1">Severity</p>
                    <SeverityBadge severity={reviewingRule.severity} />
                  </div>
                  <div className="col-span-2">
                    <p className="font-johnson-text text-[10px] font-bold text-jj-gray-05 uppercase tracking-wider mb-1">Pass Rate</p>
                    <div className="w-64">
                      <PassBar pct={reviewingRule.pass_percentage ?? 0} />
                    </div>
                  </div>
                  {reviewingRule.rule_description && (
                    <div className="col-span-2">
                      <p className="font-johnson-text text-[10px] font-bold text-jj-gray-05 uppercase tracking-wider mb-1">Description</p>
                      <p className="font-johnson-text text-sm text-jj-gray-07 leading-relaxed">
                        {reviewingRule.rule_description}
                      </p>
                    </div>
                  )}
                </div>
              </div>

              {/* Divider */}
              <div className="border-t border-jj-gray-02" />

              {/* ── AI Output ── */}
              {reviewingRule.ai_output && (() => {
                let parsed = null;
                try { parsed = JSON.parse(reviewingRule.ai_output); } catch {}
                if (!parsed) return null;
                return (
                  <div>
                    <div className="flex items-center gap-2 mb-3">
                      <p className="font-johnson-text text-xs font-bold text-jj-gray-06 uppercase tracking-wide">AI Analysis</p>
                      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full font-johnson-text text-[10px] font-bold bg-jj-violet-03 bg-opacity-10 text-jj-violet-03 border border-jj-violet-03 border-opacity-30">
                        ✦ AI
                      </span>
                    </div>
                    <div className="rounded-xl border border-jj-gray-02 overflow-hidden divide-y divide-jj-gray-02">

                      {/* Interpreted rule */}
                      {parsed.interpreted_rule && (
                        <div className="px-4 py-3 bg-white">
                          <p className="font-johnson-text text-[10px] font-bold text-jj-gray-05 uppercase tracking-wider mb-1">Interpreted Rule</p>
                          <p className="font-johnson-text text-sm text-jj-gray-08 leading-relaxed">{parsed.interpreted_rule}</p>
                        </div>
                      )}

                      {/* Confidence */}
                      {parsed.confidence !== undefined && (
                        <div className="px-4 py-3 bg-jj-gray-01 flex items-center justify-between gap-4">
                          <p className="font-johnson-text text-[10px] font-bold text-jj-gray-05 uppercase tracking-wider flex-shrink-0">AI Confidence</p>
                          <div className="flex items-center gap-2 flex-1">
                            <div className="flex-1 h-1.5 bg-jj-gray-02 rounded-full overflow-hidden">
                              <div
                                className="h-full rounded-full transition-all duration-500"
                                style={{
                                  width: `${Math.round(parsed.confidence * 100)}%`,
                                  backgroundColor: parsed.confidence >= 0.9 ? '#328714' : parsed.confidence >= 0.7 ? '#f59e0b' : '#eb1700'
                                }}
                              />
                            </div>
                            <span
                              className="font-johnson-display text-sm font-bold flex-shrink-0"
                              style={{ color: parsed.confidence >= 0.9 ? '#328714' : parsed.confidence >= 0.7 ? '#d97706' : '#eb1700' }}
                            >
                              {Math.round(parsed.confidence * 100)}%
                            </span>
                          </div>
                        </div>
                      )}

                      {/* Columns used */}
                      {parsed.columns_used?.length > 0 && (
                        <div className="px-4 py-3 bg-white">
                          <p className="font-johnson-text text-[10px] font-bold text-jj-gray-05 uppercase tracking-wider mb-2">Columns Used</p>
                          <div className="flex flex-wrap gap-1.5">
                            {parsed.columns_used.map((col, i) => (
                              <span key={i} className="font-mono text-[11px] font-bold text-jj-blue-03 bg-blue-50 px-2 py-0.5 rounded border border-jj-blue-03 border-opacity-20">
                                {col}
                              </span>
                            ))}
                          </div>
                        </div>
                      )}

                      {/* Assumptions */}
                      {parsed.assumptions?.length > 0 && (
                        <div className="px-4 py-3 bg-jj-gray-01">
                          <p className="font-johnson-text text-[10px] font-bold text-jj-gray-05 uppercase tracking-wider mb-2">Assumptions</p>
                          <ul className="space-y-1">
                            {parsed.assumptions.map((a, i) => (
                              <li key={i} className="flex items-start gap-2">
                                <span className="flex-shrink-0 mt-1.5 w-1 h-1 rounded-full bg-jj-gray-04" />
                                <span className="font-johnson-text text-xs text-jj-gray-07 leading-relaxed">{a}</span>
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}

                    </div>
                  </div>
                );
              })()}

              {reviewingRule.ai_output && <div className="border-t border-jj-gray-02" />}

              {/* ── AI Generated SQL Predicate ── */}
              <div>
                {/* Section label */}
                <div className="flex items-center gap-2 mb-2">
                  <span className="font-johnson-text text-xs font-bold text-jj-gray-06 uppercase tracking-wide">AI Generated SQL Predicate</span>
                  <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full font-johnson-text text-[10px] font-bold bg-jj-violet-03 bg-opacity-10 text-jj-violet-03 border border-jj-violet-03 border-opacity-30">
                    ✦ AI
                  </span>
                </div>

                {/* Code block — VS Code dark */}
                {reviewingRule.sql_predicate ? (
                  <div className="rounded-xl overflow-hidden" style={{ border: '1px solid #3e3e42' }}>
                    {/* Title bar */}
                    <div className="flex items-center justify-between px-4 py-2" style={{ background: '#2d2d2d', borderBottom: '1px solid #3e3e42' }}>
                      <div className="flex items-center gap-1.5">
                        <span className="w-2.5 h-2.5 rounded-full" style={{ background: '#ff5f57' }} />
                        <span className="w-2.5 h-2.5 rounded-full" style={{ background: '#febc2e' }} />
                        <span className="w-2.5 h-2.5 rounded-full" style={{ background: '#28c840' }} />
                        <span className="ml-2 font-mono text-[10px] tracking-wide" style={{ color: '#858585' }}>SQL</span>
                      </div>
                      <button
                        onClick={() => {
                          navigator.clipboard?.writeText(reviewingRule.sql_predicate);
                          setSqlCopied(true);
                          setTimeout(() => setSqlCopied(false), 2000);
                        }}
                        className="font-johnson-text text-[10px] font-bold px-2.5 py-1 rounded transition-all"
                        style={{
                          color:       sqlCopied ? '#28c840' : '#858585',
                          background:  'transparent',
                          border:      sqlCopied ? '1px solid #28c840' : '1px solid #555',
                          minWidth:    '52px',
                        }}
                        onMouseEnter={e => { if (!sqlCopied) { e.currentTarget.style.color = '#d4d4d4'; e.currentTarget.style.borderColor = '#777'; } }}
                        onMouseLeave={e => { if (!sqlCopied) { e.currentTarget.style.color = '#858585'; e.currentTarget.style.borderColor = '#555'; } }}
                      >
                        {sqlCopied ? '✓ Copied!' : 'Copy'}
                      </button>
                    </div>
                    {/* Code body */}
                    <pre
                      className="font-mono text-xs overflow-x-auto whitespace-pre-wrap leading-relaxed max-h-56 overflow-y-auto px-5 py-4"
                      style={{ background: '#1e1e1e', color: '#d4d4d4' }}
                    >
                      {reviewingRule.sql_predicate}
                    </pre>
                  </div>
                ) : (
                  <div className="rounded-xl border border-jj-gray-02 bg-jj-gray-01 px-4 py-4">
                    <span className="font-johnson-text text-xs text-jj-gray-05 italic">
                      No AI-generated SQL predicate found in the rule registry for this rule.
                    </span>
                  </div>
                )}
              </div>

              {/* Divider */}
              <div className="border-t border-jj-gray-02" />

              {/* ── Failed Rows entry point ── */}
              <div>
                <p className="font-johnson-text text-xs font-bold text-jj-gray-06 uppercase tracking-wide mb-2">Failed Rows</p>
                <div className="flex items-center justify-between gap-4 p-4 bg-jj-gray-01 rounded-xl border border-jj-gray-02">
                  <p className="font-johnson-text text-sm text-jj-gray-07 leading-snug">
                    View the rows that failed this rule, with date-based color coding identical to the Failed Rows tab.
                  </p>
                  <button
                    onClick={openFailedRowsModal}
                    className="flex-shrink-0 inline-flex items-center gap-2 px-4 py-2.5 rounded-lg font-johnson-text text-xs font-bold bg-jj-blue-03 text-white hover:opacity-90 active:opacity-80 transition-opacity"
                  >
                    <Ic src={alertSrc} className="w-3.5 h-3.5" filter={F.white} />
                    View Failed Rows
                  </button>
                </div>
              </div>

              {/* Divider */}
              <div className="border-t border-jj-gray-02" />

              {/* ── Review Note ── */}
              <div>
                <label className="font-johnson-text text-xs font-bold text-jj-gray-06 uppercase tracking-wide mb-1.5 block">
                  Review Note{' '}
                  <span className="font-normal normal-case text-jj-gray-05">(optional — saved with your decision)</span>
                </label>
                {reviewingRule.last_review_note && (
                  <p className="font-johnson-text text-xs text-jj-gray-06 italic mb-2 px-3 py-2 bg-white rounded border border-jj-gray-02">
                    Previous note: "{reviewingRule.last_review_note}"
                  </p>
                )}
                <textarea
                  className="w-full rounded-lg border border-jj-gray-03 bg-white px-3 py-2 font-johnson-text text-xs text-jj-gray-08 resize-none outline-none focus:border-jj-red focus:ring-1 focus:ring-jj-red focus:ring-opacity-20"
                  rows={3}
                  placeholder="Add context, observations, or reason for rejection…"
                  value={notes[reviewingRule.rule_id] || ''}
                  onChange={e => setNotes(n => ({ ...n, [reviewingRule.rule_id]: e.target.value }))}
                />
              </div>

            </div>

            {/* ── Panel footer — Approve / Reject ── */}
            <div className="flex-shrink-0 flex items-center justify-between gap-3 px-6 py-4 border-t border-jj-gray-02 bg-jj-gray-01">
              <button
                onClick={closeReview}
                className="inline-flex items-center gap-1.5 px-4 py-2.5 rounded-lg font-johnson-text text-xs font-bold border border-jj-gray-03 text-jj-gray-07 bg-white hover:border-jj-gray-05 transition-colors"
              >
                Cancel
              </button>
              <div className="flex items-center gap-2">
                <button
                  disabled={!!isSaving[reviewingRule.rule_id]}
                  onClick={() => { setRejectReason(''); setRejectDialogOpen(true); }}
                  className={`inline-flex items-center gap-1.5 px-5 py-2.5 rounded-lg font-johnson-text text-xs font-bold border transition-all ${
                    isSaving[reviewingRule.rule_id] === 'REJECTED'
                      ? 'bg-jj-red text-white border-jj-red opacity-60 cursor-not-allowed'
                      : 'bg-jj-red text-white border-jj-red hover:bg-jj-maroon-05 disabled:opacity-40 disabled:cursor-not-allowed'
                  }`}
                >
                  <Ic src={closeSrc} className="w-3.5 h-3.5" filter={F.white} />
                  {isSaving[reviewingRule.rule_id] === 'REJECTED' ? 'Saving…' : 'Reject'}
                </button>
                <button
                  disabled={!!isSaving[reviewingRule.rule_id]}
                  onClick={() => submitReview(reviewingRule, 'ACCEPTED')}
                  className={`inline-flex items-center gap-1.5 px-5 py-2.5 rounded-lg font-johnson-text text-xs font-bold border transition-all ${
                    isSaving[reviewingRule.rule_id] === 'ACCEPTED'
                      ? 'bg-green-100 text-jj-green-03 border-jj-green-03 opacity-60 cursor-not-allowed'
                      : 'bg-jj-green-03 text-white border-jj-green-03 hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed'
                  }`}
                >
                  <Ic src={checkSrc} className="w-3.5 h-3.5" filter={F.white} />
                  {isSaving[reviewingRule.rule_id] === 'ACCEPTED' ? 'Saving…' : 'Approve'}
                </button>
              </div>
            </div>
          </div>
        </div>,
        document.body
      )}

      {/* ── Rejection Reason Dialog ──────────────────────────────────────── */}
      {rejectDialogOpen && reviewingRule && ReactDOM.createPortal(
        <div className="fixed inset-0 z-[120] flex items-center justify-center">
          {/* Backdrop */}
          <div
            className="absolute inset-0 bg-black bg-opacity-40"
            onClick={() => setRejectDialogOpen(false)}
          />

          {/* Dialog card */}
          <div className="relative bg-white rounded-2xl shadow-2xl w-full max-w-md mx-4 overflow-hidden">

            {/* Header — neutral, not red */}
            <div className="flex items-center justify-between px-6 py-4 border-b border-jj-gray-02 bg-jj-gray-01">
              <span className="font-johnson-display text-base font-bold text-jj-gray-08">
                Reason for Rejection
              </span>
              <button
                onClick={() => setRejectDialogOpen(false)}
                className="w-7 h-7 flex items-center justify-center rounded-lg hover:bg-jj-gray-02 transition-colors"
              >
                <Ic src={closeSrc} className="w-3.5 h-3.5" filter={F.gray07} />
              </button>
            </div>

            {/* Body */}
            <div className="px-6 py-5 space-y-4">
              {/* Rule chip */}
              <div className="flex items-center gap-2 flex-wrap">
                <span className="font-johnson-text text-xs text-jj-gray-06">Rejecting:</span>
                <span className="font-johnson-text text-xs font-bold text-jj-blue-03 bg-blue-50 px-2.5 py-1 rounded border border-jj-blue-03 border-opacity-20">
                  {reviewingRule.rule_id}
                </span>
                {reviewingRule.rule_title && (
                  <span className="font-johnson-text text-xs text-jj-gray-07 truncate">
                    {reviewingRule.rule_title}
                  </span>
                )}
              </div>

              {/* Reason textarea */}
              <div>
                <label className="font-johnson-text text-xs font-bold text-jj-gray-07 uppercase tracking-wide mb-1.5 flex items-center gap-1">
                  Reason
                  <span className="text-jj-red">*</span>
                  <span className="font-normal normal-case text-jj-gray-05 tracking-normal ml-1">(required)</span>
                </label>
                <textarea
                  autoFocus
                  className="w-full rounded-lg border border-jj-gray-03 px-3 py-2.5 font-johnson-text text-sm text-jj-gray-08 resize-none outline-none focus:border-jj-gray-07 focus:ring-1 focus:ring-jj-gray-07 focus:ring-opacity-20 transition-colors"
                  rows={4}
                  placeholder="Describe why this rule is being rejected — e.g. incorrect SQL predicate, wrong target table, data quality concern…"
                  value={rejectReason}
                  onChange={e => setRejectReason(e.target.value)}
                />
                {!rejectReason.trim() && (
                  <p className="font-johnson-text text-xs text-jj-gray-05 mt-1.5">
                    Please provide a reason before confirming.
                  </p>
                )}
              </div>
            </div>

            {/* Footer */}
            <div className="flex items-center justify-end gap-2 px-6 py-4 border-t border-jj-gray-02 bg-jj-gray-01">
              <button
                onClick={() => setRejectDialogOpen(false)}
                className="inline-flex items-center px-4 py-2.5 rounded-lg font-johnson-text text-xs font-bold border border-jj-gray-03 text-jj-gray-07 bg-white hover:border-jj-gray-05 transition-colors"
              >
                Cancel
              </button>
              <button
                disabled={!rejectReason.trim() || !!isSaving[reviewingRule.rule_id]}
                onClick={() => {
                  setRejectDialogOpen(false);
                  submitReview(reviewingRule, 'REJECTED', rejectReason.trim());
                }}
                className={`inline-flex items-center gap-1.5 px-5 py-2.5 rounded-lg font-johnson-text text-xs font-bold border transition-all ${
                  !rejectReason.trim() || !!isSaving[reviewingRule.rule_id]
                    ? 'bg-jj-red text-white border-jj-red opacity-40 cursor-not-allowed'
                    : 'bg-jj-red text-white border-jj-red hover:bg-jj-maroon-05'
                }`}
              >
                <Ic src={closeSrc} className="w-3.5 h-3.5" filter={F.white} />
                {isSaving[reviewingRule.rule_id] === 'REJECTED' ? 'Saving…' : 'Confirm Rejection'}
              </button>
            </div>
          </div>
        </div>,
        document.body
      )}

      {/* ── Failed Rows Modal (layered on top of detail panel) ───────────── */}
      {frModalOpen && reviewingRule && ReactDOM.createPortal(
        (() => {
          const [frCol1, frCol2] = parseColorColumns(reviewingRule.color_columns);
          const frHasDateCols = !!(frData && frCol1 && frCol2
            && frData.cols.some(c => c.trim().toUpperCase() === frCol1.toUpperCase())
            && frData.cols.some(c => c.trim().toUpperCase() === frCol2.toUpperCase()));
          return (
            <div className="fixed inset-0 z-[110] flex">
              {/* Backdrop — click closes modal but keeps panel open */}
              <div className="absolute inset-0 bg-black bg-opacity-60" onClick={closeFailedRowsModal} />

              {/* Full-screen modal card */}
              <div
                ref={el => {
                  frModalRef.current = el;
                  if (el && !el._fsListenerAdded) {
                    el._fsListenerAdded = true;
                    const onFsChange = () => setFrFullscreen(!!document.fullscreenElement);
                    document.addEventListener('fullscreenchange', onFsChange);
                  }
                }}
                className="relative flex flex-col bg-white overflow-hidden"
                style={frFullscreen
                  ? { width: '100vw', height: '100vh', borderRadius: 0 }
                  : { margin: '2vh 2vw', width: 'calc(96vw)', height: 'calc(96vh)', borderRadius: '1rem', boxShadow: '0 25px 60px rgba(0,0,0,0.4)' }
                }
              >
                {/* Modal header — single row: left info | center filters | right controls */}
                <div className="flex-shrink-0 flex items-center gap-3 px-5 py-3 border-b border-jj-gray-02 bg-jj-gray-01">

                  {/* Left: title + rule badge + row count */}
                  <div className="flex items-center gap-2.5 flex-shrink-0">
                    <Ic src={alertSrc} className="w-4 h-4 flex-shrink-0" filter={F.red} />
                    <span className="font-johnson-display text-sm font-bold text-jj-gray-08">Failed Rows</span>
                    <span className="font-johnson-text text-xs font-bold text-jj-blue-03 bg-blue-50 px-2 py-1 rounded border border-jj-blue-03 border-opacity-20">
                      {reviewingRule.rule_id}
                    </span>
                    {frData && (
                      <span className="font-johnson-text text-xs text-jj-gray-05 whitespace-nowrap">
                        {frData.total?.toLocaleString()} {frColorFilter !== 'ALL' ? <span className="capitalize">{frColorFilter}</span> : 'total'} row{frData.total !== 1 ? 's' : ''}
                      </span>
                    )}
                  </div>

                  {/* Center: color filter pills — expands to fill available space */}
                  <div className="flex-1 flex items-center justify-center gap-2">
                    {frHasDateCols && (
                      <>
                        <span className="font-johnson-text text-xs font-bold text-jj-gray-06 uppercase tracking-wide flex-shrink-0">Filter:</span>
                        {FR_COLOR_KEYS.map(({ key, label, cls, activeCls }) => {
                          const active = frColorFilter === key;
                          return (
                            <button
                              key={key}
                              onClick={() => setFrColorFilter(active ? 'ALL' : key)}
                              className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-johnson-text font-bold border transition-all flex-shrink-0 ${active ? activeCls : cls}`}
                            >
                              <span className={`w-2 h-2 rounded-full flex-shrink-0 ${
                                active ? 'bg-white bg-opacity-60'
                                : key === 'green' ? 'bg-jj-green-03'
                                : key === 'yellow' ? 'bg-jj-yellow-01'
                                : 'bg-jj-red'
                              }`} />
                              {label}
                              {active && <Ic src={closeSrc} className="w-2.5 h-2.5" filter={F.white} />}
                            </button>
                          );
                        })}
                        {frColorFilter !== 'ALL' && (
                          <button
                            onClick={() => setFrColorFilter('ALL')}
                            className="font-johnson-text text-xs text-jj-red hover:text-jj-maroon-05 underline transition-colors flex-shrink-0"
                          >
                            Clear
                          </button>
                        )}
                      </>
                    )}
                  </div>

                  {/* Right: download + word-wrap + close */}
                  <div className="flex items-center gap-2 flex-shrink-0">
                    {!frDownloading ? (
                      <button
                        onClick={downloadFailedRows}
                        className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg font-johnson-text text-xs font-bold text-jj-gray-07 bg-white border border-jj-gray-03 hover:bg-jj-gray-01 transition-colors"
                      >
                        <Ic src={downloadSrc} className="w-3.5 h-3.5" filter={F.gray07} />
                        Download Excel
                      </button>
                    ) : (
                      <div className="flex items-center gap-2 bg-jj-gray-01 border border-jj-gray-02 rounded-lg px-3 py-2 min-w-[180px]">
                        <Ic src={activitySrc} className="w-3.5 h-3.5 animate-spin flex-shrink-0" filter={F.gray07} />
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center justify-between mb-1">
                            <span className="font-johnson-text text-xs text-jj-gray-08 truncate">{frDownloadProgress?.label ?? 'Starting…'}</span>
                            <span className="font-johnson-text text-xs font-bold text-jj-gray-07 ml-2">{frDownloadProgress?.pct ?? 0}%</span>
                          </div>
                          <div className="w-full h-1.5 bg-jj-gray-02 rounded-full overflow-hidden">
                            <div className="h-full bg-jj-gray-07 rounded-full transition-all duration-500" style={{ width: `${frDownloadProgress?.pct ?? 0}%` }} />
                          </div>
                        </div>
                      </div>
                    )}
                    <button
                      onClick={() => setFrWordWrap(w => !w)}
                      title={frWordWrap ? 'Collapse to single line' : 'Wrap cell text'}
                      className={`w-8 h-8 flex items-center justify-center rounded-lg transition-colors ${
                        frWordWrap ? 'bg-jj-red' : 'bg-white border border-jj-gray-02 hover:bg-jj-gray-01'
                      }`}
                    >
                      <Ic src={recordsSrc} className="w-3.5 h-3.5" filter={frWordWrap ? F.white : F.gray07} />
                    </button>
                    {/* Fullscreen toggle */}
                    <button
                      onClick={() => {
                        if (!document.fullscreenElement) {
                          frModalRef.current?.requestFullscreen?.();
                        } else {
                          document.exitFullscreen?.();
                        }
                      }}
                      title={frFullscreen ? 'Exit fullscreen (F11)' : 'Enter fullscreen'}
                      className="w-8 h-8 flex items-center justify-center rounded-lg bg-white border border-jj-gray-02 hover:bg-jj-gray-01 transition-colors"
                    >
                      {frFullscreen ? (
                        <svg xmlns="http://www.w3.org/2000/svg" className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ color: '#6b6b6b' }}>
                          <path d="M8 3v3a2 2 0 0 1-2 2H3m18 0h-3a2 2 0 0 1-2-2V3m0 18v-3a2 2 0 0 1 2-2h3M3 16h3a2 2 0 0 1 2 2v3" />
                        </svg>
                      ) : (
                        <svg xmlns="http://www.w3.org/2000/svg" className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ color: '#6b6b6b' }}>
                          <path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3" />
                        </svg>
                      )}
                    </button>
                    <button
                      onClick={closeFailedRowsModal}
                      title="Close (Esc)"
                      className="w-8 h-8 flex items-center justify-center rounded-lg bg-white border border-jj-gray-02 hover:bg-jj-gray-01 transition-colors"
                    >
                      <Ic src={closeSrc} className="w-4 h-4" filter={F.gray07} />
                    </button>
                  </div>

                </div>

                {/* Table body */}
                {frLoading && !frData ? (
                  <div className="flex-1 flex items-center justify-center">
                    <LoadingSpinner />
                  </div>
                ) : frData ? (
                  <>
                    <ModalDataTable
                      columns={frData.cols}
                      rows={frData.rows}
                      colWidths={frColWidths}
                      onResize={onFrResize}
                      wordWrap={frWordWrap}
                      rowOffset={(frPage - 1) * PAGE_SIZE}
                      colorCol1={frCol1}
                      colorCol2={frCol2}
                      loading={frLoading}
                      emptyMessage="No failed rows match the selected filter."
                    />

                    {/* Pagination footer */}
                    <div className="flex-shrink-0 px-5 py-3 bg-white border-t border-jj-gray-02 flex items-center justify-between">
                      <span className="font-johnson-text text-xs text-jj-gray-06">
                        {frColorFilter === 'ALL' ? (
                          <>Rows <b className="text-jj-gray-08">{((frPage - 1) * PAGE_SIZE + 1).toLocaleString()}</b>–<b className="text-jj-gray-08">{Math.min(frPage * PAGE_SIZE, frData.total).toLocaleString()}</b> of <b className="text-jj-gray-08">{frData.total.toLocaleString()}</b></>
                        ) : (
                          <>
                            <span className={`inline-block w-2 h-2 rounded-full mr-1.5 ${frColorFilter === 'green' ? 'bg-jj-green-03' : frColorFilter === 'yellow' ? 'bg-jj-yellow-01' : 'bg-jj-red'}`} />
                            Rows <b className="text-jj-gray-08">{((frPage - 1) * PAGE_SIZE + 1).toLocaleString()}</b>–<b className="text-jj-gray-08">{Math.min(frPage * PAGE_SIZE, frData.total).toLocaleString()}</b> of <b className="text-jj-gray-08">{frData.total.toLocaleString()}</b> <span className="capitalize">{frColorFilter}</span> rows
                          </>
                        )}
                      </span>
                      {frData.totalPages > 1 && (
                        <div className="flex items-center gap-1">
                          <button
                            disabled={frPage === 1 || frLoading}
                            onClick={() => loadFailedRows(reviewingRule.rule_id, 1, frColorFilter)}
                            className="px-2.5 py-1 rounded border border-jj-gray-03 bg-white font-johnson-text text-xs hover:bg-jj-gray-01 disabled:opacity-40"
                          >First</button>
                          <button
                            disabled={frPage === 1 || frLoading}
                            onClick={() => loadFailedRows(reviewingRule.rule_id, frPage - 1, frColorFilter)}
                            className="px-2.5 py-1 rounded border border-jj-gray-03 bg-white font-johnson-text text-xs hover:bg-jj-gray-01 disabled:opacity-40"
                          >
                            <Ic src={chevronLeftSrc} className="w-3.5 h-3.5" filter={F.gray07} />
                          </button>
                          <span className="font-johnson-text text-xs text-jj-gray-06 px-2">
                            Page {frPage} of {frData.totalPages}
                          </span>
                          <button
                            disabled={frPage >= frData.totalPages || frLoading}
                            onClick={() => loadFailedRows(reviewingRule.rule_id, frPage + 1, frColorFilter)}
                            className="px-2.5 py-1 rounded border border-jj-gray-03 bg-white font-johnson-text text-xs hover:bg-jj-gray-01 disabled:opacity-40"
                          >
                            <Ic src={chevronRightSrc} className="w-3.5 h-3.5" filter={F.gray07} />
                          </button>
                          <button
                            disabled={frPage >= frData.totalPages || frLoading}
                            onClick={() => loadFailedRows(reviewingRule.rule_id, frData.totalPages, frColorFilter)}
                            className="px-2.5 py-1 rounded border border-jj-gray-03 bg-white font-johnson-text text-xs hover:bg-jj-gray-01 disabled:opacity-40"
                          >Last</button>
                        </div>
                      )}
                    </div>
                  </>
                ) : null}
              </div>
            </div>
          );
        })(),
        document.body
      )}

    </div>
  );
}


export default function BusinessDefinedChecks({ onBack }) {
  const API_BASE_URL = import.meta.env.VITE_API_BASE_URL;
  const api = useMemo(() => createAPI(API_BASE_URL), [API_BASE_URL]);
  const { hasPermission } = useRBAC();
  const { user } = useAuth();
  const userEmail = user?.email || user?.username || 'unknown';
  const isAdmin = hasPermission('viewBDCSuperuser');
  const canRunBDC = hasPermission('runBDCOperations');
  const [reviewBadge, setReviewBadge]           = useState(0);
  const [dateRanges, setDateRanges]             = useState({});
  const [dateRangesError, setDateRangesError]   = useState(false);
  const [dateRangesLoaded, setDateRangesLoaded] = useState(false);

  const fetchDateRanges = useCallback(() => {
    setDateRangesError(false);
    setDateRangesLoaded(false);
    api.getActiveDateRanges()
      .then(d => {
        const map = {};
        (d.ranges || []).forEach(r => { map[r.report_set] = r; });
        setDateRanges(map);
        setDateRangesLoaded(true);
      })
      .catch(() => {
        setDateRangesError(true);
        setDateRangesLoaded(true);
      });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => { fetchDateRanges(); }, [fetchDateRanges]);

  const [searchParams, setSearchParams] = useSearchParams();

  const VALID_TABS = ['overview', 'dataset-health', 'trends', 'failed-rows', 'admin', 'review'];

  const [activeTab, setActiveTabState] = useState(() => {
    const t = searchParams.get('tab') ?? '';
    return VALID_TABS.includes(t) ? t : 'overview';
  });

  // Sync tab → URL (push so back/forward works between tabs).
  // Use replace when the tab param is already set to avoid duplicate entries
  // for the very first render (where useState already read from the URL).
  const prevTabRef = useRef(activeTab);
  // silent=true is used by the tour so it can drive tab switches without
  // polluting the audit log with every Next/Back click.
  const setActiveTab = (tab, silent = false) => {
    const isNewTab = tab !== prevTabRef.current;
    setActiveTabState(tab);
    setSearchParams(prev => {
      const next = new URLSearchParams(prev);
      next.set('tab', tab);
      return next;
    }, { replace: !isNewTab });
    prevTabRef.current = tab;
    if (!silent && isNewTab) {
      logAuditEvent(API_BASE_URL, userEmail, 'bdc_tab_viewed', tab,
        `Viewed BDC tab "${tab}"`);
    }
  };

  // React to back/forward navigation changing the URL tab param.
  useEffect(() => {
    const t = searchParams.get('tab') ?? '';
    const resolved = VALID_TABS.includes(t) ? t : 'overview';
    if (resolved !== activeTab) {
      setActiveTabState(resolved);
      prevTabRef.current = resolved;
      logAuditEvent(API_BASE_URL, userEmail, 'bdc_tab_viewed', resolved,
        `Viewed BDC tab "${resolved}" via browser navigation`);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);
  const [rules, setRules]                   = useState([]);
  const [loading, setLoading]               = useState(true);
  const [refreshKey, setRefreshKey]         = useState(0);
  const [isDeltaMode, setIsDeltaModeState] = useState(() => searchParams.get('mode') === 'delta');
  const [processModalOpen, setProcessModalOpen] = useState(false);
  const [processType, setProcessType]       = useState(null);
  const [validationMode, setValidationMode] = useState('EXECUTE_FULL');
  const [valDropdownOpen, setValDropdownOpen] = useState(false);
  const [showRulePicker, setShowRulePicker] = useState(false);
  const [showTour, setShowTour]             = useState(false);

  useEffect(() => {
    if (!isAdmin) return;
    let cancelled = false;
    const fetchBadge = async () => {
      try {
        const r = await fetch(`${API_BASE_URL}/api/review/badge?delta_mode=${isDeltaMode}`);
        if (!r.ok || cancelled) return;
        const d = await r.json();
        if (!cancelled) setReviewBadge(d.pending ?? 0);
      } catch { /* non-critical */ }
    };
    fetchBadge();
    return () => { cancelled = true; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isAdmin, isDeltaMode]);

  useEffect(() => {
    setLoading(true);
    api.getRules(isDeltaMode)
      .then(setRules)
      .catch(err => console.error('Error loading rules:', err))
      .finally(() => setLoading(false));
  }, [refreshKey, isDeltaMode]);

  const handleRefresh = () => setRefreshKey(k => k + 1);

  // Sync delta-mode toggle → URL (?mode=delta), so the view is linkable/refreshable.
  // replace: true since it's a view-state flip, not a navigation step worth a history entry.
  const setIsDeltaMode = (next) => {
    setIsDeltaModeState(next);
    setSearchParams(prev => {
      const params = new URLSearchParams(prev);
      if (next) params.set('mode', 'delta');
      else params.delete('mode');
      return params;
    }, { replace: true });
  };

  // React to back/forward navigation changing the URL mode param.
  useEffect(() => {
    const wantDelta = searchParams.get('mode') === 'delta';
    if (wantDelta !== isDeltaMode) setIsDeltaModeState(wantDelta);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  const tabs = [
    { id: 'overview',       name: 'Overview',       icon: overviewSrc         },
    { id: 'dataset-health', name: 'Dataset Info',   icon: databaseSrc      },
    { id: 'trends',         name: 'Trends',         icon: chartSrc         },
    { id: 'failed-rows',    name: 'Failed Rows',    icon: alertSrc         },
    ...(isAdmin ? [{ id: 'admin',  name: 'Superuser', icon: safetySrc, isAdmin: true }] : []),
    ...(isAdmin ? [{ id: 'review', name: 'Review',    icon: review,  isAdmin: true, badge: reviewBadge }] : []),
  ];

  if (loading) return (
    <div className="flex items-center justify-center min-h-full bg-jj-gray-01">
      <div className="text-center">
        <LoadingSpinner />
        <p className="font-johnson-display text-lg text-jj-gray-08 mt-2" style={{ lineHeight: '110%' }}>
          Loading Business Defined Checks…
        </p>

      </div>
    </div>
  );

  return (
    <div className="flex flex-col h-full bg-jj-gray-01">

      {/* ── Top Bar ── */}
      <div className="flex-shrink-0 bg-white shadow-md">
        <div className="px-6 py-3 flex items-center gap-4 flex-wrap">

          {/* Left: back + title */}
          <div className="flex items-center gap-3 flex-shrink-0">
            {hasPermission('viewITChecks') && (
            <button
              onClick={onBack}
              className="w-9 h-9 flex items-center justify-center rounded-lg bg-jj-gray-01 hover:bg-jj-gray-02 transition-colors border border-jj-gray-02"
              aria-label="Back"
            >
              <Ic src={chevronLeftSrc} className="w-4 h-4" filter={F.gray07} />
            </button>
            )}
            <div className="w-10 h-10 bg-jj-red rounded-lg flex items-center justify-center flex-shrink-0">
              <Ic src={chartSrc} className="w-5 h-5" filter={F.white} />
            </div>
            <div data-tour="bdc-env">
              <h1 className="font-johnson-display text-lg text-jj-gray-08" style={{ lineHeight: '110%' }}>
                Business Defined Checks
              </h1>
              <div className="flex items-center gap-2 font-johnson-text text-xs text-jj-gray-06">
                <span>{rules.length} rules</span>
              </div>
            </div>
          </div>

          {/* Center: delta mode toggle — flex-1 naturally centers it; wraps below on narrow viewports */}
          <div className="flex-1 flex justify-center min-w-[210px]">
            <button
              onClick={() => {
                const next = !isDeltaMode;
                setIsDeltaMode(next);
                logAuditEvent(API_BASE_URL, userEmail, 'bdc_delta_mode_toggled',
                  next ? 'delta' : 'standard',
                  `Switched to ${next ? 'Semi-Monthly' : 'quarterly'} data view`);
              }}
              title={isDeltaMode ? 'Currently showing Semi-Monthly delta data — click to switch back to quarterly view' : 'Switch to Semi-Monthly delta data view'}
              className="relative flex items-center rounded-full border overflow-hidden shadow-inner"
              style={{
                padding: 0,
                height: '30px',
                width: '210px',
                borderColor: isDeltaMode ? '#541980' : '#d5cfc9',
                background: isDeltaMode ? '#f5f0fb' : '#f1efed',
              }}
            >
              {/* Sliding pill */}
              <span
                className="absolute top-0.5 bottom-0.5 rounded-full transition-all duration-300 ease-in-out"
                style={{
                  width: 'calc(50% - 3px)',
                  left: isDeltaMode ? 'calc(50% + 1px)' : '2px',
                  background: isDeltaMode
                    ? 'linear-gradient(135deg, #8c3bbb, #541980)'
                    : '#ffffff',
                  boxShadow: isDeltaMode
                    ? '0 1px 4px rgba(140,59,187,0.5)'
                    : '0 1px 3px rgba(0,0,0,0.12)',
                }}
              />
              {/* Quarterly label */}
              <span
                className="relative z-10 flex items-center justify-center gap-1 font-johnson-text text-[11px] font-bold transition-colors duration-300 whitespace-nowrap"
                style={{ width: '50%', color: isDeltaMode ? '#a39992' : '#312c2a' }}
              >
                <Ic src={databaseSrc} className="w-3 h-3" filter={isDeltaMode ? F.gray05 : F.gray08} />
                Quarterly
              </span>
              {/* Semi-Monthly label */}
              <span
                className="relative z-10 flex items-center justify-center gap-1 font-johnson-text text-[11px] font-bold transition-colors duration-300 whitespace-nowrap"
                style={{ width: '50%', color: isDeltaMode ? '#ffffff' : '#a39992' }}
              >
                <Ic src={chartSrc} className="w-3 h-3" filter={isDeltaMode ? F.white : F.gray05} />
                Semi-Monthly
                {isDeltaMode && (
                  <span className="w-1.5 h-1.5 rounded-full bg-white opacity-80 animate-pulse" />
                )}
              </span>
            </button>
          </div>

          {/* Right: actions */}
          <div className="flex items-center gap-2 flex-wrap flex-shrink-0" data-tour="bdc-actions">
            {canRunBDC && (
            <button
              onClick={() => { setProcessType('compilation'); setProcessModalOpen(true); }}
              className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg font-johnson-text text-xs font-bold text-jj-blue-03 bg-blue-50 hover:bg-blue-100 transition-colors border border-jj-blue-03 border-opacity-30"
            >
              <Ic src={runSrc} className="w-3.5 h-3.5" filter={F.blue} />
              Run Compilation
            </button>
            )}

            {/* Run Validation — split button with dropdown */}
            {canRunBDC && (
            <div className="relative">
              <div className="inline-flex rounded-lg overflow-hidden border border-jj-green-03 border-opacity-30">
                {/* Main action */}
                <button
                  onClick={() => {
                    if (validationMode.startsWith('EXECUTE_R')) {
                      setShowRulePicker(true);
                    } else {
                      setProcessType('validation');
                      setProcessModalOpen(true);
                    }
                  }}
                  className="inline-flex items-center gap-1.5 px-3 py-2 font-johnson-text text-xs font-bold text-jj-green-03 bg-green-50 hover:bg-green-100 transition-colors cursor-pointer"
                >
                  <Ic src={runSrc} className="w-3.5 h-3.5" filter={F.green} />
                  {validationMode === 'EXECUTE_FULL'          ? 'Run Validation'      :
                   validationMode === 'EXECUTE_ALLRULES'       ? 'Run All Rules'       :
                   validationMode === 'EXECUTE_ALLRULES_DELTA'  ? 'Delta - All Rules'   :
                   `Validate ${validationMode.replace('EXECUTE_', '')}`}
                </button>
                {/* Chevron to open dropdown */}
                <button
                  onClick={() => setValDropdownOpen(o => !o)}
                  className="px-2 py-2 font-johnson-text text-jj-green-03 bg-green-50 hover:bg-green-100 transition-colors border-l border-jj-green-03 border-opacity-20 cursor-pointer"
                  aria-label="More validation options"
                >
                  <Ic src={chevronRightSrc} className={`w-3.5 h-3.5 transition-transform rotate-90 duration-150 ${valDropdownOpen ? 'rotate-90' : ''}`} filter={F.gray07} />
                </button>
              </div>

              {/* Dropdown options */}
              {valDropdownOpen && (
                <>
                  <div className="absolute right-0 mt-1 w-72 bg-white rounded-lg shadow-lg border border-jj-gray-02 py-1 z-30 overflow-hidden">
                    {[
                      { mode: 'EXECUTE_FULL',     label: 'New Rules Only',    desc: 'Validate only new rules',                    requiresPicker: false },
                      { mode: 'EXECUTE_ALLRULES', label: 'All Rules',         desc: 'Validate all active and compiled rules',          requiresPicker: false },
                      { mode: 'EXECUTE_RULE_ID',  label: 'Specific Rule…',    desc: 'Validate a specific rule by ID',      requiresPicker: true  },
                      { mode: 'EXECUTE_ALLRULES_DELTA', label: 'Delta - All Rules', desc: 'Run delta validation for all active and compiled rules', requiresPicker: false },
                    ].map(({ mode, label, desc, requiresPicker }) => (
                      <button
                        key={mode}
                        onClick={() => {
                          setValDropdownOpen(false);
                          if (requiresPicker) {
                            setShowRulePicker(true);
                          } else {
                            setValidationMode(mode);
                            setProcessType('validation');
                            setProcessModalOpen(true);
                          }
                        }}
                        className="w-full text-left px-4 py-3 hover:bg-jj-gray-01 transition-colors border-b border-jj-gray-02 last:border-0"
                      >
                        <p className="font-johnson-text text-sm font-bold text-jj-gray-08">{label}</p>
                        <p className="font-johnson-text text-xs text-jj-gray-05 mt-0.5">{desc}</p>
                      </button>
                    ))}
                  </div>
                  {/* Backdrop */}
                  <div className="fixed inset-0 z-20" onClick={() => setValDropdownOpen(false)} />
                </>
              )}
            </div>
            )}
            <button
              onClick={handleRefresh}
              disabled={loading}
              className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg font-johnson-text text-xs font-bold text-jj-gray-06 bg-gray-50 hover:bg-gray-100 transition-colors border border-jj-gray-06 border-opacity-20"
            >
              <Ic src={refreshSrc} className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} filter={F.gray07} />
              Refresh
            </button>
            <button
              onClick={onBack}
              className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg font-johnson-text text-xs font-bold text-jj-gray-07 bg-jj-gray-01 hover:bg-jj-gray-02 transition-colors border border-jj-gray-03 disabled:opacity-50"
            >
              <Ic src={databaseSrc} className="w-3.5 h-3.5" filter={F.gray07} />
              Switch Check Type
            </button>
            <button
              onClick={() => {
                setShowTour(true);
                logAuditEvent(API_BASE_URL, userEmail, 'bdc_tour_started', '',
                `Started guided tour`);
              }}
              className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg font-johnson-text text-xs font-bold text-jj-violet-03 bg-violet-50 hover:bg-violet-100 transition-colors border border-jj-violet-03 border-opacity-30"
              title="Take a guided tour of this page"
            >
              <Ic src={infoSrc} className="w-3.5 h-3.5" filter={F.gray05} />
              Take a Tour
            </button>
          </div>
        </div>

        {/* ── Dataset Range Banner ── */}
        {(() => {
          const key   = isDeltaMode ? 'RTE_REPORT_BIWEEKLY' : 'RTE_REPORT_QUARTERLY';
          const entry = dateRanges[key];
          const mode  = isDeltaMode ? 'Semi-Monthly' : 'Quarterly';

          const fmtDate = (d) => {
            if (!d) return '—';
            const [y, m, day] = d.split('-');
            const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
            return `${parseInt(day, 10)} ${months[parseInt(m, 10) - 1]} ${y}`;
          };

          // Error state
          if (dateRangesError) {
            return (
              <div className="px-6 py-1 border-t border-jj-gray-02 bg-jj-gray-01 flex items-center gap-2">
                <div className="w-0.5 h-4 rounded-full bg-jj-gray-03 flex-shrink-0" />
                <Ic src={alertSrc} className="w-3 h-3" filter={F.gray05} />
                <span className="font-johnson-text text-[10px] text-jj-gray-05">Dataset range unavailable</span>
                <button
                  onClick={fetchDateRanges}
                  className="font-johnson-text text-[10px] text-jj-blue-03 underline hover:text-jj-blue-05 transition-colors"
                >
                  Retry
                </button>
              </div>
            );
          }

          // Loading state
          if (!dateRangesLoaded) {
            return (
              <div className="px-6 py-1 border-t border-jj-gray-02 bg-jj-gray-01 flex items-center gap-2">
                <div className="w-0.5 h-4 rounded-full bg-jj-gray-03 flex-shrink-0" />
                <span className="font-johnson-text text-[10px] text-jj-gray-04 italic">Loading dataset range…</span>
              </div>
            );
          }

          // No rows at all for this report_set yet (e.g. fresh environment) —
          // backend only returns entries for report_sets that have ≥1 row,
          // so this isn't an error, just an unconfigured range. Render the
          // same "none" state as a configured-but-empty range would.
          if (!entry) {
            return (
              <div className="px-6 py-1 border-t border-jj-gray-02 bg-jj-gray-01 flex items-center gap-2 flex-wrap">
                <div className="w-0.5 h-4 rounded-full bg-jj-gray-04 flex-shrink-0" />
                <Ic src={databaseSrc} className="w-3 h-3" filter={F.gray05} />
                <span className="font-johnson-text text-[10px] font-bold uppercase tracking-widest text-jj-gray-05">Current Dataset Range</span>
                <span className="px-1 py-px rounded font-johnson-text text-[9px] font-bold uppercase tracking-wide bg-jj-gray-02 text-jj-gray-06 border border-jj-gray-03">{mode}</span>
                <div className="w-px h-3 bg-jj-gray-03 flex-shrink-0" />
                <span className="font-johnson-text text-[10px] text-jj-gray-05 italic">No active date range configured</span>
              </div>
            );
          }

          const { status, from_date, to_date, active_count } = entry;

          // ── No active row ──────────────────────────────────────────────
          if (status === 'none') {
            return (
              <div className="px-6 py-1 border-t border-jj-gray-02 bg-jj-gray-01 flex items-center gap-2 flex-wrap">
                <div className="w-0.5 h-4 rounded-full bg-jj-gray-04 flex-shrink-0" />
                <Ic src={databaseSrc} className="w-3 h-3" filter={F.gray05} />
                <span className="font-johnson-text text-[10px] font-bold uppercase tracking-widest text-jj-gray-05">Current Dataset Range</span>
                <span className="px-1 py-px rounded font-johnson-text text-[9px] font-bold uppercase tracking-wide bg-jj-gray-02 text-jj-gray-06 border border-jj-gray-03">{mode}</span>
                <div className="w-px h-3 bg-jj-gray-03 flex-shrink-0" />
                <span className="font-johnson-text text-[10px] text-jj-gray-05 italic">No active date range configured</span>
              </div>
            );
          }

          // ── Multiple active rows ───────────────────────────────────────
          if (status === 'multiple') {
            return (
              <div className="px-6 py-1 border-t border-jj-gray-02 bg-jj-yellow-01 bg-opacity-20 flex items-center gap-2 flex-wrap">
                <div className="w-0.5 h-4 rounded-full bg-jj-orange-03 flex-shrink-0" />
                <Ic src={alertSrc} className="w-3 h-3" filter={F.orange} />
                <span className="font-johnson-text text-[10px] font-bold uppercase tracking-widest text-jj-orange-03">Current Dataset Range</span>
                <span className="px-1 py-px rounded font-johnson-text text-[9px] font-bold uppercase tracking-wide bg-jj-yellow-01 text-jj-orange-03 border border-jj-orange-03 border-opacity-30">{mode}</span>
                <div className="w-px h-3 bg-jj-orange-03 bg-opacity-20 flex-shrink-0" />
                <span className="font-johnson-text text-[10px] font-bold text-jj-gray-05 uppercase tracking-wider">From</span>
                <span className="font-johnson-display text-xs font-bold text-jj-orange-03">{fmtDate(from_date)}</span>
                <Ic src={chevronRightSrc} className="w-3 h-3 flex-shrink-0" filter={F.gray04} />
                <span className="font-johnson-text text-[10px] font-bold text-jj-gray-05 uppercase tracking-wider">To</span>
                <span className="font-johnson-display text-xs font-bold text-jj-orange-03">{fmtDate(to_date)}</span>
                <div className="w-px h-3 bg-jj-orange-03 bg-opacity-20 flex-shrink-0" />
                <Ic src={alertSrc} className="w-3 h-3" filter={F.orange} />
                <span className="font-johnson-text text-[10px] font-bold text-jj-orange-03">{active_count} rows active — only 1 expected.</span>
                <span className="font-johnson-text text-[10px] text-jj-gray-06">Showing most recently updated.</span>
              </div>
            );
          }

          // ── Normal: exactly one active row ────────────────────────────
          return (
            <div className="px-6 py-1 border-t border-jj-gray-02 bg-jj-blue-01 bg-opacity-10 flex items-center gap-2 flex-wrap">
              <div className="w-0.5 h-4 rounded-full bg-jj-blue-03 flex-shrink-0" />
              <Ic src={databaseSrc} className="w-3 h-3" filter={F.blue} />
              <span className="font-johnson-text text-[10px] font-bold uppercase tracking-widest text-jj-blue-03">Current Dataset Range</span>
              <span className="px-1 py-px rounded font-johnson-text text-[9px] font-bold uppercase tracking-wide bg-jj-blue-01 bg-opacity-40 text-jj-blue-03 border border-jj-blue-03 border-opacity-20">{mode}</span>
              <div className="w-px h-3 bg-jj-blue-03 bg-opacity-20 flex-shrink-0" />
              <span className="font-johnson-text text-[10px] font-bold text-jj-gray-05 uppercase tracking-wider">From</span>
              <span className="font-johnson-display text-xs font-bold text-jj-blue-03">{fmtDate(from_date)}</span>
              <Ic src={chevronRightSrc} className="w-3 h-3 flex-shrink-0" filter={F.gray04} />
              <span className="font-johnson-text text-[10px] font-bold text-jj-gray-05 uppercase tracking-wider">To</span>
              <span className="font-johnson-display text-xs font-bold text-jj-blue-03">{fmtDate(to_date)}</span>
            </div>
          );
        })()}

        {/* ── Tabs ── */}
        <div className="flex overflow-x-auto px-6 border-t border-jj-gray-02" data-tour="bdc-tabs">
          {tabs.map(tab => {
            const active = activeTab === tab.id;
            return (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={`relative px-5 py-3 font-johnson-text text-sm whitespace-nowrap transition-colors duration-150 flex items-center gap-1.5 ${
                  tab.isAdmin
                    ? active
                      ? 'text-jj-red font-bold'
                      : 'text-jj-gray-06 hover:text-jj-red hover:bg-red-50'
                    : active
                      ? 'text-jj-red font-bold'
                      : 'text-jj-gray-06 hover:text-jj-gray-08 hover:bg-jj-gray-01'
                }`}
              >
                <Ic src={tab.icon} className="w-4 h-4" filter={active ? (tab.isAdmin ? F.red : F.red) : F.gray06} />
                {tab.name}
                {tab.isAdmin && !active && !tab.badge && (
                  <span className="w-1.5 h-1.5 rounded-full bg-jj-red ml-0.5" />
                )}
                {tab.badge > 0 && (
                  <span
                    className="inline-flex items-center justify-center min-w-[18px] h-[18px] px-1 rounded-full text-white font-bold font-johnson-text"
                    style={{ fontSize: '10px', backgroundColor: '#eb1700' }}
                  >
                    {tab.badge > 99 ? '99+' : tab.badge}
                  </span>
                )}
                {active && <span className="absolute bottom-0 left-0 right-0 h-0.5 bg-jj-red rounded-t" />}
              </button>
            );
          })}
        </div>
      </div>

      {/* ── Content ── */}
      <div className="flex-1 overflow-y-auto overflow-x-hidden">

        <div className="px-6 py-4 max-w-screen-2xl mx-auto">
          {activeTab === 'overview'       && <OverviewTab api={api} refreshKey={refreshKey} isDeltaMode={isDeltaMode} />}
          {activeTab === 'dataset-health' && <DatasetHealthTab api={api} refreshKey={refreshKey} rules={rules} isDeltaMode={isDeltaMode} />}
          {activeTab === 'trends'         && <TrendsTab rules={rules} api={api} isDeltaMode={isDeltaMode} />}
          {activeTab === 'failed-rows'    && <FailedRowsTab rules={rules} api={api} isDeltaMode={isDeltaMode} />}
          {activeTab === 'admin'  && isAdmin && <AdminTab api={api} />}
          {activeTab === 'review' && isAdmin && (
            <ReviewTab
              api={api}
              userEmail={userEmail}
              onBadgeChange={setReviewBadge}
              isDeltaMode={isDeltaMode}
              setIsDeltaMode={setIsDeltaMode}
            />
          )}
        </div>
      </div>

      {processModalOpen && (
        <ProcessModal
          onClose={() => setProcessModalOpen(false)}
          API_BASE_URL={API_BASE_URL}
          processType={processType}
          validationMode={validationMode}
        />
      )}

      {/* Rule picker for EXECUTE_<RULE_ID> */}
      {showRulePicker && ReactDOM.createPortal(
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4"
          style={{ backdropFilter: 'blur(3px)' }}
          onClick={() => setShowRulePicker(false)}>
          <div className="bg-white rounded-2xl shadow-2xl border border-jj-gray-02 w-full max-w-md overflow-hidden"
            style={{ animation: 'slideUp 0.2s ease-out' }}
            onClick={e => e.stopPropagation()}>
            <div className="px-5 py-4 border-b-4 border-jj-green-03 flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Ic src={chevronRightSrc} className="w-4 h-4" filter={F.green} />
                <h2 className="font-johnson-display text-base text-jj-gray-08" style={{ lineHeight: '110%' }}>Select Rule to Execute</h2>
              </div>
              <button onClick={() => setShowRulePicker(false)}
                className="w-8 h-8 flex items-center justify-center rounded-lg bg-jj-gray-01 hover:bg-jj-gray-02 transition-colors">
                <Ic src={closeSrc} className="w-4 h-4" filter={F.gray07} />
              </button>
            </div>
            <div className="px-5 py-4 space-y-3">
              <p className="font-johnson-text text-xs text-jj-gray-06">
                Choose a rule ID to execute — runs full-table validation for that single rule only.
              </p>
              <div className="relative">
                <select
                  defaultValue=""
                  onChange={e => {
                    if (!e.target.value) return;
                    setValidationMode(`EXECUTE_${e.target.value}`);
                    setShowRulePicker(false);
                    setProcessType('validation');
                    setProcessModalOpen(true);
                  }}
                  className="w-full px-4 py-2.5 pr-9 border border-jj-gray-03 rounded-lg font-johnson-text text-sm text-jj-gray-08 bg-white outline-none focus:border-jj-green-03 focus:ring-2 focus:ring-jj-green-03 focus:ring-opacity-10 appearance-none transition-all"
                >
                  <option value="">— Select a rule —</option>
                  {rules.map(r => (
                    <option key={r.rule_id} value={r.rule_id}>
                      {r.rule_id} | {r.rule_title}
                    </option>
                  ))}
                </select>
                <Ic src={chevronRightSrc} className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 pointer-events-none rotate-90" filter={F.gray05} />
              </div>
            </div>
          </div>
        </div>,
        document.body
      )}

      {showTour && (
        <TourGuide
          isAdmin={isAdmin}
          canRunBDC={canRunBDC}
          setActiveTab={tab => setActiveTab(tab, true)}
          onClose={() => setShowTour(false)}
        />
      )}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// Overview Tab
// ═══════════════════════════════════════════════════════════════════════════════
function OverviewTab({ api, refreshKey, isDeltaMode = false }) {
  const [kpi, setKpi]       = useState(null);
  const [rules, setRules]   = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError]   = useState(null);
  const { sortKey, sortDir, toggle } = useSortable('pass_percentage', 'asc');

  useEffect(() => {
    setLoading(true);
    Promise.all([api.getKPI(isDeltaMode), api.getRules(isDeltaMode)])
      .then(([kpiData, rulesData]) => { setKpi(kpiData); setRules(rulesData); })
      .catch(err => { console.error(err); setError('Failed to load overview data. Please try again later.'); })
      .finally(() => setLoading(false));
  }, [refreshKey, isDeltaMode]);

  if (loading) return <div className="flex items-center justify-center h-full min-h-[400px]"><LoadingSpinner /></div>;
  if (error)   return <EmptyState icon={closeSrc} title="Error Loading Data" message={error} />;

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3" data-tour="bdc-kpi-cards">
        <KPICard title="Tables Monitored"   value={kpi?.tables || 0}         icon={dashboardSrc}    color="text-jj-blue-03"  borderColor="#0f68b2" />
        <KPICard title="Rules Executed"     value={kpi?.rules || 0}          icon={checkSrc} color="text-jj-violet-03" borderColor="#8c3bbb" />
        <KPICard title="Average Pass Rate"  value={`${kpi?.avg_pass || 0}%`} icon={chartSrc}  color="text-jj-green-03" borderColor="#328714" />
      </div>

      <div data-tour="bdc-rules-table">
      <SectionCard title="Active Rules" icon={recordsSrc}>
        {rules.length === 0
          ? <EmptyState icon={recordsSrc} title="No Active Rules" message="There are no business defined check rules configured." />
          : (() => {
              const sorted = [...rules].sort((a, b) => {
                let av = sortKey === 'severity'        ? (SEVERITY_ORDER[a.severity] ?? 99)
                       : sortKey === 'pass_percentage' ? (a.pass_percentage ?? 0)
                       : String(a[sortKey] ?? '').toLowerCase();
                let bv = sortKey === 'severity'        ? (SEVERITY_ORDER[b.severity] ?? 99)
                       : sortKey === 'pass_percentage' ? (b.pass_percentage ?? 0)
                       : String(b[sortKey] ?? '').toLowerCase();
                const cmp = typeof av === 'number' ? av - bv : av.localeCompare(bv);
                return sortDir === 'asc' ? cmp : -cmp;
              });
              return (
            <div className="overflow-x-auto">
              <table className="w-full font-johnson-text text-sm">
                <thead>
                  <tr className="border-b-2 border-jj-gray-02">
                    {[
                      { label: 'Rule ID',      key: 'rule_id'        },
                      { label: 'Title',        key: 'rule_title'     },
                      { label: 'Severity',     key: 'severity'       },
                      { label: 'Target Table', key: 'target_table'   },
                      { label: 'Pass Rate',    key: 'pass_percentage'},
                      { label: 'Status',       key: 'status'         },
                    ].map(({ label, key }) => (
                      <SortableTh key={key} label={label} colKey={key} sortKey={sortKey} sortDir={sortDir} onSort={toggle} />
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-jj-gray-02">
                  {sorted.map(rule => (
                    <tr key={rule.rule_id} className="hover:bg-jj-gray-01 transition-colors">
                      <td className="px-5 py-3 whitespace-nowrap">
                        <span className="font-johnson-text text-xs font-bold text-jj-blue-03 bg-blue-50 px-2.5 py-1 rounded border border-jj-blue-03 border-opacity-20">
                          {rule.rule_id}
                        </span>
                      </td>
                      <td className="px-5 py-3 max-w-[200px]">
                        <span className="font-johnson-text text-sm text-jj-gray-08 block truncate" title={rule.rule_title}>{rule.rule_title}</span>
                      </td>
                      <td className="px-5 py-3 whitespace-nowrap">
                        <SeverityBadge severity={rule.severity} />
                      </td>
                      <td className="px-5 py-3 max-w-[260px]">
                        <span className="font-johnson-text text-sm text-jj-gray-07 block truncate" title={rule.target_table}>{rule.target_table}</span>
                      </td>
                      <td className="px-5 py-3 min-w-[180px]">
                        <PassBar pct={rule.pass_percentage} />
                      </td>
                      <td className="px-5 py-3 whitespace-nowrap">
                        <StatusBadge status={rule.status} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
              );
            })()
        }
      </SectionCard>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// Dataset Rules Modal — master / detail
// ═══════════════════════════════════════════════════════════════════════════════
function DatasetRulesModal({ dataset, rules, api, onClose, isDeltaMode = false }) {
  const [selectedRule, setSelectedRule] = useState(null);

  useEffect(() => {
    const onKey = e => {
      if (e.key !== 'Escape') return;
      if (selectedRule) setSelectedRule(null);
      else onClose();
    };
    document.addEventListener('keydown', onKey);
    document.body.style.overflow = 'hidden';
    return () => { document.removeEventListener('keydown', onKey); document.body.style.overflow = 'unset'; };
  }, [selectedRule]);

  const datasetRules = rules.filter(r =>
    (r.dataset_title || '').trim().toLowerCase() === (dataset.dataset || '').trim().toLowerCase()
  );

  const accentBorder = {
    HEALTHY:  'border-jj-green-03',
    WARNING:  'border-jj-yellow-01',
    CRITICAL: 'border-jj-red',
  }[dataset.status] || 'border-jj-gray-03';

  return ReactDOM.createPortal(
    <div
      className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4"
      style={{ backdropFilter: 'blur(3px)' }}
      onClick={() => selectedRule ? setSelectedRule(null) : onClose()}
    >
      <div
        className="bg-white rounded-2xl shadow-2xl border border-jj-gray-02 w-full flex flex-col overflow-hidden"
        style={{ maxWidth: '1060px', maxHeight: '95vh', animation: 'slideUp 0.2s ease-out' }}
        onClick={e => e.stopPropagation()}
      >
        <style>{`@keyframes slideUp { from { opacity:0; transform:translateY(16px); } to { opacity:1; transform:translateY(0); } }`}</style>

        {selectedRule
          ? <RuleDetailView rule={selectedRule} api={api} onBack={() => setSelectedRule(null)} onClose={onClose} accentBorder={accentBorder} datasetName={dataset.dataset} isDeltaMode={isDeltaMode} />
          : <RuleListView dataset={dataset} datasetRules={datasetRules} accentBorder={accentBorder} onSelectRule={setSelectedRule} onClose={onClose} />
        }
      </div>
    </div>,
    document.body
  );
}

// ─── List View ────────────────────────────────────────────────────────────────
function RuleListView({ dataset, datasetRules, accentBorder, onSelectRule, onClose }) {
  const passHex     = p => p >= 95 ? '#328714' : p >= 80 ? '#0f68b2' : '#eb1700';
  const passTrack   = p => p >= 95 ? '#d1f0d8' : p >= 80 ? '#c7dff5' : '#ffd6d0';
  const passBg      = p => p >= 95 ? '#f0faf4' : p >= 80 ? '#eff6ff' : '#fff4f2';
  const passText    = p => p >= 95 ? 'text-jj-green-03' : p >= 80 ? 'text-jj-blue-03' : 'text-jj-red';

  const healthyCount  = datasetRules.filter(r => r.pass_percentage >= 95).length;
  const warningCount  = datasetRules.filter(r => r.pass_percentage >= 80 && r.pass_percentage < 95).length;
  const criticalCount = datasetRules.filter(r => r.pass_percentage < 80).length;
  const avgPass       = datasetRules.length
    ? Math.round(datasetRules.reduce((s, r) => s + r.pass_percentage, 0) / datasetRules.length)
    : 0;

  return (
    <>
      {/* ── Hero Header ──────────────────────────────────────────────────────── */}
      <div className="flex-shrink-0 bg-white" style={{ borderBottom: '1px solid #e8e6e3' }}>
        {/* Accent line — reflects dataset health */}
        <div className={`h-1 w-full ${accentBorder.replace('border-', 'bg-')}`} />

        <div className="px-7 pt-6 pb-5">
          {/* Title row */}
          <div className="flex items-start justify-between gap-4 mb-5">
            {/* Left: label + title */}
            <div className="min-w-0 flex-1">
              <p className="font-johnson-text text-[10px] font-bold text-jj-gray-05 uppercase tracking-[0.18em] mb-1.5">Dataset</p>
              <h2 className="font-johnson-display text-2xl text-jj-gray-08 leading-tight">
                {dataset.dataset}
              </h2>
            </div>

            {/* Right: status badge + stats + close button */}
            <div className="flex items-center gap-3 flex-shrink-0 pt-0.5">
              <StatusBadge status={dataset.status} />
              <span className="font-johnson-text text-xs text-jj-gray-05">
                {datasetRules.length} rule{datasetRules.length !== 1 ? 's' : ''} configured
                {' · '}
                <span style={{ color: passHex(avgPass) }} className="font-bold">{avgPass}%</span> avg pass rate
              </span>
              <button
                onClick={onClose}
                className="w-9 h-9 flex items-center justify-center rounded-xl bg-jj-gray-01 hover:bg-jj-gray-02 transition-colors flex-shrink-0"
              >
                <Ic src={closeSrc} className="w-4 h-4" filter={F.gray07} />
              </button>
            </div>
          </div>

          {/* Pass rate threshold legend */}
          <div className="flex items-center gap-2 mb-3 flex-wrap">
            <span className="font-johnson-text text-[10px] font-bold text-jj-gray-05 uppercase tracking-wider">
              Rules by pass rate:
            </span>
            {[
              { icon: checkSrc, filter: F.green,  color: '#328714', range: '≥ 95%',    label: 'Passed'   },
              { icon: alertSrc, filter: F.blue,   color: '#0f68b2', range: '80 – 94%', label: 'Warning'  },
              { icon: closeSrc, filter: F.red,    color: '#eb1700', range: '< 80%',    label: 'Critical' },
            ].map(({ icon, filter, color, range, label }) => (
              <span
                key={label}
                className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full font-johnson-text text-[10px] font-bold"
                style={{ color, backgroundColor: `${color}14`, border: `1px solid ${color}28` }}
              >
                <Ic src={icon} className="w-2.5 h-2.5" filter={filter} />
                {range} · {label}
              </span>
            ))}
          </div>

          {/* Health summary — 3 stat tiles */}
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            {[
              { count: healthyCount,  label: 'Passed',  iconSrc: checkSrc, hex: '#328714', filter: F.green,  bg: '#f0faf4', border: '#32871422' },
              { count: warningCount,  label: 'Warning',  iconSrc: alertSrc, hex: '#0f68b2', filter: F.blue,   bg: '#eff6ff', border: '#0f68b222' },
              { count: criticalCount, label: 'Critical', iconSrc: closeSrc, hex: '#eb1700', filter: F.red,    bg: '#fff4f2', border: '#eb170022' },
            ].map(({ count, label, iconSrc, hex, filter, bg, border }) => (
              <div key={label}
                className="flex items-center gap-3 px-4 py-3 rounded-xl"
                style={{ background: bg, border: `1px solid ${border}` }}>
                <div className="w-9 h-9 rounded-xl bg-white flex items-center justify-center shadow-sm flex-shrink-0">
                  <Ic src={iconSrc} className="w-4 h-4" filter={filter} />
                </div>
                <div>
                  <p className="font-johnson-display text-2xl leading-none" style={{ color: hex }}>{count}</p>
                  <p className="font-johnson-text text-[10px] font-bold uppercase tracking-wider mt-0.5" style={{ color: hex }}>{label}</p>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* ── Rule Cards ────────────────────────────────────────────────────────── */}
      <div className="flex-1 overflow-y-auto p-5" style={{ background: '#f3f1ef' }}>
        {datasetRules.length === 0 ? (
          <EmptyState icon={recordsSrc} title="No Rules Found"
            message={`No rules are mapped to "${dataset.dataset}". The dataset_title in Snowflake may differ.`} />
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {datasetRules.map(rule => {
              const hex    = passHex(rule.pass_percentage);
              const track  = passTrack(rule.pass_percentage);
              const bg     = passBg(rule.pass_percentage);
              const txtCls = passText(rule.pass_percentage);
              const circ   = 2 * Math.PI * 17;
              const dash   = `${(rule.pass_percentage / 100) * circ} ${circ}`;

              return (
                <button
                  key={rule.rule_id}
                  onClick={() => onSelectRule(rule)}
                  className="group text-left bg-white rounded-2xl overflow-hidden flex flex-col transition-all duration-200"
                  style={{
                    border: '1px solid #a39992',
                    boxShadow: '0 2px 8px rgba(0,0,0,0.06)',
                  }}
                  onMouseEnter={e => {
                    e.currentTarget.style.border = '1px solid #81766f';
                    e.currentTarget.style.boxShadow = `0 8px 24px ${hex}22, 0 2px 8px rgba(0,0,0,0.06)`;
                  }}
                  onMouseLeave={e => {
                    e.currentTarget.style.border = '1px solid #a39992';
                    e.currentTarget.style.boxShadow = '0 2px 8px rgba(0,0,0,0.06)';
                  }}
                >
                  {/* Card body */}
                  <div className="flex flex-1">

                    {/* Left gauge panel */}
                    <div
                      className="flex-shrink-0 w-32 flex flex-col items-center justify-center gap-2 py-6"
                      style={{ background: bg, borderRight: `1px solid ${hex}18` }}
                    >
                      {/* Donut gauge */}
                      <div className="relative w-[72px] h-[72px]">
                        <svg viewBox="0 0 40 40" className="w-[72px] h-[72px] -rotate-90">
                          {/* Track */}
                          <circle cx="20" cy="20" r="17" fill="none" stroke={track} strokeWidth="3.5" />
                          {/* Progress */}
                          <circle
                            cx="20" cy="20" r="17" fill="none"
                            stroke={hex}
                            strokeWidth="3.5"
                            strokeDasharray={dash}
                            strokeLinecap="round"
                            style={{ transition: 'stroke-dasharray 0.8s cubic-bezier(0.4,0,0.2,1)' }}
                          />
                        </svg>
                        <div className="absolute inset-0 flex flex-col items-center justify-center">
                          <span className={`font-johnson-display font-bold leading-none ${txtCls}`}
                            style={{ fontSize: rule.pass_percentage === 100 ? '12px' : '13px' }}>
                            {rule.pass_percentage}%
                          </span>
                        </div>
                      </div>
                      <p className="font-johnson-text text-[9px] font-bold uppercase tracking-[0.12em]"
                        style={{ color: hex }}>
                        Pass Rate
                      </p>
                    </div>

                    {/* Right content */}
                    <div className="flex-1 min-w-0 flex flex-col px-4 pt-4 pb-4 gap-3">

                      {/* Rule ID + Status row */}
                      <div>
                        <div className="flex items-start justify-between gap-2">
                          <div>
                            <p className="font-johnson-text text-[9px] font-bold text-jj-gray-05 uppercase tracking-[0.14em] mb-1">Rule ID</p>
                            <span className="font-mono text-[11px] font-bold text-jj-blue-03 bg-blue-50 px-2.5 py-0.5 rounded-lg border border-jj-blue-03 border-opacity-25">
                              {rule.rule_id}
                            </span>
                          </div>
                          <div className="text-right flex-shrink-0">
                            <p className="font-johnson-text text-[9px] font-bold text-jj-gray-05 uppercase tracking-[0.14em] mb-1">Status</p>
                            <StatusBadge status={rule.status} />
                          </div>
                        </div>
                      </div>

                      {/* Rule name */}
                      <div className="flex-1">
                        <p className="font-johnson-text text-[9px] font-bold text-jj-gray-05 uppercase tracking-[0.14em] mb-1">Rule Name</p>
                        <p
                          className="font-johnson-display text-[15px] text-jj-gray-08 leading-snug group-hover:text-jj-red transition-colors"
                          style={{ display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}
                        >
                          {rule.rule_title}
                        </p>
                      </div>

                      {/* Target table */}
                      <div>
                        <p className="font-johnson-text text-[9px] font-bold text-jj-gray-05 uppercase tracking-[0.14em] mb-1">Target Table</p>
                        <div className="flex items-center gap-1.5">
                          <Ic src={databaseSrc} className="w-3 h-3 flex-shrink-0" filter={F.gray05} style={{ color: '#cbc4bc' }} />
                          <span className="font-mono text-[11px] text-jj-gray-05 truncate">{rule.target_table}</span>
                        </div>
                      </div>

                      {/* View details cta */}
                      <div className="flex items-center justify-end pt-1" style={{ borderTop: `1px solid ${hex}15` }}>
                        <span
                          className="font-johnson-text text-xs flex items-center gap-1 transition-colors"
                          style={{ color: '#a39992' }}
                        >
                          Click to view full details
                          <Ic src={chevronRightSrc} className="w-3 h-3 group-hover:translate-x-0.5 transition-transform" filter={F.gray07} />
                        </span>
                      </div>
                    </div>
                  </div>
                </button>
              );
            })}
          </div>
        )}
      </div>

      {/* ── Footer ───────────────────────────────────────────────────────────── */}
      <div className="flex-shrink-0 px-6 py-3 bg-white border-t border-jj-gray-02 flex items-center justify-between">
        <span className="font-johnson-text text-xs text-jj-gray-05">
          {datasetRules.length} rule{datasetRules.length !== 1 ? 's' : ''} — click a card to view full details
        </span>
        <button
          onClick={onClose}
          className="px-4 py-1.5 rounded-lg font-johnson-text text-sm font-bold text-jj-gray-07 bg-white hover:bg-jj-gray-02 transition-colors border border-jj-gray-03"
        >
          Close
        </button>
      </div>
    </>
  );
}

// ─── Detail View ──────────────────────────────────────────────────────────────
function RuleDetailView({ rule, api, onBack, onClose, accentBorder, datasetName, isDeltaMode = false }) {
  const [registry, setRegistry]         = useState(null);
  const [regLoading, setRegLoading]     = useState(true);
  const [trendData, setTrendData]       = useState([]);
  const [trendLoading, setTrendLoading] = useState(true);

  useEffect(() => {
    setRegLoading(true);
    api.getRuleRegistry(rule.rule_id)
      .then(setRegistry)
      .catch(() => setRegistry(null))
      .finally(() => setRegLoading(false));

    setTrendLoading(true);
    api.getRuleTrends(rule.rule_id, isDeltaMode)
      .then(setTrendData)
      .catch(() => setTrendData([]))
      .finally(() => setTrendLoading(false));
  }, [rule.rule_id, isDeltaMode]);

  const passColor  = rule.pass_percentage >= 95 ? '#328714' : rule.pass_percentage >= 80 ? '#0f68b2' : '#eb1700';
  const passTextCl = rule.pass_percentage >= 95 ? 'text-jj-green-03' : rule.pass_percentage >= 80 ? 'text-jj-blue-03' : 'text-jj-red';

  const Field = ({ label, children }) => (
    <div>
      <p className="font-johnson-text text-[10px] font-bold text-jj-gray-05 uppercase tracking-widest mb-1">{label}</p>
      {children}
    </div>
  );

  return (
    <>
      {/* Header */}
      <div className={`flex-shrink-0 px-5 py-3.5 border-b-4 ${accentBorder} flex items-center gap-3`}>
        <button
          onClick={onBack}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg font-johnson-text text-xs font-bold text-jj-gray-07 bg-jj-gray-01 hover:bg-jj-gray-02 transition-colors border border-jj-gray-02 flex-shrink-0"
        >
          <Ic src={chevronLeftSrc} className="w-3.5 h-3.5" filter={F.gray07} />
          Back
        </button>
        <div className="flex-1 min-w-0">
          <p className="font-johnson-text text-[10px] font-bold text-jj-gray-05 uppercase tracking-widest">{datasetName}</p>
          <h2 className="font-johnson-display text-base text-jj-gray-08 truncate" style={{ lineHeight: '115%' }}>
            {rule.rule_title}
          </h2>
        </div>
        <StatusBadge status={rule.status} />
        <button onClick={onClose} className="w-8 h-8 flex-shrink-0 flex items-center justify-center rounded-lg bg-jj-gray-01 hover:bg-jj-gray-02 transition-colors">
          <Ic src={closeSrc} className="w-4 h-4" filter={F.gray07} />
        </button>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto">
        <div className="p-5 space-y-5">

          {/* Pass rate gauge + key fields */}
          <div className="flex gap-5 flex-wrap">
            {/* Gauge */}
            <div className="flex flex-col items-center justify-center bg-jj-gray-01 rounded-xl border border-jj-gray-02 px-6 py-4 flex-shrink-0">
              <div className="relative w-24 h-24">
                <svg viewBox="0 0 36 36" className="w-24 h-24 -rotate-90">
                  <circle cx="18" cy="18" r="15.5" fill="none" stroke="#e8e6e3" strokeWidth="3" />
                  <circle
                    cx="18" cy="18" r="15.5" fill="none"
                    stroke={passColor}
                    strokeWidth="3"
                    strokeDasharray={`${rule.pass_percentage * 0.9738} 97.38`}
                    strokeLinecap="round"
                    style={{ transition: 'stroke-dasharray 0.6s ease' }}
                  />
                </svg>
                <div className="absolute inset-0 flex flex-col items-center justify-center">
                  <span className={`font-johnson-display text-1xl font-bold ${passTextCl}`} style={{ lineHeight: 1 }}>
                    {rule.pass_percentage}%
                  </span>
                  <span className="font-johnson-text text-[9px] text-jj-gray-05 uppercase tracking-wide mt-0.5">Pass Rate</span>
                </div>
              </div>
            </div>

            {/* Key fields */}
            <div className="flex-1 min-w-0 grid grid-cols-1 sm:grid-cols-2 gap-x-5 gap-y-3">
              <Field label="Rule ID">
                <span className="font-mono text-xs font-bold text-jj-blue-03 bg-blue-50 px-2.5 py-1 rounded border border-jj-blue-03 border-opacity-20 inline-block">
                  {rule.rule_id}
                </span>
              </Field>
              <Field label="Severity"><SeverityBadge severity={rule.severity} /></Field>
              <Field label="Dataset Parent">
                <span className="font-johnson-text text-xs text-jj-gray-07 bg-jj-gray-01 border border-jj-gray-02 px-2 py-1 rounded inline-block truncate max-w-full">
                  {rule.dataset_title || datasetName}
                </span>
              </Field>
              <Field label="Target Table">
                <span className="font-mono text-xs text-jj-gray-07 bg-jj-gray-01 border border-jj-gray-02 px-2 py-1 rounded inline-block break-all">
                  {rule.target_table}
                </span>
              </Field>
            </div>
          </div>

          {/* Title & Description */}
          <div className="space-y-3">
            <Field label="Title">
              <p className="font-johnson-display text-base text-jj-gray-08" style={{ lineHeight: '120%' }}>{rule.rule_title}</p>
            </Field>
            {rule.rule_description && (
              <Field label="Description">
                <p className="font-johnson-text text-sm text-jj-gray-07 leading-relaxed bg-jj-gray-01 border border-jj-gray-02 px-4 py-3 rounded-lg">
                  {rule.rule_description}
                </p>
              </Field>
            )}
          </div>

          {/* SQL Predicate */}
          <Field label="SQL Predicate">
            {regLoading ? (
              <div className="flex items-center justify-center py-4 bg-jj-gray-01 border border-jj-gray-02 rounded-lg">
                <LoadingSpinner />
              </div>
            ) : registry?.sql_predicate ? (
              <div className="rounded-lg overflow-hidden border border-jj-gray-03">
                <div className="flex items-center justify-between px-4 py-2 bg-jj-gray-08 border-b border-jj-gray-05">
                  <div className="flex items-center gap-2">
                    <div className="flex gap-1.5">
                      <span className="w-2.5 h-2.5 rounded-full bg-jj-maroon-05 opacity-70" />
                      <span className="w-2.5 h-2.5 rounded-full bg-jj-yellow-01 opacity-70" />
                      <span className="w-2.5 h-2.5 rounded-full bg-jj-green-03 opacity-70" />
                    </div>
                    <span className="font-mono text-[10px] font-bold text-jj-gray-01 uppercase tracking-widest">SQL</span>
                  </div>
                  <button
                    onClick={() => navigator.clipboard?.writeText(registry.sql_predicate)}
                    className="font-johnson-text text-[10px] font-bold text-jj-gray-05 hover:text-white transition-colors uppercase tracking-widest"
                  >
                    Copy
                  </button>
                </div>
                <pre className="font-mono text-xs px-4 py-4 overflow-x-auto whitespace-pre-wrap leading-relaxed bg-jj-black" style={{ color: '#f1efed' }}>
                  {registry.sql_predicate}
                </pre>
              </div>
            ) : (
              <p className="font-johnson-text text-xs text-jj-gray-04 italic px-4 py-3 bg-jj-gray-01 border border-jj-gray-02 rounded-lg">
                No SQL predicate available for this rule.
              </p>
            )}
          </Field>

          {/* Trend chart */}
          <Field label="Historical Pass Rate Trend">
            {trendLoading ? (
              <div className="flex items-center justify-center h-40 bg-jj-gray-01 border border-jj-gray-02 rounded-lg">
                <LoadingSpinner />
              </div>
            ) : trendData.length === 0 ? (
              <div className="flex items-center justify-center h-40 bg-jj-gray-01 border border-jj-gray-02 rounded-lg">
                <span className="font-johnson-text text-xs text-jj-gray-04 italic">No trend data available yet.</span>
              </div>
            ) : (
              <div className="bg-jj-gray-01 border border-jj-gray-02 rounded-lg p-3">
                <ResponsiveContainer width="100%" height={180}>
                  <BarChart data={trendData} barCategoryGap="40%" margin={{ top: 4, right: 36, left: -10, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#e8e6e3" vertical={false} />
                    <XAxis dataKey="date" stroke="#a39992" tick={{ fontSize: 10, fontFamily: 'JohnsonText', fill: '#81766f' }} tickLine={false} axisLine={{ stroke: '#d5cfc9' }} padding={{ right: Math.max(0, (15 - trendData.length) * 40) }} />
                    <YAxis domain={[0, 100]} stroke="#a39992" tick={{ fontSize: 10, fontFamily: 'JohnsonText', fill: '#81766f' }} tickLine={false} axisLine={false} tickFormatter={v => `${v}%`} />
                    <Tooltip
                      cursor={{ fill: '#f1efed', radius: 3 }}
                      contentStyle={{ backgroundColor: '#fff', border: '1px solid #d5cfc9', borderRadius: '8px', fontFamily: 'JohnsonText', fontSize: '12px' }}
                      formatter={v => {
                        const color = v >= 95 ? '#328714' : v >= 80 ? '#0f68b2' : '#eb1700';
                        return [<span key="pass-rate" style={{ color, fontWeight: 700 }}>{`${v}%`}</span>, 'Pass Rate'];
                      }}
                    />
                    <ReferenceLine y={95} stroke="#328714" strokeDasharray="3 3" strokeWidth={1} />
                    <ReferenceLine y={80} stroke="#0f68b2" strokeDasharray="3 3" strokeWidth={1} />
                    <Bar dataKey="pass_percentage" radius={[3, 3, 0, 0]} maxBarSize={28}>
                      {trendData.map((entry, index) => (
                        <Cell
                          key={`cell-${index}`}
                          fill={entry.pass_percentage >= 95 ? '#328714' : entry.pass_percentage >= 80 ? '#0f68b2' : '#eb1700'}
                          fillOpacity={0.85}
                        />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            )}
          </Field>
        </div>
      </div>

      {/* Footer */}
      <div className="flex-shrink-0 px-5 py-3 bg-jj-gray-01 border-t border-jj-gray-02 flex items-center justify-between">
        <button
          onClick={onBack}
          className="flex items-center gap-1.5 px-4 py-1.5 rounded-lg font-johnson-text text-sm font-bold text-jj-gray-07 bg-white hover:bg-jj-gray-02 transition-colors border border-jj-gray-03"
        >
          <Ic src={chevronLeftSrc} className="w-3.5 h-3.5" filter={F.gray07} />
          All Rules
        </button>
        <button onClick={onClose} className="px-4 py-1.5 rounded-lg font-johnson-text text-sm font-bold text-white bg-jj-red hover:bg-jj-maroon-05 transition-colors">
          Close
        </button>
      </div>
    </>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// Dataset Health Tab
// ═══════════════════════════════════════════════════════════════════════════════
function DatasetHealthTab({ api, refreshKey, rules = [], isDeltaMode = false }) {
  const [datasets, setDatasets]           = useState([]);
  const [loading, setLoading]             = useState(true);
  const [error, setError]                 = useState(null);
  const [selectedDataset, setSelectedDataset] = useState(null);
  const { sortKey: dhSortKey, sortDir: dhSortDir, toggle: dhToggle } = useSortable('pass_rate', 'asc');

  useEffect(() => {
    setLoading(true);
    api.getDatasetHealth(isDeltaMode)
      .then(setDatasets)
      .catch(err => { console.error(err); setError('Failed to load dataset health data.'); })
      .finally(() => setLoading(false));
  }, [refreshKey, isDeltaMode]);

  if (loading) return <div className="flex items-center justify-center h-full min-h-[400px]"><LoadingSpinner /></div>;
  if (error)   return <EmptyState icon={closeSrc} title="Error Loading Data" message={error} />;

  return (
    <>
      <SectionCard
        title="Dataset Health Overview"
        icon={databaseSrc}
        headerRight={
          <span className="font-johnson-text text-xs text-jj-gray-05">
            Click a row to view rules
          </span>
        }
      >
        {datasets.length === 0
          ? <EmptyState icon={activitySrc} title="No Dataset Health Data" message="No dataset health information is available." />
          : (
            <div className="overflow-x-auto" data-tour="bdc-dataset-table">
              <table className="w-full font-johnson-text text-sm">
                <thead>
                  <tr className="border-b-2 border-jj-gray-02">
                    {[
                      { label: 'Dataset Parent', key: 'dataset',     sortable: true  },
                      { label: 'Total Rules',    key: 'total_rules', sortable: true  },
                      { label: 'Avg Pass Rate',  key: 'pass_rate',   sortable: true  },
                      { label: 'Health Status',  key: 'status',      sortable: true  },
                      { label: '',               key: null,          sortable: false },
                    ].map(({ label, key, sortable }) => sortable
                      ? <SortableTh key={label} label={label} colKey={key} sortKey={dhSortKey} sortDir={dhSortDir} onSort={dhToggle} />
                      : <th key="cta" className="px-5 py-3 bg-jj-gray-01" />
                    )}
                  </tr>
                </thead>
                <tbody className="divide-y divide-jj-gray-02">
                  {[...datasets].sort((a, b) => {
                    let av = dhSortKey === 'status'      ? (HEALTH_ORDER[a.status] ?? 99)
                           : dhSortKey === 'pass_rate'   ? (a.pass_rate ?? 0)
                           : dhSortKey === 'total_rules' ? (a.total_rules ?? 0)
                           : String(a[dhSortKey] ?? '').toLowerCase();
                    let bv = dhSortKey === 'status'      ? (HEALTH_ORDER[b.status] ?? 99)
                           : dhSortKey === 'pass_rate'   ? (b.pass_rate ?? 0)
                           : dhSortKey === 'total_rules' ? (b.total_rules ?? 0)
                           : String(b[dhSortKey] ?? '').toLowerCase();
                    const cmp = typeof av === 'number' ? av - bv : av.localeCompare(bv);
                    return dhSortDir === 'asc' ? cmp : -cmp;
                  }).map((ds, i) => (
                    <tr
                      key={i}
                      onClick={() => setSelectedDataset(ds)}
                      className="hover:bg-jj-gray-02 cursor-pointer transition-colors group"
                    >
                      <td className="px-5 py-3">
                        <span className="font-johnson-text text-sm font-bold text-jj-gray-08 group-hover:text-jj-red transition-colors">
                          {ds.dataset}
                        </span>
                      </td>
                      <td className="px-5 py-3 whitespace-nowrap">
                        <span className="inline-flex items-center gap-1.5 text-xs font-bold text-jj-blue-03 bg-blue-50 border border-jj-blue-03 border-opacity-20 px-2.5 py-1 rounded">
                          <Ic src={recordsSrc} className="w-3 h-3" filter={F.blue} />
                          {ds.total_rules}
                        </span>
                      </td>
                      <td className="px-5 py-3 min-w-[220px]">
                        <PassBar pct={ds.pass_rate} />
                      </td>
                      <td className="px-5 py-3 whitespace-nowrap">
                        <StatusBadge status={ds.status} />
                      </td>
                      <td className="px-5 py-3 whitespace-nowrap text-right">
                        <span className="inline-flex items-center gap-1 font-johnson-text text-xs text-jj-gray-05 group-hover:text-jj-red transition-colors">
                          View rules
                          <Ic src={chevronRightSrc} className="w-3.5 h-3.5" filter={F.gray07} />
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        }
      </SectionCard>

      {selectedDataset && (
        <DatasetRulesModal
          dataset={selectedDataset}
          rules={rules}
          api={api}
          isDeltaMode={isDeltaMode}
          onClose={() => setSelectedDataset(null)}
        />
      )}
    </>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// Trends Tab
// ═══════════════════════════════════════════════════════════════════════════════
function TrendsTab({ rules, api, isDeltaMode = false }) {
  const [selectedRule, setSelectedRule] = useState(rules[0]);
  const [trendData, setTrendData]       = useState([]);
  const [loading, setLoading]           = useState(false);
  const [error, setError]               = useState(null);

  useEffect(() => {
    if (!selectedRule) return;
    setLoading(true); setError(null);
    api.getRuleTrends(selectedRule.rule_id, isDeltaMode)
      .then(data => { setTrendData(data); })
      .catch(err => { console.error(err); setError('Failed to load trend data.'); })
      .finally(() => setLoading(false));
  }, [selectedRule, isDeltaMode]);

  return (
    <div className="space-y-4">
      <div data-tour="bdc-rule-select">
        <RuleSelect
          rules={rules}
          value={selectedRule?.rule_id}
          onChange={setSelectedRule}
          label="Trend Analysis — Rule"
          hint="Select a business rule to view how its pass rate has changed over time across executions"
        />
      </div>

      <div data-tour="bdc-trend-chart">
        <SectionCard title="Historical Trend Analysis" icon={chartSrc} accentColor="text-jj-blue-03">
          <div className="p-5">
            {loading ? <div className="flex items-center justify-center min-h-[300px]"><LoadingSpinner /></div> : error ? (
              <EmptyState icon={closeSrc} title="Error Loading Data" message={error} />
            ) : trendData.length === 0 ? (
              <EmptyState icon={chartSrc} title="No Trend Data" message="No historical trend data is available for this rule yet." />
            ) : (
              <div>
                {/* Legend */}
                <div className="flex items-center gap-5 mb-4 px-1 flex-wrap">
                  <span className="font-johnson-text text-xs font-bold text-jj-gray-06 uppercase tracking-widest">Pass Rate Threshold:</span>
                  <span className="flex items-center gap-1.5 font-johnson-text text-xs text-jj-gray-07">
                    <span className="w-3 h-3 rounded-sm inline-block" style={{ backgroundColor: '#328714' }} />
                    ≥ 95% Healthy
                  </span>
                  <span className="flex items-center gap-1.5 font-johnson-text text-xs text-jj-gray-07">
                    <span className="w-3 h-3 rounded-sm inline-block" style={{ backgroundColor: '#0f68b2' }} />
                    80–94% Warning
                  </span>
                  <span className="flex items-center gap-1.5 font-johnson-text text-xs text-jj-gray-07">
                    <span className="w-3 h-3 rounded-sm inline-block" style={{ backgroundColor: '#eb1700' }} />
                    &lt; 80% Critical
                  </span>
                </div>
                <ResponsiveContainer width="100%" height={300}>
                  <BarChart data={trendData} barCategoryGap="40%" margin={{ top: 8, right: 48, left: -8, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#e8e6e3" vertical={false} />
                    <XAxis
                      dataKey="date"
                      stroke="#a39992"
                      tick={{ fontSize: 11, fontFamily: 'JohnsonText', fill: '#81766f' }}
                      tickLine={false}
                      axisLine={{ stroke: '#d5cfc9' }}
                      padding={{ right: Math.max(0, (15 - trendData.length) * 75) }}
                    />
                    <YAxis
                      stroke="#a39992"
                      domain={[0, 100]}
                      tick={{ fontSize: 11, fontFamily: 'JohnsonText', fill: '#81766f' }}
                      tickLine={false}
                      axisLine={false}
                      tickFormatter={v => `${v}%`}
                    />
                    <Tooltip
                      cursor={{ fill: '#f1efed', radius: 4 }}
                      contentStyle={{
                        backgroundColor: '#ffffff',
                        border: '1px solid #d5cfc9',
                        borderRadius: '10px',
                        boxShadow: '0 4px 16px rgba(0,0,0,0.10)',
                        fontFamily: 'JohnsonText',
                        fontSize: '13px',
                        padding: '10px 14px',
                      }}
                      labelStyle={{ fontWeight: 700, color: '#312c2a', marginBottom: 4, fontFamily: 'JohnsonDisplay' }}
                      formatter={(value) => {
                        const color = value >= 95 ? '#328714' : value >= 80 ? '#0f68b2' : '#eb1700';
                        return [
                          <span key="pass-rate" style={{ color, fontWeight: 700 }}>{`${value}%`}</span>,
                          'Pass Rate'
                        ];
                      }}
                    />
                    <ReferenceLine y={95} stroke="#328714" strokeDasharray="4 3" strokeWidth={1.5} label={{ value: '95%', position: 'right', fontSize: 10, fontFamily: 'JohnsonText', fill: '#328714', fontWeight: 700 }} />
                    <ReferenceLine y={80} stroke="#0f68b2" strokeDasharray="4 3" strokeWidth={1.5} label={{ value: '80%', position: 'right', fontSize: 10, fontFamily: 'JohnsonText', fill: '#0f68b2', fontWeight: 700 }} />
                    <Bar dataKey="pass_percentage" name="Pass Rate (%)" radius={[4, 4, 0, 0]} maxBarSize={28}>
                      {trendData.map((entry, index) => (
                        <Cell
                          key={`cell-${index}`}
                          fill={entry.pass_percentage >= 95 ? '#328714' : entry.pass_percentage >= 80 ? '#0f68b2' : '#eb1700'}
                          fillOpacity={0.88}
                        />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            )}
          </div>
        </SectionCard>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// Process Modal
// ═══════════════════════════════════════════════════════════════════════════════
const ProcessModal = ({ onClose, API_BASE_URL, processType, validationMode = 'EXECUTE_FULL' }) => {
  const { user } = useAuth();
  const userEmail = user?.email || user?.username || 'unknown';
  const [logs, setLogs]             = useState([]);
  const [isRunning, setIsRunning]   = useState(true);
  const [returnCode, setReturnCode] = useState(null);
  const logsEndRef                  = useRef(null);
  const eventSourceRef              = useRef(null);

  useEffect(() => { logsEndRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [logs]);

  const config = processType === 'validation'
    ? { endpoint: 'run-validation',  title: 'Validation',  color: 'text-jj-green-03', border: '#328714', bg: 'bg-green-50' }
    : { endpoint: 'run-compilation', title: 'Compilation', color: 'text-jj-blue-03',  border: '#0f68b2', bg: 'bg-blue-50' };

  const modeLabel = processType === 'validation'
    ? validationMode === 'EXECUTE_FULL'          ? 'New Rules Only'
    : validationMode === 'EXECUTE_ALLRULES'       ? 'All Rules'
    : validationMode === 'EXECUTE_ALLRULES_DELTA'  ? 'Delta - All Rules'
    : `Rule: ${validationMode.replace('EXECUTE_', '')}`
    : null;

  useEffect(() => {
    const url = processType === 'validation'
      ? `${API_BASE_URL}/api/${config.endpoint}?mode=${validationMode}`
      : `${API_BASE_URL}/api/${config.endpoint}`;
    const es = apiEventSource(url);
    eventSourceRef.current = es;

    // Log that the process was started
    const action = processType === 'validation' ? 'bdc_validation_started' : 'bdc_compilation_started';
    const details = processType === 'validation'
      ? `Started validation (mode: ${modeLabel})`
    : `Started compilation`;
    logAuditEvent(API_BASE_URL, userEmail, action, processType === 'validation' ? validationMode : 'compilation', details);

    es.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        setLogs(prev => [...prev, data]);
        if (data.type === 'done') {
          setIsRunning(false);
          setReturnCode(data.returncode);
          es.close();
          // Log completion with success/failure result
          const doneAction = processType === 'validation' ? 'bdc_validation_completed' : 'bdc_compilation_completed';
          const result = data.returncode === 0 ? 'succeeded' : `failed (exit code ${data.returncode})`;
          const doneDetails = processType === 'validation'
            ? `Validation (mode: ${modeLabel}) ${result}`
            : `Compilation ${result}`;
          logAuditEvent(API_BASE_URL, userEmail, doneAction, processType === 'validation' ? validationMode : 'compilation', doneDetails);
        }
      } catch (e) { console.error('SSE parse error:', e); }
    };

    es.onerror = () => {
      setLogs(prev => [...prev, { type: 'error', message: 'Connection lost to server', timestamp: new Date().toLocaleString('en-US', { timeZone: 'America/New_York' }) }]);
      setIsRunning(false); es.close();
    };

    return () => es.close();
  }, [API_BASE_URL, config.endpoint]);

  const handleClose = () => { eventSourceRef.current?.close(); onClose(); };

  const getLogStyle = (type) => ({
    error:   'text-jj-maroon-05 font-bold',
    success: 'text-jj-green-03 font-bold',
    status:  'text-jj-blue-03 font-bold',
    info:    'text-jj-blue-03',
    separator: '',
  }[type] || 'text-jj-gray-08');

  const formatTs = (ts) => new Date(ts).toLocaleTimeString('en-US', { timeZone: 'America/New_York', hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });

  const statusText = isRunning
    ? `${config.title} Running…`
    : returnCode === 0
      ? `${config.title} Completed Successfully`
      : `${config.title} Failed`;

  return (
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4" onClick={handleClose} style={{ backdropFilter: 'blur(3px)' }}>
      <div
        className="bg-white rounded-2xl shadow-2xl border border-jj-gray-02 w-full max-w-5xl flex flex-col overflow-hidden"
        style={{ height: 'min(85vh, calc(100vh - 2rem))', animation: 'slideUp 0.25s ease-out' }}
        onClick={e => e.stopPropagation()}
      >

        {/* Header */}
        <div className="px-6 py-4 bg-white border-b-4 flex items-center justify-between flex-shrink-0" style={{ borderBottomColor: config.border }}>
          <div className="flex items-center gap-3">
            {isRunning
              ? <Ic src={activitySrc} className={`w-5 h-5 ${config.color} animate-spin`} filter={F.gray07} />
              : returnCode === 0
                ? <Ic src={checkSrc} className="w-5 h-5" filter={F.green} />
                : <Ic src={closeSrc} className="w-5 h-5" filter={F.maroon} />
            }
            <h2 className="font-johnson-display text-lg text-jj-gray-08" style={{ lineHeight: '110%' }}>{statusText}</h2>
            <span className={`font-johnson-text text-xs font-bold px-2.5 py-1 rounded-full ${config.bg} ${config.color} border border-current border-opacity-20`}>{config.title}</span>
            {modeLabel && (
              <span className="font-johnson-text text-xs font-bold px-2.5 py-1 rounded-full bg-jj-gray-01 text-jj-gray-07 border border-jj-gray-03">
                {modeLabel}
              </span>
            )}
          </div>
          <button
            onClick={handleClose}
            disabled={isRunning}
            className="w-9 h-9 flex items-center justify-center rounded-lg bg-jj-gray-01 hover:bg-jj-gray-02 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <Ic src={closeSrc} className="w-4 h-4" filter={F.gray07} />
          </button>
        </div>

        {/* Log output */}
        <div className="flex-1 min-h-0 bg-jj-gray-01 p-4" style={{ minHeight: 0 }}>
          <div
            className="overflow-y-auto bg-white border border-jj-gray-02 rounded-lg p-4 font-johnson-text text-sm"
            style={{ height: '100%', scrollbarWidth: 'thin', scrollbarColor: '#a39992 #f1efed' }}
          >
            {logs.length === 0 && (
              <div className="font-johnson-text text-sm text-jj-gray-05 italic">Connecting to {processType} service…</div>
            )}
            {logs.map((log, i) => {
              if (log.type === 'separator') return <div key={i} className="my-2 border-t border-jj-gray-02" />;
              return (
                <div key={i} className="mb-1 flex gap-3">
                  <span className="font-johnson-text text-xs text-jj-gray-05 flex-shrink-0 pt-0.5">[{formatTs(log.timestamp)}]</span>
                  <span className={`font-johnson-text text-sm ${getLogStyle(log.type)}`}>{log.message}</span>
                </div>
              );
            })}
            <div ref={logsEndRef} />
          </div>
        </div>

        {/* Footer */}
        <div className="px-6 py-3 bg-white border-t border-jj-gray-02 flex justify-between items-center flex-shrink-0">
          <div className="font-johnson-text text-sm text-jj-gray-06 flex items-center gap-2">
            {isRunning
              ? <><Ic src={activitySrc} className="w-4 h-4 animate-spin" filter={F.gray07} />{config.title} in progress…</>
              : <span>{returnCode === 0 ? `✓ ${config.title} completed successfully` : `✗ ${config.title} failed`}</span>
            }
          </div>
          <button
            onClick={handleClose}
            disabled={isRunning}
            className={`px-5 py-2 rounded-lg font-johnson-text text-sm font-bold transition-colors ${
              isRunning ? 'bg-jj-gray-02 text-jj-gray-05 cursor-not-allowed' : 'bg-jj-red text-white hover:bg-jj-maroon-05'
            }`}
          >
            {isRunning ? 'Running…' : 'Close'}
          </button>
        </div>
      </div>
    </div>
  );
};

// ═══════════════════════════════════════════════════════════════════════════════
// Failed Rows Tab
// ═══════════════════════════════════════════════════════════════════════════════

// ═══════════════════════════════════════════════════════════════════════════════
// ModalDataTable — single reusable table used by Failed Rows, Delta, and
// Consolidated modals. All three share identical structure, styling, and
// resize / word-wrap behaviour; only the data + state wired to each differ.
// ═══════════════════════════════════════════════════════════════════════════════
function ModalDataTable({
  columns,
  rows,
  colWidths,
  onResize,
  wordWrap,
  rowOffset    = 0,    // displayed row# = rowOffset + localIndex + 1
  colorCol1    = null, // column name for start-date diff colouring
  colorCol2    = null, // column name for end-date diff colouring
  ruleColorMap = null, // per-rule color map { rule_id: [col1, col2] } — overrides colorCol1/2
  loading      = false,
  error        = null,
  emptyMessage = 'No rows found.',
}) {
  // ── Client-side sort for current page ──────────────────────────────────────
  const [sortColIdx, setSortColIdx] = useState(null);
  const [sortDir,    setSortDir]    = useState('asc');

  // Reset sort whenever a completely new result set arrives (column schema changed)
  useEffect(() => { setSortColIdx(null); setSortDir('asc'); }, [columns]);

  const compareRaw = (a, b) => {
    if (a === null || a === undefined) return 1;
    if (b === null || b === undefined) return -1;
    const sa = String(a).trim(), sb = String(b).trim();
    const na = parseFloat(sa), nb = parseFloat(sb);
    if (!isNaN(na) && !isNaN(nb) && sa !== '' && sb !== '') return na - nb;
    return sa.localeCompare(sb, undefined, { numeric: true, sensitivity: 'base' });
  };
  const sortedRows = sortColIdx !== null
    ? [...rows].sort((a, b) => {
        const cmp = compareRaw(a[sortColIdx], b[sortColIdx]);
        return sortDir === 'asc' ? cmp : -cmp;
      })
    : rows;
  if (error) return (
    <div className="flex-1 min-h-0 overflow-auto flex flex-col items-center justify-center gap-3 p-8">
      <Ic src={closeSrc} className="w-12 h-12" filter={F.red} />
      <p className="font-johnson-display text-lg text-jj-gray-08">{error}</p>
    </div>
  );

  // When ruleColorMap is provided, c1i/c2i are resolved per-row inside the loop
  const c1i = (!ruleColorMap && colorCol1) ? columns.findIndex(c => c.trim().toUpperCase() === colorCol1.toUpperCase()) : -1;
  const c2i = (!ruleColorMap && colorCol2) ? columns.findIndex(c => c.trim().toUpperCase() === colorCol2.toUpperCase()) : -1;

  return (
    <div className="flex-1 min-h-0 relative flex flex-col">
      {loading && (
        <div className="absolute inset-0 bg-white bg-opacity-80 flex items-center justify-center z-20" style={{ pointerEvents: 'all' }}>
          <LoadingSpinner />
        </div>
      )}
      <div className="flex-1 min-h-0 overflow-auto">

      {rows.length === 0 && !loading ? (
        <div className="flex flex-col items-center justify-center h-full gap-3 p-8">
          <Ic src={checkSrc} className="w-12 h-12" filter={F.green} />
          <p className="font-johnson-display text-lg text-jj-gray-08">{emptyMessage}</p>
        </div>
      ) : (
        <table style={{ tableLayout: 'fixed', width: 'max-content', minWidth: '100%', borderCollapse: 'separate', borderSpacing: 0 }}>
          <colgroup>
            <col style={{ width: '2.5rem', minWidth: '2.5rem' }} />
            {columns.map((_, ci) => (
              <col key={ci} style={{ width: columns[ci] === 'FAILURE_MODE' ? 'auto' : `${colWidths[ci] ?? COL_MAX_WIDTH}px` }} />
            ))}
          </colgroup>
          <thead>
            <tr style={{ position: 'sticky', top: 0, zIndex: 2 }}>
              {/* Row # header */}
              <th
                style={{ position: 'sticky', left: 0, zIndex: 3, borderBottom: '2px solid #eb1700', width: '2.5rem', minWidth: '2.5rem' }}
                className="bg-jj-gray-01 border-r border-jj-gray-04 px-2 py-2.5 text-center"
              >
                <span className="font-mono text-[10px] text-jj-gray-04">#</span>
              </th>

              {/* Column headers with resize handles */}
              {columns.map((col, ci) => (
                <th key={col}
                  className="bg-jj-gray-01 border-r border-jj-gray-04 px-0 py-0 relative select-none group/col"
                  style={{
                    borderBottom: '2px solid #eb1700',
                    ...(col !== 'FAILURE_MODE' && { width: `${colWidths[ci] ?? COL_MAX_WIDTH}px` }),
                  }}>

                  {/* Click-to-sort label */}
                  <div
                    className="px-3 py-2.5 overflow-hidden cursor-pointer hover:bg-jj-gray-02 transition-colors"
                    style={{ paddingRight: '1.25rem' }}
                    onClick={() => {
                      if (sortColIdx === ci) setSortDir(d => d === 'asc' ? 'desc' : 'asc');
                      else { setSortColIdx(ci); setSortDir('asc'); }
                    }}
                  >
                    <div className="flex items-center gap-1 min-w-0">
                      <span
                        className="font-mono text-xs font-bold uppercase tracking-wide block truncate flex-1"
                        title={col}
                        style={{ color: sortColIdx === ci ? '#eb1700' : '#312c2a' }}
                      >
                        {col}
                      </span>
                      <span className={`flex-shrink-0 transition-opacity ${sortColIdx === ci ? 'opacity-100' : 'opacity-0 group-hover/col:opacity-40'}`}>
                        {sortColIdx === ci && sortDir === 'asc'
                          ? <Ic src={chevronRightSrc} className="w-2.5 h-2.5 -rotate-90" filter={F.red} />
                          : sortColIdx === ci && sortDir === 'desc'
                            ? <Ic src={chevronRightSrc} className="w-2.5 h-2.5 rotate-90" filter={F.red} />
                            : <Ic src={sortSrc} className="w-2.5 h-2.5" filter={F.gray06} />
                        }
                      </span>
                    </div>
                  </div>

                  {/* Drag-to-resize handle — separate from sort click */}
                  <div
                    onMouseDown={e => onResize(e, ci)}
                    className="absolute top-0 right-0 h-full w-2 cursor-col-resize group/handle flex items-center justify-center z-10"
                  >
                    <div className="w-0.5 h-4 bg-jj-gray-03 rounded-full group-hover/handle:bg-jj-red group-hover/handle:h-full transition-all duration-150" />
                  </div>
                </th>
              ))}
            </tr>
          </thead>

          <tbody>
            {sortedRows.map((row, i) => {
              const failedRuleIdx = columns.indexOf('FAILURE_MODE');
              const isEven        = i % 2 === 0;
              const rowCls        = 'hover:bg-red-50';
              const rowBg         = { backgroundColor: isEven ? '#ffffff' : '#fafaf9' };

              // Build per-cell color styles — gradient when multiple rules give different colors
              const _BG_RANK = { '#f0fdf4': 0, '#fefce8': 1, '#fff1f2': 2 };
              const _BG_BORDER = { '#f0fdf4': '#328714', '#fefce8': '#ca8a04', '#fff1f2': '#eb1700' };
              const _cellBg = (d) => {
                if (d === null || d === undefined) return '#fff1f2';
                if (d <= 7)  return '#f0fdf4';
                if (d <= 15) return '#fefce8';
                return '#fff1f2';
              };
              const _mergeStyles = (bgs) => {
                const unique = [...new Set(bgs)];
                const worst = unique.reduce((a, b) => _BG_RANK[a] >= _BG_RANK[b] ? a : b);
                const border = `3px solid ${_BG_BORDER[worst]}`;
                if (unique.length === 1) return { backgroundColor: unique[0], borderLeft: border };
                const sorted = [...unique].sort((a, b) => _BG_RANK[b] - _BG_RANK[a]);
                const pct = 100 / sorted.length;
                const stops = sorted.map((c, i) => `${c} ${i * pct}% ${(i + 1) * pct}%`).join(', ');
                return { background: `linear-gradient(to right, ${stops})`, borderLeft: border };
              };
              // Accumulate bg colors per cell index
              const cellBgAccum = {};  // ci -> [bg, ...]
              if (ruleColorMap && failedRuleIdx !== -1 && row[failedRuleIdx]) {
                const ruleIds = String(row[failedRuleIdx]).split(',').map(r => r.trim());
                for (const rid of ruleIds) {
                  const p = ruleColorMap[rid];
                  if (p && p[0] && p[1]) {
                    const ci1 = columns.findIndex(c => c.trim().toUpperCase() === (p[0] || '').toUpperCase());
                    const ci2 = columns.findIndex(c => c.trim().toUpperCase() === (p[1] || '').toUpperCase());
                    if (ci1 !== -1 && ci2 !== -1) {
                      const bg = _cellBg(calculateDateDifference(row[ci1], row[ci2]));
                      (cellBgAccum[ci1] ??= []).push(bg);
                      (cellBgAccum[ci2] ??= []).push(bg);
                    }
                  }
                }
              } else if (c1i !== -1 && c2i !== -1) {
                const bg = _cellBg(calculateDateDifference(row[c1i], row[c2i]));
                cellBgAccum[c1i] = [bg]; cellBgAccum[c2i] = [bg];
              }
              const cellColorStyles = Object.fromEntries(
                Object.entries(cellBgAccum).map(([ci, bgs]) => [ci, _mergeStyles(bgs)])
              );

              return (
                <tr key={i} className={`transition-colors ${rowCls}`} style={rowBg}>
                  {/* Sticky row number */}
                  <td
                    style={{ position: 'sticky', left: 0, zIndex: 1, backgroundColor: isEven ? '#f1efed' : '#e8e6e3' }}
                    className="border-b border-r border-jj-gray-04 px-2 py-2.5 text-center select-none"
                  >
                    <span className="font-mono text-[10px] text-jj-gray-05 leading-none">
                      {rowOffset + i + 1}
                    </span>
                  </td>

                  {/* Data cells */}
                  {row.map((cell, j) => {
                    const colName    = columns[j];
                    const isRule     = colName === 'FAILURE_MODE';
                    const isColorCol = j in cellColorStyles;
                    const isNull     = cell === null || cell === undefined;
                    const ruleIds    = isRule && !isNull ? String(cell).split(', ') : null;
                    const colW       = colWidths[j] ?? COL_MAX_WIDTH;
                    const isFRCol    = colName === 'FAILURE_MODE';
                    return (
                      <td key={j}
                        className="border-b border-r border-jj-gray-04 px-3 py-2.5 align-top"
                        style={{
                          ...(isFRCol ? { whiteSpace: 'nowrap' } : { width: `${colW}px`, maxWidth: `${colW}px` }),

                          ...(isColorCol ? cellColorStyles[j] : {}),
                        }}
                        title={isNull ? 'NULL' : String(cell)}>
                        {isNull ? (
                          <span className="font-mono text-[10px] text-jj-gray-04 bg-jj-gray-01 px-1.5 py-0.5 rounded border border-jj-gray-04 italic">
                            NULL
                          </span>
                        ) : isRule ? (
                          <span className="flex items-center gap-x-1" style={{ flexWrap: 'nowrap' }}>
                            {ruleIds.map((id, idx) => {
                              const rid = id.trim();
                              // Derive text colour from this rule's own colour-column date diff,
                              // matching the Excel output: green ≤7d, bold-orange 8-15d, red ≥16d/null.
                              let ruleColor  = '#312C2A';
                              let ruleBold   = false;
                              if (ruleColorMap) {
                                const p = ruleColorMap[rid];
                                if (p && p[0] && p[1]) {
                                  const ri1 = columns.findIndex(c => c.trim().toUpperCase() === (p[0] || '').toUpperCase());
                                  const ri2 = columns.findIndex(c => c.trim().toUpperCase() === (p[1] || '').toUpperCase());
                                  if (ri1 !== -1 && ri2 !== -1) {
                                    const days = calculateDateDifference(row[ri1], row[ri2]);
                                    if (days === null)  { ruleColor = '#DC2626'; }
                                    else if (days <= 7) { ruleColor = '#16A34A'; }
                                    else if (days <= 15){ ruleColor = '#FB923C'; ruleBold = true; }
                                    else                { ruleColor = '#DC2626'; }
                                  }
                                }
                              }
                              return (
                                <span key={rid} className="inline-flex items-center font-johnson-text text-xs">
                                  {idx > 0 && <span className="mr-1" style={{ color: '#A39992' }}>·</span>}
                                  <span style={{ color: ruleColor, fontWeight: ruleBold ? 600 : 400 }}>{rid}</span>
                                </span>
                              );
                            })}
                          </span>
                        ) : (
                          <span
                            className="font-johnson-text text-xs text-jj-gray-08 block"
                            style={wordWrap
                              ? { whiteSpace: 'pre-wrap', wordBreak: 'break-word' }
                              : { whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}
                          >
                            {String(cell)}
                          </span>
                        )}
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      </div>{/* end inner overflow-auto */}
    </div>
  );
}

function FailedRowsTab({ rules, api, isDeltaMode = false }) {
  const PAGE_SIZE = 100;
  const { user } = useAuth();
  const userEmail = user?.email || user?.username || 'unknown';
  const { hasPermission } = useRBAC();
  const canRunBDC = hasPermission('runBDCOperations');

  const [selectedRule, setSelectedRule]       = useState(rules[0]);
  const [preloadCount, setPreloadCount]         = useState(null);
  const [registry, setRegistry]               = useState(null);
  const [registryLoading, setRegistryLoading] = useState(false);
  const [registryError, setRegistryError]     = useState(null);
  const [isModalOpen, setIsModalOpen]         = useState(false);
  const [failedData, setFailedData]           = useState(null);
  const [currentPage, setCurrentPage]         = useState(1);
  const [evaluating, setEvaluating]           = useState(false);
  const [pageLoading, setPageLoading]         = useState(false);
  const [failedDataError, setFailedDataError] = useState(null);
  const [downloading, setDownloading]             = useState(false);
  const [downloadProgress, setDownloadProgress]   = useState(null);
  const [deltaDownloading, setDeltaDownloading]   = useState(false);
  const [deltaProgress, setDeltaProgress]         = useState(null);
  const [colorFilter, setColorFilter]             = useState('ALL');
  const [isDeltaModalOpen, setIsDeltaModalOpen]   = useState(false);
  const [deltaSheet, setDeltaSheet]               = useState('new_failures');
  const [deltaData, setDeltaData]                 = useState(null);
  const [deltaPage, setDeltaPage]                 = useState(1);
  const [deltaLoading, setDeltaLoading]           = useState(false);
  const [deltaPageLoading, setDeltaPageLoading]   = useState(false);
  const [deltaError, setDeltaError]               = useState(null);
  const [selectedStgTable, setSelectedStgTable]   = useState(null); // null = default (2nd most recent)
  const [defaultPreviousRunSuffix, setDefaultPreviousRunSuffix] = useState(null);
  const [defaultLatestRunSuffix, setDefaultLatestRunSuffix]     = useState(null);

  // Advanced delta picker state
  const [isAdvPickerOpen, setIsAdvPickerOpen]     = useState(false);
  const [stgTables, setStgTables]                 = useState([]);
  const [stgTablesLoading, setStgTablesLoading]   = useState(false);
  const [stgTablesError, setStgTablesError]       = useState(null);
  const [advPickedFromTable, setAdvPickedFromTable] = useState(null);
  const [advPickedToTable, setAdvPickedToTable]     = useState(null);
  const [selectedToStgTable, setSelectedToStgTable] = useState(null);

  // Scroll sync refs for Advanced Delta picker
  const advFromScrollRef = useRef(null);
  const advToScrollRef   = useRef(null);
  const advSyncing       = useRef(false);
  const makeAdvSyncHandler = (otherRef) => (e) => {
    if (advSyncing.current) return;
    advSyncing.current = true;
    if (otherRef.current) otherRef.current.scrollTop = e.target.scrollTop;
    advSyncing.current = false;
  };



  // Consolidated report state
  const CONSOLIDATED_PAGE_SIZE = 100;
  const [isConsolidatedOpen, setIsConsolidatedOpen]     = useState(false);
  const [consolidatedLoading, setConsolidatedLoading]   = useState(false);
  const [consolidatedProgress, setConsolidatedProgress] = useState(null);
  const [consolidatedError, setConsolidatedError]       = useState(null);
  const [consolidatedGenModalOpen, setConsolidatedGenModalOpen] = useState(false);
  const [consolidatedSummary, setConsolidatedSummary]   = useState(null);
  const [consolidatedPreview, setConsolidatedPreview]   = useState(null);
  const [consolidatedToken, setConsolidatedToken]       = useState(null);
  const [consolidatedDownloading, setConsolidatedDownloading] = useState(false);
  const [consolidatedDownloadProgress, setConsolidatedDownloadProgress] = useState(null);
  const [consolidatedPage, setConsolidatedPage]         = useState(1);
  const [consolidatedSearch, setConsolidatedSearch]     = useState('');

  // ── Col widths, word wrap, drag refs per modal ────────────────────────────
  const [failedColWidths, setFailedColWidths]               = useState({});
  const [failedWordWrap, setFailedWordWrap]                 = useState(false);
  const [deltaColWidths, setDeltaColWidths]                 = useState({});
  const [deltaWordWrap, setDeltaWordWrap]                   = useState(false);
  const [consolidatedColWidths, setConsolidatedColWidths]   = useState({});
  const [consolidatedWordWrap, setConsolidatedWordWrap]     = useState(false);
  // ── Consolidated — history + Snowflake load ───────────────────────────────
  const [consolidatedLatestLoading, setConsolidatedLatestLoading]   = useState(false);
  const [consolidatedHistoryOpen, setConsolidatedHistoryOpen]       = useState(false);
  const [consolidatedHistory, setConsolidatedHistory]               = useState([]);
  const [consolidatedHistoryLoading, setConsolidatedHistoryLoading] = useState(false);
  const [consolidatedHistoryError, setConsolidatedHistoryError]     = useState(null);
  const [consolidatedHistoryFetching, setConsolidatedHistoryFetching] = useState(null);
  const [consolidatedExcelPreparing, setConsolidatedExcelPreparing] = useState(false);
  const [consolidatedExcelProgress, setConsolidatedExcelProgress]   = useState(null);
  const failedDragRef       = useRef(null);
  const deltaDragRef        = useRef(null);
  const consolidatedDragRef = useRef(null);

  // ── Fullscreen refs + state per modal ─────────────────────────────────────
  const failedModalRef       = useRef(null);
  const deltaModalRef        = useRef(null);
  const consolidatedModalRef = useRef(null);
  const [failedFullscreen,       setFailedFullscreen]       = useState(false);
  const [deltaFullscreen,        setDeltaFullscreen]        = useState(false);
  const [consolidatedFullscreen, setConsolidatedFullscreen] = useState(false);

  // Resize handlers — shared factory (see makeResizeHandler near fitColumnWidths above)
  const onFailedResize       = makeResizeHandler(failedColWidths,       setFailedColWidths,       failedDragRef);
  const onDeltaResize        = makeResizeHandler(deltaColWidths,        setDeltaColWidths,        deltaDragRef);
  const onConsolidatedResize = makeResizeHandler(consolidatedColWidths, setConsolidatedColWidths, consolidatedDragRef);

  useEffect(() => {
    if (!selectedRule) return;
    setRegistry(null); setRegistryError(null); setFailedData(null); setIsModalOpen(false); setColorFilter('ALL'); setPreloadCount(null);
    setRegistryLoading(true);
    api.getFailedRowsCount(selectedRule.rule_id, isDeltaMode)
      .then(d => setPreloadCount(d?.total_count ?? null))
      .catch(() => {});
    api.getRuleRegistry(selectedRule.rule_id)
      .then(setRegistry)
      .catch(() => setRegistryError('Failed to load registry data. Please try again.'))
      .finally(() => setRegistryLoading(false));
  }, [selectedRule, isDeltaMode]);

  useEffect(() => {
    if (!isModalOpen) return;
    const onKey = e => { if (e.key === 'Escape') setIsModalOpen(false); };
    document.addEventListener('keydown', onKey);
    document.body.style.overflow = 'hidden';
    return () => { document.removeEventListener('keydown', onKey); document.body.style.overflow = 'unset'; };
  }, [isModalOpen]);

  const fetchPage = (page, isFirstLoad, filter = colorFilter) => {
    isFirstLoad ? setEvaluating(true) : setPageLoading(true);
    setFailedDataError(null);
    api.getFailedRows(selectedRule.rule_id, page, PAGE_SIZE, filter, isDeltaMode)
      .then(data => {
        setFailedData(data);
        setCurrentPage(data.page);
        setFailedColWidths(fitColumnWidths(data.columns || [], data.rows || []));
        if (isFirstLoad) {
          setIsModalOpen(true);
          logAuditEvent(api.baseURL, userEmail, 'bdc_failed_rows_viewed', selectedRule.rule_id,
            `Viewed failed rows for rule "${selectedRule.rule_id}" — ${data.total_rows ?? data.rows?.length ?? 0} rows`);
        }
      })
      .catch(() => setFailedDataError('Failed to load failed rows. Please try again.'))
      .finally(() => { setEvaluating(false); setPageLoading(false); });
  };

  useEffect(() => {
    if (!isModalOpen || !selectedRule) return;
    fetchPage(1, false, colorFilter);
  }, [colorFilter]);

  const handlePageChange = p => {
    if (!failedData || p < 1 || p > failedData.total_pages || p === currentPage || pageLoading) return;
    fetchPage(p, false);
  };

  // Delta view
  useEffect(() => {
    if (!isDeltaModalOpen) return;
    const onKey = e => { if (e.key === 'Escape') setIsDeltaModalOpen(false); };
    document.addEventListener('keydown', onKey);
    document.body.style.overflow = 'hidden';
    return () => { document.removeEventListener('keydown', onKey); document.body.style.overflow = 'unset'; };
  }, [isDeltaModalOpen]);

  const fetchDeltaPage = (sheet, page, isFirst = false, stgTable = selectedStgTable, toStgTable = selectedToStgTable) => {
    isFirst ? setDeltaLoading(true) : setDeltaPageLoading(true);
    setDeltaError(null);
    api.getDeltaRows(selectedRule.rule_id, sheet, page, PAGE_SIZE, stgTable, toStgTable)
      .then(data => {
        setDeltaData(data);
        setDeltaPage(data.page);
        setDeltaColWidths(fitColumnWidths(data.columns || [], data.rows || []));
        if (isFirst) setIsDeltaModalOpen(true);
      })
      .catch(err => {
        setDeltaError(err.message || 'Failed to load delta data.');
        if (isFirst) setIsDeltaModalOpen(true); // open modal so user sees the error
      })
      .finally(() => { setDeltaLoading(false); setDeltaPageLoading(false); });
  };

  const openDeltaModal = (sheet, stgTable = null, toStgTable = null) => {
    setDeltaSheet(sheet);
    setDeltaData(null);
    setDeltaError(null);
    setSelectedStgTable(stgTable);
    setSelectedToStgTable(toStgTable);
    fetchDeltaPage(sheet, 1, true, stgTable, toStgTable);
    // If using default comparison (no specific table), fetch STG tables to get the previous run date
    if (!stgTable) {
      setDefaultPreviousRunSuffix(null);
      api.getStgTables(selectedRule.rule_id, isDeltaMode)
        .then(d => {
          const prev   = (d.tables || []).find(t => t.is_previous);
          const latest = (d.tables || []).find(t => t.is_latest);
          if (prev?.run_suffix)   setDefaultPreviousRunSuffix(prev.run_suffix);
          if (latest?.run_suffix) setDefaultLatestRunSuffix(latest.run_suffix);
        })
        .catch(() => {});
    }
    logAuditEvent(api.baseURL, userEmail, 'bdc_delta_report_viewed', selectedRule.rule_id,
      `Viewed delta report for rule "${selectedRule.rule_id}" (sheet: ${sheet}${stgTable ? `, snapshot: ${stgTable}` : ''})`);
  };

  const handleDeltaSheetSwitch = (sheet) => {
    setDeltaSheet(sheet);
    setDeltaData(null);
    setDeltaError(null);
    fetchDeltaPage(sheet, 1, false, selectedStgTable, selectedToStgTable);
  };

  const handleDeltaPageChange = p => {
    if (!deltaData || p < 1 || p > deltaData.total_pages || p === deltaPage || deltaPageLoading) return;
    fetchDeltaPage(deltaSheet, p, false, selectedStgTable, selectedToStgTable);
  };

  const openAdvancedPicker = () => {
    setIsAdvPickerOpen(true);
    setAdvPickedFromTable(null);
    setAdvPickedToTable(null);
    setStgTables([]);
    // will pre-select latest as TO once tables load
    setStgTablesError(null);
    setStgTablesLoading(true);
    api.getStgTables(selectedRule.rule_id, isDeltaMode)
      .then(d => {
        const tables = d.tables || [];
        setStgTables(tables);
        // Pre-select the latest snapshot as the TO run
        const latest = tables.find(t => t.is_latest);
        if (latest) setAdvPickedToTable(latest.table_name);
      })
      .catch(err => setStgTablesError(err.message || 'Failed to load STG tables.'))
      .finally(() => setStgTablesLoading(false));
    logAuditEvent(api.baseURL, userEmail, 'bdc_advanced_delta_picker_opened', selectedRule.rule_id,
      `Opened advanced delta snapshot picker for rule "${selectedRule.rule_id}"`);
  };

  const downloadAllCSV = async () => {
    if (!failedData || downloading) return;
    setDownloading(true); setDownloadProgress({ pct: 0, label: 'Starting…' });
    try {
      const prepareUrl = `${api.baseURL}/api/rules/${selectedRule.rule_id}/prepare-download?delta_mode=${isDeltaMode}`;
      await new Promise((resolve, reject) => {
        const es = apiEventSource(prepareUrl);
        es.onmessage = async (event) => {
          try {
            const data = JSON.parse(event.data);
            if (data.type === 'error') { es.close(); reject(new Error(data.message)); return; }
            if (data.type === 'ready') {
              es.close(); setDownloadProgress({ pct: 100, label: 'Downloading file…' });
              const downloadUrl = `${api.baseURL}/api/rules/${selectedRule.rule_id}/download-ready?token=${data.token}`;
              const response = await fetch(downloadUrl);
              if (!response.ok) throw new Error('Download failed');
              const contentLength = response.headers.get('Content-Length');
              const total = contentLength ? parseInt(contentLength, 10) : null;
              const reader = response.body.getReader();
              const chunks = []; let received = 0;
              while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                chunks.push(value); received += value.length;
                if (total) setDownloadProgress({ pct: Math.min(99, Math.round((received / total) * 100)), label: 'Downloading…' });
              }
              setDownloadProgress({ pct: 100, label: 'Saving file…' });
              const blob = new Blob(chunks);
              const a = Object.assign(document.createElement('a'), { href: URL.createObjectURL(blob), download: data.filename || `${selectedRule.rule_id}_failed_rows.xlsx` });
              a.click(); URL.revokeObjectURL(a.href);
              logAuditEvent(api.baseURL, userEmail, 'bdc_failed_rows_downloaded', selectedRule.rule_id,
                `Downloaded failed rows for rule "${selectedRule.rule_id}"`);
              resolve();
              return;
            }
            setDownloadProgress({ pct: data.pct, label: data.label });
          } catch (e) { es.close(); reject(e); }
        };
        es.onerror = () => { es.close(); reject(new Error('Connection lost during file preparation')); };
      });
    } catch { setFailedDataError('Failed to download. Please try again.'); }
    finally { setDownloading(false); setDownloadProgress(null); }
  };

  const downloadDelta = async () => {
    if (!selectedRule || deltaDownloading) return;
    setDeltaDownloading(true); setDeltaProgress({ pct: 0, label: 'Starting…' });
    try {
      const stgParam   = selectedStgTable   ? `&stg_table=${encodeURIComponent(selectedStgTable)}`     : '';
      const toStgParam  = selectedToStgTable  ? `&to_stg_table=${encodeURIComponent(selectedToStgTable)}` : '';
      const allParams   = [stgParam, toStgParam].filter(Boolean).join('').replace(/^&/, '?');
      const prepareUrl = `${api.baseURL}/api/rules/${selectedRule.rule_id}/prepare-delta${allParams}`;
      await new Promise((resolve, reject) => {
        const es = apiEventSource(prepareUrl);
        es.onmessage = async (event) => {
          try {
            const data = JSON.parse(event.data);
            if (data.type === 'error') { es.close(); reject(new Error(data.message)); return; }
            if (data.type === 'ready') {
              es.close(); setDeltaProgress({ pct: 100, label: 'Downloading file…' });
              const downloadUrl = `${api.baseURL}/api/rules/${selectedRule.rule_id}/download-delta?token=${data.token}`;
              const response = await fetch(downloadUrl);
              if (!response.ok) throw new Error('Delta download failed');
              const contentLength = response.headers.get('Content-Length');
              const total = contentLength ? parseInt(contentLength, 10) : null;
              const reader = response.body.getReader();
              const chunks = []; let received = 0;
              while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                chunks.push(value); received += value.length;
                if (total) setDeltaProgress({ pct: Math.min(99, Math.round((received / total) * 100)), label: 'Downloading…' });
              }
              setDeltaProgress({ pct: 100, label: 'Saving file…' });
              const blob = new Blob(chunks);
              const a = Object.assign(document.createElement('a'), {
                href: URL.createObjectURL(blob),
                download: data.filename || `${selectedRule.rule_id}_delta_report.xlsx`
              });
              a.click(); URL.revokeObjectURL(a.href);
              logAuditEvent(api.baseURL, userEmail, 'bdc_delta_report_downloaded', selectedRule.rule_id,
                `Downloaded delta report for rule "${selectedRule.rule_id}"${selectedStgTable ? ` (vs snapshot: ${selectedStgTable})` : ''}`);
              resolve();
              return;
            }
            setDeltaProgress({ pct: data.pct, label: data.label });
          } catch (e) { es.close(); reject(e); }
        };
        es.onerror = () => { es.close(); reject(new Error('Connection lost during delta preparation')); };
      });
    } catch (err) { setFailedDataError(`Delta report failed: ${err.message}`); }
    finally { setDeltaDownloading(false); setDeltaProgress(null); }
  };

  // ── Consolidated Report ───────────────────────────────────────────────────────
  const generateConsolidatedReport = async () => {
    if (consolidatedLoading) return;
    setConsolidatedLoading(true);
    setConsolidatedError(null);
    setConsolidatedSummary(null);
    setConsolidatedPreview(null);
    setConsolidatedToken(null);
    setConsolidatedPage(1);
    setConsolidatedSearch('');
    setConsolidatedProgress({ pct: 0, label: 'Starting…' });
    setConsolidatedGenModalOpen(true);
    try {
      const prepareUrl = `${api.baseURL}/api/consolidated-report/prepare?delta_mode=${isDeltaMode}`;
      await new Promise((resolve, reject) => {
        const es = apiEventSource(prepareUrl);
        es.onmessage = (event) => {
          try {
            const data = JSON.parse(event.data);
            if (data.type === 'error') { es.close(); reject(new Error(data.message)); return; }
            if (data.type === 'ready') {
              es.close();
              setConsolidatedToken(null);
              setConsolidatedSummary({ ...data.summary, report_name: data.report_name });
              setConsolidatedPreview(data.preview);
              setConsolidatedColWidths(fitColumnWidths(data.preview.columns || [], data.preview.rows || []));
              setConsolidatedProgress({ pct: 100, label: 'Done' });
              setConsolidatedGenModalOpen(false);
              setIsConsolidatedOpen(true);
              logAuditEvent(api.baseURL, userEmail, 'bdc_consolidated_report_generated', data.report_name || '',
                `Generated consolidated failed rows report (rules: ${rules.length})`);
              resolve();
              return;
            }
            setConsolidatedProgress({ pct: data.pct ?? 0, label: data.label ?? 'Working…' });
          } catch (e) { es.close(); reject(e); }
        };
        es.onerror = () => { es.close(); reject(new Error('Connection lost during report preparation.')); };
      });
    } catch (err) {
      setConsolidatedError(err.message || 'Failed to generate consolidated report. Please try again.');
    } finally {
      setConsolidatedLoading(false);
      setConsolidatedProgress(null);
      // modal stays open on error so user can read it; closes itself on success via the ready handler
    }
  };

  const downloadConsolidatedExcel = async () => {
    if (!consolidatedToken || consolidatedDownloading) return;
    setConsolidatedDownloading(true);
    setConsolidatedDownloadProgress({ pct: 0, label: 'Starting…' });
    try {
      const url = `${api.baseURL}/api/consolidated-report/download?token=${consolidatedToken}`;
      const response = await fetch(url);
      if (response.status === 404) throw new Error('Report expired — please regenerate.');
      if (!response.ok) throw new Error('Download failed — please try again.');
      const contentLength = response.headers.get('Content-Length');
      const total = contentLength ? parseInt(contentLength, 10) : null;
      const reader = response.body.getReader();
      const chunks = []; let received = 0;
      setConsolidatedDownloadProgress({ pct: 0, label: 'Downloading…' });
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        chunks.push(value); received += value.length;
        if (total) setConsolidatedDownloadProgress({ pct: Math.min(99, Math.round((received / total) * 100)), label: 'Downloading…' });
      }
      setConsolidatedDownloadProgress({ pct: 100, label: 'Saving file…' });
      const blob = new Blob(chunks);
      const a = Object.assign(document.createElement('a'), {
        href: URL.createObjectURL(blob),
        download: `consolidated_failed_rows.xlsx`,
      });
      document.body.appendChild(a); a.click();
      document.body.removeChild(a); URL.revokeObjectURL(a.href);
      logAuditEvent(api.baseURL, userEmail, 'bdc_consolidated_report_downloaded', consolidatedSummary?.report_name || '',
        `Downloaded consolidated failed rows Excel report`);
    } catch (err) { setConsolidatedError(err.message); }
    finally { setConsolidatedDownloading(false); setConsolidatedDownloadProgress(null); }
  };

  // ── Load latest report from Snowflake ────────────────────────────────────
  const loadLatestConsolidatedReport = async () => {
    if (consolidatedLatestLoading) return;
    setConsolidatedLatestLoading(true);
    setConsolidatedError(null);
    try {
      const data = await api.getConsolidatedLatest(isDeltaMode);
      if (!data.found) {
        setConsolidatedError('No consolidated report found. Generate one first.');
        return;
      }
      setConsolidatedToken(null);
      setConsolidatedSummary({ ...data.summary, report_name: data.report_name });
      setConsolidatedPreview(data.preview);
      setConsolidatedColWidths(fitColumnWidths(data.preview.columns || [], data.preview.rows || []));
      setConsolidatedPage(1);
      setConsolidatedSearch('');
      setIsConsolidatedOpen(true);
      logAuditEvent(api.baseURL, userEmail, 'bdc_consolidated_latest_viewed', data.report_name || '',
        `Viewed latest consolidated report from Snowflake: ${data.report_name}`);
    } catch (err) {
      setConsolidatedError(err.message || 'Failed to load the latest consolidated report.');
    } finally {
      setConsolidatedLatestLoading(false);
    }
  };

  // ── Open history modal and fetch list ────────────────────────────────────
  const openConsolidatedHistory = async () => {
    setConsolidatedHistoryOpen(true);
    setConsolidatedHistory([]);
    setConsolidatedHistoryLoading(true);
    setConsolidatedHistoryError(null);
    try {
      const data = await api.getConsolidatedHistory(isDeltaMode);
      setConsolidatedHistory(data.reports || []);
    } catch (err) {
      setConsolidatedHistoryError(err.message || 'Failed to load report history.');
    } finally {
      setConsolidatedHistoryLoading(false);
    }
  };

  // ── Load a specific historic report ──────────────────────────────────────
  const loadHistoricReport = async (reportName) => {
    if (consolidatedHistoryFetching) return;
    setConsolidatedHistoryFetching(reportName);
    setConsolidatedError(null);
    try {
      const data = await api.fetchConsolidatedReport(reportName, isDeltaMode);
      setConsolidatedToken(null);
      setConsolidatedSummary({ ...data.summary, report_name: data.report_name });
      setConsolidatedPreview(data.preview);
      setConsolidatedColWidths(fitColumnWidths(data.preview.columns || [], data.preview.rows || []));
      setConsolidatedPage(1);
      setConsolidatedSearch('');
      setConsolidatedHistoryOpen(false);
      setIsConsolidatedOpen(true);
      logAuditEvent(api.baseURL, userEmail, 'bdc_consolidated_history_viewed', reportName,
        `Viewed historic consolidated report: ${reportName}`);
    } catch (err) {
      setConsolidatedError(err.message || 'Failed to load this report.');
    } finally {
      setConsolidatedHistoryFetching(null);
    }
  };

  // ── Prepare Excel and immediately download ───────────────────────────────
  const prepareExcelDownload = async (reportName) => {
    if (consolidatedExcelPreparing || consolidatedDownloading) return;
    setConsolidatedExcelPreparing(true);
    setConsolidatedError(null);
    setConsolidatedExcelProgress({ pct: 0, label: 'Starting…' });
    let token = null;
    try {
      const url = api.prepareConsolidatedExcelUrl(reportName, isDeltaMode);
      await new Promise((resolve, reject) => {
        const es = apiEventSource(url);
        es.onmessage = (event) => {
          try {
            const data = JSON.parse(event.data);
            if (data.type === 'error') { es.close(); reject(new Error(data.message)); return; }
            if (data.type === 'ready') {
              es.close();
              token = data.token;
              setConsolidatedToken(data.token);
              setConsolidatedExcelProgress({ pct: 100, label: 'Downloading…' });
              resolve();
              return;
            }
            setConsolidatedExcelProgress({ pct: data.pct ?? 0, label: data.label ?? 'Working…' });
          } catch (e) { es.close(); reject(e); }
        };
        es.onerror = () => { es.close(); reject(new Error('Connection lost during Excel preparation.')); };
      });
      // ── Auto-download immediately with the token we just received ──────────
      if (token) {
        setConsolidatedExcelPreparing(false);
        setConsolidatedExcelProgress(null);
        setConsolidatedDownloading(true);
        setConsolidatedDownloadProgress({ pct: 0, label: 'Downloading…' });
        const dlUrl = `${api.baseURL}/api/consolidated-report/download?token=${token}`;
        const response = await fetch(dlUrl);
        if (response.status === 404) throw new Error('Report expired — please regenerate.');
        if (!response.ok) throw new Error('Download failed — please try again.');
        const contentLength = response.headers.get('Content-Length');
        const total = contentLength ? parseInt(contentLength, 10) : null;
        const reader = response.body.getReader();
        const chunks = []; let received = 0;
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          chunks.push(value); received += value.length;
          if (total) setConsolidatedDownloadProgress({ pct: Math.min(99, Math.round((received / total) * 100)), label: 'Downloading…' });
        }
        setConsolidatedDownloadProgress({ pct: 100, label: 'Saving file…' });
        const blob = new Blob(chunks);
        const a = Object.assign(document.createElement('a'), {
          href: URL.createObjectURL(blob),
          download: `consolidated_failed_rows.xlsx`,
        });
        document.body.appendChild(a); a.click();
        document.body.removeChild(a); URL.revokeObjectURL(a.href);
        logAuditEvent(api.baseURL, userEmail, 'bdc_consolidated_report_downloaded', consolidatedSummary?.report_name || '',
          `Downloaded consolidated failed rows Excel report`);
      }
    } catch (err) {
      setConsolidatedError(err.message || 'Failed to download Excel file.');
    } finally {
      setConsolidatedExcelPreparing(false);
      setConsolidatedExcelProgress(null);
      setConsolidatedDownloading(false);
      setConsolidatedDownloadProgress(null);
    }
  };

  // Derived values for consolidated modal — client-side search on the preview rows.
  // Color coding uses the color_columns resolved by the backend from COLOR_COLUMNS.
  const consolidatedRows     = consolidatedPreview?.rows ?? [];
  const consolidatedFiltered = consolidatedSearch.trim()
    ? consolidatedRows.filter(row =>
        row.some(cell => cell !== null && String(cell).toLowerCase()
          .includes(consolidatedSearch.trim().toLowerCase()))
      )
    : consolidatedRows;
  const consolidatedTotalPages = Math.max(1, Math.ceil(consolidatedFiltered.length / CONSOLIDATED_PAGE_SIZE));
  const consolidatedPageRows   = consolidatedFiltered.slice(
    (consolidatedPage - 1) * CONSOLIDATED_PAGE_SIZE,
    consolidatedPage * CONSOLIDATED_PAGE_SIZE,
  );

  const pageNums = () => {
    if (!failedData) return [];
    const total = failedData.total_pages;
    const start = Math.max(1, Math.min(currentPage - 2, total - 4));
    const end   = Math.min(total, start + 4);
    return Array.from({ length: end - start + 1 }, (_, i) => start + i);
  };

  const btnBase = 'px-3 py-1.5 rounded font-johnson-text text-xs font-bold transition-colors disabled:opacity-40 disabled:cursor-not-allowed';
  const btnGray = `${btnBase} bg-jj-gray-01 text-jj-gray-08 hover:bg-jj-gray-02 border border-jj-gray-03`;
  const btnRed  = `${btnBase} bg-jj-red text-white border border-jj-red`;

  const COLOR_KEYS = [
    { key: 'green',  label: '≤ 7 days',  cls: 'bg-green-50 text-jj-green-03 border-jj-green-03',   activeCls: 'bg-jj-green-03 text-white border-jj-green-03' },
    { key: 'yellow', label: '8–15 days', cls: 'bg-yellow-50 text-yellow-700 border-jj-yellow-01',  activeCls: 'bg-jj-yellow-01 text-jj-gray-08 border-jj-yellow-01' },
    { key: 'red',    label: '≥ 16 days', cls: 'bg-red-50 text-jj-maroon-05 border-jj-maroon-05',   activeCls: 'bg-jj-red text-white border-jj-red' },
  ];

  const [colorCol1, colorCol2] = parseColorColumns(selectedRule?.color_columns);
  const hasDateCols = !!(failedData && colorCol1 && colorCol2
    && failedData.columns.some(c => c.trim().toUpperCase() === colorCol1.toUpperCase())
    && failedData.columns.some(c => c.trim().toUpperCase() === colorCol2.toUpperCase()));

  return (
    <div className="space-y-4">

      <div data-tour="bdc-rule-select">
        <RuleSelect
          rules={rules.map(r => ({ ...r, rule_title: `${r.rule_title} — ${r.target_table}` }))}
          value={selectedRule?.rule_id}
          onChange={r => { setSelectedRule(rules.find(x => x.rule_id === r.rule_id)); setDefaultPreviousRunSuffix(null); setDefaultLatestRunSuffix(null); setSelectedToStgTable(null); }}
          label="Failed Row Inspection — Rule"
          hint="Select a rule to view its SQL predicate, evaluate failed rows, and download delta reports"
        />
      </div>

      {registryLoading ? (
        <div className="flex items-center justify-center min-h-[400px]"><LoadingSpinner /></div>
      ) : registryError ? (
        <EmptyState icon={closeSrc} title="Error Loading Data" message={registryError} />
      ) : !registry ? (
        <EmptyState icon={alertSrc} title="No Rule Registry Data" message="Select a rule to view its SQL predicate and show failed rows." />
      ) : (
        <div data-tour="bdc-sql-section">
        <SectionCard title="SQL Predicate & Evaluation" icon={recordsSrc}>
          <div className="p-5 space-y-4">
            {failedDataError && (
              <div className="flex items-center gap-2 px-4 py-3 bg-red-50 border border-jj-maroon-05 border-opacity-30 rounded-lg text-jj-maroon-05">
                <Ic src={closeSrc} className="w-4 h-4 flex-shrink-0" filter={F.red} />
                <span className="font-johnson-text text-sm">{failedDataError}</span>
              </div>
            )}

            <details className="group" open>
              <summary className="cursor-pointer font-johnson-display text-base text-jj-gray-08 flex items-center gap-2 hover:text-jj-red transition-colors select-none" style={{ lineHeight: '110%' }}>
                <Ic src={chevronRightSrc} className="w-4 h-4 group-open:rotate-90 transition-transform flex-shrink-0" filter={F.gray07} />
                SQL Predicate
              </summary>
              <div className="mt-3 bg-jj-gray-01 border border-jj-gray-02 rounded-lg p-4">
                <pre className="font-mono text-sm text-jj-gray-08 overflow-x-auto whitespace-pre-wrap leading-relaxed">
                  {registry.sql_predicate}
                </pre>
              </div>
            </details>

            <div className="flex items-center gap-3 flex-wrap">
              <button
                data-tour="bdc-show-failed"
                onClick={() => fetchPage(1, true)}
                disabled={evaluating || deltaLoading}
                className="inline-flex items-center gap-2 px-5 py-2.5 rounded-lg font-johnson-text text-sm font-bold text-white bg-jj-red hover:bg-jj-maroon-05 transition-colors disabled:opacity-50 disabled:cursor-not-allowed shadow-sm"
              >
                {evaluating
                  ? <><Ic src={activitySrc} className="w-4 h-4 animate-spin" filter={F.white} />Loading…</>
                  : (() => { const count = failedData?.total_count ?? preloadCount; return <><Ic src={alertSrc} className="w-4 h-4" filter={F.white} />Show Failed Rows{count != null && <span className="ml-1 bg-white/20 rounded px-1.5 py-0.5 text-xs font-bold">{count.toLocaleString()}</span>}</>; })()
                }
              </button>

              <button
                data-tour="bdc-delta"
                onClick={() => openDeltaModal('new_failures')}
                disabled={deltaLoading || evaluating}
                className="inline-flex items-center gap-2 px-5 py-2.5 rounded-lg font-johnson-text text-sm font-bold text-white bg-jj-gray-08 hover:bg-jj-gray-07 transition-colors disabled:opacity-50 disabled:cursor-not-allowed shadow-sm"
                title="Compare current run vs previous run"
              >
                {deltaLoading
                  ? <><Ic src={activitySrc} className="w-4 h-4 animate-spin" filter={F.white} />Loading…</>
                  : <><Ic src={deltaSrc} className="w-4 h-4" filter={F.white} />Delta Report</>
                }
              </button>

              <button
                data-tour="bdc-adv-delta"
                onClick={openAdvancedPicker}
                disabled={deltaLoading || evaluating}
                className="inline-flex items-center gap-2 px-4 py-2.5 rounded-lg font-johnson-text text-sm font-bold text-jj-gray-07 bg-white hover:bg-jj-gray-01 border border-jj-gray-03 transition-colors disabled:opacity-50 disabled:cursor-not-allowed shadow-sm"
                title="Choose any historical run to compare against"
              >
                <Ic src={gearSrc} className="w-4 h-4" filter={F.gray07} />
                Advanced Delta Report
              </button>
            </div>
          </div>
        </SectionCard>
        </div>
      )}

      {/* ── Consolidated Report Card ── */}
      <div className="bg-white rounded-xl border border-jj-gray-03 shadow-sm overflow-hidden">
        <div className="px-5 py-3 border-b border-jj-gray-02 bg-jj-gray-01 flex items-center gap-2">
          <Ic src={chartSrc} className="w-4 h-4" filter={F.violet} style={{ color: '#8c3bbb' }} />
          <h3 className="font-johnson-display text-base text-jj-gray-08" style={{ lineHeight: '110%' }}>Consolidated Report</h3>
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full font-johnson-text text-[10px] font-bold border border-opacity-30"
            style={{ color: isDeltaMode ? '#541980' : '#8c3bbb', backgroundColor: isDeltaMode ? '#ede5f5' : '#f5f0fb', borderColor: isDeltaMode ? '#541980' : '#8c3bbb' }}>
            {isDeltaMode ? 'Semi-Monthly' : 'Quarterly'}
          </span>
          <span className="ml-auto inline-flex items-center gap-1 px-2 py-1 rounded-full font-johnson-text text-[10px] font-bold border border-opacity-30"
            style={{ color: '#8c3bbb', backgroundColor: '#f5f0fb', borderColor: '#8c3bbb' }}>
            {rules.length} rule{rules.length !== 1 ? 's' : ''}
          </span>
        </div>
        <div className="px-5 py-4 flex items-start justify-between gap-6 flex-wrap">
          <div className="flex-1 min-w-0">
            <p className="font-johnson-text text-sm text-jj-gray-07">
              Fetches failed rows across <span className="font-bold text-jj-gray-08">all active rules</span>, deduplicates them, and appends a{' '}
              <code className="font-mono text-xs px-1.5 py-0.5 rounded border border-opacity-30"
                style={{ color: '#8c3bbb', backgroundColor: '#f5f0fb', borderColor: '#8c3bbb' }}>FAILURE_MODE</code>{' '}
              column. Color coding uses each rule's <code className="font-mono text-xs" style={{ color: '#8c3bbb' }}>COLOR_COLUMNS</code> from DQ_RULE_INPUT.
            </p>
            <div className="mt-2 flex items-center gap-1.5">
              <Ic src={alertSrc} className="w-3.5 h-3.5 flex-shrink-0" filter={F.orange} style={{ color: '#312c2a' }} />
              <p className="font-johnson-text text-xs" style={{ color: '#312c2a' }}>
                Note: This report may take a few minutes to generate depending on the volume of data.
              </p>
            </div>
            {consolidatedError && (
              <div className="mt-3 flex items-center gap-2 px-3 py-2.5 bg-red-50 border border-jj-maroon-05 border-opacity-30 rounded-lg">
                <Ic src={closeSrc} className="w-4 h-4 flex-shrink-0" filter={F.maroon} />
                <span className="font-johnson-text text-xs text-jj-maroon-05">{consolidatedError}</span>
              </div>
            )}
          </div>
          <div className="flex-shrink-0 flex flex-col items-end gap-2">
            {consolidatedLoading ? (
              <div className="flex flex-col items-end gap-2">
                {/* Primary actions — disabled while generating */}
                <div className="flex items-center gap-2 flex-wrap justify-end">
                  <button disabled className="inline-flex items-center gap-2 px-4 py-2.5 rounded-lg font-johnson-text text-xs font-bold transition-opacity opacity-40 cursor-not-allowed border"
                    style={{ color: '#8c3bbb', borderColor: '#8c3bbb', backgroundColor: '#f5f0fb' }}>
                    <Ic src={chartSrc} className="w-3.5 h-3.5" filter={F.violet} />
                    Show Latest Report
                  </button>
                  <button disabled className="inline-flex items-center gap-2 px-4 py-2.5 rounded-lg font-johnson-text text-xs font-bold text-white opacity-40 cursor-not-allowed shadow-sm"
                    style={{ backgroundColor: '#8c3bbb' }}>
                    <Ic src={activitySrc} className="w-3.5 h-3.5 animate-spin" filter={F.white} />
                    Generating…
                  </button>
                </div>
                <button disabled className="inline-flex items-center gap-1.5 font-johnson-text text-xs opacity-40 cursor-not-allowed"
                  style={{ color: '#8c3bbb' }}>
                  <Ic src={recordsSrc} className="w-3 h-3" filter={F.violet} />
                  View Historic Reports
                </button>
              </div>
            ) : (
              <div className="flex flex-col items-end gap-2">
                {/* Primary actions */}
                <div className="flex items-center gap-2 flex-wrap justify-end">
                  {/* Show Latest */}
                  {consolidatedLatestLoading ? (
                    <div className="inline-flex items-center gap-2 px-4 py-2.5 rounded-lg font-johnson-text text-xs font-bold border border-opacity-30"
                      style={{ backgroundColor: '#f5f0fb', borderColor: '#8c3bbb', color: '#8c3bbb' }}>
                      <Ic src={activitySrc} className="w-3.5 h-3.5 animate-spin" filter={F.gray07} />
                      Loading…
                    </div>
                  ) : (
                    <button
                      onClick={loadLatestConsolidatedReport}
                      disabled={rules.length === 0}
                      className="inline-flex items-center gap-2 px-4 py-2.5 rounded-lg font-johnson-text text-xs font-bold transition-opacity disabled:opacity-40 disabled:cursor-not-allowed border hover:opacity-80"
                      style={{ color: '#8c3bbb', borderColor: '#8c3bbb', backgroundColor: '#f5f0fb' }}
                    >
                      <Ic src={chartSrc} className="w-3.5 h-3.5" filter={F.violet} />
                      Show Latest Report
                    </button>
                  )}
                  {/* Generate New */}
                  {canRunBDC && (
                  <button
                    onClick={generateConsolidatedReport}
                    disabled={rules.length === 0}
                    className="inline-flex items-center gap-2 px-4 py-2.5 rounded-lg font-johnson-text text-xs font-bold text-white transition-opacity disabled:opacity-40 disabled:cursor-not-allowed shadow-sm hover:opacity-90"
                    style={{ backgroundColor: '#8c3bbb' }}
                  >
                    <Ic src={refreshSrc} className="w-3.5 h-3.5" filter={F.white} />
                    Generate New Report
                  </button>
                  )}
                </div>
                {/* View History */}
                <button
                  onClick={openConsolidatedHistory}
                  className="inline-flex items-center gap-1.5 font-johnson-text text-xs hover:underline transition-colors"
                  style={{ color: '#8c3bbb' }}
                >
                  <Ic src={recordsSrc} className="w-3 h-3" filter={F.violet} />
                  View Historic Reports
                </button>
              </div>
            )}
            {consolidatedSummary && !consolidatedLoading && (
              <button onClick={() => { setConsolidatedPage(1); setIsConsolidatedOpen(true); }}
                className="font-johnson-text text-xs hover:underline transition-colors"
                style={{ color: '#8c3bbb' }}>
                View loaded report ({consolidatedSummary.total_rows.toLocaleString()} unique rows)
              </button>
            )}
          </div>
        </div>
      </div>

      {/* ── Failed Rows Modal ── */}
      {isModalOpen && failedData && ReactDOM.createPortal(
        <div
          className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4"
          style={{ backdropFilter: 'blur(3px)' }}
          onClick={() => setIsModalOpen(false)}
        >
          <div
            ref={el => {
              failedModalRef.current = el;
              if (el && !el._fsListenerAdded) {
                el._fsListenerAdded = true;
                document.addEventListener('fullscreenchange', () => setFailedFullscreen(!!document.fullscreenElement));
              }
            }}
            className="bg-white w-full max-w-7xl flex flex-col overflow-hidden"
            style={failedFullscreen
              ? { width: '100vw', height: '100vh', borderRadius: 0 }
              : { height: 'min(88vh, calc(100vh - 2rem))', borderRadius: '1rem', boxShadow: '0 25px 60px rgba(0,0,0,0.4)', animation: 'slideUp 0.25s ease-out' }
            }
            onClick={e => e.stopPropagation()}
          >

            {/* Modal Header */}
            <div className="flex-shrink-0 px-5 py-3 bg-white border-b-4 border-jj-red rounded-t-2xl flex items-center justify-between gap-4 flex-wrap">
              <div className="flex items-center gap-3 flex-wrap">
                <span className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg font-johnson-text text-xs font-bold text-jj-gray-07 bg-jj-gray-01 hover:bg-jj-gray-02 transition-colors border border-jj-gray-03 disabled:opacity-50">
                  <Ic src={alertSrc} className="w-4 h-4" filter={F.orange} />
                  {failedData.total_count.toLocaleString()} Failed Rows
                </span>
                <span className="font-johnson-text text-xs text-jj-gray-06">
                  Page {currentPage} of {failedData.total_pages} · {PAGE_SIZE} rows / page
                </span>
              </div>

              {hasDateCols && (
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="font-johnson-text text-xs font-bold text-jj-gray-06 uppercase tracking-wide">Filter:</span>
                  {COLOR_KEYS.map(({ key, label, cls, activeCls }) => {
                    const active = colorFilter === key;
                    return (
                      <button
                        key={key}
                        onClick={() => setColorFilter(active ? 'ALL' : key)}
                        className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-johnson-text font-bold border transition-all ${active ? activeCls : cls}`}
                      >
                        <span className={`w-2 h-2 rounded-full ${active ? 'bg-white bg-opacity-60' : ''} ${!active && key === 'green' ? 'bg-jj-green-03' : !active && key === 'yellow' ? 'bg-jj-yellow-01' : !active && key === 'red' ? 'bg-jj-red' : ''}`} />
                        {label}
                        {active && <Ic src={closeSrc} className="w-2.5 h-2.5" filter={F.white} />}
                      </button>
                    );
                  })}
                  {colorFilter !== 'ALL' && (
                    <button onClick={() => setColorFilter('ALL')} className="font-johnson-text text-xs text-jj-red hover:text-jj-maroon-05 underline transition-colors">
                      Clear
                    </button>
                  )}
                </div>
              )}

              <div className="flex items-center gap-2 ml-auto">
                {/* ── Download Excel ── */}
                {!downloading ? (
                  <button
                    onClick={downloadAllCSV}
                    disabled={deltaDownloading}
                    className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg font-johnson-text text-xs font-bold text-jj-gray-07 bg-white border border-jj-gray-03 hover:bg-jj-gray-01 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    <Ic src={downloadSrc} className="w-3.5 h-3.5" filter={F.gray07} />
                    Download Excel
                  </button>
                ) : (
                  <div className="flex items-center gap-2 bg-jj-gray-01 border border-jj-gray-02 rounded-lg px-3 py-2 min-w-[200px]">
                    <Ic src={activitySrc} className="w-4 h-4 animate-spin flex-shrink-0" filter={F.gray07} />
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center justify-between mb-1">
                        <span className="font-johnson-text text-xs text-jj-gray-08 truncate">{downloadProgress?.label ?? 'Starting…'}</span>
                        <span className="font-johnson-text text-xs font-bold text-jj-gray-07 ml-2">{downloadProgress?.pct ?? 0}%</span>
                      </div>
                      <div className="w-full h-1.5 bg-jj-gray-02 rounded-full overflow-hidden">
                        <div className="h-full bg-jj-gray-07 rounded-full transition-all duration-500" style={{ width: `${downloadProgress?.pct ?? 0}%` }} />
                      </div>
                    </div>
                  </div>
                )}

                {/* ── Delta Report download ── */}
                {!deltaDownloading ? (
                  <button
                    onClick={downloadDelta}
                    disabled={downloading}
                    className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg font-johnson-text text-xs font-bold text-white bg-jj-red hover:bg-jj-maroon-05 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                    title="Download delta report — Current vs previous run"
                  >
                    <Ic src={chartSrc} className="w-3.5 h-3.5" filter={F.white} />
                    Delta Report
                  </button>
                ) : (
                  <div className="flex items-center gap-2 bg-red-50 border border-jj-red border-opacity-20 rounded-lg px-3 py-2 min-w-[200px]">
                    <Ic src={activitySrc} className="w-4 h-4 animate-spin flex-shrink-0" filter={F.white} />
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center justify-between mb-1">
                        <span className="font-johnson-text text-xs text-jj-gray-08 truncate">{deltaProgress?.label ?? 'Starting…'}</span>
                        <span className="font-johnson-text text-xs font-bold text-jj-red ml-2">{deltaProgress?.pct ?? 0}%</span>
                      </div>
                      <div className="w-full h-1.5 bg-red-100 rounded-full overflow-hidden">
                        <div className="h-full bg-jj-red rounded-full transition-all duration-500" style={{ width: `${deltaProgress?.pct ?? 0}%` }} />
                      </div>
                    </div>
                  </div>
                )}
                {/* Word wrap toggle */}
                <button
                  onClick={() => setFailedWordWrap(w => !w)}
                  title={failedWordWrap ? 'Collapse to single line' : 'Wrap cell text'}
                  className={`w-9 h-9 flex items-center justify-center rounded-lg transition-colors ${
                    failedWordWrap ? 'bg-jj-red text-white' : 'bg-jj-gray-01 hover:bg-jj-gray-02 text-jj-gray-07'
                  }`}
                >
                  <Ic src={recordsSrc} className="w-4 h-4" filter={failedWordWrap ? F.white : F.gray07} />
                </button>
                {/* Fullscreen toggle */}
                <button
                  onClick={() => {
                    if (!document.fullscreenElement) {
                      failedModalRef.current?.requestFullscreen?.();
                    } else {
                      document.exitFullscreen?.();
                    }
                  }}
                  title={failedFullscreen ? 'Exit fullscreen (F11)' : 'Enter fullscreen'}
                  className="w-9 h-9 flex items-center justify-center rounded-lg bg-jj-gray-01 hover:bg-jj-gray-02 transition-colors"
                >
                  {failedFullscreen ? (
                    <svg xmlns="http://www.w3.org/2000/svg" className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ color: '#6b6b6b' }}>
                      <path d="M8 3v3a2 2 0 0 1-2 2H3m18 0h-3a2 2 0 0 1-2-2V3m0 18v-3a2 2 0 0 1 2-2h3M3 16h3a2 2 0 0 1 2 2v3" />
                    </svg>
                  ) : (
                    <svg xmlns="http://www.w3.org/2000/svg" className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ color: '#6b6b6b' }}>
                      <path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3" />
                    </svg>
                  )}
                </button>
                <button onClick={() => setIsModalOpen(false)} className="w-9 h-9 flex items-center justify-center rounded-lg bg-jj-gray-01 hover:bg-jj-gray-02 transition-colors">
                  <Ic src={closeSrc} className="w-4 h-4" filter={F.gray07} />
                </button>
              </div>
            </div>

            {/* Scrollable Table */}
            <ModalDataTable
              columns={failedData.columns}
              rows={failedData.rows}
              colWidths={failedColWidths}
              onResize={onFailedResize}
              wordWrap={failedWordWrap}
              rowOffset={(currentPage - 1) * PAGE_SIZE}
              colorCol1={colorCol1}
              colorCol2={colorCol2}
              loading={pageLoading}
              emptyMessage="No rows match the selected filter."
            />

            {/* Pagination Footer */}
            <div className="flex-shrink-0 px-5 py-3 bg-white border-t border-jj-gray-02 rounded-b-2xl flex items-center justify-between">
              <span className="font-johnson-text text-xs text-jj-gray-06">
                {colorFilter === 'ALL' ? (
                  <>Rows <b className="text-jj-gray-08">{((currentPage - 1) * PAGE_SIZE + 1).toLocaleString()}</b>–<b className="text-jj-gray-08">{Math.min(currentPage * PAGE_SIZE, failedData.total_count).toLocaleString()}</b> of <b className="text-jj-gray-08">{failedData.total_count.toLocaleString()}</b></>
                ) : (
                  <>
                    <span className={`inline-block w-2 h-2 rounded-full mr-1.5 ${colorFilter === 'green' ? 'bg-jj-green-03' : colorFilter === 'yellow' ? 'bg-jj-yellow-01' : 'bg-jj-red'}`} />
                    Rows <b className="text-jj-gray-08">{((currentPage - 1) * PAGE_SIZE + 1).toLocaleString()}</b>–<b className="text-jj-gray-08">{Math.min(currentPage * PAGE_SIZE, failedData.total_count).toLocaleString()}</b> of <b className="text-jj-gray-08">{failedData.total_count.toLocaleString()}</b> <span className="capitalize">{colorFilter}</span> rows
                  </>
                )}
              </span>
              <div className="flex items-center gap-1">
                <button className={btnGray} disabled={currentPage === 1 || pageLoading} onClick={() => handlePageChange(1)}>First</button>
                <button className={btnGray} disabled={currentPage === 1 || pageLoading} onClick={() => handlePageChange(currentPage - 1)}>
                  <Ic src={chevronLeftSrc} className="w-3.5 h-3.5" filter={F.gray07} />
                </button>
                {pageNums().map(p => (
                  <button key={p} className={p === currentPage ? btnRed : btnGray} disabled={pageLoading} onClick={() => handlePageChange(p)}>{p}</button>
                ))}
                <button className={btnGray} disabled={currentPage === failedData.total_pages || pageLoading} onClick={() => handlePageChange(currentPage + 1)}>
                  <Ic src={chevronRightSrc} className="w-3.5 h-3.5" filter={F.gray07} />
                </button>
                <button className={btnGray} disabled={currentPage === failedData.total_pages || pageLoading} onClick={() => handlePageChange(failedData.total_pages)}>Last</button>
              </div>
            </div>
          </div>
        </div>
      , document.body)}

      {/* ── Advanced Delta Picker Modal ── */}
      {isAdvPickerOpen && ReactDOM.createPortal(
        <div
          className="fixed inset-0 flex items-center justify-center z-50 p-4"
          style={{ backgroundColor: 'rgba(49,44,42,0.65)', backdropFilter: 'blur(4px)' }}
          onClick={() => setIsAdvPickerOpen(false)}
        >
          <style>{`
            @keyframes advPickerIn {
              from { opacity: 0; transform: translateY(18px) scale(0.98); }
              to   { opacity: 1; transform: translateY(0)   scale(1);    }
            }
            .adv-picker-enter { animation: advPickerIn 0.22s cubic-bezier(0.16,1,0.3,1) forwards; }
            @keyframes staggerIn {
              from { opacity: 0; transform: translateX(-8px); }
              to   { opacity: 1; transform: translateX(0); }
            }
          `}</style>

          <div
            className="bg-white w-full flex flex-col overflow-hidden adv-picker-enter"
            style={{
              maxWidth: '700px',
              maxHeight: 'min(85vh, calc(100vh - 2rem))',
              borderRadius: '16px',
              boxShadow: '0 32px 64px rgba(49,44,42,0.28), 0 0 0 1px rgba(49,44,42,0.08)',
            }}
            onClick={e => e.stopPropagation()}
          >
            {/* Dark header banner */}
            <div className="flex-shrink-0 bg-jj-gray-08 px-6 pt-5 pb-4">
              <div className="flex items-start justify-between gap-4">
                <div>
                  <div className="w-8 h-1 bg-jj-red rounded-full mb-3" />
                  <h2 className="font-johnson-display text-white mb-1" style={{ fontSize: '1.35rem', lineHeight: '115%', fontWeight: 400 }}>
                    Advanced Delta Report
                  </h2>
                  <p className="font-johnson-text text-jj-gray-05 text-xs leading-relaxed">
                    Choose both the FROM (older) and TO (newer) runs to compare between.
                  </p>
                </div>
                <button
                  onClick={() => setIsAdvPickerOpen(false)}
                  className="flex-shrink-0 w-8 h-8 flex items-center justify-center rounded-lg transition-colors mt-0.5"
                  style={{ background: 'rgba(255,255,255,0.08)' }}
                  onMouseEnter={e => e.currentTarget.style.background = 'rgba(255,255,255,0.15)'}
                  onMouseLeave={e => e.currentTarget.style.background = 'rgba(255,255,255,0.08)'}
                >
                  <Ic src={closeSrc} className="w-4 h-4" filter={F.gray05} />
                </button>
              </div>
            </div>

            {/* Two-column FROM / TO run selector */}
            {(() => {
              const formatRunSuffix = (suffix) => {
                const m = suffix?.match(/^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})$/);
                if (!m) return suffix;
                const [, yr, mo, dy, hh, mm, ss] = m;
                return new Date(`${yr}-${mo}-${dy}T${hh}:${mm}:${ss}`).toLocaleString('en-US', {
                  timeZone: 'America/New_York', month: 'short', day: 'numeric',
                  year: 'numeric', hour: '2-digit', minute: '2-digit', hour12: true,
                });
              };

              const RunList = ({ label, accentColor, bgColor, scrollRef, onScroll, picked, onPick, disabledTable }) => (
                <div className="flex flex-col flex-1 min-w-0 overflow-hidden" style={{ borderRight: label === 'FROM Run' ? '1px solid #e8e6e3' : 'none' }}>
                  {/* Column header */}
                  <div className="flex-shrink-0 px-4 py-3 border-b border-jj-gray-02" style={{ background: '#fafaf9' }}>
                    <div className="flex items-center gap-2">
                      <span className="w-2.5 h-2.5 rounded-full flex-shrink-0" style={{ background: accentColor }} />
                      <p className="font-johnson-text text-[10px] font-bold text-jj-gray-05 uppercase tracking-widest">{label}</p>
                    </div>
                    <p className="font-johnson-text text-[10px] text-jj-gray-05 mt-0.5">
                      {label === 'FROM Run' ? 'Select the older / baseline run' : 'Select the newer run to compare against'}
                    </p>
                  </div>
                  {/* List */}
                  <div className="flex-1 overflow-y-auto p-3" ref={scrollRef} onScroll={onScroll}>
                    {stgTablesLoading ? (
                      <div className="flex items-center justify-center py-8"><LoadingSpinner /></div>
                    ) : stgTablesError ? (
                      <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-red-50 border border-jj-red border-opacity-20">
                        <Ic src={closeSrc} className="w-3.5 h-3.5" filter={F.red} />
                        <span className="font-johnson-text text-xs text-jj-maroon-05">{stgTablesError}</span>
                      </div>
                    ) : stgTables.length === 0 ? (
                      <div className="flex flex-col items-center justify-center py-8 gap-2">
                        <Ic src={databaseSrc} className="w-7 h-7" filter={F.gray05} />
                        <p className="font-johnson-text text-xs text-jj-gray-05 italic text-center">No historical runs found.</p>
                      </div>
                    ) : (
                      <ul className="space-y-1.5">
                        {stgTables.map((t, i) => {
                          const isSelected = picked === t.table_name;
                          const isDisabled = disabledTable === t.table_name;
                          return (
                            <li key={t.table_name} style={{ animation: 'staggerIn 0.18s cubic-bezier(0.16,1,0.3,1) ' + (i * 0.04) + 's both' }}>
                              <button
                                onClick={() => !isDisabled && onPick(isSelected ? null : t.table_name)}
                                disabled={isDisabled}
                                className="w-full text-left rounded-lg border-2 transition-all duration-150 overflow-hidden disabled:opacity-30 disabled:cursor-not-allowed"
                                style={{ borderColor: isSelected ? accentColor : '#e8e6e3', background: isSelected ? bgColor : 'white', boxShadow: isSelected ? `0 0 0 3px ${accentColor}22` : 'none' }}
                              >
                                <div className="flex items-center gap-2 px-3 py-2">
                                  <div
                                    className="flex-shrink-0 w-4 h-4 rounded-full border-2 flex items-center justify-center transition-all"
                                    style={{ borderColor: isSelected ? accentColor : '#d5cfc9', background: isSelected ? accentColor : 'white' }}
                                  >
                                    {isSelected && (
                                      <svg viewBox="0 0 10 8" className="w-2 h-2" fill="none">
                                        <path d="M1 4l3 3 5-6" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
                                      </svg>
                                    )}
                                  </div>
                                  <div className="flex-1 min-w-0">
                                    {t.run_suffix && (
                                      <p className="font-johnson-display text-sm truncate" style={{ lineHeight: '120%', color: isSelected ? accentColor : '#312c2a' }}>
                                        {formatRunSuffix(t.run_suffix)}
                                      </p>
                                    )}

                                  </div>
                                  <div className="flex items-center gap-1 flex-shrink-0">
                                    {t.is_latest && (
                                      <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full font-johnson-text text-[9px] font-bold text-jj-green-03 bg-green-50 border border-jj-green-03 border-opacity-30">
                                        <span className="w-1 h-1 rounded-full bg-jj-green-03 animate-pulse" />Latest
                                      </span>
                                    )}
                                    {t.is_previous && label === 'FROM Run' && (
                                      <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full font-johnson-text text-[9px] font-bold text-jj-blue-03 bg-blue-50 border border-jj-blue-03 border-opacity-30">
                                        Default
                                      </span>
                                    )}

                                  </div>
                                </div>
                              </button>
                            </li>
                          );
                        })}
                      </ul>
                    )}
                  </div>
                </div>
              );

              return (
                <div className="flex flex-1 overflow-hidden">
                  <RunList
                    label="FROM Run"
                    accentColor="#0f68b2"
                    bgColor="#f0f6ff"
                    picked={advPickedFromTable}
                    onPick={setAdvPickedFromTable}
                    disabledTable={advPickedToTable}
                    scrollRef={advFromScrollRef}
                    onScroll={makeAdvSyncHandler(advToScrollRef)}
                  />
                  <RunList
                    label="TO Run"
                    accentColor="#328714"
                    bgColor="#f0faf0"
                    picked={advPickedToTable}
                    onPick={setAdvPickedToTable}
                    disabledTable={advPickedFromTable}
                    scrollRef={advToScrollRef}
                    onScroll={makeAdvSyncHandler(advFromScrollRef)}
                  />
                </div>
              );
            })()}

            {/* Footer */}
            <div className="flex-shrink-0 px-6 py-4 flex items-center justify-between gap-4" style={{ borderTop: '1px solid #e8e6e3', background: '#fafaf9' }}>
              <div className="min-w-0">
                {(() => {
                  const getD = (name) => {
                    const t = stgTables.find(x => x.table_name === name);
                    if (!t?.run_suffix) return null;
                    const m = t.run_suffix.match(/^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})$/);
                    return m ? new Date(`${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6]}`) : null;
                  };
                  const fd = advPickedFromTable ? getD(advPickedFromTable) : null;
                  const td = advPickedToTable   ? getD(advPickedToTable)   : null;
                  return fd && td && fd >= td;
                })() && (
                  <div className="flex items-center gap-1.5 mb-2 px-2 py-1 rounded-lg bg-yellow-50 border border-yellow-300">
                    <svg className="w-3.5 h-3.5 text-yellow-600 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v4m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
                    </svg>
                    <span className="font-johnson-text text-[10px] font-bold text-yellow-700">FROM date is newer than TO date — results may be unexpected</span>
                  </div>
                )}
                {advPickedFromTable && advPickedToTable ? (
                  <p className="font-johnson-text text-xs text-jj-gray-07">
                    <span className="font-bold text-jj-gray-08">Comparing:</span>{' '}
                    <span className="text-jj-blue-03 font-bold">
                      {stgTables.find(t => t.table_name === advPickedFromTable)?.run_suffix
                        ? (() => { const s = stgTables.find(t => t.table_name === advPickedFromTable).run_suffix; const m = s.match(/^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})$/); return m ? new Date(`${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6]}`).toLocaleString('en-US', { timeZone: 'America/New_York', month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit', hour12: true }) : s; })()
                        : advPickedFromTable}
                    </span>
                    {' → '}
                    <span className="text-jj-green-03 font-bold">
                      {stgTables.find(t => t.table_name === advPickedToTable)?.run_suffix
                        ? (() => { const s = stgTables.find(t => t.table_name === advPickedToTable).run_suffix; const m = s.match(/^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})$/); return m ? new Date(`${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6]}`).toLocaleString('en-US', { timeZone: 'America/New_York', month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit', hour12: true }) : s; })()
                        : advPickedToTable}
                    </span>
                  </p>
                ) : (
                  <p className="font-johnson-text text-xs text-jj-gray-05 italic">
                    {!advPickedFromTable && !advPickedToTable ? 'Select a FROM and TO run to compare'
                      : !advPickedFromTable ? 'Select a FROM run'
                      : 'Select a TO run'}
                  </p>
                )}
              </div>
              <div className="flex items-center gap-2 flex-shrink-0">
                <button
                  onClick={() => setIsAdvPickerOpen(false)}
                  className="px-4 py-2 rounded-lg font-johnson-text text-sm font-bold text-jj-gray-07 bg-white hover:bg-jj-gray-01 transition-colors border border-jj-gray-02"
                >
                  Cancel
                </button>
                <button
                  onClick={() => {
                    if (!advPickedFromTable || !advPickedToTable) return;
                    setIsAdvPickerOpen(false);
                    openDeltaModal('new_failures', advPickedFromTable, advPickedToTable);
                  }}
                  disabled={!advPickedFromTable || !advPickedToTable}
                  className="inline-flex items-center gap-2 px-5 py-2 rounded-lg font-johnson-text text-sm font-bold text-white bg-jj-red hover:bg-jj-maroon-05 transition-colors shadow-sm disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  <Ic src={deltaSrc} className="w-4 h-4" filter={F.white} />
                  Run Delta
                </button>
              </div>
            </div>
          </div>
        </div>
      , document.body)}

      {/* ── Delta Rows Modal ── */}
      {isDeltaModalOpen && ReactDOM.createPortal(
        <div
          className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4"
          style={{ backdropFilter: 'blur(3px)' }}
          onClick={() => setIsDeltaModalOpen(false)}
        >
          <div
            ref={el => {
              deltaModalRef.current = el;
              if (el && !el._fsListenerAdded) {
                el._fsListenerAdded = true;
                document.addEventListener('fullscreenchange', () => setDeltaFullscreen(!!document.fullscreenElement));
              }
            }}
            className="bg-white w-full max-w-7xl flex flex-col overflow-hidden"
            style={deltaFullscreen
              ? { width: '100vw', height: '100vh', borderRadius: 0 }
              : { height: 'min(88vh, calc(100vh - 2rem))', borderRadius: '1rem', boxShadow: '0 25px 60px rgba(0,0,0,0.4)', animation: 'slideUp 0.25s ease-out' }
            }
            onClick={e => e.stopPropagation()}
          >
            {/* Header */}
            <div className="flex-shrink-0 px-5 py-3 bg-white border-b-4 border-jj-red rounded-t-2xl flex items-center justify-between gap-4 flex-wrap">
              <div className="flex items-center gap-3 flex-wrap">
                {/* Sheet toggle */}
                <div className="flex rounded-lg border border-jj-gray-03 overflow-hidden">
                  <button
                    onClick={() => handleDeltaSheetSwitch('new_failures')}
                    disabled={deltaPageLoading}
                    className={`flex items-center gap-1.5 px-3 py-2 font-johnson-text text-xs font-bold transition-colors ${
                      deltaSheet === 'new_failures'
                        ? 'bg-jj-gray-08 text-white'
                        : 'bg-white text-jj-gray-07 hover:bg-jj-gray-01'
                    }`}
                  >
                    <Ic src={chartSrc} className="w-3.5 h-3.5" filter={deltaSheet === 'new_failures' ? F.white : F.gray07} />
                    New Failures
                    {deltaData && deltaSheet === 'new_failures' && (
                      <span className="ml-1 px-1.5 py-0.5 rounded-full text-[10px] font-bold bg-white bg-opacity-20">
                        {deltaData.total_count.toLocaleString()}
                      </span>
                    )}
                  </button>
                  <button
                    onClick={() => handleDeltaSheetSwitch('resolved_rows')}
                    disabled={deltaPageLoading}
                    className={`flex items-center gap-1.5 px-3 py-2 font-johnson-text text-xs font-bold transition-colors border-l border-jj-gray-03 ${
                      deltaSheet === 'resolved_rows'
                        ? 'bg-jj-gray-08 text-white'
                        : 'bg-white text-jj-gray-07 hover:bg-jj-gray-01'
                    }`}
                  >
                    <Ic src={checkSrc} className="w-3.5 h-3.5" filter={deltaSheet === 'resolved_rows' ? F.white : F.gray07} />
                    Resolved Rows
                    {deltaData && deltaSheet === 'resolved_rows' && (
                      <span className="ml-1 px-1.5 py-0.5 rounded-full text-[10px] font-bold bg-white bg-opacity-20">
                        {deltaData.total_count.toLocaleString()}
                      </span>
                    )}
                  </button>
                </div>

                {deltaData && (
                  <span className="font-johnson-text text-xs text-jj-gray-06">
                    Page {deltaPage} of {deltaData.total_pages} · {PAGE_SIZE} rows / page
                  </span>
                )}
              </div>

              <div className="flex items-center gap-2 ml-auto">
                {/* Download delta Excel */}
                {!deltaDownloading ? (
                  <button
                    onClick={downloadDelta}
                    className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg font-johnson-text text-xs font-bold text-white bg-jj-red hover:bg-jj-maroon-05 transition-colors"
                  >
                    <Ic src={downloadSrc} className="w-3.5 h-3.5" filter={F.white} />
                    Download Excel
                  </button>
                ) : (
                  <div className="flex items-center gap-2 bg-red-50 border border-jj-red border-opacity-20 rounded-lg px-3 py-2 min-w-[200px]">
                    <Ic src={activitySrc} className="w-4 h-4 animate-spin flex-shrink-0" filter={F.red} />
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center justify-between mb-1">
                        <span className="font-johnson-text text-xs text-jj-gray-08 truncate">{deltaProgress?.label ?? 'Starting…'}</span>
                        <span className="font-johnson-text text-xs font-bold text-jj-red ml-2">{deltaProgress?.pct ?? 0}%</span>
                      </div>
                      <div className="w-full h-1.5 bg-red-100 rounded-full overflow-hidden">
                        <div className="h-full bg-jj-red rounded-full transition-all duration-500" style={{ width: `${deltaProgress?.pct ?? 0}%` }} />
                      </div>
                    </div>
                  </div>
                )}
                {/* Word wrap toggle */}
                <button
                  onClick={() => setDeltaWordWrap(w => !w)}
                  title={deltaWordWrap ? 'Collapse to single line' : 'Wrap cell text'}
                  className={`w-9 h-9 flex items-center justify-center rounded-lg transition-colors ${
                    deltaWordWrap ? 'bg-jj-red text-white' : 'bg-jj-gray-01 hover:bg-jj-gray-02 text-jj-gray-07'
                  }`}
                >
                  <Ic src={recordsSrc} className="w-4 h-4" filter={deltaWordWrap ? F.white : F.gray07} />
                </button>
                {/* Fullscreen toggle */}
                <button
                  onClick={() => {
                    if (!document.fullscreenElement) {
                      deltaModalRef.current?.requestFullscreen?.();
                    } else {
                      document.exitFullscreen?.();
                    }
                  }}
                  title={deltaFullscreen ? 'Exit fullscreen (F11)' : 'Enter fullscreen'}
                  className="w-9 h-9 flex items-center justify-center rounded-lg bg-jj-gray-01 hover:bg-jj-gray-02 transition-colors"
                >
                  {deltaFullscreen ? (
                    <svg xmlns="http://www.w3.org/2000/svg" className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ color: '#6b6b6b' }}>
                      <path d="M8 3v3a2 2 0 0 1-2 2H3m18 0h-3a2 2 0 0 1-2-2V3m0 18v-3a2 2 0 0 1 2-2h3M3 16h3a2 2 0 0 1 2 2v3" />
                    </svg>
                  ) : (
                    <svg xmlns="http://www.w3.org/2000/svg" className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ color: '#6b6b6b' }}>
                      <path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3" />
                    </svg>
                  )}
                </button>
                <button onClick={() => setIsDeltaModalOpen(false)} className="w-9 h-9 flex items-center justify-center rounded-lg bg-jj-gray-01 hover:bg-jj-gray-02 transition-colors">
                  <Ic src={closeSrc} className="w-4 h-4" filter={F.gray07} />
                </button>
              </div>
            </div>

            {/* Description + run comparison banner */}
            {(() => {
              const formatStgDate = (tableName) => {
                if (!tableName) return null;
                const m = tableName.match(/(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})$/);
                if (!m) return null;
                const [, yr, mo, dy, hh, mm, ss] = m;
                return new Date(`${yr}-${mo}-${dy}T${hh}:${mm}:${ss}`).toLocaleString('en-US', {
                  timeZone: 'America/New_York', month: 'short', day: 'numeric',
                  year: 'numeric', hour: '2-digit', minute: '2-digit', hour12: true,
                });
              };
              const fromDate = selectedStgTable
                ? formatStgDate(selectedStgTable)
                : defaultPreviousRunSuffix
                  ? formatStgDate(defaultPreviousRunSuffix)
                  : null;
              const toDate = selectedToStgTable
                ? formatStgDate(selectedToStgTable)
                : defaultLatestRunSuffix
                  ? formatStgDate(defaultLatestRunSuffix)
                  : null;

              const action = deltaSheet === 'new_failures' ? 'appeared' : 'were resolved';

              return (
                <div className="flex-shrink-0 px-5 py-3 text-center border-b border-jj-gray-02 bg-jj-gray-01">
                  <p className="font-johnson-text text-xs text-jj-gray-07 leading-relaxed">
                    Showing failures that {action} since{' '}
                    <span className="font-bold text-jj-gray-08 bg-white border border-jj-gray-03 px-1.5 py-0.5 rounded">
                      {fromDate || 'the previous run'}
                    </span>
                    {' — '}compared against{' '}
                    <span className="font-bold text-jj-green-03 bg-white border border-jj-green-03 border-opacity-40 px-1.5 py-0.5 rounded">
                      {toDate || 'the latest run'}
                    </span>.
                  </p>
                </div>
              );
            })()}

            {/* Table body */}
            <ModalDataTable
              columns={deltaData?.columns ?? []}
              rows={deltaData?.rows ?? []}
              colWidths={deltaColWidths}
              onResize={onDeltaResize}
              wordWrap={deltaWordWrap}
              rowOffset={(deltaPage - 1) * PAGE_SIZE}
              colorCol1={colorCol1}
              colorCol2={colorCol2}
              loading={deltaLoading || deltaPageLoading}
              error={deltaError}
              emptyMessage={deltaSheet === 'new_failures'
                ? 'No new failures — no failures appeared since the last execution.'
                : 'No resolved rows — no failures have been resolved or dropped since the last execution.'}
            />

            {/* Pagination footer */}
            {deltaData && (
              <div className="flex-shrink-0 px-5 py-3 bg-white border-t border-jj-gray-02 rounded-b-2xl flex items-center justify-between">
                <span className="font-johnson-text text-xs text-jj-gray-06">
                  Rows <b className="text-jj-gray-08">{((deltaPage - 1) * PAGE_SIZE + 1).toLocaleString()}</b>–<b className="text-jj-gray-08">{Math.min(deltaPage * PAGE_SIZE, deltaData.total_count).toLocaleString()}</b> of <b className="text-jj-gray-08">{deltaData.total_count.toLocaleString()}</b>
                </span>
                <div className="flex items-center gap-1">
                  <button className={btnGray} disabled={deltaPage === 1 || deltaPageLoading} onClick={() => handleDeltaPageChange(1)}>First</button>
                  <button className={btnGray} disabled={deltaPage === 1 || deltaPageLoading} onClick={() => handleDeltaPageChange(deltaPage - 1)}>
                    <Ic src={chevronLeftSrc} className="w-3.5 h-3.5" filter={F.gray07} />
                  </button>
                  {(() => {
                    const total = deltaData.total_pages;
                    const start = Math.max(1, Math.min(deltaPage - 2, total - 4));
                    const end   = Math.min(total, start + 4);
                    return Array.from({ length: end - start + 1 }, (_, i) => start + i).map(p => (
                      <button key={p} className={p === deltaPage ? btnRed : btnGray} disabled={deltaPageLoading} onClick={() => handleDeltaPageChange(p)}>{p}</button>
                    ));
                  })()}
                  <button className={btnGray} disabled={deltaPage === deltaData.total_pages || deltaPageLoading} onClick={() => handleDeltaPageChange(deltaPage + 1)}>
                    <Ic src={chevronRightSrc} className="w-3.5 h-3.5" filter={F.gray07} />
                  </button>
                  <button className={btnGray} disabled={deltaPage === deltaData.total_pages || deltaPageLoading} onClick={() => handleDeltaPageChange(deltaData.total_pages)}>Last</button>
                </div>
              </div>
            )}
          </div>
        </div>
      , document.body)}

      {/* ── Consolidated Report Modal ── */}
      {isConsolidatedOpen && consolidatedPreview && ReactDOM.createPortal(
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4"
          style={{ backdropFilter: 'blur(3px)' }} onClick={() => setIsConsolidatedOpen(false)}>
          <div
            ref={el => {
              consolidatedModalRef.current = el;
              if (el && !el._fsListenerAdded) {
                el._fsListenerAdded = true;
                document.addEventListener('fullscreenchange', () => setConsolidatedFullscreen(!!document.fullscreenElement));
              }
            }}
            className="bg-white w-full max-w-7xl flex flex-col overflow-hidden"
            style={consolidatedFullscreen
              ? { width: '100vw', height: '100vh', borderRadius: 0 }
              : { height: 'min(90vh, calc(100vh - 2rem))', borderRadius: '1rem', boxShadow: '0 25px 60px rgba(0,0,0,0.4)', animation: 'slideUp 0.25s ease-out' }
            }
            onClick={e => e.stopPropagation()}
          >

            {/* Header */}
            <div className="flex-shrink-0 px-5 py-3 rounded-t-2xl flex items-center justify-between gap-4 flex-wrap border-b-4"
              style={{ borderBottomColor: '#8c3bbb' }}>
              <div className="flex items-center gap-3 flex-wrap">
                <Ic src={chartSrc} className="w-5 h-5 flex-shrink-0" filter={F.violet} style={{ color: '#8c3bbb' }} />
                <span className="font-johnson-display text-base text-jj-gray-08" style={{ lineHeight: '110%' }}>
                  Consolidated Failed Rows
                </span>
                {consolidatedSummary && (<>
                  <span className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg font-johnson-text text-xs font-bold border border-opacity-30"
                    style={{ color: '#8c3bbb', backgroundColor: '#f5f0fb', borderColor: '#8c3bbb' }}>
                    <Ic src={alertSrc} className="w-3.5 h-3.5" filter={F.orange} />
                    {consolidatedSummary.total_rows.toLocaleString()} unique rows
                  </span>
                  <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg font-johnson-text text-xs text-jj-gray-06 bg-jj-gray-01 border border-jj-gray-02">
                    {consolidatedSummary.duplicates_removed.toLocaleString()} duplicates removed
                  </span>
                  {consolidatedSummary.generated_at && (
                    <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg font-johnson-text text-xs text-jj-gray-06 bg-jj-gray-01 border border-jj-gray-02">
                      <Ic src={databaseSrc} className="w-3 h-3" filter={F.gray06} />
                      {fmtEastern(consolidatedSummary.generated_at)}
                    </span>
                  )}
                </>)}
              </div>
              <div className="flex items-center gap-2 ml-auto">
                <div className="relative">
                  <Ic src={infoSrc} className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 pointer-events-none" filter={F.gray05} />
                  <input type="text" placeholder="Search preview…" value={consolidatedSearch}
                    onChange={e => { setConsolidatedSearch(e.target.value); setConsolidatedPage(1); }}
                    className="pl-8 pr-3 py-1.5 rounded-lg font-johnson-text text-xs border border-jj-gray-03 text-jj-gray-08 bg-jj-gray-01 outline-none w-44" />
                </div>
                {/* Download area — single Download Excel button for all states */}
                {consolidatedSummary?.report_name && (
                  (consolidatedExcelPreparing || consolidatedDownloading) ? (
                    <div className="flex items-center gap-2 rounded-lg px-3 py-2 min-w-[220px] border border-opacity-30"
                      style={{ backgroundColor: '#f5f0fb', borderColor: '#8c3bbb' }}>
                      <Ic src={activitySrc} className="w-4 h-4 animate-spin flex-shrink-0" filter={F.gray07} />
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center justify-between mb-1">
                          <span className="font-johnson-text text-xs text-jj-gray-08 truncate">
                            {consolidatedDownloading
                              ? (consolidatedDownloadProgress?.label ?? 'Downloading…')
                              : (consolidatedExcelProgress?.label ?? 'Preparing…')}
                          </span>
                          <span className="font-johnson-text text-xs font-bold ml-2" style={{ color: '#8c3bbb' }}>
                            {consolidatedDownloading
                              ? (consolidatedDownloadProgress?.pct ?? 0)
                              : (consolidatedExcelProgress?.pct ?? 0)}%
                          </span>
                        </div>
                        <div className="w-full h-1.5 rounded-full overflow-hidden" style={{ backgroundColor: '#e8daf5' }}>
                          <div className="h-full rounded-full transition-all duration-300" style={{
                            width: `${consolidatedDownloading ? (consolidatedDownloadProgress?.pct ?? 0) : (consolidatedExcelProgress?.pct ?? 0)}%`,
                            backgroundColor: '#8c3bbb'
                          }} />
                        </div>
                      </div>
                    </div>
                  ) : (
                    <button
                      onClick={() => consolidatedToken ? downloadConsolidatedExcel() : prepareExcelDownload(consolidatedSummary.report_name)}
                      className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg font-johnson-text text-xs font-bold text-white transition-opacity hover:opacity-90 shadow-sm"
                      style={{ backgroundColor: '#8c3bbb' }}>
                      <Ic src={downloadSrc} className="w-3.5 h-3.5" filter={F.white} />
                      Download Excel
                    </button>
                  )
                )}
                {/* Word wrap toggle */}
                <button
                  onClick={() => setConsolidatedWordWrap(w => !w)}
                  title={consolidatedWordWrap ? 'Collapse to single line' : 'Wrap cell text'}
                  className={`w-9 h-9 flex items-center justify-center rounded-lg transition-colors ${
                    consolidatedWordWrap ? 'bg-jj-red text-white' : 'bg-jj-gray-01 hover:bg-jj-gray-02 text-jj-gray-07'
                  }`}
                >
                  <Ic src={recordsSrc} className="w-4 h-4" filter={consolidatedWordWrap ? F.white : F.gray07} />
                </button>
                {/* Fullscreen toggle */}
                <button
                  onClick={() => {
                    if (!document.fullscreenElement) {
                      consolidatedModalRef.current?.requestFullscreen?.();
                    } else {
                      document.exitFullscreen?.();
                    }
                  }}
                  title={consolidatedFullscreen ? 'Exit fullscreen (F11)' : 'Enter fullscreen'}
                  className="w-9 h-9 flex items-center justify-center rounded-lg bg-jj-gray-01 hover:bg-jj-gray-02 transition-colors"
                >
                  {consolidatedFullscreen ? (
                    <svg xmlns="http://www.w3.org/2000/svg" className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ color: '#6b6b6b' }}>
                      <path d="M8 3v3a2 2 0 0 1-2 2H3m18 0h-3a2 2 0 0 1-2-2V3m0 18v-3a2 2 0 0 1 2-2h3M3 16h3a2 2 0 0 1 2 2v3" />
                    </svg>
                  ) : (
                    <svg xmlns="http://www.w3.org/2000/svg" className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ color: '#6b6b6b' }}>
                      <path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3" />
                    </svg>
                  )}
                </button>
                <button onClick={() => setIsConsolidatedOpen(false)}
                  className="w-9 h-9 flex items-center justify-center rounded-lg bg-jj-gray-01 hover:bg-jj-gray-02 transition-colors">
                  <Ic src={closeSrc} className="w-4 h-4" filter={F.gray07} />
                </button>
              </div>
            </div>

            {/* Sub-header: FAILURE_MODE legend + truncation notice */}
            <div className="flex-shrink-0 px-5 py-2 border-b border-jj-gray-02 bg-jj-gray-01 flex items-center justify-between gap-4 flex-wrap text-xs font-johnson-text">
              <div className="flex items-center gap-2 text-jj-gray-06">
                <code className="font-mono px-1.5 py-0.5 rounded border border-opacity-30 font-bold"
                  style={{ color: '#8c3bbb', backgroundColor: '#f5f0fb', borderColor: '#8c3bbb' }}>FAILURE_MODE</code>
                <span>lists all rule IDs that flagged this row (comma-separated for multiple).</span>
              </div>
            </div>

            {/* Table */}
            <ModalDataTable
              columns={consolidatedPreview.columns}
              rows={consolidatedPageRows}
              colWidths={consolidatedColWidths}
              onResize={onConsolidatedResize}
              wordWrap={consolidatedWordWrap}
              rowOffset={(consolidatedPage - 1) * CONSOLIDATED_PAGE_SIZE}
              ruleColorMap={consolidatedSummary?.rule_color_map ?? null}
              emptyMessage={consolidatedSearch ? 'No rows match your search.' : 'No failed rows found.'}
            />

            {/* Pagination footer */}
            <div className="flex-shrink-0 px-5 py-3 bg-white border-t border-jj-gray-02 rounded-b-2xl flex items-center justify-between flex-wrap gap-3">
              <span className="font-johnson-text text-xs text-jj-gray-06">
                Rows{' '}
                <b className="text-jj-gray-08">{((consolidatedPage - 1) * CONSOLIDATED_PAGE_SIZE + 1).toLocaleString()}</b>–
                <b className="text-jj-gray-08">{Math.min(consolidatedPage * CONSOLIDATED_PAGE_SIZE, consolidatedFiltered.length).toLocaleString()}</b>
                {' '}of{' '}
                <b className="text-jj-gray-08">{consolidatedFiltered.length.toLocaleString()}</b>
                {consolidatedFiltered.length !== consolidatedRows.length && (
                  <span className="ml-1 text-jj-gray-05">(filtered from {consolidatedRows.length.toLocaleString()})</span>
                )}
              </span>
              <div className="flex items-center gap-1">
                <button className={btnGray} disabled={consolidatedPage === 1} onClick={() => setConsolidatedPage(1)}>First</button>
                <button className={btnGray} disabled={consolidatedPage === 1} onClick={() => setConsolidatedPage(p => p - 1)}>
                  <Ic src={chevronLeftSrc} className="w-3.5 h-3.5" filter={F.gray07} />
                </button>
                {(() => {
                  const total = consolidatedTotalPages;
                  const start = Math.max(1, Math.min(consolidatedPage - 2, total - 4));
                  const end   = Math.min(total, start + 4);
                  return Array.from({ length: end - start + 1 }, (_, i) => start + i).map(p => (
                    <button key={p} className={p === consolidatedPage ? btnRed : btnGray}
                      onClick={() => setConsolidatedPage(p)}>{p}</button>
                  ));
                })()}
                <button className={btnGray} disabled={consolidatedPage === consolidatedTotalPages}
                  onClick={() => setConsolidatedPage(p => p + 1)}>
                  <Ic src={chevronRightSrc} className="w-3.5 h-3.5" filter={F.gray07} />
                </button>
                <button className={btnGray} disabled={consolidatedPage === consolidatedTotalPages}
                  onClick={() => setConsolidatedPage(consolidatedTotalPages)}>Last</button>
              </div>
            </div>

          </div>
        </div>
      , document.body)}

      {/* ── Consolidated Generation Progress Modal ── */}
      {consolidatedGenModalOpen && ReactDOM.createPortal(
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4"
          style={{ backdropFilter: 'blur(3px)' }}>
          <div className="bg-white rounded-2xl shadow-2xl border border-jj-gray-02 w-full max-w-md flex flex-col"
            style={{ animation: 'slideUp 0.25s ease-out' }}>

            {/* Header */}
            <div className="flex-shrink-0 px-5 py-3 rounded-t-2xl border-b-4 flex items-center justify-between gap-3"
              style={{ borderBottomColor: '#8c3bbb' }}>
              <div className="flex items-center gap-2">
                {consolidatedLoading
                  ? <Ic src={activitySrc} className="w-5 h-5 animate-spin" filter={F.violet} />
                  : consolidatedError
                    ? <Ic src={alertSrc} className="w-5 h-5" filter={F.maroon} />
                    : <Ic src={checkSrc} className="w-5 h-5" filter={F.green} />
                }
                <span className="font-johnson-display text-base text-jj-gray-08" style={{ lineHeight: '110%' }}>
                  {consolidatedLoading ? 'Generating Consolidated Report' : consolidatedError ? 'Generation Failed' : 'Report Ready'}
                </span>
              </div>
              {!consolidatedLoading && (
                <button
                  onClick={() => { setConsolidatedGenModalOpen(false); setConsolidatedError(null); }}
                  className="w-8 h-8 flex items-center justify-center rounded-lg bg-jj-gray-01 hover:bg-jj-gray-02 transition-colors">
                  <Ic src={closeSrc} className="w-4 h-4" filter={F.gray07} />
                </button>
              )}
            </div>

            {/* Body */}
            <div className="px-5 py-5 flex flex-col gap-4">
              {/* Progress bar */}
              {!consolidatedError && (
                <div className="flex flex-col gap-2">
                  <div className="flex items-center justify-between">
                    <span className="font-johnson-text text-sm text-jj-gray-08 truncate">
                      {consolidatedProgress?.label ?? 'Working…'}
                    </span>
                    <span className="font-johnson-text text-sm font-bold ml-3 flex-shrink-0" style={{ color: '#8c3bbb' }}>
                      {consolidatedProgress?.pct ?? 0}%
                    </span>
                  </div>
                  <div className="w-full h-2.5 rounded-full overflow-hidden" style={{ backgroundColor: '#e8daf5' }}>
                    <div
                      className="h-full rounded-full transition-all duration-300"
                      style={{ width: `${consolidatedProgress?.pct ?? 0}%`, backgroundColor: '#8c3bbb' }}
                    />
                  </div>
                  {consolidatedLoading && (
                    <p className="font-johnson-text text-xs text-jj-gray-05 mt-1">
                      This may take a few minutes depending on the volume of data. Please keep this window open.
                    </p>
                  )}
                </div>
              )}

              {/* Error state */}
              {consolidatedError && (
                <div className="flex items-start gap-2 px-3 py-3 bg-red-50 border border-jj-maroon-05 border-opacity-30 rounded-lg">
                  <Ic src={closeSrc} className="w-4 h-4 flex-shrink-0 mt-0.5" filter={F.maroon} />
                  <span className="font-johnson-text text-xs text-jj-maroon-05">{consolidatedError}</span>
                </div>
              )}
            </div>

            {/* Footer — only shown on error */}
            {consolidatedError && (
              <div className="flex-shrink-0 px-5 pb-4 flex justify-end gap-2">
                <button
                  onClick={() => { setConsolidatedGenModalOpen(false); setConsolidatedError(null); }}
                  className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg font-johnson-text text-xs font-bold border border-jj-gray-03 text-jj-gray-07 hover:bg-jj-gray-01 transition-colors">
                  Dismiss
                </button>
                <button
                  onClick={() => { setConsolidatedGenModalOpen(false); setConsolidatedError(null); generateConsolidatedReport(); }}
                  className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg font-johnson-text text-xs font-bold text-white hover:opacity-90 transition-opacity"
                  style={{ backgroundColor: '#8c3bbb' }}>
                  <Ic src={refreshSrc} className="w-3.5 h-3.5" filter={F.white} />
                  Retry
                </button>
              </div>
            )}

          </div>
        </div>
      , document.body)}

      {/* ── Consolidated History Modal ── */}
      {consolidatedHistoryOpen && ReactDOM.createPortal(
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4"
          style={{ backdropFilter: 'blur(3px)' }} onClick={() => setConsolidatedHistoryOpen(false)}>
          <div className="bg-white rounded-2xl shadow-2xl border border-jj-gray-02 w-full max-w-3xl flex flex-col"
            style={{ maxHeight: 'min(80vh, calc(100vh - 2rem))', animation: 'slideUp 0.25s ease-out' }} onClick={e => e.stopPropagation()}>

            {/* Header */}
            <div className="flex-shrink-0 px-5 py-3 rounded-t-2xl border-b-4 flex items-center justify-between gap-3"
              style={{ borderBottomColor: '#8c3bbb' }}>
              <div className="flex items-center gap-2">
                <Ic src={recordsSrc} className="w-5 h-5" filter={F.violet} />
                <span className="font-johnson-display text-base text-jj-gray-08" style={{ lineHeight: '110%' }}>
                  Historic Consolidated Reports
                </span>
                {!consolidatedHistoryLoading && (
                  <span className="inline-flex items-center gap-1 px-2 py-1 rounded-full font-johnson-text text-[10px] font-bold border border-opacity-30"
                    style={{ color: '#8c3bbb', backgroundColor: '#f5f0fb', borderColor: '#8c3bbb' }}>
                    {consolidatedHistory.length} report{consolidatedHistory.length !== 1 ? 's' : ''}
                  </span>
                )}
              </div>
              <button onClick={() => setConsolidatedHistoryOpen(false)}
                className="w-8 h-8 flex items-center justify-center rounded-lg bg-jj-gray-01 hover:bg-jj-gray-02 transition-colors">
                <Ic src={closeSrc} className="w-4 h-4" filter={F.gray07} />
              </button>
            </div>

            {/* Body */}
            <div className="flex-1 overflow-y-auto px-5 py-4">
              {consolidatedHistoryLoading ? (
                <div className="flex items-center justify-center py-12 gap-3">
                  <Ic src={activitySrc} className="w-5 h-5 animate-spin" filter={F.violet} />
                  <span className="font-johnson-text text-sm text-jj-gray-06">Loading report history…</span>
                </div>
              ) : consolidatedHistoryError ? (
                <div className="flex items-center gap-2 px-4 py-3 bg-red-50 border border-jj-maroon-05 border-opacity-30 rounded-lg">
                  <Ic src={closeSrc} className="w-4 h-4 flex-shrink-0" filter={F.maroon} />
                  <span className="font-johnson-text text-xs text-jj-maroon-05">{consolidatedHistoryError}</span>
                </div>
              ) : consolidatedHistory.length === 0 ? (
                <EmptyState icon={recordsSrc} title="No historic reports" message="No consolidated reports have been generated yet." />
              ) : (
                <div className="flex flex-col gap-2">
                  {consolidatedHistory.map((report, i) => {
                    const isFetching = consolidatedHistoryFetching === report.report_name;
                    const isLatest   = i === 0;
                    return (
                      <div key={report.report_name}
                        className="rounded-xl border border-jj-gray-02 bg-white hover:bg-jj-gray-01 transition-colors overflow-hidden">
                        <div className="px-4 py-3 flex items-center justify-between gap-4">
                          <div className="flex-1 min-w-0">
                            <div className="flex items-center gap-2 mb-1 flex-wrap">
                              <code className="font-mono text-xs font-bold text-jj-gray-07 truncate">{report.report_name}</code>
                              {isLatest && (
                                <span className="flex-shrink-0 inline-flex items-center gap-1 px-2 py-0.5 rounded-full font-johnson-text text-[10px] font-bold border border-opacity-30"
                                  style={{ color: '#8c3bbb', backgroundColor: '#f5f0fb', borderColor: '#8c3bbb' }}>
                                  Latest
                                </span>
                              )}
                            </div>
                            <div className="flex items-center gap-4 flex-wrap">
                              <span className="font-johnson-text text-xs text-jj-gray-06 flex items-center gap-1">
                                <Ic src={databaseSrc} className="w-3 h-3" filter={F.gray06} />
                                {fmtEastern(report.generated_at)}
                              </span>
                              <span className="font-johnson-text text-xs text-jj-gray-06">
                                <b className="text-jj-gray-08">{report.total_rows?.toLocaleString()}</b> unique rows
                              </span>
                              <span className="font-johnson-text text-xs text-jj-gray-06">
                                <b className="text-jj-gray-08">{report.duplicates_removed?.toLocaleString()}</b> dupes removed
                              </span>
                              <span className="font-johnson-text text-xs text-jj-gray-06">
                                <b className="text-jj-gray-08">{report.rules_count}</b> rule{report.rules_count !== 1 ? 's' : ''}
                              </span>
                            </div>
                          </div>
                          <button
                            onClick={() => loadHistoricReport(report.report_name)}
                            disabled={!!consolidatedHistoryFetching}
                            className="flex-shrink-0 inline-flex items-center gap-1.5 px-3 py-2 rounded-lg font-johnson-text text-xs font-bold text-white transition-opacity disabled:opacity-40 disabled:cursor-not-allowed hover:opacity-90"
                            style={{ backgroundColor: '#8c3bbb' }}
                          >
                            {isFetching
                              ? <><Ic src={activitySrc} className="w-3.5 h-3.5 animate-spin" filter={F.white} />Loading…</>
                              : <><Ic src={chartSrc} className="w-3.5 h-3.5" filter={F.white} />View Report</>
                            }
                          </button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>

          </div>
        </div>
      , document.body)}
    </div>
  );
}