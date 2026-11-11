import React, { useState, useMemo, useEffect, useCallback, useRef, memo } from 'react';
import { useSearchParams, Navigate } from 'react-router-dom';
import ExcelJS from 'exceljs';
import LoadingSpinner from './components/LoadingSpinner';
import BusinessDefinedChecks from './BusinessDefinedChecks';
import { useRBAC } from '../context/RBACContext';

// J&J Custom Icons
import checkCircleIcon from "/icons/JJ_Icon_Check_RGB.svg";
import alertTriangleIcon from "/icons/JJ_Icon_Alert_RGB.svg";
import xCircleIcon from "/icons/JJ_Icon_Close.svg";
import searchIcon from "/icons/JJ_Icon_Search_RGB.svg";
import databaseIcon from "/icons/JJ_Icon_Database_RGB.svg";
import minusIcon from "/icons/JJ_Icon_Remove.svg";
import barChartIcon from "/icons/JJ_Icon_Graph_Chart_RGB.svg";
import shieldCheckIcon from "/icons/JJ_Icon_Safety.svg";
import activityIcon from "/icons/JJIM_Icon_Fingernail.svg";
import runIcon from "/icons/JJ_Icon_Video_Play_RGB.svg";
import loadingIcon from "/icons/JJ_Icon_Loading.svg";
import clockIcon from "/icons/JJ_Icon_Timer_RGB.svg";
import chevronDownIcon from "/icons/JJ_Icon_Chevron_Down.svg";
import chevronRightIcon from "/icons/JJ_Icon_Chevron_Right.svg";
import infoIcon from "/icons/JJ_Icon_Info_RGB.svg";
import chevronLeftIcon from "/icons/JJ_Icon_Chevron_Left.svg";
import refreshIcon from "/icons/JJ_Icon_Refresh_RGB.svg";
import downloadIcon from "/icons/JJ_Icon_Download_PDF_RGB.svg";
import arrowUpDownIcon from "/icons/JJ_Icon_Sort.svg";
import { apiEventSource } from '../utils/apiClient';

// ─── Icon Wrapper ─────────────────────────────────────────────────────────────
const Icon = ({ src, alt, className = "w-6 h-6", color }) => {
  const getColorFilter = (colorClass) => {
    if (!colorClass) return {};
    const c = color || colorClass;
    if (c.includes('jj-green-03') || c.includes('text-green'))
      return { filter: 'brightness(0) saturate(100%) invert(37%) sepia(87%) saturate(1158%) hue-rotate(71deg) brightness(91%) contrast(93%)' };
    if (c.includes('jj-maroon-05') || c.includes('text-red'))
      return { filter: 'brightness(0) saturate(100%) invert(13%) sepia(97%) saturate(6477%) hue-rotate(359deg) brightness(91%) contrast(99%)' };
    if (c.includes('jj-orange-03'))
      return { filter: 'brightness(0) saturate(100%) invert(48%) sepia(97%) saturate(2446%) hue-rotate(347deg) brightness(102%) contrast(105%)' };
    if (c.includes('jj-blue-03'))
      return { filter: 'brightness(0) saturate(100%) invert(37%) sepia(93%) saturate(1535%) hue-rotate(189deg) brightness(91%) contrast(101%)' };
    if (c.includes('jj-blue-05'))
      return { filter: 'brightness(0) saturate(100%) invert(20%) sepia(93%) saturate(1535%) hue-rotate(200deg) brightness(91%) contrast(101%)' };
    if (c.includes('jj-red'))
      return { filter: 'brightness(0) saturate(100%) invert(17%) sepia(100%) saturate(7426%) hue-rotate(3deg) brightness(92%) contrast(118%)' };
    if (c.includes('jj-gray-08'))
      return { filter: 'brightness(0) saturate(100%) invert(15%) sepia(7%) saturate(806%) hue-rotate(346deg) brightness(95%) contrast(89%)' };
    if (c.includes('jj-gray-06'))
      return { filter: 'brightness(0) saturate(100%) invert(50%) sepia(6%) saturate(656%) hue-rotate(346deg) brightness(92%) contrast(88%)' };
    if (c.includes('jj-gray-05'))
      return { filter: 'brightness(0) saturate(100%) invert(65%) sepia(6%) saturate(556%) hue-rotate(346deg) brightness(92%) contrast(88%)' };
    if (c.includes('jj-gray-04'))
      return { filter: 'brightness(0) saturate(100%) invert(82%) sepia(6%) saturate(356%) hue-rotate(346deg) brightness(92%) contrast(88%)' };
    if (c.includes('white') || c.includes('invert'))
      return { filter: 'brightness(0) invert(1)' };
    return {};
  };
  return (
    <img src={src} alt={alt} className={className} style={getColorFilter(className)} />
  );
};

// ─── Constants ────────────────────────────────────────────────────────────────
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL;

const STATUS = {
  PASSED: 'PASSED', CRITICAL: 'CRITICAL', ERROR: 'ERROR',
  MATCH: 'MATCH', MISMATCH: 'MISMATCH', DEFINITION_MISMATCH: 'DEFINITION_MISMATCH',
  NONE: 'NONE', FAULTY: 'FAULTY', TIMEOUT: 'TIMEOUT', ALL: 'ALL'
};

// ─── Helpers ──────────────────────────────────────────────────────────────────
const getTableValidations = (pipeline) => {
  try { return pipeline?.validation_results?.table_validations || []; }
  catch { return []; }
};

const formatTimestamp = (timestamp) => {
  if (!timestamp) return '';
  try {
    return new Date(timestamp).toLocaleString('en-US', {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit', hour12: true
    });
  } catch { return timestamp; }
};

// ─── Toast ────────────────────────────────────────────────────────────────────
const Toast = memo(({ message, type = 'success', onClose }) => {
  useEffect(() => {
    const t = setTimeout(onClose, 5000);
    return () => clearTimeout(t);
  }, [onClose]);

  const configs = {
    success: { bg: 'bg-jj-green-03', icon: checkCircleIcon },
    error:   { bg: 'bg-jj-maroon-05', icon: xCircleIcon },
    info:    { bg: 'bg-jj-blue-03', icon: infoIcon },
  };
  const { bg, icon } = configs[type] || configs.info;

  return (
    <div className="fixed top-6 right-6 z-[9999] animate-slide-in">
      <div className={`${bg} text-white px-5 py-4 rounded-lg shadow-2xl flex items-center gap-3 min-w-[320px] max-w-md border border-white border-opacity-20`}>
        <Icon src={icon} alt={type} className="w-5 h-5 flex-shrink-0 text-white" />
        <p className="font-johnson-text text-sm flex-1">{message}</p>
        <button onClick={onClose} className="hover:opacity-70 transition-opacity ml-2">
          <Icon src={xCircleIcon} alt="Close" className="w-4 h-4 text-white" />
        </button>
      </div>
    </div>
  );
});
Toast.displayName = 'Toast';

// ─── Error Boundary ───────────────────────────────────────────────────────────
class ErrorBoundary extends React.Component {
  constructor(props) { super(props); this.state = { hasError: false }; }
  static getDerivedStateFromError() { return { hasError: true }; }
  componentDidCatch(error, info) { console.error('DQEngine Error:', error, info); }
  render() {
    if (this.state.hasError) return (
      <div className="min-h-screen flex items-center justify-center bg-jj-gray-01 p-6">
        <div className="bg-white rounded-xl shadow-xl p-8 max-w-lg w-full border-t-4 border-jj-red">
          <div className="flex items-start gap-4 mb-6">
            <div className="w-12 h-12 bg-red-50 rounded-lg flex items-center justify-center flex-shrink-0">
              <Icon src={alertTriangleIcon} alt="Error" className="w-7 h-7 text-jj-maroon-05" />
            </div>
            <div>
              <h2 className="font-johnson-display text-xl text-jj-gray-08 mb-1" style={{ lineHeight: '110%' }}>Something went wrong</h2>
              <p className="font-johnson-text text-sm text-jj-gray-06">An unexpected error occurred while loading the Data Quality Engine.</p>
            </div>
          </div>
          <button onClick={() => window.location.reload()}
            className="w-full py-3 rounded-lg font-johnson-text font-bold text-sm bg-jj-red text-white hover:bg-jj-maroon-05 transition-colors duration-200">
            Reload Page
          </button>
        </div>
      </div>
    );
    return this.props.children;
  }
}

// ─── Summary Card ─────────────────────────────────────────────────────────────
const SummaryCard = memo(({ title, value, icon, colorClass = 'text-jj-gray-08', borderColor = 'border-jj-gray-03', onClick }) => (
  <button
    onClick={onClick}
    className={`bg-white rounded-xl shadow-sm hover:shadow-md transition-all duration-200 border border-jj-gray-02 border-l-4 ${borderColor} p-4 text-left w-full group`}
  >
    <div className="flex items-center justify-between mb-3">
      <p className="font-johnson-text text-xs font-bold text-jj-gray-06 uppercase tracking-wide">{title}</p>
      <div className="w-8 h-8 rounded-lg bg-jj-gray-01 flex items-center justify-center group-hover:bg-jj-gray-02 transition-colors">
        <Icon src={icon} alt={title} className={`w-4 h-4 ${colorClass}`} />
      </div>
    </div>
    <p className={`font-johnson-display text-3xl font-bold ${colorClass}`} style={{ lineHeight: '110%' }}>
      {typeof value === 'number' ? value.toLocaleString() : value}
    </p>
  </button>
));
SummaryCard.displayName = 'SummaryCard';

// ─── Status Badge ─────────────────────────────────────────────────────────────
const StatusBadge = memo(({ status }) => {
  const configs = {
    [STATUS.PASSED]:             { text: 'Passed',              icon: checkCircleIcon,  cls: 'text-jj-green-03 bg-green-50 border-jj-green-03' },
    [STATUS.CRITICAL]:           { text: 'Critical',            icon: xCircleIcon,      cls: 'text-jj-maroon-05 bg-red-50 border-jj-maroon-05' },
    [STATUS.ERROR]:              { text: 'Error',               icon: xCircleIcon,      cls: 'text-jj-maroon-05 bg-red-50 border-jj-maroon-05' },
    [STATUS.MATCH]:              { text: 'Match',               icon: checkCircleIcon,  cls: 'text-jj-green-03 bg-green-50 border-jj-green-03' },
    [STATUS.MISMATCH]:           { text: 'Mismatch',            icon: xCircleIcon,      cls: 'text-jj-maroon-05 bg-red-50 border-jj-maroon-05' },
    [STATUS.DEFINITION_MISMATCH]:{ text: 'Definition Mismatch', icon: alertTriangleIcon,cls: 'text-jj-orange-03 bg-orange-50 border-jj-orange-03' },
    [STATUS.NONE]:               { text: 'None',                icon: minusIcon,        cls: 'text-jj-gray-06 bg-jj-gray-01 border-jj-gray-04' },
    [STATUS.FAULTY]:             { text: 'Faulty',              icon: alertTriangleIcon,cls: 'text-jj-orange-03 bg-orange-50 border-jj-orange-03' },
    [STATUS.TIMEOUT]:            { text: 'Timeout',             icon: clockIcon,        cls: 'text-jj-orange-03 bg-orange-50 border-jj-orange-03' },
  };
  const c = configs[status] || { text: status || 'Unknown', icon: minusIcon, cls: 'text-jj-gray-06 bg-jj-gray-01 border-jj-gray-04' };
  return (
    <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-johnson-text font-bold border ${c.cls}`}>
      <Icon src={c.icon} alt={c.text} className={`w-3 h-3 ${c.cls.split(' ')[0]}`} />
      {c.text}
    </span>
  );
});
StatusBadge.displayName = 'StatusBadge';

// ─── Multi-Select Dropdown ────────────────────────────────────────────────────
const MultiSelectDropdown = memo(({ label, options, selected, onChange, disabled = false, placeholder = "Select..." }) => {
  const [isOpen, setIsOpen] = useState(false);
  const handleToggle = useCallback((value) => {
    onChange(prev => prev.includes(value) ? prev.filter(v => v !== value) : [...prev, value]);
  }, [onChange]);
  const handleSelectAll = useCallback(() => {
    onChange(selected.length === options.length ? [] : options);
  }, [onChange, selected.length, options]);

  const activeCount = selected.length;

  return (
    <div className="relative">
      <label className="font-johnson-text block text-xs font-bold mb-1.5 text-jj-gray-07">{label}</label>
      <button
        onClick={() => !disabled && setIsOpen(!isOpen)}
        disabled={disabled}
        aria-expanded={isOpen}
        aria-haspopup="listbox"
        className={`w-full px-3 py-2 rounded-lg text-sm font-johnson-text text-left flex items-center justify-between border transition-all duration-150 ${
          disabled
            ? 'bg-jj-gray-01 text-jj-gray-05 cursor-not-allowed border-jj-gray-02'
            : isOpen
              ? 'bg-white text-jj-gray-08 border-jj-red ring-2 ring-jj-red ring-opacity-20 cursor-pointer'
              : 'bg-white text-jj-gray-08 border-jj-gray-03 hover:border-jj-gray-05 cursor-pointer'
        }`}
      >
        <span className="truncate text-sm">
          {activeCount === 0
            ? <span className="text-jj-gray-05">{placeholder}</span>
            : activeCount === 1
              ? <span className="font-medium">{selected[0]}</span>
              : <span className="font-bold text-jj-red">{activeCount} selected</span>
          }
        </span>
        <Icon src={chevronDownIcon} alt="Toggle" className={`w-4 h-4 flex-shrink-0 ml-2 transition-transform duration-200 text-jj-gray-06 ${isOpen ? 'rotate-180' : ''}`} />
      </button>

      {isOpen && !disabled && (
        <>
          <div className="fixed inset-0 z-[60]" onClick={() => setIsOpen(false)} aria-hidden="true" />
          <div className="absolute z-[70] mt-1 w-full bg-white border border-jj-gray-03 rounded-lg shadow-xl max-h-56 overflow-y-auto">
            {options.length > 1 && (
              <div
                onClick={handleSelectAll}
                className="px-3 py-2 hover:bg-jj-gray-01 cursor-pointer border-b border-jj-gray-02 font-johnson-text text-xs font-bold text-jj-red sticky top-0 bg-white"
              >
                {selected.length === options.length ? '− Deselect All' : '+ Select All'}
              </div>
            )}
            {options.length === 0
              ? <div className="font-johnson-text px-3 py-3 text-xs text-jj-gray-05 text-center">No options available</div>
              : options.map(option => (
                  <div
                    key={option}
                    onClick={() => handleToggle(option)}
                    className={`px-3 py-2 cursor-pointer flex items-center gap-2.5 hover:bg-jj-gray-01 transition-colors ${selected.includes(option) ? 'bg-red-50' : ''}`}
                  >
                    <div className={`w-4 h-4 rounded border-2 flex items-center justify-center flex-shrink-0 transition-colors ${
                      selected.includes(option) ? 'bg-jj-red border-jj-red' : 'border-jj-gray-04'
                    }`}>
                      {selected.includes(option) && <Icon src={checkCircleIcon} alt="✓" className="w-3 h-3 text-white" />}
                    </div>
                    <span className="font-johnson-text text-sm text-jj-gray-08">{option}</span>
                  </div>
                ))
            }
          </div>
        </>
      )}
    </div>
  );
});
MultiSelectDropdown.displayName = 'MultiSelectDropdown';

// ─── Pagination Controls ──────────────────────────────────────────────────────
const PaginationControls = memo(({ currentPage, totalPages, pageSize, totalItems, onPageChange, onPageSizeChange, showPageSize = false }) => {
  const pages = useMemo(() => {
    const count = Math.min(5, totalPages);
    return [...Array(count)].map((_, i) => {
      if (totalPages <= 5) return i + 1;
      if (currentPage <= 3) return i + 1;
      if (currentPage >= totalPages - 2) return totalPages - 4 + i;
      return currentPage - 2 + i;
    });
  }, [currentPage, totalPages]);

  const btnBase = "px-3 py-1.5 rounded font-johnson-text text-xs font-bold transition-colors duration-150 disabled:opacity-40 disabled:cursor-not-allowed";
  const btnDefault = `${btnBase} bg-jj-gray-01 text-jj-gray-08 hover:bg-jj-gray-02 border border-jj-gray-03`;
  const btnActive = `${btnBase} bg-jj-red text-white border border-jj-red`;

  return (
    <div className="flex flex-col sm:flex-row items-center justify-between gap-3 py-3 px-4 bg-white rounded-lg border border-jj-gray-02">
      <div className="flex items-center gap-4">
        {showPageSize && (
          <div className="flex items-center gap-2">
            <span className="font-johnson-text text-xs text-jj-gray-06">Show</span>
            <select
              value={pageSize} onChange={onPageSizeChange}
              className="font-johnson-text px-2 py-1 rounded border border-jj-gray-03 text-xs text-jj-gray-08 bg-white outline-none focus:border-jj-red"
            >
              {[25, 50, 100, 200].map(n => <option key={n} value={n}>{n}</option>)}
            </select>
            <span className="font-johnson-text text-xs text-jj-gray-06">per page</span>
          </div>
        )}
        <p className="font-johnson-text text-xs text-jj-gray-06">
          Showing <span className="font-bold text-jj-gray-08">{((currentPage - 1) * pageSize) + 1}</span>–
          <span className="font-bold text-jj-gray-08">{Math.min(currentPage * pageSize, totalItems)}</span> of{' '}
          <span className="font-bold text-jj-gray-08">{totalItems}</span> tables
        </p>
      </div>
      <div className="flex items-center gap-1">
        <button onClick={() => onPageChange(1)} disabled={currentPage === 1} className={btnDefault}>First</button>
        <button onClick={() => onPageChange(currentPage - 1)} disabled={currentPage === 1} className={btnDefault}>
          <Icon src={chevronLeftIcon} alt="Prev" className="w-3.5 h-3.5 text-jj-gray-08" />
        </button>
        {pages.map(p => (
          <button key={p} onClick={() => onPageChange(p)} className={p === currentPage ? btnActive : btnDefault}>{p}</button>
        ))}
        <button onClick={() => onPageChange(currentPage + 1)} disabled={currentPage === totalPages} className={btnDefault}>
          <Icon src={chevronRightIcon} alt="Next" className="w-3.5 h-3.5 text-jj-gray-08" />
        </button>
        <button onClick={() => onPageChange(totalPages)} disabled={currentPage === totalPages} className={btnDefault}>Last</button>
      </div>
    </div>
  );
});
PaginationControls.displayName = 'PaginationControls';

// ─── Column-Only Section ──────────────────────────────────────────────────────
const ColumnOnlySection = memo(({ title, columns, bgColor, borderColor, textColor, icon: iconSrc }) => {
  if (!columns || columns.length === 0) return null;
  return (
    <div className={`${bgColor} rounded-lg p-4 border-l-4 ${borderColor}`}>
      <h4 className={`font-johnson-display text-base mb-3 flex items-center gap-2 ${textColor}`} style={{ lineHeight: '110%' }}>
        <Icon src={iconSrc} alt={title} className={`w-4 h-4 ${textColor}`} />
        {title} <span className="font-johnson-text text-xs opacity-70">({columns.length})</span>
      </h4>
      <div className="flex flex-wrap gap-2">
        {columns.map((col, idx) => (
          <span key={idx} className={`px-2.5 py-1 rounded font-johnson-text text-xs font-bold ${textColor} bg-white bg-opacity-60 border border-current border-opacity-30`}>
            {col}
          </span>
        ))}
      </div>
    </div>
  );
});
ColumnOnlySection.displayName = 'ColumnOnlySection';

// ─── Quality Analysis Card ────────────────────────────────────────────────────
const QualityAnalysisCard = memo(({ title, match, sourceLabel, sourceValue, targetLabel, targetValue }) => (
  <div className={`bg-white rounded-xl p-6 shadow-sm border border-jj-gray-02 border-l-4 ${match ? 'border-l-jj-green-03' : 'border-l-jj-maroon-05'}`}
    style={{ borderLeftColor: match ? '#328714' : '#9e0000' }}>
    <div className="flex items-center justify-between mb-4">
      <h3 className="font-johnson-display text-base text-jj-gray-08 flex items-center gap-2" style={{ lineHeight: '110%' }}>
        <Icon src={barChartIcon} alt="Chart" className="w-4 h-4 text-jj-gray-06" />
        {title}
      </h3>
      <StatusBadge status={match ? STATUS.MATCH : STATUS.MISMATCH} />
    </div>
    <div className="grid grid-cols-3 gap-4">
      {[
        { label: sourceLabel, value: sourceValue?.toLocaleString() || '0' },
        { label: targetLabel, value: targetValue?.toLocaleString() || '0' },
        {
          label: 'Match',
          value: match
            ? <span className="flex items-center gap-1.5 text-jj-green-03"><Icon src={checkCircleIcon} alt="Yes" className="w-4 h-4 text-jj-green-03" />Yes</span>
            : <span className="flex items-center gap-1.5 text-jj-maroon-05"><Icon src={xCircleIcon} alt="No" className="w-4 h-4 text-jj-maroon-05" />No</span>
        }
      ].map((item, i) => (
        <div key={i}>
          <p className="font-johnson-text text-xs text-jj-gray-06 mb-1">{item.label}</p>
          <p className="font-johnson-display text-2xl text-jj-gray-08" style={{ lineHeight: '110%' }}>{item.value}</p>
        </div>
      ))}
    </div>
  </div>
));
QualityAnalysisCard.displayName = 'QualityAnalysisCard';

// ─── Table Row ────────────────────────────────────────────────────────────────
const TableRow = memo(({ table, onSelect, onRefresh, refreshingTable, canRefresh }) => {
  const tableKey = `${table.pipeline_name}_${table.table_name}`;
  const isRefreshing = refreshingTable === tableKey;

  const handleClick = useCallback(() => onSelect(table), [table, onSelect]);
  const handleDetails = useCallback((e) => { e.stopPropagation(); onSelect(table); }, [table, onSelect]);
  const handleRefresh = useCallback((e) => { e.stopPropagation(); onRefresh(table, e); }, [table, onRefresh]);

  const rowMatch = table.row_counts?.match;

  return (
    <tr onClick={handleClick} className="border-t border-jj-gray-02 hover:bg-jj-gray-01 transition-colors cursor-pointer group">
      <td className="px-4 py-3 font-johnson-text">
        <div className="text-sm font-bold text-jj-gray-08 truncate max-w-[200px]" title={table.table_name}>
          {table.table_name}
        </div>
        {table.source_schema && (
          <div className="text-xs text-jj-gray-05 truncate max-w-[200px] mt-0.5" title={table.source_schema}>
            {table.source_schema}
          </div>
        )}
      </td>
      <td className="px-4 py-3 font-johnson-text">
        <div className="text-xs text-jj-gray-06 truncate max-w-[200px]" title={table.snowflake_table}>
          {table.snowflake_table}
        </div>
        {table.target_schema && (
          <div className="text-xs text-jj-gray-05 truncate max-w-[200px] mt-0.5" title={`${table.target_database ? table.target_database + '.' : ''}${table.target_schema}`}>
            {table.target_database ? `${table.target_database}.${table.target_schema}` : table.target_schema}
          </div>
        )}
      </td>
      <td className="px-4 py-3 text-center">
        <StatusBadge status={table.status} />
      </td>
      <td className="px-4 py-3 text-center">
        <span className={`font-johnson-text text-sm font-bold ${rowMatch ? 'text-jj-green-03' : 'text-jj-maroon-05'}`}>
          {table.row_counts?.oracle?.toLocaleString() || 0}
        </span>
      </td>
      <td className="px-4 py-3 text-center">
        <span className={`font-johnson-text text-sm font-bold ${rowMatch ? 'text-jj-green-03' : 'text-jj-maroon-05'}`}>
          {table.row_counts?.snowflake?.toLocaleString() || 0}
        </span>
      </td>
      <td className="px-4 py-3 text-center">
        <span className="font-johnson-text text-xs text-jj-gray-06">
          {table.Last_Checked ? formatTimestamp(table.Last_Checked) : '—'}
        </span>
      </td>
      <td className="px-4 py-3">
        <div className="flex items-center justify-center gap-2">
          {canRefresh === 'active' ? (
            <button
              onClick={handleRefresh}
              disabled={isRefreshing}
              title={isRefreshing ? 'Validating…' : 'Re-validate table'}
              className="p-2 rounded-lg border border-jj-gray-03 text-jj-gray-06 hover:border-jj-green-03 hover:text-jj-green-03 hover:bg-green-50 transition-all duration-150 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <Icon src={refreshIcon} alt="Refresh" className={`w-4 h-4 ${isRefreshing ? 'animate-spin text-jj-green-03' : 'text-jj-gray-06'}`} />
            </button>
          ) : canRefresh === 'no-connection' ? (
            <button
              disabled
              title="No Oracle connection configured for this database"
              className="p-2 rounded-lg border border-jj-gray-02 text-jj-gray-04 cursor-not-allowed opacity-40"
            >
              <Icon src={refreshIcon} alt="No connection" className="w-4 h-4 text-jj-gray-04" />
            </button>
          ) : null}
          <button
            onClick={handleDetails}
            className="px-3 py-1.5 rounded-lg font-johnson-text text-xs font-bold text-white bg-jj-red hover:bg-jj-maroon-05 transition-colors duration-150"
          >
            Details
          </button>
        </div>
      </td>
    </tr>
  );
}, (prev, next) =>
  prev.table.table_name === next.table.table_name &&
  prev.table.status === next.table.status &&
  prev.table.row_counts?.match === next.table.row_counts?.match &&
  prev.refreshingTable === next.refreshingTable &&
  prev.canRefresh === next.canRefresh
);
TableRow.displayName = 'TableRow';

// ─── Column Details Section ───────────────────────────────────────────────────
const ColumnDetailsSection = memo(({ tables, selectedSourceColumns, selectedTargetColumns }) => {
  const [sortConfig, setSortConfig] = useState({ key: null, direction: 'asc' });

  const columnStats = useMemo(() => {
    try {
      const stats = {};
      tables.forEach(table => {
        if (!table.column_statistics) return;
        table.column_statistics.forEach(col => {
          const name = col.column_name;
          if (!selectedSourceColumns.includes(name) && !selectedTargetColumns.includes(name)) return;
          if (!stats[name]) stats[name] = {
            column_name: name, occurrences: 0,
            source_types: new Set(), target_types: new Set(),
            total_source_distinct: 0, total_target_distinct: 0,
            total_source_nulls: 0, total_target_nulls: 0, tables: []
          };
          stats[name].occurrences++;
          if (col.oracle_type) stats[name].source_types.add(col.oracle_type);
          if (col.snowflake_type) stats[name].target_types.add(col.snowflake_type);
          stats[name].tables.push(table.table_name || 'Unknown');
          if (col.oracle_distinct_count) stats[name].total_source_distinct += col.oracle_distinct_count;
          if (col.snowflake_distinct_count) stats[name].total_target_distinct += col.snowflake_distinct_count;
          if (col.oracle_null_count) stats[name].total_source_nulls += col.oracle_null_count;
          if (col.snowflake_null_count) stats[name].total_target_nulls += col.snowflake_null_count;
        });
      });
      return Object.values(stats);
    } catch { return []; }
  }, [tables, selectedSourceColumns, selectedTargetColumns]);

  const sorted = useMemo(() => {
    if (!sortConfig.key) return columnStats;
    return [...columnStats].sort((a, b) => {
      let av = a[sortConfig.key], bv = b[sortConfig.key];
      if (av instanceof Set) av = av.size;
      if (bv instanceof Set) bv = bv.size;
      return sortConfig.direction === 'asc' ? (av < bv ? -1 : av > bv ? 1 : 0) : (av > bv ? -1 : av < bv ? 1 : 0);
    });
  }, [columnStats, sortConfig]);

  const handleSort = useCallback((key) => {
    setSortConfig(prev => ({ key, direction: prev.key === key && prev.direction === 'asc' ? 'desc' : 'asc' }));
  }, []);

  if (selectedSourceColumns.length === 0 && selectedTargetColumns.length === 0) return null;
  if (columnStats.length === 0) return (
    <div className="bg-blue-50 rounded-xl p-6 border border-jj-blue-03 border-opacity-30 mb-3 text-center">
      <Icon src={infoIcon} alt="Info" className="w-10 h-10 mx-auto mb-3 text-jj-blue-03" />
      <p className="font-johnson-text text-sm font-bold text-jj-gray-08">Selected columns not found in current filtered tables</p>
    </div>
  );

  const SortableHeader = ({ label, sortKey }) => (
    <th onClick={() => handleSort(sortKey)}
      className="px-4 py-3 text-left text-xs font-johnson-text font-bold text-jj-gray-08 bg-jj-gray-01 cursor-pointer hover:bg-jj-gray-02 transition-colors select-none">
      <span className="flex items-center gap-1.5">
        {label}
        <Icon src={arrowUpDownIcon} alt="Sort"
          className={`w-3 h-3 ${sortConfig.key === sortKey ? 'text-jj-red' : 'text-jj-gray-05'}`} />
      </span>
    </th>
  );

  return (
    <div className="bg-white rounded-xl shadow-sm border border-jj-gray-02 overflow-hidden mb-3">
      <div className="px-5 py-3 bg-red-50 border-b border-jj-gray-02 flex items-center gap-2">
        <Icon src={barChartIcon} alt="Chart" className="w-4 h-4 text-jj-red" />
        <h3 className="font-johnson-display text-base text-jj-red" style={{ lineHeight: '110%' }}>
          Column Details — {columnStats.length} column{columnStats.length !== 1 ? 's' : ''}
        </h3>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr>
              <SortableHeader label="Column Name" sortKey="column_name" />
              <th className="px-4 py-3 text-left text-xs font-johnson-text font-bold text-jj-gray-08 bg-jj-gray-01">Source Type(s)</th>
              <th className="px-4 py-3 text-left text-xs font-johnson-text font-bold text-jj-gray-08 bg-jj-gray-01">Target Type(s)</th>
              <SortableHeader label="Src Distinct" sortKey="total_source_distinct" />
              <SortableHeader label="Tgt Distinct" sortKey="total_target_distinct" />
              <SortableHeader label="Src Nulls" sortKey="total_source_nulls" />
              <SortableHeader label="Tgt Nulls" sortKey="total_target_nulls" />
              <SortableHeader label="In Tables" sortKey="occurrences" />
            </tr>
          </thead>
          <tbody>
            {sorted.map(stat => (
              <tr key={stat.column_name} className="border-t border-jj-gray-02 hover:bg-jj-gray-01 transition-colors">
                <td className="px-4 py-3 font-johnson-text font-bold text-jj-gray-08 text-sm">{stat.column_name}</td>
                <td className="px-4 py-3 font-johnson-text text-xs text-jj-gray-07">{Array.from(stat.source_types).join(', ') || '—'}</td>
                <td className="px-4 py-3 font-johnson-text text-xs text-jj-gray-07">{Array.from(stat.target_types).join(', ') || '—'}</td>
                <td className="px-4 py-3 font-johnson-text text-sm font-bold text-jj-gray-08 text-center">{stat.total_source_distinct.toLocaleString()}</td>
                <td className="px-4 py-3 font-johnson-text text-sm font-bold text-jj-gray-08 text-center">{stat.total_target_distinct.toLocaleString()}</td>
                <td className="px-4 py-3 font-johnson-text text-sm font-bold text-jj-gray-08 text-center">{stat.total_source_nulls.toLocaleString()}</td>
                <td className="px-4 py-3 font-johnson-text text-sm font-bold text-jj-gray-08 text-center">{stat.total_target_nulls.toLocaleString()}</td>
                <td className="px-4 py-3 text-center">
                  <span className="font-johnson-text text-xs font-bold px-2 py-0.5 rounded-full bg-jj-blue-03 bg-opacity-10 text-jj-blue-03">
                    {stat.occurrences}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
});
ColumnDetailsSection.displayName = 'ColumnDetailsSection';

// ─── Table Detail Modal ───────────────────────────────────────────────────────
const TableDetailModal = memo(({ table, onClose }) => {
  const [activeTab, setActiveTab] = useState('overview');
  const [constraintWarningsExpanded, setConstraintWarningsExpanded] = useState(false);

  useEffect(() => {
    const handler = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [onClose]);

  if (!table) return null;
  const safeTableName = table.table_name || 'Unknown Table';

  const TABS = ['overview', 'columns', 'quality', 'constraints', 'issues'];

  return (
    <div
      className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4"
      onClick={onClose}
      style={{ backdropFilter: 'blur(4px)' }}
      role="dialog" aria-modal="true" aria-labelledby="modal-title"
    >
      <div
        className="bg-white rounded-2xl shadow-2xl max-w-7xl w-full flex flex-col"
        onClick={e => e.stopPropagation()}
        style={{ maxHeight: '92vh', animation: 'slideUp 0.25s ease-out' }}
      >
        <style>{`@keyframes slideUp { from { opacity:0; transform:translateY(16px); } to { opacity:1; transform:translateY(0); } }`}</style>

        {/* Modal Header */}
        <div className="flex items-center justify-between px-6 py-4 bg-jj-gray-07 rounded-t-2xl flex-shrink-0">
          <div className="flex-1 min-w-0 mr-4">
            <div className="flex items-center gap-4 mb-2 flex-wrap">
              {/* Source */}
              <div className="flex flex-col min-w-0">
                <span className="font-johnson-text text-xs text-white text-opacity-70 uppercase tracking-wider mb-0.5">Source</span>
                <span className="font-johnson-display text-lg text-white font-bold truncate" title={safeTableName} style={{ lineHeight: '110%' }}>
                  {safeTableName}
                </span>
                {table.source_schema && (
                  <span className="font-johnson-text text-xs text-white text-opacity-50 mt-0.5 truncate" title={table.source_schema}>
                    {table.source_schema}
                  </span>
                )}
              </div>

              <span className="text-white text-opacity-40 text-xl flex-shrink-0 self-center">→</span>

              {/* Target */}
              <div className="flex flex-col min-w-0">
                <span className="font-johnson-text text-xs text-white text-opacity-70 uppercase tracking-wider mb-0.5">Snowflake</span>
                <span className="font-johnson-display text-base text-white truncate" title={table.snowflake_table} style={{ lineHeight: '110%' }}>
                  {table.snowflake_table || '—'}
                </span>
                {(table.target_database || table.target_schema) && (
                  <span className="font-johnson-text text-xs text-white text-opacity-50 mt-0.5 truncate"
                    title={[table.target_database, table.target_schema].filter(Boolean).join('.')}>
                    {[table.target_database, table.target_schema].filter(Boolean).join('.')}
                  </span>
                )}
              </div>
            </div>
            <div className="flex items-center gap-3 flex-wrap">
              <StatusBadge status={table.status} />
              {table.Last_Checked && (
                <span className="inline-flex items-center gap-1.5 font-johnson-text text-xs text-white text-opacity-80">
                  <Icon src={clockIcon} alt="Time" className="w-3.5 h-3.5 text-white" />
                  {formatTimestamp(table.Last_Checked)}
                </span>
              )}
            </div>
          </div>
          <button onClick={onClose} aria-label="Close"
            className="w-9 h-9 flex items-center justify-center rounded-lg bg-white bg-opacity-20 hover:bg-opacity-30 transition-colors flex-shrink-0">
            <Icon src={xCircleIcon} alt="Close" className="w-5 h-5 text-white" />
          </button>
        </div>

        {/* Tabs */}
        <div className="flex border-b border-jj-gray-02 flex-shrink-0 overflow-x-auto bg-white">
          {TABS.map(tab => (
            <button
              key={tab}
              onClick={() => setActiveTab(tab)}
              className={`px-6 py-3 font-johnson-text text-sm whitespace-nowrap transition-colors duration-150 relative ${
                activeTab === tab
                  ? 'text-jj-red font-bold'
                  : 'text-jj-gray-06 hover:text-jj-gray-08 hover:bg-jj-gray-01'
              }`}
            >
              {tab.charAt(0).toUpperCase() + tab.slice(1)}
              {activeTab === tab && <span className="absolute bottom-0 left-0 right-0 h-0.5 bg-jj-red rounded-t" />}
            </button>
          ))}
        </div>

        {/* Scrollable Body */}
        <div className="flex-1 overflow-y-auto bg-jj-gray-01 p-6 rounded-xl">

          {/* ── Overview ── */}
          {activeTab === 'overview' && (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              {/* Row Counts */}
              <div className="bg-white rounded-xl p-5 shadow-sm border border-jj-gray-02 border-l-4" style={{ borderLeftColor: '#0f68b2' }}>
                <h3 className="font-johnson-display text-base mb-4 flex items-center gap-2 text-jj-gray-08" style={{ lineHeight: '110%' }}>
                  <Icon src={databaseIcon} alt="DB" className="w-4 h-4 text-jj-blue-03" />
                  Row Counts
                </h3>
                <div className="space-y-3">
                  {[
                    { label: 'Source rows', value: table.row_counts?.oracle?.toLocaleString() || '0' },
                    { label: 'Snowflake rows', value: table.row_counts?.snowflake?.toLocaleString() || '0' },
                  ].map(({ label, value }) => (
                    <div key={label} className="flex justify-between items-center">
                      <span className="font-johnson-text text-sm text-jj-gray-06">{label}</span>
                      <span className="font-johnson-display text-2xl text-jj-gray-08" style={{ lineHeight: '110%' }}>{value}</span>
                    </div>
                  ))}
                  <div className="pt-3 border-t border-jj-gray-02 flex justify-between items-center">
                    <span className="font-johnson-text text-sm text-jj-gray-06">Match</span>
                    <span className={`font-johnson-text text-sm font-bold flex items-center gap-1.5 ${table.row_counts?.match ? 'text-jj-green-03' : 'text-jj-maroon-05'}`}>
                      <Icon src={table.row_counts?.match ? checkCircleIcon : xCircleIcon} alt="match"
                        className={`w-4 h-4 ${table.row_counts?.match ? 'text-jj-green-03' : 'text-jj-maroon-05'}`} />
                      {table.row_counts?.match ? 'Yes' : 'No'}
                    </span>
                  </div>
                </div>
              </div>

              {/* Data Quality */}
              <div className="bg-white rounded-xl p-5 shadow-sm border border-jj-gray-02 border-l-4" style={{ borderLeftColor: '#328714' }}>
                <h3 className="font-johnson-display text-base mb-4 flex items-center gap-2 text-jj-gray-08" style={{ lineHeight: '110%' }}>
                  <Icon src={barChartIcon} alt="Chart" className="w-4 h-4 text-jj-green-03" />
                  Data Quality
                </h3>
                <div className="space-y-3">
                  {[
                    { label: 'Source nulls', value: table.data_quality?.oracle_nulls?.toLocaleString() || '0' },
                    { label: 'Snowflake nulls', value: table.data_quality?.snowflake_nulls?.toLocaleString() || '0' },
                  ].map(({ label, value }) => (
                    <div key={label} className="flex justify-between items-center">
                      <span className="font-johnson-text text-sm text-jj-gray-06">{label}</span>
                      <span className="font-johnson-display text-2xl text-jj-gray-08" style={{ lineHeight: '110%' }}>{value}</span>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}

          {/* ── Columns ── */}
          {activeTab === 'columns' && (
            <div className="space-y-4">
              <div className="bg-white rounded-xl p-5 shadow-sm border border-jj-gray-02">
                <h3 className="font-johnson-display text-base text-jj-gray-08 mb-4" style={{ lineHeight: '110%' }}>Column Analysis</h3>
                <div className="grid grid-cols-3 gap-4">
                  {[
                    { label: 'Source columns', value: table.column_analysis?.oracle_columns || 0, color: 'text-jj-blue-03' },
                    { label: 'Snowflake columns', value: table.column_analysis?.snowflake_columns || 0, color: 'text-jj-blue-03' },
                    { label: 'Common columns', value: table.column_analysis?.common_columns || 0, color: 'text-jj-green-03' },
                  ].map(item => (
                    <div key={item.label} className="text-center p-4 rounded-lg bg-jj-gray-01">
                      <div className={`font-johnson-display text-4xl font-bold mb-1 ${item.color}`} style={{ lineHeight: '110%' }}>{item.value}</div>
                      <div className="font-johnson-text text-xs text-jj-gray-06">{item.label}</div>
                    </div>
                  ))}
                </div>
              </div>

              {table.column_statistics?.length > 0 && (
                <div className="bg-white rounded-xl shadow-sm border border-jj-gray-02 overflow-hidden">
                  <div className="overflow-x-auto">
                    <table className="w-full text-sm table-fixed">
                      <thead>
                        <tr>
                          {['Column Name', 'Source Type', 'Snowflake Type', 'Src Distinct', 'Tgt Distinct', 'Src Nulls', 'Tgt Nulls'].map((h, i) => (
                            <th key={h} className={`px-4 py-3 text-xs font-johnson-text font-bold text-jj-gray-08 bg-jj-gray-01 ${i < 3 ? 'text-left' : 'text-center'}`}>
                              {h}
                            </th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {table.column_statistics.map((col, idx) => {
                          const od = col.oracle_stats?.distinct_count ?? col.oracle_distinct_count ?? 0;
                          const sd = col.snowflake_stats?.distinct_count ?? col.snowflake_distinct_count ?? 0;
                          const on_ = col.oracle_stats?.null_count ?? col.oracle_null_count ?? 0;
                          const sn = col.snowflake_stats?.null_count ?? col.snowflake_null_count ?? 0;
                          const dm = od === sd, nm = on_ === sn;
                          return (
                            <tr key={idx} className="border-t border-jj-gray-02 hover:bg-jj-gray-01 transition-colors">
                              <td className="px-4 py-3 font-johnson-text font-bold text-jj-gray-08 text-sm truncate" title={col.column_name}>{col.column_name}</td>
                              <td className="px-4 py-3 font-johnson-text text-xs text-jj-gray-07 truncate">{col.oracle_type || '—'}</td>
                              <td className="px-4 py-3 font-johnson-text text-xs text-jj-gray-07 truncate">{col.snowflake_type || '—'}</td>
                              <td className="px-4 py-3 text-center"><span className={`font-johnson-text text-sm font-bold ${dm ? 'text-jj-green-03' : 'text-jj-maroon-05'}`}>{od.toLocaleString()}</span></td>
                              <td className="px-4 py-3 text-center"><span className={`font-johnson-text text-sm font-bold ${dm ? 'text-jj-green-03' : 'text-jj-maroon-05'}`}>{sd.toLocaleString()}</span></td>
                              <td className="px-4 py-3 text-center"><span className={`font-johnson-text text-sm font-bold ${nm ? 'text-jj-green-03' : 'text-jj-maroon-05'}`}>{on_.toLocaleString()}</span></td>
                              <td className="px-4 py-3 text-center"><span className={`font-johnson-text text-sm font-bold ${nm ? 'text-jj-green-03' : 'text-jj-maroon-05'}`}>{sn.toLocaleString()}</span></td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}

              <ColumnOnlySection title="Source-Only Columns" columns={table.column_analysis?.oracle_only_columns}
                bgColor="bg-orange-50" borderColor="border-jj-orange-03" textColor="text-jj-orange-03" icon={alertTriangleIcon} />
              <ColumnOnlySection title="Snowflake-Only Columns" columns={table.column_analysis?.snowflake_only_columns}
                bgColor="bg-blue-50" borderColor="border-jj-blue-03" textColor="text-jj-blue-03" icon={infoIcon} />
            </div>
          )}

          {/* ── Quality ── */}
          {activeTab === 'quality' && (
            <div className="space-y-4">
              {table.data_quality ? (
                <>
                  <QualityAnalysisCard title="Null Count Analysis" match={table.data_quality.null_match}
                    sourceLabel="Source Nulls" sourceValue={table.data_quality.oracle_nulls}
                    targetLabel="Snowflake Nulls" targetValue={table.data_quality.snowflake_nulls} />
                  <QualityAnalysisCard title="Duplicate Count Analysis" match={table.data_quality.duplicate_match}
                    sourceLabel="Source Duplicates" sourceValue={table.data_quality.oracle_duplicates}
                    targetLabel="Snowflake Duplicates" targetValue={table.data_quality.snowflake_duplicates} />
                </>
              ) : (
                <div className="text-center py-16 bg-white rounded-xl shadow-sm border border-jj-gray-02">
                  <Icon src={barChartIcon} alt="Chart" className="w-16 h-16 mx-auto mb-4 text-jj-gray-04 opacity-40" />
                  <p className="font-johnson-text font-bold text-jj-gray-06">No data quality information available</p>
                </div>
              )}
            </div>
          )}

          {/* ── Constraints ── */}
          {activeTab === 'constraints' && (
            <div className="space-y-4">
              {table.constraint_analysis ? (
                <>
                  {table.constraint_analysis.summary && (
                    <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
                      {Object.entries(table.constraint_analysis.summary).map(([type, status]) => {
                        const borderMap = { [STATUS.MATCH]: '#328714', [STATUS.MISMATCH]: '#9e0000', [STATUS.DEFINITION_MISMATCH]: '#ff6017' };
                        return (
                          <div key={type} className="bg-white rounded-xl p-4 shadow-sm border border-jj-gray-02 border-l-4"
                            style={{ borderLeftColor: borderMap[status] || '#a39992' }}>
                            <p className="font-johnson-text text-xs font-bold mb-2 text-jj-gray-07">{type.replace(/_/g, ' ')}</p>
                            <StatusBadge status={status} />
                          </div>
                        );
                      })}
                    </div>
                  )}

                  {table.constraint_analysis.constraint_details && (
                    <div className="bg-white rounded-xl shadow-sm border border-jj-gray-02 overflow-hidden">
                      <div className="grid grid-cols-1 lg:grid-cols-2 divide-y lg:divide-y-0 lg:divide-x divide-jj-gray-02">
                        {[
                          { label: 'Source Constraints', data: table.constraint_analysis.constraint_details.oracle, accentColor: 'text-jj-blue-03' },
                          { label: 'Snowflake Constraints', data: table.constraint_analysis.constraint_details.snowflake, accentColor: 'text-jj-red' },
                        ].map(({ label, data, accentColor }) => (
                          <div key={label} className="p-5">
                            <h3 className={`font-johnson-display text-base mb-4 flex items-center gap-2 ${accentColor}`} style={{ lineHeight: '110%' }}>
                              <Icon src={databaseIcon} alt="DB" className={`w-4 h-4 ${accentColor}`} /> {label}
                            </h3>
                            {[
                              { name: 'Primary Keys', content: data.primary_keys?.length > 0
                                ? <div className="flex flex-wrap gap-1.5">{data.primary_keys.map((k,i) => <span key={i} className="px-2 py-0.5 rounded font-johnson-text text-xs font-bold text-jj-blue-03 bg-blue-50 border border-jj-blue-03 border-opacity-20">{k}</span>)}</div>
                                : <span className="font-johnson-text text-xs text-jj-gray-05">None</span>
                              },
                              { name: 'Unique Constraints', content: <span className="font-johnson-text text-xs text-jj-gray-08">{data.unique_constraints?.length || 0} constraint(s)</span> },
                              { name: 'NOT NULL Columns', content: data.not_null_columns?.length > 0
                                ? <div className="flex flex-wrap gap-1.5">{data.not_null_columns.map((c,i) => <span key={i} className="px-2 py-0.5 rounded font-johnson-text text-xs font-bold text-jj-green-03 bg-green-50">{c}</span>)}</div>
                                : <span className="font-johnson-text text-xs text-jj-gray-05">None</span>
                              },
                              { name: 'Foreign Keys', content: <span className="font-johnson-text text-xs text-jj-gray-08">{data.foreign_keys?.length || 0} key(s)</span> },
                            ].map(item => (
                              <div key={item.name} className="mb-4">
                                <h4 className="font-johnson-text text-xs font-bold text-jj-gray-07 mb-1.5">{item.name}</h4>
                                {item.content}
                              </div>
                            ))}
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {table.constraint_analysis.validation_results?.not_null_constraints?.details && (
                    <div className="bg-white rounded-xl shadow-sm border border-jj-gray-02 overflow-hidden">
                      <div className="flex items-center justify-between px-5 py-3 border-b border-jj-gray-02">
                        <h3 className="font-johnson-display text-base text-jj-gray-08 flex items-center gap-2" style={{ lineHeight: '110%' }}>
                          <Icon src={shieldCheckIcon} alt="Shield" className="w-4 h-4 text-jj-red" />
                          NOT NULL Constraint Validation
                        </h3>
                        <StatusBadge status={table.constraint_analysis.validation_results.not_null_constraints.status} />
                      </div>
                      <div className="overflow-x-auto">
                        <table className="w-full text-sm">
                          <thead>
                            <tr>
                              {['Column Name', 'Snowflake Null Count', 'Status'].map((h, i) => (
                                <th key={h} className={`px-4 py-3 text-xs font-johnson-text font-bold text-jj-gray-08 bg-jj-gray-01 ${i === 0 ? 'text-left' : 'text-center'}`}>{h}</th>
                              ))}
                            </tr>
                          </thead>
                          <tbody>
                            {table.constraint_analysis.validation_results.not_null_constraints.details.map((d, i) => (
                              <tr key={i} className="border-t border-jj-gray-02 hover:bg-jj-gray-01 transition-colors">
                                <td className="px-4 py-3 font-johnson-text font-bold text-jj-gray-08">{d.column}</td>
                                <td className="px-4 py-3 text-center">
                                  <span className={`font-johnson-text text-sm font-bold px-2 py-0.5 rounded-full ${d.snowflake_null_count > 0 ? 'text-jj-maroon-05 bg-red-50' : 'text-jj-green-03 bg-green-50'}`}>
                                    {d.snowflake_null_count?.toLocaleString() || 0}
                                  </span>
                                </td>
                                <td className="px-4 py-3 text-center">{d.validation_status && <StatusBadge status={d.validation_status} />}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </div>
                  )}

                  {table.constraint_analysis.constraint_warnings?.length > 0 && (
                    <div className="bg-orange-50 rounded-xl border border-jj-orange-03 border-opacity-30 overflow-hidden">
                      <button
                        onClick={() => setConstraintWarningsExpanded(!constraintWarningsExpanded)}
                        className="w-full px-5 py-3 flex items-center justify-between hover:bg-orange-100 transition-colors"
                      >
                        <div className="flex items-center gap-2">
                          <Icon src={alertTriangleIcon} alt="Warning" className="w-4 h-4 text-jj-orange-03" />
                          <span className="font-johnson-text font-bold text-sm text-jj-orange-03">
                            Constraint Warnings ({table.constraint_analysis.constraint_warnings.length})
                          </span>
                        </div>
                        <Icon src={constraintWarningsExpanded ? chevronDownIcon : chevronRightIcon} alt="Toggle" className="w-4 h-4 text-jj-orange-03" />
                      </button>
                      {constraintWarningsExpanded && (
                        <div className="px-5 pb-4 space-y-2">
                          {table.constraint_analysis.constraint_warnings.map((w, i) => (
                            <div key={i} className="bg-white p-3 rounded-lg flex items-start gap-2">
                              <Icon src={alertTriangleIcon} alt="Warning" className="w-4 h-4 mt-0.5 flex-shrink-0 text-jj-orange-03" />
                              <p className="font-johnson-text text-sm text-jj-gray-08">{w}</p>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
                </>
              ) : (
                <div className="text-center py-16 bg-white rounded-xl shadow-sm border border-jj-gray-02">
                  <Icon src={shieldCheckIcon} alt="Shield" className="w-16 h-16 mx-auto mb-4 text-jj-gray-04 opacity-40" />
                  <p className="font-johnson-text font-bold text-jj-gray-06">No constraint analysis available</p>
                </div>
              )}
            </div>
          )}

          {/* ── Issues ── */}
          {activeTab === 'issues' && (
            <div className="space-y-2">
              {table.validation_issues?.length > 0
                ? table.validation_issues.map((issue, idx) => (
                    <div key={idx} className="bg-red-50 rounded-lg p-4 flex items-start gap-3 border border-jj-maroon-05 border-opacity-20 border-l-4"
                      style={{ borderLeftColor: '#9e0000' }}>
                      <Icon src={xCircleIcon} alt="Error" className="w-4 h-4 mt-0.5 flex-shrink-0 text-jj-maroon-05" />
                      <p className="font-johnson-text text-sm text-jj-gray-08">{issue}</p>
                    </div>
                  ))
                : (
                    <div className="text-center py-16 bg-white rounded-xl shadow-sm border border-jj-gray-02">
                      <div className="w-16 h-16 rounded-full bg-green-50 flex items-center justify-center mx-auto mb-4">
                        <Icon src={checkCircleIcon} alt="OK" className="w-10 h-10 text-jj-green-03" />
                      </div>
                      <p className="font-johnson-display text-xl text-jj-green-03 mb-1" style={{ lineHeight: '110%' }}>No Issues Found</p>
                      <p className="font-johnson-text text-sm text-jj-gray-06">This table validation completed successfully</p>
                    </div>
                  )
              }
            </div>
          )}
        </div>
      </div>
    </div>
  );
});
TableDetailModal.displayName = 'TableDetailModal';

// ─── URL param allowlists ─────────────────────────────────────────────────────
const VALID_CHECKS = ['IT_Checks', 'Business_defined_checks'];

// ═══════════════════════════════════════════════════════════════════════════════
// IT Validation — Database Selector Modal
// ═══════════════════════════════════════════════════════════════════════════════
function ITValidationSelectorModal({ onConfirm, onClose }) {
  const [selected, setSelected] = useState({ insight7: false, rimdocs: false });
  const noneSelected = !selected.insight7 && !selected.rimdocs;

  const toggle = (db) => setSelected(prev => ({ ...prev, [db]: !prev[db] }));

  const handleConfirm = () => {
    const dbs = [];
    if (selected.insight7) dbs.push('insight7');
    if (selected.rimdocs)  dbs.push('rimdocs');
    onConfirm(dbs);
  };

  const DB_OPTIONS = [
    { id: 'insight7', label: 'Insight7',  desc: 'Oracle → Snowflake validation for Insight7 database' },
    { id: 'rimdocs',  label: 'RIMDocs',   desc: 'Oracle → Snowflake validation for RIMDocs database'  },
  ];

  return (
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4"
         onClick={onClose} style={{ backdropFilter: 'blur(3px)' }}>
      <div className="bg-white rounded-2xl shadow-2xl border border-jj-gray-02 w-full max-w-md overflow-hidden"
           onClick={e => e.stopPropagation()}>

        {/* Header */}
        <div className="px-6 py-4 bg-white border-b-4 border-jj-green-03 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <Icon src={databaseIcon} alt="" className="w-5 h-5" color="jj-green-03" />
            <h2 className="font-johnson-display text-lg text-jj-gray-08" style={{ lineHeight: '110%' }}>
              Select Database(s) to Validate
            </h2>
          </div>
          <button onClick={onClose}
            className="w-8 h-8 flex items-center justify-center rounded-full hover:bg-jj-gray-02 transition-colors border border-jj-gray-03">
            <Icon src={xCircleIcon} alt="Close" className="w-4 h-4" color="jj-gray-08" />
          </button>
        </div>

        {/* Options */}
        <div className="p-6 space-y-3">

          {/* Row above Insight7: hint text left, Both pill right */}
          <div className="flex items-center justify-between">
            <span className="font-johnson-text text-xs text-jj-gray-05">Choose one or both</span>
            <button
              onClick={() => setSelected(prev => {
                const allOn = prev.insight7 && prev.rimdocs;
                return { insight7: !allOn, rimdocs: !allOn };
              })}
              className={`font-johnson-text text-xs font-bold px-3 py-1 rounded-full border transition-colors ${
                selected.insight7 && selected.rimdocs
                  ? 'bg-jj-green-03 text-white border-jj-green-05'
                  : 'bg-green-50 text-jj-green-03 border-jj-green-03'
              }`}
            >
              Select Both - run in parallel
            </button>
          </div>

          {DB_OPTIONS.map(opt => (
            <button key={opt.id} onClick={() => toggle(opt.id)}
              className={`w-full text-left px-4 py-4 rounded-xl border-2 transition-all ${
                selected[opt.id]
                  ? 'border-jj-green-03 bg-green-50'
                  : 'border-jj-gray-02 bg-white hover:border-jj-gray-04 hover:bg-jj-gray-01'
              }`}>
              <div className="flex items-center gap-3">
                <div className={`w-5 h-5 rounded border-2 flex items-center justify-center flex-shrink-0 transition-colors ${
                  selected[opt.id] ? 'bg-jj-green-03 border-jj-green-03' : 'border-jj-gray-04 bg-white'
                }`}>
                  {selected[opt.id] && (
                    <svg viewBox="0 0 12 10" className="w-3 h-3" fill="none">
                      <path d="M1 5l3.5 3.5L11 1" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
                    </svg>
                  )}
                </div>
                <div>
                  <p className="font-johnson-display text-sm text-jj-gray-08" style={{ lineHeight: '110%' }}>{opt.label}</p>
                  <p className="font-johnson-text text-xs text-jj-gray-05 mt-0.5">{opt.desc}</p>
                </div>
              </div>
            </button>
          ))}
        </div>

        {/* Footer */}
        <div className="px-6 pb-6 flex gap-3">
          <button onClick={onClose}
            className="flex-1 px-4 py-2.5 rounded-lg font-johnson-text text-sm font-bold text-jj-gray-07 bg-jj-gray-01 hover:bg-jj-gray-02 transition-colors border border-jj-gray-03">
            Cancel
          </button>
          <button onClick={handleConfirm} disabled={noneSelected}
            className={`flex-1 px-4 py-2.5 rounded-lg font-johnson-text text-sm font-bold text-white transition-colors ${
              noneSelected ? 'bg-jj-gray-03 cursor-not-allowed' : 'bg-jj-green-03 hover:bg-jj-green-05'
            }`}>
            Start Validation
          </button>
        </div>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// IT Validation Modal — one log panel per db, all running in parallel
// ═══════════════════════════════════════════════════════════════════════════════
const DB_LABELS = { insight7: 'Insight7', rimdocs: 'RIMDocs' };

function ITValidationLogPanel({ API_BASE_URL, db, onDone }) {
  const [logs, setLogs]           = useState([]);
  const [isRunning, setIsRunning] = useState(true);
  const [returnCode, setReturnCode] = useState(null);
  const logsEndRef                = useRef(null);

  useEffect(() => { logsEndRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [logs]);

  useEffect(() => {
    const es = apiEventSource(`${API_BASE_URL}/api/run-it-validation?db=${db}`);

    es.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'run') return; // run id only, nothing to show
        setLogs(prev => [...prev, data]);
        if (data.type === 'done') {
          setIsRunning(false);
          setReturnCode(data.returncode);
          es.close();
          onDone(db, data.returncode);
        }
      } catch (e) { console.error('SSE parse error:', e); }
    };

    es.onerror = () => {
      setLogs(prev => [...prev, { type: 'error', message: 'Connection lost to server', timestamp: new Date().toISOString() }]);
      setIsRunning(false);
      es.close();
      onDone(db, -1);
    };

    return () => es.close();
  }, [API_BASE_URL, db]);

  const getLogStyle = (type) => ({
    error:   'text-jj-maroon-05 font-bold',
    success: 'text-jj-green-03 font-bold',
    status:  'text-jj-blue-03 font-bold',
    info:    'text-jj-blue-03',
  }[type] || 'text-jj-gray-08');

  const formatTs = (ts) => {
    try { return new Date(ts).toLocaleTimeString('en-US', { timeZone: 'America/New_York', hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' }); }
    catch { return ''; }
  };

  const borderColor = isRunning ? '#0f68b2' : returnCode === 0 ? '#328714' : '#9e0000';
  const statusLabel = isRunning ? 'Running…' : returnCode === 0 ? 'Completed' : 'Failed';

  return (
    <div className="flex flex-col flex-1 min-w-0 rounded-xl border border-jj-gray-02 overflow-hidden">
      {/* Panel header */}
      <div className="px-4 py-2.5 bg-white border-b-2 flex items-center gap-2 flex-shrink-0" style={{ borderBottomColor: borderColor }}>
        {isRunning
          ? <Icon src={loadingIcon} alt="" className="w-4 h-4 animate-spin" color="jj-gray-08" />
          : returnCode === 0
            ? <Icon src={checkCircleIcon} alt="" className="w-4 h-4" color="jj-green-03" />
            : <Icon src={xCircleIcon} alt="" className="w-4 h-4" color="jj-maroon-05" />
        }
        <span className="font-johnson-display text-sm text-jj-gray-08">{DB_LABELS[db]}</span>
        <span className={`ml-auto font-johnson-text text-xs font-bold px-2 py-0.5 rounded-full ${
          isRunning   ? 'bg-blue-50 text-jj-blue-03' :
          returnCode === 0 ? 'bg-green-50 text-jj-green-03' : 'bg-red-50 text-jj-maroon-05'
        }`}>{statusLabel}</span>
      </div>

      {/* Log output */}
      <div className="flex-1 min-h-0 bg-jj-gray-01 p-2">
        <div className="h-full overflow-y-auto bg-white border border-jj-gray-02 rounded-lg p-3"
             style={{ scrollbarWidth: 'thin', scrollbarColor: '#a39992 #f1efed' }}>
          {logs.length === 0 && (
            <p className="font-johnson-text text-xs text-jj-gray-05 italic">Connecting…</p>
          )}
          {logs.map((log, i) => {
            if (log.type === 'separator') return <div key={i} className="my-1.5 border-t border-jj-gray-02" />;
            return (
              <div key={i} className="mb-0.5 flex gap-2">
                <span className="font-johnson-text text-xs text-jj-gray-05 flex-shrink-0 pt-px">[{formatTs(log.timestamp)}]</span>
                <span className={`font-johnson-text text-xs ${getLogStyle(log.type)}`}>{log.message}</span>
              </div>
            );
          })}
          <div ref={logsEndRef} />
        </div>
      </div>
    </div>
  );
}

function ITValidationModal({ API_BASE_URL, databases, onClose }) {
  // Track per-db completion: { insight7: returncode | null, rimdocs: returncode | null }
  const [doneMap, setDoneMap] = useState(() =>
    Object.fromEntries(databases.map(db => [db, null]))
  );

  const handleDone = useCallback((db, code) => {
    setDoneMap(prev => ({ ...prev, [db]: code }));
  }, []);

  const allDone    = databases.every(db => doneMap[db] !== null);
  const anyFailed  = databases.some(db => doneMap[db] !== null && doneMap[db] !== 0);
  const allSuccess = allDone && !anyFailed;

  const overallStatus = !allDone   ? 'Running…'
    : allSuccess ? `${databases.length > 1 ? 'All validations' : 'Validation'} completed successfully`
    : `${databases.length > 1 ? 'One or more validations' : 'Validation'} failed`;

  const borderColor = !allDone ? '#0f68b2' : allSuccess ? '#328714' : '#9e0000';

  return (
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4"
         onClick={allDone ? onClose : undefined}
         style={{ backdropFilter: 'blur(3px)' }}>
      <div className="bg-white rounded-2xl shadow-2xl border border-jj-gray-02 w-full flex flex-col overflow-hidden"
           style={{ maxWidth: databases.length > 1 ? '72rem' : '56rem', height: '85vh' }}
           onClick={e => e.stopPropagation()}>

        {/* Header */}
        <div className="px-6 py-4 bg-white border-b-4 flex items-center justify-between flex-shrink-0"
             style={{ borderBottomColor: borderColor }}>
          <div className="flex items-center gap-3">
            {!allDone
              ? <Icon src={loadingIcon} alt="" className="w-5 h-5 animate-spin" color="jj-green-03" />
              : allSuccess
                ? <Icon src={checkCircleIcon} alt="" className="w-5 h-5" color="jj-green-03" />
                : <Icon src={xCircleIcon} alt="" className="w-5 h-5" color="jj-maroon-05" />
            }
            <h2 className="font-johnson-display text-lg text-jj-gray-08" style={{ lineHeight: '110%' }}>
              IT Validation — {overallStatus}
            </h2>
            {databases.length > 1 && (
              <span className="font-johnson-text text-xs font-bold px-2.5 py-1 rounded-full bg-blue-50 text-jj-blue-03 border border-jj-blue-03 border-opacity-20">
                Parallel
              </span>
            )}
          </div>
          <button onClick={onClose} disabled={!allDone}
            className="w-9 h-9 flex items-center justify-center rounded-lg bg-jj-gray-01 hover:bg-jj-gray-02 transition-colors disabled:opacity-40 disabled:cursor-not-allowed">
            <Icon src={xCircleIcon} alt="Close" className="w-4 h-4" color="jj-gray-07" />
          </button>
        </div>

        {/* Log panels — side by side when two dbs */}
        <div className={`flex-1 min-h-0 p-4 gap-4 ${databases.length > 1 ? 'flex flex-row' : 'flex flex-col'}`}>
          {databases.map(db => (
            <ITValidationLogPanel
              key={db}
              API_BASE_URL={API_BASE_URL}
              db={db}
              onDone={handleDone}
            />
          ))}
        </div>

        {/* Footer */}
        <div className="px-6 py-3 bg-white border-t border-jj-gray-02 flex justify-between items-center flex-shrink-0">
          <span className="font-johnson-text text-sm text-jj-gray-06">
            {!allDone
              ? `${databases.map(db => DB_LABELS[db]).join(' & ')} validation in progress…`
              : allSuccess
                ? '✓ Results saved to Snowflake'
                : '✗ Check logs above for errors'
            }
          </span>
          <button onClick={onClose} disabled={!allDone}
            className={`px-5 py-2 rounded-lg font-johnson-text text-sm font-bold transition-colors ${
              !allDone ? 'bg-jj-gray-02 text-jj-gray-05 cursor-not-allowed' : 'bg-jj-red text-white hover:bg-jj-maroon-05'
            }`}>
            {!allDone ? 'Running…' : 'Close & Refresh'}
          </button>
        </div>
      </div>
    </div>
  );
}

const NetworkCanvas = () => {
  const wrapRef = useRef(null);
  const canvasRef = useRef(null);

  useEffect(() => {
    const wrap = wrapRef.current;
    const canvas = canvasRef.current;
    if (!wrap || !canvas) return undefined;
    const ctx = canvas.getContext('2d');

    const resize = () => {
      const dpr = window.devicePixelRatio || 1;
      canvas.width = canvas.offsetWidth * dpr;
      canvas.height = canvas.offsetHeight * dpr;
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.scale(dpr, dpr);
    };
    resize();
    const resizeObs = new ResizeObserver(resize);
    resizeObs.observe(wrap);

    const NODES = [
      { fx: .82, fy: .12, r: 7, hue: 'blue', pulse: 0,   ps: .007 },
      { fx: .94, fy: .38, r: 9, hue: 'red',  pulse: 1.6, ps: .005 },
      { fx: .70, fy: .62, r: 6, hue: 'blue', pulse: 3.1, ps: .008 },
      { fx: .58, fy: .22, r: 5, hue: 'red',  pulse: .9,  ps: .009 },
      { fx: .88, fy: .78, r: 7, hue: 'blue', pulse: 2.4, ps: .006 },
      { fx: .50, fy: .48, r: 5, hue: 'blue', pulse: 4.2, ps: .010 },
      { fx: .96, fy: .60, r: 6, hue: 'red',  pulse: 1.1, ps: .007 },
      { fx: .65, fy: .88, r: 5, hue: 'red',  pulse: 5.0, ps: .008 },
      { fx: .78, fy: .46, r: 6, hue: 'blue', pulse: 0.5, ps: .006 },
    ];
    const EDGES = [
      [0, 1], [1, 2], [0, 3], [3, 1], [2, 4], [1, 4],
      [3, 5], [5, 2], [1, 6], [4, 6], [1, 8], [8, 2],
      [8, 4], [0, 8], [5, 7], [2, 7], [6, 4],
    ];
    const PACKETS = EDGES.map(([a, b], i) => ({
      a, b, t: Math.random(), speed: .0025 + Math.random() * .003,
      hue: i % 3 === 0 ? 'red' : 'blue',
    }));

    const BLUE = (a) => `rgba(15,104,178,${a})`;
    const RED = (a) => `rgba(235,23,0,${a})`;
    const col = (hue, a) => (hue === 'blue' ? BLUE(a) : RED(a));

    let raf;
    const draw = () => {
      const W = canvas.offsetWidth, H = canvas.offsetHeight;
      ctx.clearRect(0, 0, W, H);

      // Edges
      EDGES.forEach(([ai, bi]) => {
        const na = NODES[ai], nb = NODES[bi];
        const ax = na.fx * W, ay = na.fy * H, bx = nb.fx * W, by = nb.fy * H;
        const grad = ctx.createLinearGradient(ax, ay, bx, by);
        grad.addColorStop(0, BLUE(.18));
        grad.addColorStop(.5, BLUE(.28));
        grad.addColorStop(1, BLUE(.18));
        ctx.beginPath();
        ctx.moveTo(ax, ay);
        ctx.lineTo(bx, by);
        ctx.strokeStyle = grad;
        ctx.lineWidth = 1.2;
        ctx.stroke();
      });

      // Packets travelling along edges
      PACKETS.forEach(p => {
        p.t = (p.t + p.speed) % 1;
        const na = NODES[p.a], nb = NODES[p.b];
        const x = na.fx * W + (nb.fx * W - na.fx * W) * p.t;
        const y = na.fy * H + (nb.fy * H - na.fy * H) * p.t;
        const tx = na.fx * W + (nb.fx * W - na.fx * W) * Math.max(0, p.t - .06);
        const ty = na.fy * H + (nb.fy * H - na.fy * H) * Math.max(0, p.t - .06);
        const grad = ctx.createLinearGradient(tx, ty, x, y);
        grad.addColorStop(0, col(p.hue, 0));
        grad.addColorStop(1, col(p.hue, .65));
        ctx.beginPath();
        ctx.moveTo(tx, ty);
        ctx.lineTo(x, y);
        ctx.strokeStyle = grad;
        ctx.lineWidth = 2;
        ctx.stroke();
        ctx.beginPath();
        ctx.arc(x, y, 2.8, 0, Math.PI * 2);
        ctx.fillStyle = col(p.hue, .85);
        ctx.fill();
      });

      // Nodes with pulsing glow
      NODES.forEach(n => {
        n.pulse = (n.pulse + n.ps) % (Math.PI * 2);
        const x = n.fx * W, y = n.fy * H;
        const sin = (Math.sin(n.pulse) + 1) / 2;

        const pR = n.r + 8 + sin * 22;
        ctx.beginPath();
        ctx.arc(x, y, pR, 0, Math.PI * 2);
        ctx.strokeStyle = col(n.hue, .08 + sin * .18);
        ctx.lineWidth = 1.2;
        ctx.stroke();

        const mR = n.r + 3 + sin * 10;
        ctx.beginPath();
        ctx.arc(x, y, mR, 0, Math.PI * 2);
        ctx.strokeStyle = col(n.hue, .18 + sin * .22);
        ctx.lineWidth = 1.2;
        ctx.stroke();

        ctx.shadowColor = n.hue === 'blue' ? 'rgba(15,104,178,.5)' : 'rgba(235,23,0,.5)';
        ctx.shadowBlur = 8;
        ctx.beginPath();
        ctx.arc(x, y, n.r, 0, Math.PI * 2);
        ctx.fillStyle = col(n.hue, .22 + sin * .18);
        ctx.fill();
        ctx.strokeStyle = col(n.hue, .55 + sin * .3);
        ctx.lineWidth = 1.8;
        ctx.stroke();
        ctx.shadowBlur = 0;

        ctx.beginPath();
        ctx.arc(x, y, n.r * .4, 0, Math.PI * 2);
        ctx.fillStyle = col(n.hue, .7 + sin * .3);
        ctx.fill();
      });

      raf = requestAnimationFrame(draw);
    };
    draw();

    return () => {
      cancelAnimationFrame(raf);
      resizeObs.disconnect();
    };
  }, []);

  return (
    <div ref={wrapRef} className="absolute inset-0 pointer-events-none overflow-hidden" style={{ zIndex: 1 }}>
      <canvas ref={canvasRef} className="absolute inset-0 w-full h-full block" />
    </div>
  );
};

// ═══════════════════════════════════════════════════════════════════════════════
// Main Component
// ═══════════════════════════════════════════════════════════════════════════════
function DataQualityEngine() {
  const { hasPermission } = useRBAC();
  const [searchParams, setSearchParams] = useSearchParams();
  const [reportData, setReportData] = useState(null);
  const [selectedTable, setSelectedTable] = useState(null);
  const [refreshingTable, setRefreshingTable] = useState(null);
  const [toast, setToast] = useState(null);
  const [searchTerm, setSearchTerm] = useState('');
  const [debouncedSearchTerm, setDebouncedSearchTerm] = useState('');
  const [isSearching, setIsSearching] = useState(false);
  const [statusFilter, setStatusFilter] = useState(STATUS.ALL);
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [selectedSourceSchemas, setSelectedSourceSchemas] = useState([]);
  const [selectedSourceTables, setSelectedSourceTables] = useState([]);
  const [selectedSourceColumns, setSelectedSourceColumns] = useState([]);
  const [selectedTargetDatabases, setSelectedTargetDatabases] = useState([]);
  const [selectedTargetSchemas, setSelectedTargetSchemas] = useState([]);
  const [selectedTargetTables, setSelectedTargetTables] = useState([]);
  const [selectedTargetColumns, setSelectedTargetColumns] = useState([]);
  const [filtersExpanded, setFiltersExpanded] = useState(false);
  const [showWelcome, setShowWelcome] = useState(true);
  const [selectedCheckType, setSelectedCheckType] = useState(() => {
    const v = searchParams.get('check') ?? '';
    return VALID_CHECKS.includes(v) ? v : '';
  });
  const [isLoading, setIsLoading] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [availableRuns, setAvailableRuns] = useState([]);
  const [selectedRun,   setSelectedRun]   = useState(null);   // currently loaded (for display)
  const [pendingRuns,   setPendingRuns]   = useState([]);     // checkbox state in the picker ([] = all)
  const [runPickerOpen, setRunPickerOpen] = useState(false);
  const [runsMerged,    setRunsMerged]    = useState(null);
  const [newestDate, setNewestDate]   = useState(null);
  const [oldestDate, setOldestDate]   = useState(null);
  const [runsLoading, setRunsLoading] = useState(false);
  const [oracleConnections, setOracleConnections] = useState({ configured: [], has_default: true });
  const [showITValidationSelector, setShowITValidationSelector] = useState(false);
  const [itValidationDbs, setItValidationDbs] = useState(null); // null = closed, [...] = running
  const searchInputRef = useRef(null);

  // ── Unmount guard — prevents async callbacks (especially setSearchParams)
  //    from firing after the user has already navigated away from /dqe.
  //    Without this, a slow fetch completing after navigation calls setSearchParams
  //    which acts like navigate() and yanks the user back to the DQE page.
  const isMountedRef = useRef(true);
  useEffect(() => {
    isMountedRef.current = true;
    return () => { isMountedRef.current = false; };
  }, []);

  // ── Derive which check types this user can access ──
  const availableCheckTypes = useMemo(() => [
    hasPermission('viewITChecks')       && 'IT_Checks',
    hasPermission('viewBusinessChecks') && 'Business_defined_checks',
  ].filter(Boolean), [hasPermission]);

  // ── Fetch which Oracle connections are configured ──
  useEffect(() => {
    fetch(`${API_BASE_URL}/dq/oracle_connections`)
      .then(r => r.ok ? r.json() : null)
      .then(data => { if (data) setOracleConnections(data); })
      .catch(() => {});
  }, []);
  // Skips the welcome screen entirely and jumps straight to the content.
  const autoLoadedRef = useRef(false);
  useEffect(() => {
    if (autoLoadedRef.current) return;           // already fired
    if (availableCheckTypes.length !== 1) return; // not a single-access user
    if (searchParams.get('check')) return;        // URL already has a check (back/forward nav)
    autoLoadedRef.current = true;
    const only = availableCheckTypes[0];
    setSelectedCheckType(only);
    handleLoadData(null, null, only, false);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [availableCheckTypes]);

  // ── Track what params are currently "loaded" so the URL effect can compare
  //    against the actual loaded state instead of stale closure values.
  const loadedParamsRef = useRef({ check: '' });

  // ── Debounce Search ──
  useEffect(() => {
    setIsSearching(true);
    const t = setTimeout(() => { setDebouncedSearchTerm(searchTerm); setCurrentPage(1); setIsSearching(false); }, 500);
    return () => { clearTimeout(t); setIsSearching(false); };
  }, [searchTerm]);

  // ── Keyboard Shortcuts ──
  useEffect(() => {
    const handler = (e) => {
      if (e.key === 'Escape' && selectedTable) setSelectedTable(null);
      if ((e.metaKey || e.ctrlKey) && e.key === 'f') { e.preventDefault(); searchInputRef.current?.focus(); }
      if (document.activeElement?.tagName !== 'INPUT' && !selectedTable) {
        if (e.key === 'ArrowLeft' && currentPage > 1) handlePageChange(currentPage - 1);
        else if (e.key === 'ArrowRight' && currentPage < totalPages) handlePageChange(currentPage + 1);
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [selectedTable, currentPage]);

  // ── Data Memo ──
  const allTables = useMemo(() => {
    try {
      if (!reportData?.pipeline_results) return [];
      return reportData.pipeline_results.flatMap(pipeline => {
        const tvs = getTableValidations(pipeline);
        return tvs.map(table => ({
          ...table,
          pipeline_name:   pipeline.pipeline_name || 'Unknown',
          source_database: 'Insight7',
          source_db:       table._source_db          || '',
          // Use per-table annotation (set during multi-run merge) so each table
          // keeps its own run's schema values; fall back to pipeline-level for
          // single-run loads where the annotation is not present.
          source_schema:   table._oracle_schema      || pipeline.oracle_schema      || 'Unknown',
          target_database: table._snowflake_database || pipeline.snowflake_database || 'Unknown',
          target_schema:   table._snowflake_schema   || pipeline.snowflake_schema   || 'Unknown',
          table_name:      table.table_name    || 'Unknown',
          snowflake_table: table.snowflake_table || 'Unknown',
          status:          table.status         || 'Unknown',
          row_counts:      table.row_counts     || { oracle: 0, snowflake: 0, match: false },
          Last_Checked:    table.Last_Checked   || ''
        }));
      });
    } catch { return []; }
  }, [reportData]);

  const schemaFiltered = useMemo(() => selectedSourceSchemas.length === 0 ? allTables : allTables.filter(t => selectedSourceSchemas.includes(t.source_schema)), [allTables, selectedSourceSchemas]);
  const srcTableFiltered = useMemo(() => selectedSourceTables.length === 0 ? schemaFiltered : schemaFiltered.filter(t => selectedSourceTables.includes(t.table_name)), [schemaFiltered, selectedSourceTables]);
  const targetFiltered = useMemo(() => {
    let t = srcTableFiltered;
    if (selectedTargetDatabases.length > 0) t = t.filter(x => selectedTargetDatabases.includes(x.target_database));
    if (selectedTargetSchemas.length > 0) t = t.filter(x => selectedTargetSchemas.includes(x.target_schema));
    if (selectedTargetTables.length > 0) t = t.filter(x => selectedTargetTables.includes(x.snowflake_table));
    return t;
  }, [srcTableFiltered, selectedTargetDatabases, selectedTargetSchemas, selectedTargetTables]);

  const columnFiltered = useMemo(() => {
    if (selectedSourceColumns.length === 0 && selectedTargetColumns.length === 0) return targetFiltered;
    return targetFiltered.filter(t => {
      if (!t.column_statistics) return false;
      const cols = t.column_statistics.map(c => c.column_name);
      return (selectedSourceColumns.length === 0 || selectedSourceColumns.some(c => cols.includes(c))) &&
             (selectedTargetColumns.length === 0 || selectedTargetColumns.some(c => cols.includes(c)));
    });
  }, [targetFiltered, selectedSourceColumns, selectedTargetColumns]);

  const filteredTables = useMemo(() => {
    let t = columnFiltered;
    if (debouncedSearchTerm) {
      const s = debouncedSearchTerm.toLowerCase();
      t = t.filter(x => x.table_name.toLowerCase().includes(s) || x.snowflake_table.toLowerCase().includes(s));
    }
    if (statusFilter !== STATUS.ALL) t = t.filter(x => x.status === statusFilter);
    return t;
  }, [columnFiltered, debouncedSearchTerm, statusFilter]);

  const filterBySource = useCallback((tables) => {
    let f = tables;
    if (selectedSourceSchemas.length > 0) f = f.filter(t => selectedSourceSchemas.includes(t.source_schema));
    if (selectedSourceTables.length > 0) f = f.filter(t => selectedSourceTables.includes(t.table_name));
    return f;
  }, [selectedSourceSchemas, selectedSourceTables]);

  // ── Cascade Clears ──
  useEffect(() => {
    if (selectedSourceSchemas.length === 0) { setSelectedSourceTables([]); setSelectedSourceColumns([]); }
    else setSelectedSourceTables(prev => prev.filter(t => allTables.some(x => x.table_name === t && selectedSourceSchemas.includes(x.source_schema))));
  }, [selectedSourceSchemas, allTables]);

  useEffect(() => {
    if (selectedSourceTables.length === 0) { setSelectedSourceColumns([]); }
    else {
      const valid = new Set();
      allTables.filter(t => selectedSourceTables.includes(t.table_name)).forEach(t => t.column_statistics?.forEach(c => valid.add(c.column_name)));
      setSelectedSourceColumns(prev => prev.filter(c => valid.has(c)));
    }
  }, [selectedSourceTables, allTables]);

  useEffect(() => {
    if (selectedTargetDatabases.length === 0) { setSelectedTargetSchemas([]); setSelectedTargetTables([]); setSelectedTargetColumns([]); }
    else setSelectedTargetSchemas(prev => prev.filter(s => allTables.some(t => t.target_schema === s && selectedTargetDatabases.includes(t.target_database))));
  }, [selectedTargetDatabases, allTables]);

  useEffect(() => {
    if (selectedTargetSchemas.length === 0) { setSelectedTargetTables([]); setSelectedTargetColumns([]); }
    else setSelectedTargetTables(prev => prev.filter(t => allTables.some(x => x.snowflake_table === t && selectedTargetSchemas.includes(x.target_schema))));
  }, [selectedTargetSchemas, allTables]);

  useEffect(() => {
    if (selectedTargetTables.length === 0) { setSelectedTargetColumns([]); }
    else {
      const valid = new Set();
      allTables.filter(t => selectedTargetTables.includes(t.snowflake_table)).forEach(t => t.column_statistics?.forEach(c => valid.add(c.column_name)));
      setSelectedTargetColumns(prev => prev.filter(c => valid.has(c)));
    }
  }, [selectedTargetTables, allTables]);

  // ── Available Options ──
  const availableSourceSchemas = useMemo(() => [...new Set(allTables.map(t => t.source_schema))].sort(), [allTables]);
  const availableSourceTables  = useMemo(() => selectedSourceSchemas.length === 0 ? [] : [...new Set(allTables.filter(t => selectedSourceSchemas.includes(t.source_schema)).map(t => t.table_name))].sort(), [allTables, selectedSourceSchemas]);
  const availableSourceColumns = useMemo(() => {
    if (selectedSourceTables.length === 0) return [];
    const s = new Set();
    allTables.filter(t => selectedSourceTables.includes(t.table_name)).forEach(t => t.column_statistics?.forEach(c => s.add(c.column_name)));
    return [...s].sort();
  }, [allTables, selectedSourceTables]);
  const availableTargetDatabases = useMemo(() => [...new Set(filterBySource(allTables).map(t => t.target_database))].sort(), [allTables, filterBySource]);
  const availableTargetSchemas   = useMemo(() => selectedTargetDatabases.length === 0 ? [] : [...new Set(filterBySource(allTables.filter(t => selectedTargetDatabases.includes(t.target_database))).map(t => t.target_schema))].sort(), [allTables, selectedTargetDatabases, filterBySource]);
  const availableTargetTables    = useMemo(() => selectedTargetSchemas.length === 0 ? [] : [...new Set(filterBySource(allTables.filter(t => selectedTargetSchemas.includes(t.target_schema))).map(t => t.snowflake_table))].sort(), [allTables, selectedTargetSchemas, filterBySource]);
  const availableTargetColumns   = useMemo(() => {
    if (selectedTargetTables.length === 0) return [];
    const s = new Set();
    filterBySource(allTables.filter(t => selectedTargetTables.includes(t.snowflake_table))).forEach(t => t.column_statistics?.forEach(c => s.add(c.column_name)));
    return [...s].sort();
  }, [allTables, selectedTargetTables, filterBySource]);

  // ── Table Sorting ──
  const [tableSortConfig, setTableSortConfig] = useState({ key: null, direction: 'asc' });

  const handleTableSort = useCallback((key) => {
    setTableSortConfig(prev => ({
      key,
      direction: prev.key === key && prev.direction === 'asc' ? 'desc' : 'asc',
    }));
    setCurrentPage(1);
  }, []);

  const sortedTables = useMemo(() => {
    if (!tableSortConfig.key) return filteredTables;
    return [...filteredTables].sort((a, b) => {
      let av, bv;
      switch (tableSortConfig.key) {
        case 'table_name':      av = a.table_name?.toLowerCase() ?? '';      bv = b.table_name?.toLowerCase() ?? '';      break;
        case 'snowflake_table': av = a.snowflake_table?.toLowerCase() ?? ''; bv = b.snowflake_table?.toLowerCase() ?? ''; break;
        case 'status':          av = a.status ?? '';                          bv = b.status ?? '';                          break;
        case 'source_rows':     av = a.row_counts?.oracle ?? 0;               bv = b.row_counts?.oracle ?? 0;               break;
        case 'sf_rows':         av = a.row_counts?.snowflake ?? 0;            bv = b.row_counts?.snowflake ?? 0;            break;
        case 'last_checked':    av = a.Last_Checked ?? '';                    bv = b.Last_Checked ?? '';                    break;
        default: return 0;
      }
      if (av < bv) return tableSortConfig.direction === 'asc' ? -1 : 1;
      if (av > bv) return tableSortConfig.direction === 'asc' ?  1 : -1;
      return 0;
    });
  }, [filteredTables, tableSortConfig]);

  // ── Pagination ──
  const totalPages = Math.max(1, Math.ceil(sortedTables.length / Math.max(1, pageSize)));
  const paginatedTables = useMemo(() => {
    const start = (currentPage - 1) * pageSize;
    return sortedTables.slice(start, start + pageSize);
  }, [sortedTables, currentPage, pageSize]);

  useEffect(() => { setCurrentPage(1); }, [selectedSourceSchemas, selectedSourceTables, selectedTargetDatabases, selectedTargetSchemas, selectedTargetTables, statusFilter]);

  // ── Summary ──
  const summary = useMemo(() => ({
    total:    filteredTables.length,
    passed:   filteredTables.filter(t => t.status === STATUS.PASSED).length,
    critical: filteredTables.filter(t => t.status === STATUS.CRITICAL).length,
    faulty:   filteredTables.filter(t => t.status === STATUS.FAULTY).length,
    errors:   filteredTables.filter(t => t.status === STATUS.ERROR).length,
    notFound: filteredTables.filter(t => t.status === STATUS.TIMEOUT || t.status === 'TABLE_NOT_FOUND').length,
  }), [filteredTables]);

  // ── Handlers ──
  const handlePageChange = useCallback((page) => setCurrentPage(Math.max(1, Math.min(page, totalPages))), [totalPages]);
  const handlePageSizeChange = useCallback((e) => { setPageSize(Number(e.target.value)); setCurrentPage(1); }, []);

  const handleClearAllFilters = useCallback((e) => {
    e?.stopPropagation();
    setSelectedSourceSchemas([]); setSelectedSourceTables([]); setSelectedSourceColumns([]);
    setSelectedTargetDatabases([]); setSelectedTargetSchemas([]); setSelectedTargetTables([]); setSelectedTargetColumns([]);
    setSearchTerm(''); setStatusFilter(STATUS.ALL);
  }, []);

  const handleTableSelect = useCallback((table) => setSelectedTable(table), []);
  const showToast = useCallback((message, type = 'success') => setToast({ message, type }), []);
  const formatLocalStamp = (date) => {
    const pad = n => String(n).padStart(2, '0');
    let hours = date.getHours();
    const ampm = hours >= 12 ? 'PM' : 'AM';
    hours = hours % 12 || 12;
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}-` +
           `${pad(hours)}-${pad(date.getMinutes())}${ampm}`;
  };

  const handleExportExcel = useCallback(async () => {
    if (!sortedTables.length) return;

    const columns = [
      { header: 'Source Schema',     key: 'srcSchema',  width: 20 },
      { header: 'Source Table',      key: 'srcTable',   width: 28 },
      { header: 'Target Database',   key: 'tgtDb',      width: 20 },
      { header: 'Target Schema',     key: 'tgtSchema',  width: 20 },
      { header: 'Target Table',      key: 'tgtTable',   width: 28 },
      { header: 'Status',            key: 'status',     width: 14 },
      { header: 'Source Rows',       key: 'srcRows',    width: 16 },
      { header: 'Snowflake Rows',    key: 'sfRows',     width: 16 },
      { header: 'Row Count Diff',    key: 'rowDiff',    width: 16 },
      { header: 'Last Checked',      key: 'lastChecked',width: 22 },
    ];

    const wb = new ExcelJS.Workbook();
    const ws = wb.addWorksheet('IT Checks');
    ws.columns = columns;

    sortedTables.forEach(t => {
      const srcRows = t.row_counts?.oracle ?? 0;
      const sfRows  = t.row_counts?.snowflake ?? 0;
      ws.addRow({
        srcSchema:   t.source_schema,
        srcTable:    t.table_name,
        tgtDb:       t.target_database,
        tgtSchema:   t.target_schema,
        tgtTable:    t.snowflake_table,
        status:      t.status,
        srcRows,
        sfRows,
        rowDiff:     srcRows - sfRows,
        lastChecked: formatTimestamp(t.Last_Checked),
      });
    });

    const thinBorder = { style: 'thin', color: { argb: 'FFB7B7B7' } };

    // ── Header styling: bold text on a yellow fill ──
    const headerRow = ws.getRow(1);
    headerRow.eachCell(cell => {
      cell.font = { bold: true, color: { argb: 'FF1A1A1A' } };
      cell.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: 'FFFFD400' } };
      cell.alignment = { vertical: 'middle', horizontal: 'center' };
      cell.border = { top: thinBorder, left: thinBorder, right: thinBorder, bottom: thinBorder };
    });
    headerRow.height = 20;

    // ── Borders on every cell + zebra striping + colour the diff column ──
    ws.eachRow((row, rowNumber) => {
      row.eachCell({ includeEmpty: true }, cell => {
        cell.border = { top: thinBorder, left: thinBorder, right: thinBorder, bottom: thinBorder };
      });
      if (rowNumber === 1) return;
      const diffCell = row.getCell('rowDiff');
      diffCell.font = { bold: diffCell.value !== 0, color: { argb: diffCell.value === 0 ? 'FF1E7B34' : 'FFB3261E' } };
      if (rowNumber % 2 === 0) {
        row.eachCell(cell => { cell.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: 'FFF5F5F5' } }; });
      }
    });

    ws.autoFilter = { from: 'A1', to: `${String.fromCharCode(64 + columns.length)}1` };
    ws.views = [{ state: 'frozen', ySplit: 1 }];

    const buffer = await wb.xlsx.writeBuffer();
    const blob = new Blob([buffer], { type: 'application/octet-stream' });

    const runDatabases = [...new Set(sortedTables.map(t => t.source_db).filter(Boolean))];
    const dbLabel = runDatabases.length > 0 ? runDatabases.join('_') : 'IT_Checks';
    const runTimestamp = selectedRun || newestDate;
    const stampSource = runTimestamp ? new Date(runTimestamp) : new Date();
    const stamp = formatLocalStamp(stampSource);

    const a = Object.assign(document.createElement('a'), {
      href: URL.createObjectURL(blob),
      download: `${dbLabel}_${stamp}.xlsx`,
    });
    a.click();
    URL.revokeObjectURL(a.href);
  }, [sortedTables, selectedRun, newestDate]);


  // A table can be refreshed only if its source Oracle connection is configured.
  const canRefreshTable = useCallback((table) => {
    if (!hasPermission('runITChecks')) return 'hidden';
    if (oracleConnections.configured.length === 0 && !oracleConnections.has_default) return 'no-connection';
    const key = (table.source_db || '').toUpperCase();
    if (!key) return oracleConnections.has_default ? 'active' : 'no-connection';
    const connected = oracleConnections.has_default ||
      oracleConnections.configured.some(k => key.includes(k.toUpperCase()) || k.toUpperCase().includes(key));
    return connected ? 'active' : 'no-connection';
  }, [hasPermission, oracleConnections]);

  const handleCheckTypeChange = useCallback((t) => {
    setSelectedCheckType(t);
    setLoadError('');
  }, []);

  const resetState = useCallback(() => {
    // Do NOT clear loadedParamsRef here. The URL effect reads it to decide whether
    // a reset is needed — if we clear it before setSearchParams fires, the guard
    // (loadedParamsRef.current.env !== '') evaluates false and setShowWelcome(true)
    // is never called, leaving the page stuck on the current view.
    // The URL effect clears loadedParamsRef itself once it runs the reset.
    setSearchParams({});
  }, [setSearchParams]);

  const handleLoadData = useCallback(async (loadDates = null, _envArg = null, checkArg = null, fromNavigation = false) => {
    // loadDates: null = all runs, string = single run, string[] = subset of runs
    const check = checkArg ?? selectedCheckType;

    if (!check) { setLoadError('Please select a check type.'); return; }

    if (check === 'IT_Checks' && !hasPermission('viewITChecks')) {
      setLoadError('You do not have permission to access IT Checks.');
      return;
    }

    if (check === 'Business_defined_checks') {
      if (!hasPermission('viewBusinessChecks')) {
        setLoadError('You do not have permission to access Business Defined Checks.');
        return;
      }
      if (!fromNavigation) { setSearchParams({ check }); }
      loadedParamsRef.current = { check };
      setShowWelcome(false);
      setLoadError('');
      return;
    }

    setIsLoading(true); setLoadError('');
    try {
      if (!loadDates) {
        setRunsLoading(true);
        const datesRes = await fetch(`${API_BASE_URL}/dq/validation_report_dates`);
        if (datesRes.ok) {
          const datesData = await datesRes.json();
          setAvailableRuns(datesData.runs || []);
        }
        setRunsLoading(false);
      }

      // Build URL based on what was selected
      let url = `${API_BASE_URL}/dq/validation_report`;
      if (Array.isArray(loadDates) && loadDates.length > 0) {
        url += loadDates.length === 1
          ? `?load_date=${encodeURIComponent(loadDates[0])}`
          : `?load_dates=${encodeURIComponent(loadDates.join(','))}`;
      } else if (typeof loadDates === 'string' && loadDates) {
        url += `?load_date=${encodeURIComponent(loadDates)}`;
      }
      // null → no params → all runs combined

      const res = await fetch(url);
      if (!res.ok) throw new Error(`Failed to load report: ${res.statusText}`);
      const data = await res.json();
      if (!data.report_data) throw new Error('Invalid data format from server');

      if (!isMountedRef.current) return;

      setReportData(data.report_data);
      const singleDate = Array.isArray(loadDates) && loadDates.length === 1
        ? loadDates[0]
        : typeof loadDates === 'string' ? loadDates : null;
      setSelectedRun(singleDate);
      setRunsMerged(data.runs_merged ?? 1);
      setNewestDate(data.newest_date || null);
      setOldestDate(data.oldest_date || null);
      handleClearAllFilters();
      if (!fromNavigation) { setSearchParams({ check }); }
      loadedParamsRef.current = { check };
      setShowWelcome(false);
    } catch (err) {
      console.error(err);
      setLoadError(`Failed to load data: ${err.message}`);
    } finally { setIsLoading(false); }
  }, [selectedCheckType, handleClearAllFilters, setSearchParams]);

  // ── React to URL changes from back / forward navigation ──────────────────────
  // Uses loadedParamsRef (not state closures) so the comparison is always fresh.
  //
  // History flow:
  //   /dqe                        → welcome screen (params absent)
  //   /dqe?check=IT_Checks → data view     (params present)
  //
  //  Back  from data view → URL loses params → restore welcome screen
  //  Forward to data view → URL gains params  → auto-load data
  useEffect(() => {
    const check =  searchParams.get('check') ?? '';
    const isValidNav = VALID_CHECKS.includes(check);

    if (isValidNav) {
      if (check !== loadedParamsRef.current.check) {
        setSelectedCheckType(check);
        handleLoadData(null, null, check, true);
      }
    } else {
      if (loadedParamsRef.current.check !== '') {
        loadedParamsRef.current = { check: '' };
        setShowWelcome(true);
        setReportData(null);
        setSearchTerm('');
        setStatusFilter(STATUS.ALL);
        setSelectedSourceSchemas([]);
        setSelectedSourceTables([]);
        setSelectedSourceColumns([]);
        setSelectedTargetDatabases([]);
        setSelectedTargetSchemas([]);
        setSelectedTargetTables([]);
        setSelectedTargetColumns([]);
        setCurrentPage(1);
        setAvailableRuns([]);
        setSelectedRun(null);
        setRunsMerged(null);
        setNewestDate(null);
        setOldestDate(null);
        setSelectedCheckType('');
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]); // only re-run when the URL changes

  const handleRefreshTable = useCallback(async (table, e) => {
    e?.stopPropagation();
    const key = `${table.pipeline_name}_${table.table_name}`;
    setRefreshingTable(key);
    try {
      const res = await fetch(`${API_BASE_URL}/dq/validate_single_table`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          pipeline_name:   table.pipeline_name,
          table_name:      table.table_name,
          source_schema:   table.source_schema,
          target_database: table.target_database,
          target_schema:   table.target_schema,
          snowflake_table: table.snowflake_table,
          source_db:       table.source_db || '',
        })
      });
      if (!res.ok) { const err = await res.json().catch(() => ({})); throw new Error(err.detail || 'Validation failed'); }
      const data = await res.json();
      if (data.success && data.validation_result) {
        setReportData(prev => {
          if (!prev?.pipeline_results) return prev;
          return {
            ...prev,
            pipeline_results: prev.pipeline_results.map(pipeline => {
              if (pipeline.pipeline_name !== table.pipeline_name || !pipeline.validation_results?.table_validations) return pipeline;
              return {
                ...pipeline,
                validation_results: {
                  ...pipeline.validation_results,
                  table_validations: pipeline.validation_results.table_validations.map(t =>
                    t.table_name === table.table_name ? { ...t, ...data.validation_result } : t
                  )
                }
              };
            })
          };
        });
        const resultStatus = data.validation_result?.status;
        const passed = resultStatus === 'PASSED';
        const issues = data.validation_result?.validation_issues || [];
        const firstIssue = issues[0] || '';
        // Strip Snowflake error codes: "002003 (02000): uuid: " and "Validation error: " prefix
        const cleanIssue = firstIssue
          .replace(/\d{6}\s*\(\w+\):\s*/g, '')
          .replace(/[a-f0-9]{8}-[a-f0-9-]{27}:\s*/gi, '')
          .replace(/^validation error:\s*/i, '')
          .trim();
        const truncated = cleanIssue.length > 80 ? cleanIssue.slice(0, 80).trimEnd() + '…' : cleanIssue;
        const errorDetail = truncated
          ? ` — ${truncated}${issues.length > 1 ? ` (+${issues.length - 1} more)` : ''}`
          : '';
        showToast(
          passed
            ? `${table.table_name} passed validation`
            : `${table.table_name} ${resultStatus || 'failed'}${errorDetail}`,
          passed ? 'success' : 'error'
        );
      }
    } catch (err) {
      console.error(err);
      const rawMsg = err.message || 'Unknown error';
      const cleanMsg = rawMsg.includes(':') ? rawMsg.split(':').pop().trim() : rawMsg;
      const shortMsg = cleanMsg.length > 80 ? cleanMsg.slice(0, 80) + '…' : cleanMsg;
      showToast(`Failed to validate ${table.table_name}: ${shortMsg}`, 'error');
    } finally { setRefreshingTable(null); }
  }, [showToast]);

  // ── Active filter count ──
  const activeFilterCount = selectedSourceSchemas.length + selectedSourceTables.length + selectedSourceColumns.length +
    selectedTargetDatabases.length + selectedTargetSchemas.length + selectedTargetTables.length + selectedTargetColumns.length;

  // ──────────────────────────────────────────────────────────────────────────
  // WELCOME SCREEN
  // ──────────────────────────────────────────────────────────────────────────
  if (showWelcome) {
    if (isLoading) return (
      <div className="flex items-center justify-center min-h-full bg-jj-gray-01">
        <div className="text-center">
          <LoadingSpinner />
          <p className="font-johnson-text mt-4 text-sm font-bold text-jj-gray-07">Loading validation data…</p>
        </div>
      </div>
    );

    const checkTypeOptions = [
      {
        value: 'IT_Checks', num: '01', label: 'IT Checks',
        desc: 'Row counts, schema validation, constraint reconciliation etc.',
      },
      {
        value: 'Business_defined_checks', num: '02', label: 'Business Defined Checks',
        desc: 'Custom logic and data-quality rules tailored to your domain.',
      },
    ].filter(item => availableCheckTypes.includes(item.value));

    return (
      <div className="min-h-full flex bg-jj-gray-01" style={{ fontFamily: "'JohnsonText', Arial, sans-serif" }}>

        {/* ═══ LEFT PANEL ═══ */}
        <div className="hidden lg:flex relative overflow-hidden flex-col" style={{ flex: '0 0 58%' }}>
          {/* BG: warm base + dot grid */}
          <div className="absolute inset-0" style={{ background: '#f7f5f2' }} />
          <div className="absolute inset-0" style={{ backgroundImage: 'radial-gradient(circle, rgba(175,165,155,.45) 1px, transparent 1px)', backgroundSize: '26px 26px' }} />

          {/* BG: concentric data rings */}
          <div className="absolute" style={{
            right: -180, top: '50%', transform: 'translateY(-50%)', width: 760, height: 760, borderRadius: '50%',
            border: '1px solid rgba(15,104,178,.13)',
            boxShadow: '0 0 0 70px rgba(15,104,178,.05), 0 0 0 140px rgba(15,104,178,.04), 0 0 0 210px rgba(15,104,178,.03), 0 0 0 290px rgba(235,23,0,.025), 0 0 0 380px rgba(235,23,0,.015)',
            animation: 'dqe-ring 6s ease-in-out infinite',
          }} />
          <div className="absolute" style={{
            right: -180, top: '50%', transform: 'translateY(-50%)', width: 340, height: 340, borderRadius: '50%',
            border: '1.5px solid rgba(235,23,0,.12)', animation: 'dqe-ring 5s ease-in-out 1s infinite',
          }} />

          {/* BG: horizontal data streams */}
          <div className="absolute left-0 right-0" style={{ top: '28%', height: 1, overflow: 'hidden', opacity: .35 }}>
            <div style={{ height: 1, background: 'linear-gradient(90deg,transparent 0%,rgba(15,104,178,.6) 30%,rgba(15,104,178,.6) 70%,transparent 100%)', animation: 'dqe-stream 7s linear 0s infinite' }} />
          </div>
          <div className="absolute left-0 right-0" style={{ top: '44%', height: 1, overflow: 'hidden', opacity: .22 }}>
            <div style={{ height: 1, background: 'linear-gradient(90deg,transparent 0%,rgba(235,23,0,.5) 35%,rgba(235,23,0,.5) 65%,transparent 100%)', animation: 'dqe-stream 9s linear 2.5s infinite' }} />
          </div>
          <div className="absolute left-0 right-0" style={{ top: '66%', height: 1, overflow: 'hidden', opacity: .28 }}>
            <div style={{ height: 1, background: 'linear-gradient(90deg,transparent 0%,rgba(15,104,178,.5) 30%,rgba(15,104,178,.5) 70%,transparent 100%)', animation: 'dqe-stream 8s linear 1.2s infinite' }} />
          </div>

          {/* BG: radar sweep + dashed orbits */}
          <div className="absolute" style={{
            right: -180, top: '50%', width: 760, height: 760, borderRadius: '50%',
            background: 'conic-gradient(from 0deg,rgba(15,104,178,.07) 0deg,rgba(15,104,178,.03) 40deg,transparent 80deg,transparent 360deg)',
            animation: 'dqe-radar 12s linear infinite', transformOrigin: 'center center', transform: 'translateY(-50%)',
          }} />
          <div className="absolute" style={{ right: -180, top: '50%', width: 920, height: 920, borderRadius: '50%', border: '1.5px dashed rgba(15,104,178,.1)', animation: 'dqe-orbit 28s linear infinite' }} />
          <div className="absolute" style={{ right: -180, top: '50%', width: 480, height: 480, borderRadius: '50%', border: '1px dashed rgba(235,23,0,.1)', animation: 'dqe-radar 18s linear infinite' }} />

          {/* BG: scanline sweeps */}
          <div className="absolute left-0 right-0" style={{ height: '1.5px', background: 'linear-gradient(90deg,transparent 0%,rgba(15,104,178,.18) 25%,rgba(15,104,178,.3) 50%,rgba(15,104,178,.18) 75%,transparent 100%)', animation: 'dqe-scan 9s linear 0s infinite' }} />
          <div className="absolute left-0 right-0" style={{ height: 1, background: 'linear-gradient(90deg,transparent 0%,rgba(235,23,0,.15) 30%,rgba(235,23,0,.25) 50%,rgba(235,23,0,.15) 70%,transparent 100%)', animation: 'dqe-scan 9s linear 4.5s infinite' }} />

          {/* BG: vertical data streams */}
          <div className="absolute top-0 bottom-0" style={{ left: '62%', width: 1, overflow: 'hidden', opacity: .3 }}>
            <div style={{ width: 1, height: '100%', background: 'linear-gradient(180deg,transparent 0%,rgba(15,104,178,.55) 30%,rgba(15,104,178,.55) 70%,transparent 100%)', animation: 'dqe-vstream 6s linear 1s infinite' }} />
          </div>
          <div className="absolute top-0 bottom-0" style={{ left: '40%', width: 1, overflow: 'hidden', opacity: .18 }}>
            <div style={{ width: 1, height: '100%', background: 'linear-gradient(180deg,transparent 0%,rgba(235,23,0,.5) 35%,rgba(235,23,0,.5) 65%,transparent 100%)', animation: 'dqe-vstream 8s linear 3s infinite' }} />
          </div>

          {/* BG: floating data particles */}
          {[
            { right: '22%', top: '20%', size: 5, color: 'rgba(15,104,178,.4)',  dur: '5s',   delay: '0s' },
            { right: '38%', top: '35%', size: 4, color: 'rgba(235,23,0,.35)',  dur: '6.5s', delay: '.8s' },
            { right: '15%', top: '58%', size: 6, color: 'rgba(15,104,178,.3)', dur: '7s',   delay: '.4s' },
            { right: '45%', top: '72%', size: 3, color: 'rgba(235,23,0,.32)',  dur: '5.5s', delay: '1.6s' },
            { right: '28%', top: '12%', size: 4, color: 'rgba(15,104,178,.28)', dur: '8s',  delay: '2.2s' },
            { right: '52%', top: '52%', size: 5, color: 'rgba(235,23,0,.22)',  dur: '6s',   delay: '3s' },
            { right: '10%', top: '82%', size: 4, color: 'rgba(15,104,178,.25)', dur: '7.5s', delay: '1s' },
            { right: '33%', top: '88%', size: 3, color: 'rgba(235,23,0,.2)',   dur: '5.8s', delay: '4s' },
          ].map((p, i) => (
            <div key={i} className="absolute rounded-full" style={{ right: p.right, top: p.top, width: p.size, height: p.size, background: p.color, animation: `dqe-float ${p.dur} ease-in-out ${p.delay} infinite` }} />
          ))}

          {/* Animated canvas network graph, layered above the decorative backdrop */}
          <NetworkCanvas />

          {/* BG: fades for text legibility */}
          <div className="absolute inset-0" style={{ background: 'linear-gradient(100deg,rgba(247,245,242,.96) 0%,rgba(247,245,242,.88) 36%,rgba(247,245,242,.5) 58%,rgba(247,245,242,0) 100%)' }} />
          <div className="absolute bottom-0 left-0 right-0" style={{ height: 160, background: 'linear-gradient(to top,rgba(247,245,242,.92) 0%,transparent 100%)' }} />

          {/* ── Content layer ── */}
          <div className="relative flex flex-col h-full" style={{ zIndex: 2, padding: '36px 52px' }}>

            {/* Engine status */}
            <div className="flex items-center justify-between flex-wrap" style={{ gap: 16 }}>
              <div className="inline-flex items-center" style={{ gap: 8, padding: '7px 14px', borderRadius: 999, background: 'rgba(255,255,255,.68)', border: '1px solid #e8e6e3', backdropFilter: 'blur(10px)' }}>
                <span className="relative block flex-none rounded-full" style={{ width: 7, height: 7, background: '#328714' }}>
                  <span className="absolute inset-0 block rounded-full" style={{ background: '#328714', animation: 'dqe-pulse 2.6s ease-out infinite' }} />
                </span>
                <span className="font-johnson-text font-bold uppercase" style={{ fontSize: '10.5px', letterSpacing: '.08em', color: '#6e6259' }}>
                  Engine Online
                </span>
              </div>
            </div>

            {/* Hero typography */}
            <div className="flex-1 flex flex-col justify-center" style={{ padding: '20px 0' }}>
              <p className="font-johnson-text font-bold uppercase" style={{ fontSize: 11, letterSpacing: '.26em', color: '#0f68b2', margin: '0 0 28px' }}>
                Source-to-Target Validation
              </p>
              <h1 className="font-johnson-display" style={{ fontWeight: 700, fontSize: 'clamp(40px,4.6vw,72px)', lineHeight: .97, color: '#312c2a', margin: 0, letterSpacing: '-.025em' }}>
                Data Quality
              </h1>
              <h1 className="font-johnson-display italic" style={{ fontWeight: 300, fontSize: 'clamp(40px,4.6vw,72px)', lineHeight: .97, color: '#eb1700', margin: 0, letterSpacing: '-.025em' }}>
                Engine.
              </h1>
              <div style={{ margin: '28px 0' }}>
                <div style={{ width: 52, height: 3, background: '#eb1700' }} />
              </div>
              <p className="font-johnson-text" style={{ fontWeight: 300, fontSize: 'clamp(14px,1.1vw,17px)', lineHeight: 1.55, color: '#6e6259', maxWidth: 380, margin: '0 0 36px' }}>
                One platform, two lenses: automated IT checks confirm structural
                integrity, while business-defined checks apply your own data-quality
                rules — together catching anomalies before they reach production.
              </p>

              {/* Connected environments */}
              <div className="flex items-center flex-wrap" style={{ gap: 10 }}>
                <span className="font-johnson-text font-bold uppercase" style={{ fontSize: 10, letterSpacing: '.14em', color: '#a39992', marginRight: 4 }}>
                  Connected
                </span>
                {[
                  { label: 'DEV', dot: '#69d0ff' },
                  { label: 'QA',  dot: '#0f68b2' },
                  { label: 'PRD', dot: '#328714' },
                ].map(env => (
                  <span
                    key={env.label}
                    className="inline-flex items-center font-johnson-text font-bold"
                    style={{ gap: 6, padding: '5px 12px', borderRadius: 999, background: 'rgba(255,255,255,.72)', border: '1px solid #e8e6e3', fontSize: 11, letterSpacing: '.08em', color: '#312c2a' }}
                  >
                    <span className="block flex-none rounded-full" style={{ width: 6, height: 6, background: env.dot }} />
                    {env.label}
                  </span>
                ))}
              </div>
            </div>

            {/* Footer */}
            <p className="font-johnson-text" style={{ fontWeight: 300, fontSize: 11, color: '#a39992', margin: 0, letterSpacing: '.02em' }}>
              Confidential · Internal Use Only · © {new Date().getFullYear()} Regulatory Integrated Data · Johnson &amp; Johnson
            </p>
          </div>
        </div>

        {/* ═══ RIGHT PANEL ═══ */}
        <div
          className="relative flex-1 flex flex-col bg-white overflow-hidden"
          style={{ backgroundImage: 'radial-gradient(circle, rgba(203,196,188,.22) 1px, transparent 1px)', backgroundSize: '24px 24px', borderLeft: '1px solid #e8e6e3' }}
        >
          <div className="flex-1 flex flex-col justify-center mx-auto w-full" style={{ padding: '56px 52px', maxWidth: 480 }}>

            {/* Compact hero — shown only when the left panel is hidden (small screens) */}
            <div className="lg:hidden text-center mb-8">
              <div className="inline-flex items-center justify-center w-14 h-14 rounded-2xl bg-jj-red shadow-lg mb-4">
                <Icon src={shieldCheckIcon} alt="Shield" className="w-8 h-8 text-white" />
              </div>
              <h1 className="font-johnson-display text-2xl text-jj-gray-08" style={{ lineHeight: '110%' }}>
                Data Quality Engine
              </h1>
            </div>

            <p className="font-johnson-text font-bold uppercase" style={{ fontSize: '10.5px', letterSpacing: '.22em', color: '#a39992', margin: '0 0 14px' }}>
              Validation Suite
            </p>
            <h2 className="font-johnson-display" style={{ fontWeight: 700, fontSize: 26, lineHeight: 1.08, color: '#312c2a', margin: '0 0 8px', letterSpacing: '-.01em' }}>
              Choose your check type
            </h2>
            <p className="font-johnson-text" style={{ fontWeight: 400, fontSize: 14, color: '#81766f', margin: '0 0 36px', lineHeight: 1.5 }}>
              Select a suite to load its latest validation analysis.
            </p>

            {/* Check type options */}
            <div className="flex flex-col" style={{ gap: 14, marginBottom: 32 }}>
              {checkTypeOptions.map(item => {
                const selected = selectedCheckType === item.value;
                return (
                  <button
                    key={item.value}
                    onClick={() => handleCheckTypeChange(item.value)}
                    aria-pressed={selected}
                    className="flex items-center w-full text-left"
                    style={{
                      gap: 18, padding: '18px 20px', borderRadius: 14, cursor: 'pointer',
                      transition: 'box-shadow .18s ease, transform .18s ease, border-color .18s ease, background .18s ease',
                      background: selected ? '#fff5f4' : '#faf9f8',
                      border: selected ? '1.5px solid #eb1700' : '1.5px solid #e8e6e3',
                      boxShadow: selected ? '0 10px 28px -12px rgba(235,23,0,.28)' : '0 1px 3px rgba(49,44,42,.04)',
                      transform: selected ? 'translateX(3px)' : 'none',
                    }}
                  >
                    <span
                      className="flex-none text-right font-johnson-display italic"
                      style={{ fontWeight: 300, fontSize: 30, lineHeight: 1, width: 32, color: selected ? '#eb1700' : '#cbc4bc', transition: 'color .18s ease' }}
                    >
                      {item.num}
                    </span>
                    <span className="flex-1 min-w-0">
                      <span className="flex items-center" style={{ gap: 10, marginBottom: 6 }}>
                        <span className="flex items-center justify-center flex-none rounded-lg" style={{ width: 30, height: 30, background: 'rgba(235,23,0,.07)' }}>
                          <Icon src={barChartIcon} alt="" className="w-4 h-4 text-jj-red" />
                        </span>
                        <span className="font-johnson-display" style={{ fontWeight: 700, fontSize: 16, color: '#312c2a', letterSpacing: '-.01em' }}>
                          {item.label}
                        </span>
                      </span>
                      <span className="block font-johnson-text" style={{ fontSize: '12.5px', color: '#81766f', lineHeight: 1.45 }}>
                        {item.desc}
                      </span>
                    </span>
                    <span
                      className="flex-none flex items-center justify-center rounded-full"
                      style={{
                        width: 20, height: 20,
                        border: selected ? '2px solid #eb1700' : '2px solid #cbc4bc',
                        background: selected ? '#fff0ef' : 'transparent',
                        transition: 'border-color .18s ease, background .18s ease',
                      }}
                    >
                      {selected && <span className="block rounded-full" style={{ width: 8, height: 8, background: '#eb1700' }} />}
                    </span>
                  </button>
                );
              })}
            </div>

            {/* Error */}
            {loadError && (
              <div className="flex items-start gap-3 px-4 py-3 bg-red-50 rounded-lg border border-jj-maroon-05 border-opacity-30 mb-6">
                <Icon src={alertTriangleIcon} alt="Error" className="w-4 h-4 mt-0.5 flex-shrink-0 text-jj-maroon-05" />
                <p className="font-johnson-text text-sm text-jj-gray-08">{loadError}</p>
              </div>
            )}

            {/* Submit */}
            <button
              onClick={() => handleLoadData()}
              disabled={isLoading || !selectedCheckType}
              className="flex items-center justify-center w-full font-johnson-display"
              style={{
                gap: 10, padding: '15px 24px', borderRadius: 13, border: 'none', fontWeight: 700, fontSize: 16,
                letterSpacing: '-.01em', color: '#fff', cursor: (!selectedCheckType || isLoading) ? 'not-allowed' : 'pointer',
                transition: 'opacity .2s ease, box-shadow .2s ease',
                background: (!selectedCheckType || isLoading) ? '#cbc4bc' : '#eb1700',
                boxShadow: (!selectedCheckType || isLoading) ? 'none' : '0 12px 28px -10px rgba(235,23,0,.5)',
                opacity: (!selectedCheckType || isLoading) ? .7 : 1,
              }}
            >
              {isLoading ? (
                <>
                  <span className="block flex-none rounded-full" style={{ width: 17, height: 17, border: '2.5px solid rgba(255,255,255,.4)', borderTopColor: '#fff', animation: 'dqe-spin .75s linear infinite' }} />
                  Loading validation data…
                </>
              ) : (
                <>
                  Load Validation Analysis
                  {selectedCheckType && <Icon src={chevronRightIcon} alt="" className="w-4 h-4 text-white" />}
                </>
              )}
            </button>
          </div>

          {/* Right panel footer */}
          <div className="flex items-center justify-between flex-wrap" style={{ padding: '0 52px 32px', gap: 16 }}>
            <div className="flex items-center" style={{ gap: 8 }}>
              <Icon src={databaseIcon} alt="" className="w-3.5 h-3.5 text-jj-gray-05" style={{ opacity: .55 }} />
              <span className="font-johnson-text" style={{ fontSize: '11.5px', color: '#a39992' }}>
                DEV · QA · PRD environments
              </span>
            </div>
          </div>
        </div>
      </div>
    );
  }

  // ── Loading ──
  if (isLoading) return (
    <div className="flex items-center justify-center min-h-full bg-jj-gray-01">
      <div className="text-center">
        <LoadingSpinner />
        <p className="font-johnson-text mt-4 text-sm font-bold text-jj-gray-07">Loading validation data…</p>
      </div>
    </div>
  );

  // ── Business Checks ──
  if (selectedCheckType === 'Business_defined_checks') {
    if (!hasPermission('viewBusinessChecks')) return <Navigate to="/dashboard" replace />;
    return <BusinessDefinedChecks onBack={resetState} />;
  }

  if (!reportData) return null;

  // Guard: user could land here via direct URL with no IT Checks permission
  if (selectedCheckType === 'IT_Checks' && !hasPermission('viewITChecks')) {
    return <Navigate to="/dashboard" replace />;
  }

  // ──────────────────────────────────────────────────────────────────────────
  // MAIN VIEW
  // ──────────────────────────────────────────────────────────────────────────
  return (
    <ErrorBoundary>
      <div className="flex flex-col h-full bg-jj-gray-01">

        {/* ── Top Bar ── */}
        <div className="flex-shrink-0 bg-white border-b-2 border-jj-red shadow-sm">
          <div className="px-6 py-3 flex items-center justify-between gap-4 flex-wrap">
            <div className="flex items-center gap-4">
              <div className="w-10 h-10 bg-jj-red rounded-lg flex items-center justify-center flex-shrink-0">
                <Icon src={shieldCheckIcon} alt="DQE" className="w-5 h-5 text-white" />
              </div>
              <div>
                <h1 className="font-johnson-display text-lg text-jj-gray-08" style={{ lineHeight: '110%' }}>
                  Data Quality Engine
                </h1>
                <div className="flex items-center gap-3 font-johnson-text text-xs text-jj-gray-06 flex-wrap">
                  <span>IT Checks</span>
                  {runsMerged > 1 ? (
                    <>
                      <span>·</span>
                      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full font-bold bg-jj-blue-03 bg-opacity-10 text-jj-blue-03 border border-jj-blue-03 border-opacity-20">
                        {runsMerged} runs combined
                      </span>
                      {oldestDate && newestDate && (
                        <>
                          <span>·</span>
                          <span className="flex items-center gap-1">
                            <Icon src={clockIcon} alt="Range" className="w-3 h-3 text-jj-gray-05" />
                            {new Date(oldestDate).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })}
                            {' → '}
                            {new Date(newestDate).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })}
                          </span>
                        </>
                      )}
                    </>
                  ) : (
                    reportData.report_metadata?.generated_timestamp && (
                      <><span>·</span>
                      <span className="flex items-center gap-1.5">
                        <Icon src={clockIcon} alt="Time" className="w-3 h-3 text-jj-gray-05" />
                        {formatTimestamp(reportData.report_metadata.generated_timestamp)}
                      </span></>
                    )
                  )}
                </div>
              </div>
            </div>
            <div className="flex items-center gap-2 flex-wrap">
              {/* ── Run History Picker ── */}
              {availableRuns.length > 0 && (
                <div className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg bg-jj-gray-01 border border-jj-gray-02 relative">
                  <Icon src={clockIcon} alt="Run" className="w-3.5 h-3.5 text-jj-gray-06 flex-shrink-0" />
                  <span className="font-johnson-text text-xs font-bold text-jj-gray-06 whitespace-nowrap">View:</span>

                  {/* Custom multi-select trigger */}
                  <button
                    onClick={() => setRunPickerOpen(p => !p)}
                    className="font-johnson-text text-xs text-jj-gray-08 flex items-center gap-1 max-w-[200px] truncate"
                  >
                    <span className="truncate">
                      {pendingRuns.length === 0
                        ? `All runs combined (${availableRuns.length})`
                        : pendingRuns.length === 1
                          ? (() => { const r = availableRuns.find(x => x.load_date === pendingRuns[0]); return r ? `${new Date(r.load_date).toLocaleString('en-US', { month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit', hour12: true })}${r.database ? ` · ${r.database}` : ''}` : '1 run'; })()
                          : `${pendingRuns.length} runs selected`
                      }
                    </span>
                    <Icon src={chevronDownIcon} alt="▾" className={`w-3 h-3 text-jj-gray-05 flex-shrink-0 transition-transform ${runPickerOpen ? 'rotate-180' : ''}`} />
                  </button>

                  {/* Dropdown panel */}
                  {runPickerOpen && (
                    <>
                      <div className="fixed inset-0 z-[60]" onClick={() => setRunPickerOpen(false)} />
                      <div className="absolute top-full right-0 mt-1 w-80 bg-white border border-jj-gray-03 rounded-lg shadow-xl z-[70] overflow-hidden">
                        {/* All combined option */}
                        <div
                          onClick={() => setPendingRuns([])}
                          className={`flex items-center gap-2.5 px-3 py-2.5 cursor-pointer border-b border-jj-gray-02 hover:bg-jj-gray-01 transition-colors ${pendingRuns.length === 0 ? 'bg-red-50' : ''}`}
                        >
                          <div className={`w-4 h-4 rounded border-2 flex items-center justify-center flex-shrink-0 transition-colors ${pendingRuns.length === 0 ? 'bg-jj-red border-jj-red' : 'border-jj-gray-04'}`}>
                            {pendingRuns.length === 0 && <Icon src={checkCircleIcon} alt="✓" className="w-3 h-3 text-white" />}
                          </div>
                          <div>
                            <p className="font-johnson-text text-xs font-bold text-jj-gray-08">All runs combined</p>
                            <p className="font-johnson-text text-xs text-jj-gray-05">{availableRuns.length} runs merged, newest wins</p>
                          </div>
                        </div>

                        {/* Individual run checkboxes */}
                        <div className="max-h-56 overflow-y-auto">
                          {availableRuns.map((run, i) => {
                            const checked = pendingRuns.includes(run.load_date);
                            const dateLabel = run.load_date
                              ? new Date(run.load_date).toLocaleString('en-US', { month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit', hour12: true })
                              : `Run ${availableRuns.length - i}`;
                            return (
                              <div
                                key={run.load_date}
                                onClick={() => setPendingRuns(prev =>
                                  prev.includes(run.load_date)
                                    ? prev.filter(d => d !== run.load_date)
                                    : [...prev, run.load_date]
                                )}
                                className={`flex items-center gap-2.5 px-3 py-2 cursor-pointer hover:bg-jj-gray-01 transition-colors ${checked ? 'bg-red-50' : ''}`}
                              >
                                <div className={`w-4 h-4 rounded border-2 flex items-center justify-center flex-shrink-0 transition-colors ${checked ? 'bg-jj-red border-jj-red' : 'border-jj-gray-04'}`}>
                                  {checked && <Icon src={checkCircleIcon} alt="✓" className="w-3 h-3 text-white" />}
                                </div>
                                <div className="min-w-0 flex-1">
                                  <p className="font-johnson-text text-xs font-bold text-jj-gray-08 truncate">
                                    {dateLabel}{i === 0 ? ' (latest)' : ''}
                                  </p>
                                  {run.database && (
                                    <p className="font-johnson-text text-xs text-jj-gray-05">{run.database}</p>
                                  )}
                                </div>
                              </div>
                            );
                          })}
                        </div>

                        {/* Apply footer */}
                        <div className="border-t border-jj-gray-02 px-3 py-2 flex items-center justify-between bg-jj-gray-01">
                          <span className="font-johnson-text text-xs text-jj-gray-06">
                            {pendingRuns.length === 0 ? 'All runs' : `${pendingRuns.length} run${pendingRuns.length > 1 ? 's' : ''} selected`}
                          </span>
                          <button
                            onClick={() => {
                              setRunPickerOpen(false);
                              handleLoadData(pendingRuns.length > 0 ? pendingRuns : null);
                            }}
                            className="px-3 py-1.5 rounded-lg bg-jj-red text-white font-johnson-text text-xs font-bold hover:bg-jj-maroon-05 transition-colors"
                          >
                            Apply
                          </button>
                        </div>
                      </div>
                    </>
                  )}
                </div>
              )}
              {runsLoading && (
                <Icon src={activityIcon} alt="Loading runs" className="w-4 h-4 text-jj-gray-05 animate-spin" />
              )}
              {hasPermission('runITChecks') && (
                <button
                  onClick={() => { setPendingRuns([]); handleLoadData(); }} disabled={isLoading}
                  className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg font-johnson-text text-xs font-bold text-jj-gray-06 bg-gray-50 hover:bg-gray-100 transition-colors border border-jj-gray-06 border-opacity-20"
                >
                  <Icon src={refreshIcon} alt="Refresh" className={`w-4 h-4 text-jj-gray-06 ${isLoading ? 'animate-spin' : ''}`} />
                  Refresh
                </button>
              )}
              {sortedTables.length > 0 && (
                <button
                  onClick={handleExportExcel}
                  className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg font-johnson-text text-xs font-bold text-jj-gray-06 bg-gray-50 hover:bg-gray-100 transition-colors border border-jj-gray-06 border-opacity-20"
                >
                  <Icon src={downloadIcon} alt="Export" className="w-4 h-4 text-jj-gray-06" />
                  Export Excel
                </button>
              )}
              {hasPermission('runITChecks') && (
                <button
                  onClick={() => setShowITValidationSelector(true)}
                  className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg font-johnson-text text-xs font-bold text-white bg-jj-green-03 hover:bg-jj-green-05 transition-colors"
                >
                  <Icon src={runIcon} alt="Run" className="w-4 h-4" color="jj-white" />
                  Run Validation
                </button>
              )}
              {availableCheckTypes.length > 1 && (
              <button
                onClick={resetState}
                className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg font-johnson-text text-xs font-bold text-jj-gray-07 bg-jj-gray-01 hover:bg-jj-gray-02 transition-colors border border-jj-gray-03 disabled:opacity-50"
              >
                <Icon src={databaseIcon} alt="Change" className="w-4 h-4 text-jj-gray-06" />
                Switch Check Type
              </button>
              )}
            </div>
          </div>
        </div>

        {/* ── Scrollable Content ── */}
        <div className="flex-1 overflow-y-auto">
          <div className="px-6 py-4 space-y-3 mx-auto">

            {/* ── Summary Cards ── */}
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
              {[
                { title: 'Total Tables', value: summary.total,    icon: activityIcon,      colorClass: 'text-jj-gray-08',    borderColor: 'border-l-jj-gray-06',   onClick: () => setStatusFilter(STATUS.ALL) },
                { title: 'Passed',       value: summary.passed,   icon: checkCircleIcon,   colorClass: 'text-jj-green-03',   borderColor: 'border-l-jj-green-03',  onClick: () => setStatusFilter(STATUS.PASSED) },
                { title: 'Critical',     value: summary.critical, icon: xCircleIcon,       colorClass: 'text-jj-maroon-05',  borderColor: 'border-l-jj-maroon-05', onClick: () => setStatusFilter(STATUS.CRITICAL) },
                { title: 'Faulty',       value: summary.faulty,   icon: alertTriangleIcon, colorClass: 'text-jj-orange-03',  borderColor: 'border-l-jj-orange-03', onClick: () => setStatusFilter(STATUS.FAULTY) },
                { title: 'Errors',       value: summary.errors,   icon: xCircleIcon,       colorClass: 'text-jj-maroon-05',  borderColor: 'border-l-jj-maroon-05', onClick: () => setStatusFilter(STATUS.ERROR) },
                { title: 'Not Found',    value: summary.notFound, icon: alertTriangleIcon, colorClass: 'text-jj-orange-03',  borderColor: 'border-l-jj-orange-03', onClick: () => setStatusFilter(STATUS.TIMEOUT) },
              ].map((card, i) => <SummaryCard key={i} {...card} />)}
            </div>

            {/* ── Filters Panel ── */}
            <div className="bg-white rounded-xl border border-jj-gray-02 overflow-visible shadow-sm">
              <div
                onClick={() => setFiltersExpanded(!filtersExpanded)}
                className="w-full px-5 py-4 flex items-center justify-between hover:bg-jj-gray-01 transition-colors rounded-t-xl cursor-pointer select-none"
              >
                {/* Left: red accent bar + title + subtitle */}
                <div className="flex items-center gap-3 min-w-0">
                  <div className="w-1 h-8 rounded-full bg-jj-red flex-shrink-0" />
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="font-johnson-display text-base text-jj-gray-08" style={{ lineHeight: '110%' }}>
                        Filter Options
                      </span>
                      {activeFilterCount > 0 && (
                        <span className="px-2 py-0.5 rounded-full font-johnson-text text-xs font-bold bg-jj-red text-white">
                          {activeFilterCount} active
                        </span>
                      )}
                    </div>
                    <p className="font-johnson-text text-xs text-jj-gray-05 mt-0.5">
                      Narrow results by source schema, target database, or column
                    </p>
                  </div>
                </div>

                {/* Right: Clear All + chevron as separate controls */}
                <div className="flex items-center gap-2 flex-shrink-0">
                  <button
                    onClick={e => { e.stopPropagation(); handleClearAllFilters(e); }}
                    className="font-johnson-text text-xs font-bold text-jj-red hover:text-jj-maroon-05 transition-colors border border-jj-red border-opacity-30 flex items-center gap-1.5 px-3 py-1.5 rounded-lg hover:bg-red-50"
                  >
                    <Icon src={xCircleIcon} alt="Clear" className="w-3.5 h-3.5 text-jj-red" />
                    Clear All
                  </button>
                  <div className="w-8 h-8 rounded-lg border border-jj-gray-02 bg-jj-gray-01 flex items-center justify-center flex-shrink-0">
                    <Icon
                      src={filtersExpanded ? chevronDownIcon : chevronRightIcon}
                      alt="Toggle"
                      className="w-4 h-4 text-jj-gray-06"
                    />
                  </div>
                </div>
              </div>

              {filtersExpanded && (
                <div className="border-t border-jj-gray-02 px-5 py-5 space-y-5">
                  {/* Source */}
                  <div>
                    <div className="flex items-center gap-2 mb-3">
                      <div className="w-2 h-2 rounded-full bg-jj-blue-03 flex-shrink-0"></div>
                      <span className="font-johnson-display text-sm text-jj-blue-03" style={{ lineHeight: '110%' }}>Source (Oracle)</span>
                    </div>
                    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
                      <div>
                        <label className="font-johnson-text text-xs font-bold text-jj-gray-07 mb-1.5 block">Database</label>
                        <div className="w-full px-3 py-2 rounded-lg text-sm font-johnson-text bg-jj-gray-01 text-jj-gray-05 border border-jj-gray-02 select-none">
                          Insight7, RIMDOCS
                        </div>
                      </div>
                      <MultiSelectDropdown label="Schema" options={availableSourceSchemas} selected={selectedSourceSchemas} onChange={setSelectedSourceSchemas} placeholder="Select schemas…" />
                      <MultiSelectDropdown label="Table" options={availableSourceTables} selected={selectedSourceTables} onChange={setSelectedSourceTables} disabled={selectedSourceSchemas.length === 0} placeholder={selectedSourceSchemas.length === 0 ? "Select schema first" : "Select tables…"} />
                      <MultiSelectDropdown label="Columns (optional)" options={availableSourceColumns} selected={selectedSourceColumns} onChange={setSelectedSourceColumns} disabled={selectedSourceTables.length === 0} placeholder={selectedSourceTables.length === 0 ? "Select table first" : "Select columns…"} />
                    </div>
                  </div>

                  {/* Target */}
                  <div>
                    <div className="flex items-center justify-between mb-3">
                      <div className="flex items-center gap-2">
                        <div className="w-2 h-2 rounded-full bg-jj-red flex-shrink-0"></div>
                        <span className="font-johnson-display text-sm text-jj-red" style={{ lineHeight: '110%' }}>Target (Snowflake)</span>
                      </div>
                      {(selectedSourceSchemas.length > 0 || selectedSourceTables.length > 0) && (
                        <span className="font-johnson-text text-xs text-jj-blue-03 bg-blue-50 px-2.5 py-1 rounded-full flex items-center gap-1.5">
                          <Icon src={infoIcon} alt="Info" className="w-3 h-3 text-jj-blue-03" />
                          Filtered by source
                        </span>
                      )}
                    </div>
                    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
                      <MultiSelectDropdown label="Database" options={availableTargetDatabases} selected={selectedTargetDatabases} onChange={setSelectedTargetDatabases} placeholder="Select databases…" />
                      <MultiSelectDropdown label="Schema" options={availableTargetSchemas} selected={selectedTargetSchemas} onChange={setSelectedTargetSchemas} disabled={selectedTargetDatabases.length === 0} placeholder={selectedTargetDatabases.length === 0 ? "Select database first" : "Select schemas…"} />
                      <MultiSelectDropdown label="Table" options={availableTargetTables} selected={selectedTargetTables} onChange={setSelectedTargetTables} disabled={selectedTargetSchemas.length === 0} placeholder={selectedTargetSchemas.length === 0 ? "Select schema first" : "Select tables…"} />
                      <MultiSelectDropdown label="Columns (optional)" options={availableTargetColumns} selected={selectedTargetColumns} onChange={setSelectedTargetColumns} disabled={selectedTargetTables.length === 0} placeholder={selectedTargetTables.length === 0 ? "Select table first" : "Select columns…"} />
                    </div>
                  </div>
                </div>
              )}
            </div>

            {/* ── Column Details ── */}
            <ColumnDetailsSection tables={filteredTables} selectedSourceColumns={selectedSourceColumns} selectedTargetColumns={selectedTargetColumns} />

            {/* ── Search + Status Bar ── */}
            <div className="flex flex-col sm:flex-row items-stretch sm:items-center gap-3 bg-white rounded-xl border border-jj-gray-02 px-3 py-3 shadow-sm">
              <div className="flex-1 relative">
                <Icon src={searchIcon} alt="Search" className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-jj-gray-05" />
                <input
                  ref={searchInputRef}
                  type="text"
                  placeholder="Search tables… (Ctrl+F)"
                  value={searchTerm}
                  onChange={e => setSearchTerm(e.target.value)}
                  className="w-full pl-9 pr-9 py-2 rounded-lg font-johnson-text text-sm bg-jj-gray-01 border border-jj-gray-02 text-jj-gray-08 outline-none focus:border-jj-red focus:ring-2 focus:ring-jj-red focus:ring-opacity-10 transition-all"
                />
                {isSearching && (
                  <div className="absolute right-3 top-1/2 -translate-y-1/2">
                    <Icon src={activityIcon} alt="Searching" className="w-4 h-4 text-jj-gray-05 animate-spin" />
                  </div>
                )}
              </div>
              <div className="relative">
                <select
                  value={statusFilter}
                  onChange={e => setStatusFilter(e.target.value)}
                  className="font-johnson-text px-3 py-2 pr-8 rounded-lg text-sm bg-jj-gray-01 border border-jj-gray-02 text-jj-gray-08 outline-none focus:border-jj-red appearance-none w-full sm:w-auto"
                >
                  <option value={STATUS.ALL}>All Statuses</option>
                  <option value={STATUS.PASSED}>Passed</option>
                  <option value={STATUS.CRITICAL}>Critical</option>
                  <option value={STATUS.ERROR}>Error</option>
                  <option value={STATUS.FAULTY}>Faulty</option>
                  <option value={STATUS.TIMEOUT}>Timeout</option>
                </select>
                <Icon src={chevronDownIcon} alt="▾" className="absolute right-2 top-1/2 -translate-y-1/2 w-4 h-4 text-jj-gray-05 pointer-events-none" />
              </div>
            </div>

            {/* ── Pagination (top) ── */}
            {filteredTables.length > 0 && (
              <PaginationControls currentPage={currentPage} totalPages={totalPages} pageSize={pageSize}
                totalItems={filteredTables.length} onPageChange={handlePageChange} onPageSizeChange={handlePageSizeChange} showPageSize />
            )}

            {/* ── Table ── */}
            <div className="bg-white rounded-xl border border-jj-gray-04 shadow-sm overflow-hidden">
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b-2 border-jj-gray-02">
                      {[
                        { label: 'Source Table',    key: 'table_name',      align: 'left',   minW: '180px' },
                        { label: 'Snowflake Table', key: 'snowflake_table', align: 'left',   minW: '180px' },
                        { label: 'Status',          key: 'status',          align: 'center', minW: '110px' },
                        { label: 'Source Rows',     key: 'source_rows',     align: 'center', minW: '110px' },
                        { label: 'Snowflake Rows',  key: 'sf_rows',         align: 'center', minW: '120px' },
                        { label: 'Last Validated',  key: 'last_checked',    align: 'center', minW: '160px' },
                      ].map(({ label, key, align, minW }) => {
                        const active = tableSortConfig.key === key;
                        const asc    = active && tableSortConfig.direction === 'asc';
                        return (
                          <th
                            key={key}
                            onClick={() => handleTableSort(key)}
                            style={{ minWidth: minW }}
                            className={`px-4 py-3 font-johnson-text text-xs font-bold text-jj-gray-08 bg-jj-gray-01 cursor-pointer select-none hover:bg-jj-gray-02 transition-colors ${align === 'left' ? 'text-left' : 'text-center'}`}
                          >
                            <span className={`inline-flex items-center gap-1 ${align === 'center' ? 'justify-center w-full' : ''}`}>
                              {label}
                              <span className="flex flex-col leading-none ml-0.5">
                                <svg width="7" height="5" viewBox="0 0 7 5" className={active && asc  ? 'opacity-100' : 'opacity-25'}><path d="M3.5 0L7 5H0z" fill={active && asc  ? '#eb1700' : 'currentColor'}/></svg>
                                <svg width="7" height="5" viewBox="0 0 7 5" className={active && !asc ? 'opacity-100' : 'opacity-25'}><path d="M3.5 5L0 0h7z" fill={active && !asc ? '#eb1700' : 'currentColor'}/></svg>
                              </span>
                            </span>
                          </th>
                        );
                      })}
                      <th className="px-4 py-3 text-center font-johnson-text text-xs font-bold text-jj-gray-08 bg-jj-gray-01 min-w-[130px]">Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {paginatedTables.map(table => (
                      <TableRow
                        key={`${table.pipeline_name}_${table.table_name}`}
                        table={table} onSelect={handleTableSelect}
                        onRefresh={handleRefreshTable} refreshingTable={refreshingTable}
                        canRefresh={canRefreshTable(table)}
                      />
                    ))}
                  </tbody>
                </table>
              </div>

              {filteredTables.length === 0 && (
                <div className="text-center py-16 px-4">
                  <div className="w-16 h-16 rounded-full bg-jj-gray-01 flex items-center justify-center mx-auto mb-4">
                    <Icon src={databaseIcon} alt="Empty" className="w-9 h-9 text-jj-gray-04" />
                  </div>
                  <p className="font-johnson-display text-lg text-jj-gray-07 mb-1" style={{ lineHeight: '110%' }}>
                    {searchTerm ? `No tables match "${searchTerm}"` : statusFilter !== STATUS.ALL ? `No tables with status: ${statusFilter}` : 'No tables available'}
                  </p>
                  <p className="font-johnson-text text-sm text-jj-gray-05 mb-4">
                    {activeFilterCount > 0 ? 'Try adjusting your filters or search term' : 'Please check back later or contact support'}
                  </p>
                  {(searchTerm || activeFilterCount > 0) && (
                    <button onClick={handleClearAllFilters}
                      className="font-johnson-text text-xs font-bold text-jj-red hover:text-jj-maroon-05 transition-colors border border-jj-red border-opacity-30 px-4 py-2 rounded-lg hover:bg-red-50">
                      Clear all filters
                    </button>
                  )}
                </div>
              )}
            </div>

            {/* ── Pagination (bottom) ── */}
            {filteredTables.length > 0 && (
              <PaginationControls currentPage={currentPage} totalPages={totalPages} pageSize={pageSize}
                totalItems={filteredTables.length} onPageChange={handlePageChange} onPageSizeChange={handlePageSizeChange} />
            )}
          </div>
        </div>
      </div>

      {selectedTable && <TableDetailModal table={selectedTable} onClose={() => setSelectedTable(null)} />}
      {toast && <Toast message={toast.message} type={toast.type} onClose={() => setToast(null)} />}
      {showITValidationSelector && (
        <ITValidationSelectorModal
          onClose={() => setShowITValidationSelector(false)}
          onConfirm={(dbs) => {
            setShowITValidationSelector(false);
            setItValidationDbs(dbs);
          }}
        />
      )}
      {itValidationDbs && (
        <ITValidationModal
          API_BASE_URL={API_BASE_URL}
          databases={itValidationDbs}
          onClose={() => {
            setItValidationDbs(null);
            handleLoadData();
          }}
        />
      )}
    </ErrorBoundary>
  );
}

export default DataQualityEngine;