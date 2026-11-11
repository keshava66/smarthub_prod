import React, { useState, useEffect, useMemo, useRef } from 'react';
import ReactDOM from 'react-dom';
import { useSearchParams } from 'react-router-dom';
import { useRBAC } from '../context/RBACContext';
import { useAuth } from '../context/AuthContext';
import mailSrc     from "/icons/JJ_Icon_Communication_RGB.svg"; // Email / phone fields
import userSrc     from "/icons/JJ_Icon_user_black.svg";     // Job / work fields
import buildingSrc from "/icons/JJ_Icon_Office_RGB.svg";        // Org / location fields
import phoneSrc    from "/icons/JJ_Icon_Phone_RGB.svg";        // Org / location fields
import globeSrc    from "/icons/JJ_Icon_Globe_RGB.svg";        // Org / location fields
import closeIcon   from "/icons/JJ_Icon_Close.svg";
import plusIcon    from "/icons/JJ_Icon_Add.svg";
import editIcon    from "/icons/JJ_Icon_Edit_RGB.svg";
import trashIcon   from "/icons/JJ_Icon_Delete_RGB.svg";
import saveIcon    from "/icons/JJ_Icon_Save_RGB.svg";
import alertIcon   from "/icons/JJ_Icon_Alert_RGB.svg";
import checkIcon   from "/icons/JJ_Icon_Check_RGB.svg";
import searchIcon  from "/icons/JJ_Icon_Search_RGB.svg";
import chevronLeftIcon  from "/icons/JJ_Icon_Chevron_Left.svg";
import chevronRightIcon from "/icons/JJ_Icon_Chevron_Right.svg";
import userPlusIcon     from "/icons/JJ_Icon_Person_RGB.svg";
import shieldCheckIcon  from "/icons/JJ_Icon_Safety.svg";
import refreshSvgIcon   from "/icons/JJ_Icon_Refresh_RGB.svg";
import { useMsal } from '@azure/msal-react';

import shieldIcon from "/icons/JJ_Icon_Gear_RGB.svg";
import groupIcon  from "/icons/JJ_Icon_Community_RGB.svg";
import Shield     from "/icons/JJ_Icon_Shield_RGB.svg";

// ─── Shared helpers ──────────────────────────────────────────────────────────
const formatPerm = (key) => key.replace(/([A-Z])/g, ' $1').replace(/^./, s => s.toUpperCase());

// ─── Permission hierarchy definition ────────────────────────────────────────
// Each group has a parent permission and its child sub-permissions.
// Standalone items have no children.
const PERMISSION_GROUPS = [
  {
    key: 'viewDashboard',
    label: 'Dashboard',
    description: 'Access to the main dashboard',
    children: [],
  },
  {
    key: 'viewDQE',
    label: 'Data Quality Engine',
    description: 'Access to DQE and its sub-features',
    children: [
      { key: 'viewITChecks', label: 'View IT Checks', description: 'View IT-defined quality checks',
        children: [
          { key: 'runITChecks', label: 'Run IT Validations', description: 'Trigger re-validation on individual tables and refresh its full report' },
        ],
      },
      { key: 'viewBusinessChecks', label: 'View Business Checks', description: 'View business-defined quality checks',
        children: [
          { key: 'runBDCOperations', label: 'Run Validation Operations', description: 'Run Compilation and Validation' },
          { key: 'viewBDCSuperuser', label: 'View Superuser Tab',      description: 'Access the Superuser configuration tab' },
        ],
      },
    ],
  },
  {
    key: 'manageDataAPIExports',
    label: 'Data API Export',
    description: 'Publish tables as APIs, manage keys and watch their usage',
    children: [],
  },
  {
    key: 'viewAudit',
    label: 'Audit Log',
    description: 'Access to audit log',
    children: [],
  },
  {
    key: 'viewUserManagement',
    label: 'User Management',
    description: 'Access to user management',
    children: [
      { key: 'manageUsers', label: 'Manage Users', description: 'Assign and remove user roles' },
      { key: 'manageRoles', label: 'Manage Roles', description: 'Create, edit and delete roles' },
    ],
  },
];

// ─── PermissionTree component ────────────────────────────────────────────────
const PermissionTree = ({ permissions, onChange }) => {

  const handleParentClick = (group) => {
    const next = !permissions[group.key];
    const updates = { [group.key]: next };
    if (!next) {
      (group.children || []).forEach(child => {
        updates[child.key] = false;
        (child.children || []).forEach(gc => { updates[gc.key] = false; });
      });
    }
    onChange(updates);
  };

  const handleChildClick = (child, parentOn) => {
    if (!parentOn) return;
    const next = !permissions[child.key];
    const updates = { [child.key]: next };
    if (!next) {
      (child.children || []).forEach(gc => { updates[gc.key] = false; });
    }
    onChange(updates);
  };

  const handleGrandchildClick = (gc, childOn) => {
    if (!childOn) return;
    onChange({ [gc.key]: !permissions[gc.key] });
  };

  const Checkbox = ({ checked, size = 18 }) => (
    <div style={{
      width: size, height: size, borderRadius: 4, flexShrink: 0,
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      border: checked ? 'none' : '2px solid #d5cfc9',
      backgroundColor: checked ? '#eb1700' : '#ffffff',
      transition: 'all 0.15s',
    }}>
      {checked && (
        <svg width="9" height="7" viewBox="0 0 9 7" fill="none">
          <path d="M1 3.5L3.2 5.5L8 1" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      )}
    </div>
  );

  const LockTag = ({ label }) => (
    <div className="flex items-center gap-1.5 flex-shrink-0">
      <svg width="10" height="12" viewBox="0 0 10 12" fill="none">
        <rect x="1" y="5" width="8" height="7" rx="1.5" stroke="#cbc4bc" strokeWidth="1.5" />
        <path d="M3 5V3.5C3 2.4 3.9 1.5 5 1.5C6.1 1.5 7 2.4 7 3.5V5" stroke="#cbc4bc" strokeWidth="1.5" strokeLinecap="round" />
      </svg>
      <span className="text-xs font-johnson-text text-jj-gray-04">Requires {label}</span>
    </div>
  );

  return (
    <div className="space-y-1.5">
      {PERMISSION_GROUPS.map((group) => {
        const parentOn = !!permissions[group.key];
        const hasChildren = (group.children || []).length > 0;

        return (
          <div key={group.key}>

            {/* ── Level 1: Parent ── */}
            <div
              onClick={() => handleParentClick(group)}
              className={`flex items-center gap-3 px-4 py-3 rounded-lg border cursor-pointer transition-all duration-150 ${
                parentOn
                  ? 'bg-jj-gray-01 border-jj-gray-03 border-l-4 border-l-jj-red'
                  : 'bg-white border-jj-gray-03 hover:bg-jj-gray-01'
              }`}
            >
              <Checkbox checked={parentOn} />
              <div className="flex-1 min-w-0">
                <p className="text-sm font-johnson-text text-jj-gray-08">{group.label}</p>
                <p className="text-xs font-johnson-text text-jj-gray-05 mt-0.5">{group.description}</p>
              </div>
              {hasChildren && (
                <span className="text-xs font-johnson-text px-2.5 py-1 rounded-full bg-jj-gray-01 border border-jj-gray-03 text-jj-gray-05">
                  {group.children.length} sub-feature{group.children.length > 1 ? 's' : ''}
                </span>
              )}
            </div>

            {/* ── Level 2 + Level 3 ── */}
            {hasChildren && (
              <div className="ml-7 mt-1">
                {group.children.map((child, idx) => {
                  const isLast = idx === group.children.length - 1;
                  const childOn = !!permissions[child.key];
                  const childDisabled = !parentOn;
                  const hasGrandchildren = (child.children || []).length > 0;

                  return (
                    <div
                      key={child.key}
                      style={{
                        position: 'relative',
                        // flow-root creates a new block formatting context — prevents child
                        // margins (e.g. mb-1 on grandchild container) from collapsing OUT of
                        // this wrapper, which would create a gap between siblings and break
                        // the absolute connector line.
                        display: 'flow-root',
                      }}
                    >
                      {/* Vertical line for non-last: full height of wrapper (top→bottom).
                          Works correctly because flow-root above contains child margins. */}
                      {!isLast && (
                        <div className="absolute bg-jj-gray-03"
                          style={{ left: '-16px', top: 0, bottom: 0, width: '2px' }} />
                      )}

                      {/* ── Level-2 row ── */}
                      <div style={{ position: 'relative', marginBottom: '4px' }}>
                        {/* Last item: "└" — only top-half of the row height */}
                        {isLast && (
                          <div className="absolute bg-jj-gray-03"
                            style={{ left: '-16px', top: 0, height: '50%', width: '2px' }} />
                        )}
                        {/* Horizontal branch */}
                        <div className="absolute bg-jj-gray-03"
                          style={{ left: '-16px', top: '50%', transform: 'translateY(-50%)', width: '16px', height: '2px' }} />

                        <div
                          onClick={() => handleChildClick(child, parentOn)}
                          className={`flex items-center gap-3 px-4 py-2.5 rounded-lg border transition-all duration-150 ${
                            childDisabled
                              ? 'bg-jj-gray-01 border-jj-gray-02 cursor-not-allowed opacity-40'
                              : childOn
                                ? 'bg-jj-gray-01 border-jj-gray-02 cursor-pointer'
                                : 'bg-white border-jj-gray-02 cursor-pointer hover:bg-jj-gray-01'
                          }`}
                        >
                          <Checkbox checked={childOn && !childDisabled} size={16} />
                          <div className="flex-1 min-w-0">
                            <p className="text-sm font-johnson-text text-jj-gray-08">{child.label}</p>
                            <p className="text-xs font-johnson-text text-jj-gray-05 mt-0.5">{child.description}</p>
                          </div>
                          {childDisabled
                            ? <LockTag label={group.label} />
                            : hasGrandchildren && (
                              <span className="text-xs font-johnson-text px-2 py-0.5 rounded-full bg-jj-gray-01 border border-jj-gray-03 text-jj-gray-05">
                                {child.children.length} more
                              </span>
                            )
                          }
                        </div>
                      </div>

                      {/* ── Level-3 grandchildren ── */}
                      {hasGrandchildren && (
                        <div className="ml-7 mt-1 mb-1 space-y-1" style={{ position: 'relative' }}>
                          <div className="absolute bg-jj-gray-02"
                            style={{ left: '-16px', top: 0, bottom: '18px', width: '2px', borderRadius: '2px' }} />
                          {child.children.map((gc) => {
                            const gcOn = !!permissions[gc.key];
                            const gcDisabled = !childOn || childDisabled;
                            return (
                              <div key={gc.key} style={{ position: 'relative' }}>
                                <div className="absolute bg-jj-gray-02"
                                  style={{ left: '-16px', top: '50%', width: '16px', height: '2px' }} />
                                <div
                                  onClick={() => handleGrandchildClick(gc, childOn && !childDisabled)}
                                  className={`flex items-center gap-3 px-4 py-2 rounded-lg border transition-all duration-150 ${
                                    gcDisabled
                                      ? 'bg-jj-gray-01 border-jj-gray-02 cursor-not-allowed opacity-40'
                                      : gcOn
                                        ? 'bg-jj-gray-01 border-jj-gray-02 cursor-pointer'
                                        : 'bg-white border-jj-gray-02 cursor-pointer hover:bg-jj-gray-01'
                                  }`}
                                >
                                  <Checkbox checked={gcOn && !gcDisabled} size={14} />
                                  <div className="flex-1 min-w-0">
                                    <p className="text-sm font-johnson-text text-jj-gray-08">{gc.label}</p>
                                    <p className="text-xs font-johnson-text text-jj-gray-05 mt-0.5">{gc.description}</p>
                                  </div>
                                  {gcDisabled && <LockTag label={child.label} />}
                                </div>
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}

          </div>
        );
      })}
    </div>
  );
};

const Icon = ({ src, alt = '', className = 'w-5 h-5', style = {} }) => (
  <img src={src} alt={alt} className={className} style={style} />
);

const MorePermissionsTag = ({ remaining }) => {
  const [visible, setVisible] = useState(false);
  const [coords, setCoords] = useState({ top: 0, left: 0 });
  const tagRef = useRef(null);

  const handleMouseEnter = () => {
    if (tagRef.current) {
      const rect = tagRef.current.getBoundingClientRect();
      setCoords({
        top: rect.bottom + window.scrollY + 8,
        left: rect.left + window.scrollX + rect.width / 2,
      });
    }
    setVisible(true);
  };

  return (
    <div
      ref={tagRef}
      style={{ position: 'relative', display: 'inline-block' }}
      onMouseEnter={handleMouseEnter}
      onMouseLeave={() => setVisible(false)}
    >
      <span className="text-xs font-johnson-text px-2 py-0.5 bg-jj-gray-02 text-jj-gray-07 rounded border border-jj-gray-03 cursor-default select-none">
        +{remaining.length} more
      </span>

      {visible && ReactDOM.createPortal(
        <div style={{
          position: 'absolute',
          top: coords.top,
          left: coords.left,
          transform: 'translateX(-50%)',
          zIndex: 9999,
          pointerEvents: 'none',
        }}>
          {/* Arrow */}
          <div style={{
            position: 'absolute',
            top: '-5px',
            left: '50%',
            transform: 'translateX(-50%) rotate(45deg)',
            width: '10px',
            height: '10px',
            backgroundColor: '#312c2a',
          }} />
          {/* Card */}
          <div style={{
            backgroundColor: '#312c2a',
            borderRadius: '10px',
            padding: '10px 12px',
            boxShadow: '0 8px 24px rgba(49,44,42,0.2)',
            minWidth: '160px',
            maxWidth: '260px',
          }}>
            <p style={{
              fontFamily: 'JohnsonText, Arial, sans-serif',
              fontSize: '10px',
              fontWeight: 700,
              color: '#a39992',
              textTransform: 'uppercase',
              letterSpacing: '0.06em',
              marginBottom: '8px',
            }}>
              {remaining.length} more permission{remaining.length !== 1 ? 's' : ''}
            </p>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px' }}>
              {remaining.map(([key]) => (
                <span key={key} style={{
                  fontFamily: 'JohnsonText, Arial, sans-serif',
                  fontSize: '11px',
                  color: '#ffffff',
                  backgroundColor: '#6e6259',
                  padding: '2px 8px',
                  borderRadius: '4px',
                  whiteSpace: 'nowrap',
                }}>
                  {formatPerm(key)}
                </span>
              ))}
            </div>
          </div>
        </div>,
        document.body
      )}
    </div>
  );
};


// Reusable CSS filter map — all from J&J palette
const F = {
  white:  'brightness(0) invert(1)',
  black:  'brightness(0)',
  red:    'brightness(0) saturate(100%) invert(17%) sepia(100%) saturate(7426%) hue-rotate(3deg) brightness(92%) contrast(118%)',
  gray04: 'brightness(0) saturate(100%) invert(82%) sepia(6%) saturate(356%) hue-rotate(346deg) brightness(92%) contrast(88%)',
  gray05: 'brightness(0) saturate(100%) invert(65%) sepia(6%) saturate(556%) hue-rotate(346deg) brightness(92%) contrast(88%)',
  gray06: 'brightness(0) saturate(100%) invert(50%) sepia(6%) saturate(656%) hue-rotate(346deg) brightness(92%) contrast(88%)',
  gray07: 'brightness(0) saturate(100%) invert(42%) sepia(6%) saturate(656%) hue-rotate(346deg) brightness(92%) contrast(88%)',
  gray08: 'brightness(0) saturate(100%) invert(15%) sepia(7%) saturate(806%) hue-rotate(346deg) brightness(95%) contrast(89%)',
};

const UserDetailPanel = ({ graphUser, assignedRole, photo, ext, profileInfoOpen, setProfileInfoOpen, onFetchExtended, getRoleDefinition, onClose, onAssignRole, onRemoveRole }) => {
  const email = graphUser.mail || graphUser.userPrincipalName;
  const roleKey = assignedRole || 'viewer';
  const roleDef = getRoleDefinition(roleKey);
  const enabledPerms = Object.entries(roleDef.permissions || {}).filter(([, v]) => v);
  const disabledPerms = Object.entries(roleDef.permissions || {}).filter(([, v]) => !v);
  const formatDate = (iso) => iso ? new Date(iso).toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' }) : null;

  const hasExtended = Object.keys(ext).length > 0;

  const infoFields = [
    { icon: mailSrc, label: 'Email', value: email },
    { icon: buildingSrc, label: 'Department', value: ext.department || graphUser.department },
    { icon: userSrc, label: 'Job Title', value: ext.jobTitle || graphUser.jobTitle },
    { icon: buildingSrc, label: 'Company', value: ext.companyName },
    { icon: buildingSrc, label: 'Office', value: ext.officeLocation },
    { icon: globeSrc, label: 'City', value: ext.city },
    { icon: globeSrc, label: 'Country', value: ext.country },
    { icon: phoneSrc, label: 'Mobile', value: ext.mobilePhone },
    { icon: phoneSrc, label: 'Business Phone', value: ext.businessPhones?.[0] },
    { icon: userSrc, label: 'Employee ID', value: ext.employeeId },
    { icon: userSrc, label: 'Employee Type', value: ext.employeeType },
    { icon: userSrc, label: 'User Type', value: ext.userType },
    { icon: userSrc, label: 'Account Status', value: ext.accountEnabled != null ? (ext.accountEnabled ? 'Enabled' : 'Disabled') : null },
    { icon: userSrc, label: 'Created', value: formatDate(ext.createdDateTime) },
  ].filter(f => f.value);

  return ReactDOM.createPortal(
    <>
      {/* Backdrop */}
      <div className="fixed inset-0 bg-black bg-opacity-40 z-40" onClick={onClose} />

      {/* Slide-in panel */}
      <div className="fixed top-0 right-0 h-full w-full max-w-md bg-white z-50 flex flex-col shadow-2xl">

        {/* Panel header */}
        <div className="flex items-center justify-between px-6 py-5 border-b-4 border-jj-red bg-white flex-shrink-0">
          <div>
            <h2 className="text-lg font-johnson-display text-jj-gray-08">User Details</h2>
            <p className="text-xs font-johnson-text text-jj-gray-05 mt-0.5">{email}</p>
          </div>
          <button onClick={onClose} className="p-2 rounded-lg hover:bg-jj-gray-01 transition-colors">
            <Icon src={closeIcon} className="w-5 h-5" style={{ filter: F.gray06 }} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto">

          {/* Profile hero */}
          <div className="px-6 py-8 border-b border-jj-gray-02 bg-jj-gray-01 flex flex-col items-center text-center">
            <div className="w-20 h-20 rounded-full bg-white border-2 border-jj-gray-03 overflow-hidden flex items-center justify-center mb-4 flex-shrink-0">
              {photo ? (
                <img src={photo} alt={graphUser.displayName} className="w-full h-full object-cover" />
              ) : (
                <Icon src={userPlusIcon} className="w-10 h-10" style={{ filter: F.red }} />
              )}
            </div>
            <h3 className="text-xl font-johnson-display text-jj-gray-08 mb-1" style={{ lineHeight: '115%' }}>
              {graphUser.displayName || email}
            </h3>
            {(ext.jobTitle || graphUser.jobTitle) && (
              <p className="text-sm font-johnson-text text-jj-gray-05 mb-3">{ext.jobTitle || graphUser.jobTitle}</p>
            )}
            <span
              className="px-4 py-1.5 rounded-full text-white font-johnson-text text-sm"
              style={{ backgroundColor: roleDef.color || '#a39992' }}
            >
              {assignedRole ? roleDef.name : 'No Role Assigned'}
            </span>
          </div>

          {/* Profile Information — collapsible */}
          <div className="border-b border-jj-gray-02">
            <button
              onClick={() => { setProfileInfoOpen(prev => !prev); if (!profileInfoOpen) onFetchExtended(); }}
              className="w-full flex items-center justify-between px-6 py-4 hover:bg-jj-gray-01 transition-colors text-left"
            >
              <span className="text-sm font-johnson-text font-bold text-jj-gray-08">Profile Information</span>
              <div className="flex items-center gap-2">
                {!profileInfoOpen && <span className="text-xs font-johnson-text text-jj-gray-05">Click to expand</span>}
                <Icon
                  src={chevronRightIcon}
                  className={`w-4 h-4 transition-transform duration-200 ${profileInfoOpen ? 'rotate-90' : ''}`}
                  style={{ filter: F.black }}
                />
              </div>
            </button>
            {profileInfoOpen && (
              <div className="px-6 pb-6">
                {!hasExtended && graphUser.id && (
                  <div className="space-y-3 mb-3">
                    {[1, 2, 3].map(i => (
                      <div key={i} className="flex items-center gap-3">
                        <div className="w-4 h-4 rounded bg-jj-gray-02 animate-pulse flex-shrink-0" />
                        <div className="flex-1">
                          <div className="h-3 w-16 bg-jj-gray-02 rounded animate-pulse mb-1" />
                          <div className="h-4 w-40 bg-jj-gray-02 rounded animate-pulse" />
                        </div>
                      </div>
                    ))}
                  </div>
                )}
                <div className="space-y-4">
                  {infoFields.map(({ icon: fieldIconSrc, label, value }) => (
                    <div key={label} className="flex items-start gap-3">
                      <img src={fieldIconSrc} alt="" className="w-4 h-4 flex-shrink-0 mt-0.5" style={{ filter: 'brightness(0) saturate(100%) invert(82%) sepia(6%) saturate(356%) hue-rotate(346deg) brightness(92%) contrast(88%)' }} />
                      <div className="min-w-0">
                        <p className="text-xs font-johnson-text text-jj-gray-05 uppercase tracking-wide mb-0.5">{label}</p>
                        <p className="text-sm font-johnson-text text-jj-gray-08 break-all">{value}</p>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>

          {/* Access & Permissions */}
          <div className="px-6 py-5">
            <h4 className="text-sm font-johnson-text font-bold text-jj-gray-08 mb-4">Access & Permissions</h4>
            {!assignedRole && (
              <div className="p-3 bg-jj-gray-01 rounded-lg border border-jj-gray-03 mb-4">
                <p className="text-sm font-johnson-text text-jj-gray-06">
                  Permissions derived from AD group membership or default viewer access.
                </p>
              </div>
            )}
            <div className="grid grid-cols-1 gap-3">
              {enabledPerms.length > 0 && (
                <div>
                  <p className="text-xs font-johnson-text font-bold text-jj-gray-07 uppercase tracking-wide mb-2">Has Access</p>
                  <div className="space-y-1.5">
                    {enabledPerms.map(([key]) => (
                      <div key={key} className="flex items-center gap-2 py-1 px-2 rounded bg-jj-gray-01">
                        <Icon src={shieldCheckIcon} className="w-3.5 h-3.5 flex-shrink-0" style={{ filter: F.gray07 }} />
                        <span className="text-sm font-johnson-text text-jj-gray-07">{formatPerm(key)}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
              {disabledPerms.length > 0 && (
                <div>
                  <p className="text-xs font-johnson-text font-bold text-jj-gray-05 uppercase tracking-wide mb-2">No Access</p>
                  <div className="space-y-1.5">
                    {disabledPerms.map(([key]) => (
                      <div key={key} className="flex items-center gap-2 py-1 px-2">
                        <Icon src={shieldCheckIcon} className="w-3.5 h-3.5 flex-shrink-0" style={{ filter: F.gray04 }} />
                        <span className="text-sm font-johnson-text text-jj-gray-05">{formatPerm(key)}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>

        {/* Footer */}
        <div className="px-6 py-4 border-t border-jj-gray-02 flex gap-3 flex-shrink-0 bg-white">
          <button
            onClick={onAssignRole}
            className="flex-1 bg-jj-red text-white py-2.5 rounded-lg hover:bg-jj-maroon-05 transition-colors font-johnson-text text-sm flex items-center justify-center gap-2"
          >
            <Icon src={userPlusIcon} className="w-4 h-4" style={{ filter: F.white }} />
            {assignedRole ? 'Change Role' : 'Assign Role'}
          </button>
          {assignedRole && (
            <button
              onClick={() => onRemoveRole(email)}
              className="px-4 py-2.5 border-2 border-jj-red text-jj-red rounded-lg hover:bg-jj-gray-01 transition-colors font-johnson-text text-sm flex items-center gap-2"
            >
              <Icon src={trashIcon} className="w-4 h-4" style={{ filter: F.red }} />
              Remove
            </button>
          )}
        </div>
      </div>
    </>,
    document.body
  );
};

export default function UserManagement() {
  const { user } = useAuth();
  const { instance } = useMsal();
  const {
    userRole,
    getAllRoles,
    getRoleDefinition,
    getAllUserOverrides,
    assignRoleToUser,
    removeUserRoleOverride,
    createCustomRole,
    updateCustomRole,
    deleteCustomRole,
    customRoles,
    AD_GROUP_ROLES
  } = useRBAC();

  // Built-in role IDs (cannot be deleted, but can be modified)
  const BUILTIN_ROLE_IDS = ['admin', 'dqe_righttime_superuser', 'it_viewer', 'viewer'];

  const [searchParams, setSearchParams] = useSearchParams();

  const VALID_TABS = ['groups', 'roles', 'users'];
  const [activeTab, setActiveTab] = useState(() => {
    const tab = searchParams.get('tab');
    return VALID_TABS.includes(tab) ? tab : 'groups';
  });

  // Keep URL in sync when tab changes
  const handleTabChange = (tab) => {
    if (tab === activeTab) return;
    setActiveTab(tab);
    setSearchParams({ tab });
  };

  // Sync tab if URL changes externally (e.g. back/forward)
  useEffect(() => {
    const tab = searchParams.get('tab');
    if (VALID_TABS.includes(tab) && tab !== activeTab) {
      setActiveTab(tab);
    }
  }, [searchParams]);
  const [notification, setNotification] = useState(null);

  // Users tab state
  const [users, setUsers] = useState([]);
  const [usersLoading, setUsersLoading] = useState(false);
  const [usersError, setUsersError] = useState(null);
  const [usersSearchTerm, setUsersSearchTerm] = useState('');
  const [usersPagination, setUsersPagination] = useState({
    currentPage: 1,
    pageSize: 20,
    totalUsers: 0,
    nextLink: null,
    previousLinks: [] // Stack to store previous page links
  });
  const [selectedUserForRole, setSelectedUserForRole] = useState(null);
  const [selectedRoleForUser, setSelectedRoleForUser] = useState('');
  const [roleAssignmentTrigger, setRoleAssignmentTrigger] = useState(0); // Trigger to force re-render
  const [showAssignedOnly, setShowAssignedOnly] = useState(true);
  const [roleFilter, setRoleFilter] = useState('');
  const [assignedUsersDetails, setAssignedUsersDetails] = useState({}); // email → graph user object
  const [assignedUsersLoading, setAssignedUsersLoading] = useState(false);
  const [assignedUsersPhotos, setAssignedUsersPhotos] = useState({}); // email → blob URL
  const [selectedUserDetail, setSelectedUserDetail] = useState(null); // user clicked for detail panel
  const [extendedUserDetails, setExtendedUserDetails] = useState({}); // id → full graph profile
  const [profileInfoOpen, setProfileInfoOpen] = useState(false); // collapsible section in detail panel


  // Groups tab state
  const [groups, setGroups] = useState([]);
  const [groupsLoading, setGroupsLoading] = useState(false);
  const [selectedGroup, setSelectedGroup] = useState(null);
  const [groupMembers, setGroupMembers] = useState([]);
  const [groupMembersLoading, setGroupMembersLoading] = useState(false);
  const [groupMemberPhotos, setGroupMemberPhotos] = useState({});

  // Roles tab state
  const [showCreateRole, setShowCreateRole] = useState(false);
  const [editingRole, setEditingRole] = useState(null);
  const [newRoleId, setNewRoleId] = useState('');
  const [newRoleName, setNewRoleName] = useState('');
  const [newRoleDescription, setNewRoleDescription] = useState('');
  const [newRoleColor, setNewRoleColor] = useState('#0f68b2');
  const [newRolePermissions, setNewRolePermissions] = useState({
    viewDashboard: true,
    viewDQE: false,
    viewITChecks: false,
    runITChecks: false,
    viewBusinessChecks: false,
    runBDCOperations: false,
    viewBDCSuperuser: false,
    manageDataAPIExports: false,
    viewAudit: false,
    viewUserManagement: false,
    manageUsers: false,
    manageRoles: false
  });

  const showNotification = (message, type = 'success') => {
    setNotification({ message, type });
    setTimeout(() => setNotification(null), 3000);
  };

  // Get access token for Microsoft Graph
  const getGraphToken = async () => {
    try {
      const accounts = instance.getAllAccounts();
      if (accounts.length === 0) throw new Error('No accounts found');

      const scopes = ['User.Read.All', 'Group.Read.All', 'GroupMember.Read.All'];

      try {
        // Try silent token acquisition first
        const response = await instance.acquireTokenSilent({
          scopes: scopes,
          account: accounts[0]
        });
        return response.accessToken;
      } catch (silentError) {
        console.log('Silent token acquisition failed, trying popup...', silentError);

        // If silent fails, prompt user for consent
        try {
          const response = await instance.acquireTokenPopup({
            scopes: scopes,
            account: accounts[0]
          });
          return response.accessToken;
        } catch (popupError) {
          console.error('Popup token acquisition failed:', popupError);
          throw new Error('Failed to acquire permissions. Please contact your administrator to grant User.Read.All permission.');
        }
      }
    } catch (error) {
      console.error('Error getting token:', error);
      throw error;
    }
  };

  // Fetch users from Microsoft Graph with pagination
  const fetchUsers = async (direction = 'initial', searchQuery = '') => {
    setUsersLoading(true);
    setUsersError(null);
    try {
      const token = await getGraphToken();
      let url;

      if (direction === 'initial' || searchQuery) {
        // Initial load or new search
        if (searchQuery) {
          // Search query - no orderby with filter
          url = `https://graph.microsoft.com/v1.0/users?$filter=startswith(displayName,'${searchQuery}') or startswith(mail,'${searchQuery}') or startswith(userPrincipalName,'${searchQuery}')&$top=${usersPagination.pageSize}&$select=id,displayName,mail,userPrincipalName,jobTitle,department`;
        } else {
          // Initial load with ordering
          url = `https://graph.microsoft.com/v1.0/users?$top=${usersPagination.pageSize}&$select=id,displayName,mail,userPrincipalName,jobTitle,department&$orderby=displayName`;
        }

        setUsersPagination(prev => ({ ...prev, currentPage: 1, previousLinks: [] }));
      } else if (direction === 'next' && usersPagination.nextLink) {
        // Navigate to next page
        url = usersPagination.nextLink;
      } else if (direction === 'previous' && usersPagination.previousLinks.length > 0) {
        // Navigate to previous page
        const previousLinks = [...usersPagination.previousLinks];
        url = previousLinks.pop();
        setUsersPagination(prev => ({
          ...prev,
          previousLinks,
          currentPage: prev.currentPage - 1
        }));
      } else {
        return; // No valid navigation
      }

      const response = await fetch(url, {
        headers: {
          'Authorization': `Bearer ${token}`,
          'Content-Type': 'application/json'
        }
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.error?.message || 'Failed to fetch users');
      }

      const data = await response.json();

      setUsers(data.value || []);

      if (direction === 'next') {
        setUsersPagination(prev => ({
          ...prev,
          nextLink: data['@odata.nextLink'] || null,
          currentPage: prev.currentPage + 1,
          previousLinks: [...prev.previousLinks, url]
        }));
      } else if (direction === 'initial') {
        setUsersPagination(prev => ({
          ...prev,
          nextLink: data['@odata.nextLink'] || null,
          currentPage: 1,
          previousLinks: []
        }));
      }
    } catch (error) {
      console.error('Error fetching users:', error);
      setUsersError(error.message);
      showNotification(error.message, 'error');
    } finally {
      setUsersLoading(false);
    }
  };

  // Fetch mapped groups from Microsoft Graph
  const fetchGroups = async () => {
    setGroupsLoading(true);
    try {
      const token = await getGraphToken();
      const mappedGroupNames = Object.keys(AD_GROUP_ROLES);

      const groupPromises = mappedGroupNames.map(async (groupName) => {
        try {
          const response = await fetch(
            `https://graph.microsoft.com/v1.0/groups?$filter=displayName eq '${groupName}'&$select=id,displayName,description,mail`,
            {
              headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json'
              }
            }
          );

          if (response.ok) {
            const data = await response.json();
            if (data.value && data.value.length > 0) {
              return {
                ...data.value[0],
                mappedRole: AD_GROUP_ROLES[groupName]
              };
            }
          }
          return null;
        } catch (error) {
          console.error(`Error fetching group ${groupName}:`, error);
          return null;
        }
      });

      const groupsData = await Promise.all(groupPromises);
      setGroups(groupsData.filter(g => g !== null));
    } catch (error) {
      console.error('Error fetching groups:', error);
      showNotification('Failed to fetch groups', 'error');
    } finally {
      setGroupsLoading(false);
    }
  };

  // Fetch group members
  const fetchGroupMembers = async (groupId) => {
    setGroupMembersLoading(true);
    setGroupMemberPhotos({});
    try {
      const token = await getGraphToken();
      const response = await fetch(
        `https://graph.microsoft.com/v1.0/groups/${groupId}/members?$select=id,displayName,mail,userPrincipalName,jobTitle`,
        { headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' } }
      );
      if (!response.ok) throw new Error('Failed to fetch group members');
      const data = await response.json();
      const members = data.value || [];
      setGroupMembers(members);

      // Fetch photos in background — non-blocking
      const photoResults = await Promise.all(
        members.slice(0, 50).map(async (m) => {
          if (!m.id) return null;
          try {
            const res = await fetch(
              `https://graph.microsoft.com/v1.0/users/${m.id}/photo/$value`,
              { headers: { 'Authorization': `Bearer ${token}` } }
            );
            if (!res.ok) return null;
            const blob = await res.blob();
            return { id: m.id, url: URL.createObjectURL(blob) };
          } catch { return null; }
        })
      );
      const photos = {};
      photoResults.forEach(r => { if (r) photos[r.id] = r.url; });
      setGroupMemberPhotos(photos);
    } catch (error) {
      console.error('Error fetching group members:', error);
      showNotification('Failed to fetch group members', 'error');
    } finally {
      setGroupMembersLoading(false);
    }
  };

  // Load data when tab changes
  useEffect(() => {
    if (activeTab === 'users' && users.length === 0) {
      fetchUsers();
    } else if (activeTab === 'groups' && groups.length === 0) {
      fetchGroups();
    }
  }, [activeTab]);

  // Handle user search with debounce
  useEffect(() => {
    if (activeTab !== 'users') return;

    const timer = setTimeout(() => {
      fetchUsers('initial', usersSearchTerm);
    }, 500);

    return () => clearTimeout(timer);
  }, [usersSearchTerm]);

  // Fetch full Graph details for all users in userOverrides
  const fetchAssignedUsersDetails = async () => {
    const emails = Object.keys(userOverrides);
    if (emails.length === 0) return;

    // Skip emails we already have details for
    const missing = emails.filter(e => !assignedUsersDetails[e]);
    if (missing.length === 0) return;

    setAssignedUsersLoading(true);
    try {
      const token = await getGraphToken();
      const results = await Promise.all(
        missing.map(async (email) => {
          try {
            const res = await fetch(
              `https://graph.microsoft.com/v1.0/users/${encodeURIComponent(email)}?$select=id,displayName,mail,userPrincipalName,jobTitle,department`,
              { headers: { 'Authorization': `Bearer ${token}` } }
            );
            if (!res.ok) return null;
            const data = await res.json();
            return { email, data };
          } catch {
            return null;
          }
        })
      );
      const newDetails = {};
      results.forEach(r => { if (r) newDetails[r.email] = r.data; });
      setAssignedUsersDetails(prev => ({ ...prev, ...newDetails }));

      // Fetch profile photos for successfully resolved users
      const photoResults = await Promise.all(
        Object.entries(newDetails).map(async ([email, userData]) => {
          if (!userData?.id) return null;
          try {
            const res = await fetch(
              `https://graph.microsoft.com/v1.0/users/${userData.id}/photo/$value`,
              { headers: { 'Authorization': `Bearer ${token}` } }
            );
            if (!res.ok) return null;
            const blob = await res.blob();
            return { email, url: URL.createObjectURL(blob) };
          } catch {
            return null;
          }
        })
      );
      const newPhotos = {};
      photoResults.forEach(r => { if (r) newPhotos[r.email] = r.url; });
      setAssignedUsersPhotos(prev => ({ ...prev, ...newPhotos }));
    } catch (error) {
      console.error('Error fetching assigned user details:', error);
    } finally {
      setAssignedUsersLoading(false);
    }
  };

  // Fetch extended profile for a single user when panel opens
  const fetchExtendedUserDetails = async (userId) => {
    if (!userId || extendedUserDetails[userId]) return;
    try {
      const token = await getGraphToken();
      const fields = [
        'id','displayName','mail','userPrincipalName','jobTitle','department',
        'officeLocation','city','country','companyName','mobilePhone',
        'businessPhones','employeeId','employeeType','userType','accountEnabled',
        'createdDateTime','onPremisesDistinguishedName'
      ].join(',');
      const res = await fetch(
        `https://graph.microsoft.com/v1.0/users/${userId}?$select=${fields}`,
        { headers: { 'Authorization': `Bearer ${token}` } }
      );
      if (!res.ok) return;
      const data = await res.json();
      setExtendedUserDetails(prev => ({ ...prev, [userId]: data }));
    } catch (error) {
      console.error('Error fetching extended user details:', error);
    }
  };
  useEffect(() => {
    if (activeTab === 'users' && (showAssignedOnly || roleFilter)) {
      fetchAssignedUsersDetails();
    }
  }, [activeTab, showAssignedOnly, roleFilter, roleAssignmentTrigger]);

  // Reset profile info accordion when panel opens for a new user
  useEffect(() => {
    setProfileInfoOpen(false);
  }, [selectedUserDetail]);

  // Handle assign role to user
  const handleAssignRoleToUser = async () => {
    if (!selectedUserForRole || !selectedRoleForUser) {
      showNotification('Please select a user and role', 'error');
      return;
    }

    try {
      const email = selectedUserForRole.mail || selectedUserForRole.userPrincipalName;
      assignRoleToUser(email, selectedRoleForUser);
      showNotification(`Role assigned to ${selectedUserForRole.displayName}`);
      setSelectedUserForRole(null);
      setSelectedRoleForUser('');
      // Trigger re-render to show updated role
      setRoleAssignmentTrigger(prev => prev + 1);
    } catch (error) {
      showNotification(error.message, 'error');
    }
  };

  // Role management functions
  const handleCreateRole = () => {
    if (!newRoleName) {
      showNotification('Please enter a role name', 'error');
      return;
    }

    if (!newRoleId) {
      showNotification('Role ID could not be generated. Please enter a valid role name.', 'error');
      return;
    }

    const allRoles = getAllRoles();
    if (allRoles[newRoleId]) {
      showNotification('A role with this ID already exists', 'error');
      return;
    }

    try {
      createCustomRole(newRoleId, {
        name: newRoleName,
        description: newRoleDescription,
        color: newRoleColor,
        permissions: newRolePermissions
      });
      showNotification('Custom role created successfully');
      setShowCreateRole(false);
      resetRoleForm();
    } catch (error) {
      showNotification(error.message, 'error');
    }
  };

  const handleUpdateRole = () => {
    if (!editingRole) return;

    try {
      updateCustomRole(editingRole, {
        name: newRoleName,
        description: newRoleDescription,
        color: newRoleColor,
        permissions: newRolePermissions
      });
      showNotification('Role updated successfully');
      setEditingRole(null);
      setShowCreateRole(false);
      resetRoleForm();
    } catch (error) {
      showNotification(error.message, 'error');
    }
  };

  // Generate unique role ID from role name
  const generateRoleId = (roleName) => {
    if (!roleName) return '';

    // Convert to lowercase, replace spaces and special chars with underscore
    let baseId = roleName
      .toLowerCase()
      .trim()
      .replace(/[^a-z0-9]+/g, '_')
      .replace(/^_+|_+$/g, ''); // Remove leading/trailing underscores

    // Check if ID exists
    const allRoles = getAllRoles();
    let uniqueId = baseId;
    let counter = 1;

    while (allRoles[uniqueId]) {
      uniqueId = `${baseId}_${counter}`;
      counter++;
    }

    return uniqueId;
  };

  // Handle role name change and auto-generate ID
  const handleRoleNameChange = (name) => {
    setNewRoleName(name);
    if (!editingRole) {
      // Only auto-generate ID when creating new role, not editing
      const generatedId = generateRoleId(name);
      setNewRoleId(generatedId);
    }
  };

  const handleDeleteRole = (roleId) => {
    const allRoles = getAllRoles();
    if (window.confirm(`Delete custom role "${allRoles[roleId].name}"? Users assigned to this role will lose their permissions.`)) {
      try {
        deleteCustomRole(roleId);
        showNotification('Role deleted successfully');
      } catch (error) {
        showNotification(error.message, 'error');
      }
    }
  };

  const handleResetToDefault = (roleId) => {
    const allRoles = getAllRoles();
    if (window.confirm(`Reset "${allRoles[roleId].name}" to default settings? This will remove all custom modifications.`)) {
      try {
        deleteCustomRole(roleId); // Removing from customRoles will revert to ROLE_DEFINITIONS
        showNotification('Role reset to default successfully');
      } catch (error) {
        showNotification(error.message, 'error');
      }
    }
  };

  const startEditingRole = (roleId) => {
    const allRoles = getAllRoles();
    const role = allRoles[roleId];
    setShowCreateRole(false);
    setEditingRole(roleId);
    setNewRoleId(roleId);
    setNewRoleName(role.name);
    setNewRoleDescription(role.description);
    setNewRoleColor(role.color);
    setNewRolePermissions(role.permissions);
  };

  const resetRoleForm = () => {
    setNewRoleId('');
    setNewRoleName('');
    setNewRoleDescription('');
    setNewRoleColor('#0f68b2');
    setNewRolePermissions({
      viewDashboard: true,
      viewDQE: false,
      viewITChecks: false,
      runITChecks: false,
      viewBusinessChecks: false,
      runBDCOperations: false,
      viewBDCSuperuser: false,
      manageDataAPIExports: false,
      viewAudit: false,
      viewUserManagement: false,
      manageUsers: false,
      manageRoles: false
    });
  };

  const togglePermission = (updates) => {
    setNewRolePermissions(prev => ({ ...prev, ...updates }));
  };

  const getRoleBadgeColor = (roleName) => {
    const role = getRoleDefinition(roleName);
    return role.color || '#a39992';
  };

  const allRoles = getAllRoles();

  // Make userOverrides reactive to changes
  const userOverrides = useMemo(() => {
    return getAllUserOverrides();
  }, [roleAssignmentTrigger, getAllUserOverrides]);


  return (
    <div className="h-full bg-jj-gray-01 overflow-auto">
      <div className="flex flex-col w-full px-8 pt-5 pb-8 mx-auto">

        {/* ── Toast notification ── */}
        {notification && (
          <div className="fixed top-20 right-8 z-50 animate-slide-in">
            <div className={`${notification.type === 'success' ? 'bg-jj-gray-08' : 'bg-jj-red'} text-white px-5 py-4 rounded-xl shadow-xl flex items-center gap-3 min-w-64`}>
              <Icon src={notification.type === 'success' ? checkIcon : alertIcon} className="w-5 h-5 flex-shrink-0" style={{ filter: F.white }} />
              <span className="font-johnson-text text-sm">{notification.message}</span>
            </div>
          </div>
        )}

        {/* ── Page header ── */}
        <div className="mb-6">
          <div className="flex items-center gap-4">
            <div className="w-12 h-12 bg-jj-red rounded-xl flex items-center justify-center flex-shrink-0">
              <Icon src={shieldIcon} className="w-6 h-6" style={{ filter: F.white }} />
            </div>
            <div>
              <h1 className="text-3xl font-johnson-display text-jj-gray-08" style={{ lineHeight: '110%' }}>User Management</h1>
              <p className="text-sm font-johnson-text text-jj-gray-06 mt-0.5">Manage users, groups, roles and permissions</p>
            </div>
          </div>
        </div>

        {/* ── Tabs ── */}
        <div className="flex border-b border-jj-gray-03 mb-6">
          {[
            { id: 'groups', label: 'Groups',              icon: groupIcon },
            { id: 'roles',  label: 'Roles & Permissions', icon: Shield    },
          ].map(tab => (
            <button
              key={tab.id}
              onClick={() => handleTabChange(tab.id)}
              className={`flex items-center gap-2 px-6 py-3.5 font-johnson-text font-bold text-sm transition-all border-b-4 -mb-px ${
                activeTab === tab.id
                  ? 'text-jj-red border-jj-red'
                  : 'text-jj-gray-06 border-transparent hover:text-jj-gray-08 hover:border-jj-gray-03'
              }`}
            >
              <Icon src={tab.icon} className="w-4 h-4" style={{ filter: activeTab === tab.id ? F.red : F.gray06 }} />
              {tab.label}
            </button>
          ))}
        </div>

        {/* ══ USERS TAB ══════════════════════════════════════ */}
        {activeTab === 'users' && (
          <div className="space-y-5">

            {/* Toolbar */}
            <div className="bg-white rounded-xl border border-jj-gray-02 px-5 py-4 flex flex-wrap items-center gap-3">
              <div className="flex-1 min-w-52 max-w-sm relative">
                <Icon src={searchIcon} className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4" style={{ filter: F.gray05 }} />
                <input
                  type="text"
                  placeholder="Search users by name or email..."
                  value={usersSearchTerm}
                  onChange={(e) => setUsersSearchTerm(e.target.value)}
                  className="w-full pl-9 pr-8 py-2 border border-jj-gray-03 rounded-lg text-sm font-johnson-text focus:outline-none focus:ring-1 focus:ring-jj-red text-jj-gray-08"
                />
                {usersSearchTerm && (
                  <button onClick={() => setUsersSearchTerm('')} className="absolute right-3 top-1/2 -translate-y-1/2" aria-label="Clear search">
                    <Icon src={closeIcon} className="w-3.5 h-3.5" style={{ filter: F.gray05 }} />
                  </button>
                )}
              </div>

              <div className="relative">
                <select
                  value={roleFilter}
                  onChange={(e) => { setRoleFilter(e.target.value); setShowAssignedOnly(false); }}
                  className={`pl-3 pr-8 py-2 border rounded-lg text-sm font-johnson-text bg-white appearance-none cursor-pointer focus:outline-none focus:ring-1 focus:ring-jj-red transition-all ${roleFilter ? 'border-jj-red text-jj-red font-bold' : 'border-jj-gray-03 text-jj-gray-07'}`}
                >
                  <option value="" style={{ color: '#312c2a' }}>All Roles</option>
                  {Object.entries(allRoles).map(([roleId, role]) => (
                    <option key={roleId} value={roleId} style={{ color: '#312c2a' }}>{role.name}</option>
                  ))}
                </select>
                <svg className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5" fill="none" stroke="#a39992" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                </svg>
              </div>

              <button
                onClick={() => { setShowAssignedOnly(prev => !prev); setRoleFilter(''); }}
                className={`flex items-center gap-2 px-4 py-2 rounded-lg border text-sm font-johnson-text transition-all ${showAssignedOnly ? 'bg-jj-red text-white border-jj-red' : 'bg-white text-jj-gray-07 border-jj-gray-03 hover:border-jj-red hover:text-jj-red'}`}
              >
                <Icon src={userPlusIcon} className="w-4 h-4" style={{ filter: showAssignedOnly ? F.white : F.black }} />
                Assigned Only
                {showAssignedOnly ? (
                  <Icon src={closeIcon} className="w-3.5 h-3.5" style={{ filter: F.white }} />
                ) : Object.keys(userOverrides).length > 0 ? (
                  <span className="w-5 h-5 rounded-full bg-jj-red text-white text-xs font-bold flex items-center justify-center">
                    {Object.keys(userOverrides).length}
                  </span>
                ) : null}
              </button>

              {!showAssignedOnly && !roleFilter && (
                <p className="ml-auto text-sm font-johnson-text text-jj-gray-05">
                  Page {usersPagination.currentPage} · {users.length} users
                </p>
              )}
            </div>

            {/* Users table */}
            <div className="bg-white rounded-xl border border-jj-gray-02 overflow-hidden">
              {usersLoading && !showAssignedOnly && !roleFilter ? (
                <div className="py-16 flex flex-col items-center gap-4">
                  <div className="w-10 h-10 border-2 border-jj-gray-02 border-t-jj-red rounded-full animate-spin" />
                  <p className="text-sm font-johnson-text text-jj-gray-05">Loading users...</p>
                </div>
              ) : usersError && !showAssignedOnly && !roleFilter ? (
                <div className="py-16 px-6 flex flex-col items-center gap-4 text-center">
                  <Icon src={alertIcon} className="w-12 h-12" style={{ filter: F.red }} />
                  <div>
                    <p className="text-lg font-johnson-display text-jj-gray-08 mb-1">Failed to Load Users</p>
                    <p className="text-sm font-johnson-text text-jj-gray-06 max-w-md">{usersError}</p>
                  </div>
                  <button onClick={() => fetchUsers('initial')} className="px-5 py-2 bg-jj-red text-white rounded-lg hover:bg-jj-maroon-05 transition-colors font-johnson-text text-sm">
                    Try Again
                  </button>
                  <div className="w-full max-w-md p-4 bg-jj-gray-01 rounded-lg border border-jj-gray-03 text-left">
                    <p className="text-sm font-johnson-text font-bold text-jj-gray-07 mb-2">Common Solutions</p>
                    <ul className="text-sm font-johnson-text text-jj-gray-06 space-y-1 list-disc list-inside">
                      <li>Log out and back in to refresh permissions</li>
                      <li>Contact IT to grant User.Read.All permission</li>
                      <li>Verify admin consent for this application</li>
                    </ul>
                  </div>
                </div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full">
                    <thead>
                      <tr className="bg-jj-gray-02 border-b border-jj-gray-03">
                        <th className="px-6 py-3.5 text-left text-xs font-johnson-text font-bold text-jj-gray-07 uppercase tracking-wide">User</th>
                        <th className="px-6 py-3.5 text-left text-xs font-johnson-text font-bold text-jj-gray-07 uppercase tracking-wide">Email</th>
                        <th className="px-6 py-3.5 text-left text-xs font-johnson-text font-bold text-jj-gray-07 uppercase tracking-wide">Department</th>
                        <th className="px-6 py-3.5 text-left text-xs font-johnson-text font-bold text-jj-gray-07 uppercase tracking-wide">Role</th>
                        <th className="px-6 py-3.5 text-right text-xs font-johnson-text font-bold text-jj-gray-07 uppercase tracking-wide">Actions</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-jj-gray-02">
                      {(() => {
                        const useLocalData = showAssignedOnly || !!roleFilter;

                        if (useLocalData) {
                          const overrideEntries = Object.entries(userOverrides)
                            .filter(([, role]) => !roleFilter || role === roleFilter)
                            .filter(([email]) => !usersSearchTerm || email.toLowerCase().includes(usersSearchTerm.toLowerCase()) || assignedUsersDetails[email]?.displayName?.toLowerCase().includes(usersSearchTerm.toLowerCase()));

                          if (overrideEntries.length === 0) {
                            return (
                              <tr>
                                <td colSpan={5} className="px-6 py-16 text-center">
                                  <p className="text-sm font-johnson-text text-jj-gray-05">
                                    {roleFilter ? `No users assigned to "${allRoles[roleFilter]?.name}".` : 'No manual role assignments yet.'}
                                  </p>
                                </td>
                              </tr>
                            );
                          }

                          return overrideEntries.map(([email, assignedRole]) => {
                            const details = assignedUsersDetails[email];
                            const photo = assignedUsersPhotos[email];
                            return (
                              <tr key={email} className="hover:bg-jj-gray-01 transition-colors cursor-pointer"
                                onClick={(e) => {
                                  if (!e.target.closest('button')) {
                                    const u = details || { displayName: email, mail: email, userPrincipalName: email };
                                    setSelectedUserDetail({ graphUser: u, assignedRole, photo: assignedUsersPhotos[email] });
                                    if (u.id) fetchExtendedUserDetails(u.id);
                                  }
                                }}>
                                <td className="px-6 py-4">
                                  <div className="flex items-center gap-3">
                                    <div className="w-9 h-9 rounded-full bg-jj-gray-02 flex items-center justify-center overflow-hidden flex-shrink-0 border border-jj-gray-03">
                                      {photo ? (
                                        <img src={photo} alt={details?.displayName || email} className="w-full h-full object-cover" />
                                      ) : assignedUsersLoading && !details ? (
                                        <div className="w-full h-full bg-jj-gray-03 animate-pulse rounded-full" />
                                      ) : (
                                        <Icon src={userPlusIcon} className="w-5 h-5" style={{ filter: F.black }} />
                                      )}
                                    </div>
                                    <div>
                                      {assignedUsersLoading && !details ? (
                                        <div className="h-4 w-32 bg-jj-gray-02 rounded animate-pulse" />
                                      ) : (
                                        <p className="text-sm font-johnson-text text-jj-gray-08">{details?.displayName || email}</p>
                                      )}
                                      {details?.jobTitle && <p className="text-xs font-johnson-text text-jj-gray-05">{details.jobTitle}</p>}
                                    </div>
                                  </div>
                                </td>
                                <td className="px-6 py-4"><span className="text-sm font-johnson-text text-jj-gray-06">{details?.mail || details?.userPrincipalName || email}</span></td>
                                <td className="px-6 py-4"><span className="text-sm font-johnson-text text-jj-gray-06">{details?.department || '—'}</span></td>
                                <td className="px-6 py-4">
                                  <span className="inline-block px-3 py-1 rounded-full text-white font-johnson-text text-xs" style={{ backgroundColor: getRoleBadgeColor(assignedRole) }}>
                                    {getRoleDefinition(assignedRole).name}
                                  </span>
                                </td>
                                <td className="px-6 py-4 text-right">
                                  <div className="flex items-center justify-end gap-1">
                                    <button
                                      onClick={() => { setSelectedUserForRole(details || { displayName: email, mail: email, userPrincipalName: email }); setSelectedRoleForUser(assignedRole); }}
                                      className="p-2 rounded-lg hover:bg-jj-gray-01 transition-colors border border-transparent hover:border-jj-gray-03" title="Change role">
                                      <Icon src={editIcon} className="w-4 h-4" style={{ filter: F.black }} />
                                    </button>
                                    <button
                                      onClick={() => { const dn = details?.displayName || email; if (window.confirm(`Remove role from ${dn}?`)) { try { removeUserRoleOverride(email); showNotification('Role removed successfully'); setRoleAssignmentTrigger(prev => prev + 1); } catch (err) { showNotification(err.message, 'error'); } } }}
                                      className="p-2 rounded-lg hover:bg-jj-gray-01 transition-colors border border-transparent hover:border-jj-gray-03" title="Remove role">
                                      <Icon src={trashIcon} className="w-4 h-4" style={{ filter: F.red }} />
                                    </button>
                                  </div>
                                </td>
                              </tr>
                            );
                          });
                        }

                        return users.map((graphUser) => {
                          const email = graphUser.mail || graphUser.userPrincipalName;
                          const assignedRole = userOverrides[email?.toLowerCase()];
                          return (
                            <tr key={graphUser.id} className="hover:bg-jj-gray-01 transition-colors cursor-pointer"
                              onClick={(e) => { if (!e.target.closest('button')) { setSelectedUserDetail({ graphUser, assignedRole, photo: null }); fetchExtendedUserDetails(graphUser.id); } }}>
                              <td className="px-6 py-4">
                                <div className="flex items-center gap-3">
                                  <div className="w-9 h-9 rounded-full bg-jj-gray-02 flex items-center justify-center border border-jj-gray-03">
                                    <Icon src={userPlusIcon} className="w-5 h-5" style={{ filter: F.black }} />
                                  </div>
                                  <div>
                                    <p className="text-sm font-johnson-text text-jj-gray-08">{graphUser.displayName}</p>
                                    {graphUser.jobTitle && <p className="text-xs font-johnson-text text-jj-gray-05">{graphUser.jobTitle}</p>}
                                  </div>
                                </div>
                              </td>
                              <td className="px-6 py-4"><span className="text-sm font-johnson-text text-jj-gray-06">{email}</span></td>
                              <td className="px-6 py-4"><span className="text-sm font-johnson-text text-jj-gray-06">{graphUser.department || '—'}</span></td>
                              <td className="px-6 py-4">
                                {assignedRole ? (
                                  <span className="inline-block px-3 py-1 rounded-full text-white font-johnson-text text-xs" style={{ backgroundColor: getRoleBadgeColor(assignedRole) }}>
                                    {getRoleDefinition(assignedRole).name}
                                  </span>
                                ) : (
                                  <span className="text-sm font-johnson-text text-jj-gray-04">No role assigned</span>
                                )}
                              </td>
                              <td className="px-6 py-4 text-right">
                                <div className="flex items-center justify-end gap-1">
                                  <button
                                    onClick={() => { setSelectedUserForRole(graphUser); setSelectedRoleForUser(assignedRole || 'viewer'); }}
                                    className="p-2 rounded-lg hover:bg-jj-gray-01 transition-colors border border-transparent hover:border-jj-gray-03" title="Assign role">
                                    <Icon src={editIcon} className="w-4 h-4" style={{ filter: F.black }} />
                                  </button>
                                  {assignedRole && (
                                    <button
                                      onClick={() => { if (window.confirm(`Remove role from ${graphUser.displayName}?`)) { try { removeUserRoleOverride(email); showNotification('Role removed successfully'); setRoleAssignmentTrigger(prev => prev + 1); } catch (err) { showNotification(err.message, 'error'); } } }}
                                      className="p-2 rounded-lg hover:bg-jj-gray-01 transition-colors border border-transparent hover:border-jj-gray-03" title="Remove role">
                                      <Icon src={trashIcon} className="w-4 h-4" style={{ filter: F.red }} />
                                    </button>
                                  )}
                                </div>
                              </td>
                            </tr>
                          );
                        });
                      })()}
                    </tbody>
                  </table>
                </div>
              )}

              {!usersError && !usersLoading && !showAssignedOnly && !roleFilter && (
                <div className="bg-jj-gray-01 border-t border border-jj-gray-04 px-6 py-3 flex items-center justify-between">
                  <button
                    onClick={() => fetchUsers('previous', usersSearchTerm)}
                    disabled={usersPagination.currentPage === 1 || usersLoading}
                    className="flex items-center gap-2 px-4 py-2 border border-jj-gray-03 rounded-lg text-sm font-johnson-text text-jj-gray-07 hover:bg-white transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    <Icon src={chevronLeftIcon} className="w-4 h-4" style={{ filter: F.gray07 }} /> Previous
                  </button>
                  <span className="text-sm font-johnson-text text-jj-gray-06">Page {usersPagination.currentPage}</span>
                  <button
                    onClick={() => fetchUsers('next', usersSearchTerm)}
                    disabled={!usersPagination.nextLink || usersLoading}
                    className="flex items-center gap-2 px-4 py-2 border border-jj-gray-03 rounded-lg text-sm font-johnson-text text-jj-gray-07 hover:bg-white transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    Next <Icon src={chevronRightIcon} className="w-4 h-4" style={{ filter: F.gray07 }} />
                  </button>
                </div>
              )}
            </div>

            {/* Assign Role Modal */}
            {selectedUserForRole && ReactDOM.createPortal(
              <div className="fixed inset-0 bg-black bg-opacity-40 flex items-center justify-center z-50 p-4">
                <div className="bg-white rounded-2xl w-full max-w-sm shadow-2xl overflow-hidden">
                  <div className="px-6 py-5 border-b-4 border-jj-red">
                    <h3 className="text-lg font-johnson-display text-jj-gray-08">Assign Role</h3>
                    <p className="text-sm font-johnson-text text-jj-gray-05 mt-0.5">{selectedUserForRole.displayName}</p>
                  </div>
                  <div className="px-6 py-5">
                    <label className="block text-xs font-johnson-text font-bold text-jj-gray-07 uppercase tracking-wide mb-2">Select Role</label>
                    <select
                      value={selectedRoleForUser}
                      onChange={(e) => setSelectedRoleForUser(e.target.value)}
                      className="w-full px-4 py-2.5 border border-jj-gray-03 rounded-lg font-johnson-text text-jj-gray-08 focus:outline-none focus:ring-1 focus:ring-jj-red"
                    >
                      {Object.keys(allRoles).map(roleId => (
                        <option key={roleId} value={roleId}>{allRoles[roleId].name}</option>
                      ))}
                    </select>
                  </div>
                  <div className="px-6 py-4 bg-jj-gray-01 border-t border-jj-gray-02 flex justify-end gap-3">
                    <button onClick={() => { setSelectedUserForRole(null); setSelectedRoleForUser(''); }} className="px-4 py-2 border border-jj-gray-03 rounded-lg font-johnson-text text-sm text-jj-gray-07 hover:bg-white transition-colors">Cancel</button>
                    <button onClick={handleAssignRoleToUser} className="px-5 py-2 bg-jj-red text-white rounded-lg font-johnson-text text-sm hover:bg-jj-maroon-05 transition-colors">Assign Role</button>
                  </div>
                </div>
              </div>,
              document.body
            )}

            {selectedUserDetail && (
              <UserDetailPanel
                graphUser={selectedUserDetail.graphUser}
                assignedRole={selectedUserDetail.assignedRole}
                photo={selectedUserDetail.photo}
                ext={extendedUserDetails[selectedUserDetail.graphUser?.id] || {}}
                profileInfoOpen={profileInfoOpen}
                setProfileInfoOpen={setProfileInfoOpen}
                onFetchExtended={() => { if (!extendedUserDetails[selectedUserDetail.graphUser?.id] && selectedUserDetail.graphUser?.id) fetchExtendedUserDetails(selectedUserDetail.graphUser.id); }}
                getRoleDefinition={getRoleDefinition}
                onClose={() => setSelectedUserDetail(null)}
                onAssignRole={() => { setSelectedUserForRole(selectedUserDetail.graphUser); setSelectedRoleForUser(selectedUserDetail.assignedRole || 'viewer'); setSelectedUserDetail(null); }}
                onRemoveRole={(email) => { if (window.confirm(`Remove role from ${selectedUserDetail.graphUser.displayName || email}?`)) { try { removeUserRoleOverride(email); showNotification('Role removed successfully'); setRoleAssignmentTrigger(prev => prev + 1); setSelectedUserDetail(null); } catch (err) { showNotification(err.message, 'error'); } } }}
              />
            )}
          </div>
        )}

        {/* ══ GROUPS TAB ══════════════════════════════════════ */}
        {activeTab === 'groups' && (
          <div className="space-y-5">
            <p className="text-sm font-johnson-text text-jj-gray-05">{groups.length} mapped security group{groups.length !== 1 ? 's' : ''}</p>

            {groupsLoading ? (
              <div className="py-16 flex flex-col items-center gap-4">
                <div className="w-10 h-10 border-2 border-jj-gray-02 border-t-jj-red rounded-full animate-spin" />
                <p className="text-sm font-johnson-text text-jj-gray-05">Loading groups...</p>
              </div>
            ) : groups.length === 0 ? (
              <div className="bg-white rounded-xl border border-jj-gray-02 py-16 flex flex-col items-center gap-3">
                <Icon src={groupIcon} className="w-12 h-12" style={{ filter: F.gray04 }} />
                <p className="text-sm font-johnson-text text-jj-gray-05">No mapped groups found</p>
              </div>
            ) : (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {groups.map((group) => (
                  <div key={group.id} className="bg-white rounded-xl border border-jj-gray-02 p-5 hover:shadow-md transition-shadow">
                    <div className="flex items-start justify-between gap-4">
                      <div className="flex items-start gap-3 flex-1 min-w-0">
                        <div className="w-10 h-10 bg-jj-gray-01 rounded-lg flex items-center justify-center flex-shrink-0 border border-jj-gray-02">
                          <Icon src={groupIcon} className="w-5 h-5" style={{ filter: F.gray06 }} />
                        </div>
                        <div className="min-w-0">
                          <h3 className="text-sm font-johnson-text font-bold text-jj-gray-08 truncate">{group.displayName}</h3>
                          {group.description && <p className="text-xs font-johnson-text text-jj-gray-05 mt-0.5 truncate">{group.description}</p>}
                          <div className="flex items-center gap-2 mt-2">
                            <span className="text-xs font-johnson-text text-jj-gray-05">Role:</span>
                            <span className="text-xs font-johnson-text text-white px-2.5 py-0.5 rounded-full" style={{ backgroundColor: getRoleBadgeColor(group.mappedRole) }}>
                              {getRoleDefinition(group.mappedRole).name}
                            </span>
                          </div>
                        </div>
                      </div>
                      <button
                        onClick={() => { setSelectedGroup(group); fetchGroupMembers(group.id); }}
                        className="flex-shrink-0 flex items-center gap-1.5 px-3 py-2 bg-jj-red text-white rounded-lg hover:bg-jj-maroon-05 transition-colors font-johnson-text text-xs"
                      >
                        <Icon src={groupIcon} className="w-3.5 h-3.5" style={{ filter: F.white }} />
                        Members
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}

            {selectedGroup && ReactDOM.createPortal(
              <div className="fixed inset-0 bg-black bg-opacity-40 flex items-center justify-center z-50 p-4">
                <div className="bg-white rounded-2xl max-w-2xl w-full max-h-[80vh] overflow-hidden flex flex-col shadow-2xl">
                  <div className="px-6 py-5 border-b-4 border-jj-red flex items-center justify-between flex-shrink-0">
                    <div>
                      <h3 className="text-lg font-johnson-display text-jj-gray-08">Group Members</h3>
                      <p className="text-sm font-johnson-text text-jj-gray-05 mt-0.5">{selectedGroup.displayName}</p>
                    </div>
                    <button onClick={() => { setSelectedGroup(null); setGroupMembers([]); setGroupMemberPhotos({}); }} className="p-2 rounded-lg hover:bg-jj-gray-01 transition-colors">
                      <Icon src={closeIcon} className="w-5 h-5" style={{ filter: F.gray06 }} />
                    </button>
                  </div>
                  <div className="flex-1 overflow-y-auto p-5">
                    {groupMembersLoading ? (
                      <div className="py-10 flex flex-col items-center gap-3">
                        <div className="w-8 h-8 border-2 border-jj-gray-02 border-t-jj-red rounded-full animate-spin" />
                        <p className="text-sm font-johnson-text text-jj-gray-05">Loading members...</p>
                      </div>
                    ) : groupMembers.length === 0 ? (
                      <p className="py-10 text-center text-sm font-johnson-text text-jj-gray-05">No members found</p>
                    ) : (
                      <div className="space-y-2">
                        {groupMembers.map((member) => (
                          <div key={member.id} className="flex items-center gap-3 px-4 py-3 rounded-lg border border-jj-gray-02 hover:bg-jj-gray-01 transition-colors">
                            <div className="w-8 h-8 rounded-full bg-jj-gray-02 flex items-center justify-center border border-jj-gray-03 flex-shrink-0 overflow-hidden">
                              {groupMemberPhotos[member.id] ? (
                                <img src={groupMemberPhotos[member.id]} alt={member.displayName} className="w-full h-full object-cover" />
                              ) : (
                                <Icon src={userPlusIcon} className="w-4 h-4" style={{ filter: F.black }} />
                              )}
                            </div>
                            <div className="flex-1 min-w-0">
                              <p className="text-sm font-johnson-text text-jj-gray-08 truncate">{member.displayName}</p>
                              <p className="text-xs font-johnson-text text-jj-gray-05 truncate">{member.mail || member.userPrincipalName}</p>
                            </div>
                            {member.jobTitle && (
                              <span className="text-xs font-johnson-text text-jj-gray-06 bg-jj-gray-01 px-2.5 py-1 rounded-full border border-jj-gray-02 flex-shrink-0">
                                {member.jobTitle}
                              </span>
                            )}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              </div>,
              document.body
            )}
          </div>
        )}

        {/* ══ ROLES TAB ══════════════════════════════════════ */}
        {activeTab === 'roles' && (
          <div className="space-y-6">

            <div className="flex items-center justify-between">
              <p className="text-sm font-johnson-text text-jj-gray-05">
                {Object.keys(allRoles).length} role{Object.keys(allRoles).length !== 1 ? 's' : ''} configured
              </p>
              <button
                onClick={() => { resetRoleForm(); setShowCreateRole(true); }}
                className="flex items-center gap-2 px-4 py-2 bg-jj-red text-white rounded-lg hover:bg-jj-maroon-05 transition-colors font-johnson-text text-sm"
              >
                <Icon src={plusIcon} className="w-4 h-4" style={{ filter: F.white }} />
                Create Role
              </button>
            </div>

            {/* Create / Edit Role Modal */}
            {(showCreateRole || editingRole) && ReactDOM.createPortal(
              <div className="fixed inset-0 bg-black bg-opacity-40 flex items-center justify-center z-50 p-4">
                <div className="bg-white rounded-2xl w-full max-w-4xl max-h-[90vh] flex flex-col shadow-2xl overflow-hidden">

                  {/* Modal header */}
                  <div className="px-6 py-5 border-b border-jj-gray-02 flex items-center justify-between flex-shrink-0 bg-white">
                    <div className="flex items-center gap-3">
                      <div className="w-9 h-9 rounded-lg bg-jj-red flex items-center justify-center flex-shrink-0">
                        <Icon src={Shield} className="w-4 h-4" style={{ filter: F.white }} />
                      </div>
                      <div>
                        <h3 className="text-lg font-johnson-display text-jj-gray-08">
                          {editingRole ? 'Edit Role' : 'Create New Role'}
                        </h3>
                        {editingRole && (
                          <p className="text-xs font-johnson-text text-jj-gray-05 mt-0.5">
                            {newRoleName}
                            {' · '}
                            {BUILTIN_ROLE_IDS.includes(editingRole) ? 'Built-in role' : 'Custom role'}
                          </p>
                        )}
                      </div>
                    </div>
                    <button
                      onClick={() => { setShowCreateRole(false); setEditingRole(null); resetRoleForm(); }}
                      className="p-2 rounded-lg hover:bg-jj-gray-01 transition-colors"
                    >
                      <Icon src={closeIcon} className="w-5 h-5" style={{ filter: F.gray06 }} />
                    </button>
                  </div>

                  {/* Built-in notice — subtle, not alarming */}
                  {editingRole && BUILTIN_ROLE_IDS.includes(editingRole) && (
                    <div className="px-6 py-3 bg-jj-gray-01 border-b border-jj-gray-02 flex items-center gap-2 flex-shrink-0">
                      <Icon src={alertIcon} className="w-4 h-4 flex-shrink-0" style={{ filter: F.gray06 }} />
                      <p className="text-xs font-johnson-text text-jj-gray-06">
                        Changes to built-in roles override defaults. You can reset from the role card at any time.
                      </p>
                    </div>
                  )}

                  {/* Scrollable form body */}
                  <div className="flex-1 overflow-y-auto px-6 py-5 space-y-5">

                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                      <div>
                        <label className="block text-xs font-johnson-text font-bold text-jj-gray-07 uppercase tracking-wide mb-1.5">Role Name</label>
                        <input
                          type="text"
                          value={newRoleName}
                          onChange={(e) => handleRoleNameChange(e.target.value)}
                          placeholder="e.g. Custom Analyst"
                          className="w-full px-4 py-2.5 border border-jj-gray-03 rounded-lg font-johnson-text text-jj-gray-08 text-sm focus:outline-none focus:ring-1 focus:ring-jj-red"
                          autoFocus
                        />
                      </div>
                      <div>
                        <label className="block text-xs font-johnson-text font-bold text-jj-gray-07 uppercase tracking-wide mb-1.5">Role ID (auto-generated)</label>
                        <input
                          type="text"
                          value={newRoleId}
                          readOnly
                          className="w-full px-4 py-2.5 border border-jj-gray-03 rounded-lg font-johnson-text text-jj-gray-05 text-sm bg-jj-gray-01 cursor-not-allowed"
                        />
                        {!editingRole && newRoleId && (
                          <p className="text-xs font-johnson-text text-jj-gray-04 mt-1">Auto-generated from role name</p>
                        )}
                      </div>
                    </div>

                    <div>
                      <label className="block text-xs font-johnson-text font-bold text-jj-gray-07 uppercase tracking-wide mb-1.5">Description</label>
                      <textarea
                        value={newRoleDescription}
                        onChange={(e) => setNewRoleDescription(e.target.value)}
                        placeholder="Describe what this role can access..."
                        rows={2}
                        className="w-full px-4 py-2.5 border border-jj-gray-03 rounded-lg font-johnson-text text-jj-gray-08 text-sm focus:outline-none focus:ring-1 focus:ring-jj-red resize-none"
                      />
                    </div>

                    <div>
                      <label className="block text-xs font-johnson-text font-bold text-jj-gray-07 uppercase tracking-wide mb-1.5">Badge Color</label>
                      <div className="flex items-center gap-3">
                        <input
                          type="color"
                          value={newRoleColor}
                          onChange={(e) => setNewRoleColor(e.target.value)}
                          className="w-10 h-10 border border-jj-gray-03 rounded-lg cursor-pointer p-0.5"
                        />
                        <span className="px-4 py-1.5 rounded-full text-white font-johnson-text text-sm" style={{ backgroundColor: newRoleColor }}>
                          {newRoleName || 'Preview'}
                        </span>
                      </div>
                    </div>

                    <div>
                      <div className="flex items-center justify-between mb-3">
                        <label className="text-xs font-johnson-text font-bold text-jj-gray-07 uppercase tracking-wide">Permissions</label>
                        <button
                          type="button"
                          onClick={() => {
                            const allSelected = Object.values(newRolePermissions).every(Boolean);
                            const updates = {};
                            Object.keys(newRolePermissions).forEach(k => { updates[k] = !allSelected; });
                            togglePermission(updates);
                          }}
                          className="text-xs font-johnson-text px-3 py-1 rounded-lg border border-jj-gray-03 text-jj-gray-07 hover:border-jj-red hover:text-jj-red transition-colors"
                        >
                          {Object.values(newRolePermissions).every(Boolean) ? 'Deselect all' : 'Select all'}
                        </button>
                      </div>
                      <PermissionTree permissions={newRolePermissions} onChange={togglePermission} />
                    </div>

                  </div>

                  {/* Modal footer */}
                  <div className="px-6 py-4 bg-jj-gray-01 border-t border-jj-gray-02 flex justify-end gap-3 flex-shrink-0">
                    <button
                      onClick={() => { setShowCreateRole(false); setEditingRole(null); resetRoleForm(); }}
                      className="px-5 py-2 border border-jj-gray-03 rounded-lg font-johnson-text text-sm text-jj-gray-07 hover:bg-white transition-colors"
                    >
                      Cancel
                    </button>
                    <button
                      onClick={editingRole ? handleUpdateRole : handleCreateRole}
                      className="flex items-center gap-2 px-5 py-2.5 bg-jj-red text-white rounded-lg font-johnson-text text-sm hover:bg-jj-maroon-05 transition-colors"
                    >
                      <Icon src={saveIcon} className="w-4 h-4" style={{ filter: F.white }} />
                      {editingRole ? 'Update Role' : 'Create Role'}
                    </button>
                  </div>

                </div>
              </div>,
              document.body
            )}


            {/* Roles Grid */}
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
              {Object.entries(allRoles).map(([roleId, role]) => {
                const isOriginallyBuiltIn = BUILTIN_ROLE_IDS.includes(roleId);
                const isModified = isOriginallyBuiltIn && customRoles[roleId];
                const isCustom = !isOriginallyBuiltIn;
                const isADMapped = Object.values(AD_GROUP_ROLES).includes(roleId);
                const enabledCount = Object.values(role.permissions || {}).filter(Boolean).length;

                return (
                  <div key={roleId} className="bg-white rounded-xl border border-jj-gray-02 overflow-hidden hover:shadow-md transition-shadow">
                    <div className="h-1.5 w-full" style={{ backgroundColor: role.color || '#a39992' }} />
                    <div className="p-5">
                      <div className="flex items-start justify-between mb-3">
                        <div className="flex items-center gap-3">
                          <div className="w-10 h-10 rounded-lg flex items-center justify-center flex-shrink-0" style={{ backgroundColor: role.color || '#a39992' }}>
                            <Icon src={Shield} className="w-5 h-5" style={{ filter: F.white }} />
                          </div>
                          <div>
                            <h3 className="text-base font-johnson-display text-jj-gray-08">{role.name}</h3>
                            <div className="flex items-center gap-1.5 mt-0.5 flex-wrap">
                              {isOriginallyBuiltIn && !isModified && (
                                <span className="text-xs font-johnson-text px-2 py-0.5 bg-jj-gray-02 text-jj-gray-06 rounded-full">{isADMapped ? 'AD Mapped' : 'Built-in'}</span>
                              )}
                              {isModified && <span className="text-xs font-johnson-text px-2 py-0.5 bg-jj-gray-02 text-jj-red rounded-full">Modified</span>}
                              {isCustom && <span className="text-xs font-johnson-text px-2 py-0.5 bg-jj-gray-02 text-jj-gray-07 rounded-full">Custom</span>}
                            </div>
                          </div>
                        </div>
                        <div className="flex items-center gap-0.5 flex-shrink-0">
                          <button onClick={() => startEditingRole(roleId)} className="p-1.5 rounded-lg hover:bg-jj-gray-01 transition-colors" title="Edit">
                            <Icon src={editIcon} className="w-4 h-4" style={{ filter: F.black }} />
                          </button>
                          {isCustom && (
                            <button onClick={() => handleDeleteRole(roleId)} className="p-1.5 rounded-lg hover:bg-jj-gray-01 transition-colors" title="Delete">
                              <Icon src={trashIcon} className="w-4 h-4" style={{ filter: F.red }} />
                            </button>
                          )}
                          {isModified && (
                            <button onClick={() => handleResetToDefault(roleId)} className="p-1.5 rounded-lg hover:bg-jj-gray-01 transition-colors" title="Reset to default">
                              <Icon src={refreshSvgIcon} className="w-4 h-4" style={{ filter: F.red }} />
                            </button>
                          )}
                        </div>
                      </div>

                      <p className="text-xs font-johnson-text text-jj-gray-06 mb-4 line-clamp-2">{role.description}</p>

                      <div>
                        <p className="text-xs font-johnson-text font-bold text-jj-gray-07 mb-2">
                          {enabledCount} permission{enabledCount !== 1 ? 's' : ''} enabled
                        </p>
                        <div className="flex flex-wrap gap-1">
                          {Object.entries(role.permissions).filter(([, v]) => v).slice(0, 5).map(([key]) => (
                            <span key={key} className="text-xs font-johnson-text px-2 py-0.5 bg-jj-gray-01 text-jj-gray-07 rounded border border-jj-gray-02">
                              {formatPerm(key)}
                            </span>
                          ))}
                          {enabledCount > 5 && (() => {
                            const remaining = Object.entries(role.permissions).filter(([, v]) => v).slice(5);
                            return (
                              <MorePermissionsTag remaining={remaining} />
                            );
                          })()}
                        </div>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>

            {/* AD Group Mappings */}
            <div className="bg-white rounded-xl border border-jj-gray-04 overflow-hidden">
              <div className="px-6 py-4 border-b border-jj-gray-04 bg-jj-gray-01 flex items-center gap-3">
                <div className="w-8 h-8 rounded-lg bg-jj-red flex items-center justify-center flex-shrink-0">
                  <Icon src={Shield} className="w-4 h-4" style={{ filter: F.white }} />
                </div>
                <div>
                  <h3 className="text-sm font-johnson-text font-bold text-jj-gray-08">Active Directory Group Mappings</h3>
                  <p className="text-xs font-johnson-text text-jj-gray-06">Automatic role assignments from AD groups</p>
                </div>
              </div>
              <div className="divide-y divide-jj-gray-02">
                {Object.entries(AD_GROUP_ROLES).map(([groupName, roleId]) => (
                  <div key={groupName} className="flex items-center justify-between px-6 py-3 hover:bg-jj-gray-01 transition-colors">
                    <span className="text-sm font-johnson-text text-jj-gray-07 flex-1 min-w-0 truncate mr-4">{groupName}</span>
                    <div className="flex items-center gap-2 flex-shrink-0">
                      <span className="text-xs font-johnson-text text-jj-gray-04">→</span>
                      <span className="text-xs font-johnson-text text-white px-3 py-1 rounded-full" style={{ backgroundColor: getRoleBadgeColor(roleId) }}>
                        {getRoleDefinition(roleId).name}
                      </span>
                    </div>
                  </div>
                ))}
              </div>
              <div className="px-6 py-3 bg-jj-gray-01 border-t border-jj-gray-04">
                <p className="text-xs font-johnson-text text-jj-gray-06">
                  Manual role assignments take precedence over AD group mappings.
                </p>
              </div>
            </div>

          </div>
        )}

      </div>
    </div>
  );
}