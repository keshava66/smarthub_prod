import React, { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import { useProfilePhoto } from '../hooks/useProfilePhoto';
import { useRBAC } from '../context/RBACContext';

// Import J&J icons
import dqIcon       from "/icons/JJ_Icon_Analysis_RGB.svg";
import userIcon     from "/icons/JJ_Icon_Profile_red.svg";
import timeIcon     from "/icons/JJ_Icon_Time_RGB.svg";
import sessionIcon  from "/icons/JJ_Icon_Slow_RGB.svg";
import statusIcon   from "/icons/JJIM_Icon_Fingernail.svg";
import arrowIcon    from "/icons/JJ_Icon_Chevron_Right.svg";
import calendarIcon from "/icons/JJ_Icon_Calendar_RGB.svg";
import searchIcon   from "/icons/JJ_Icon_Search_RGB.svg";
import aiIcon       from "/icons/JJ_Icon_AI_RGB.svg";
import gearIcon     from "/icons/JJ_Icon_Gear_RGB.svg";
import data_validation   from "/icons/JJ_Icon_Records_Information_RGB.svg";
import networkIcon      from "/icons/JJ_Icon_AI_RGB.svg";

const Icon = ({ src, alt, className = "w-6 h-6", style = {} }) => (
  <img src={src} alt={alt} className={className} style={style} />
);

const RED_ICON_FILTER    = 'brightness(0) saturate(100%) invert(17%) sepia(100%) saturate(7426%) hue-rotate(3deg) brightness(92%) contrast(118%)';
const WHITE_ICON_FILTER  = 'brightness(0) invert(1)';
const BLUE_ICON_FILTER   = 'brightness(0) saturate(100%) invert(37%) sepia(93%) saturate(1535%) hue-rotate(189deg) brightness(91%) contrast(101%)';
const VIOLET_ICON_FILTER = 'brightness(0) saturate(100%) invert(29%) sepia(60%) saturate(1500%) hue-rotate(255deg) brightness(85%) contrast(95%)';
const GREEN_ICON_FILTER  = 'brightness(0) saturate(100%) invert(35%) sepia(80%) saturate(800%) hue-rotate(85deg) brightness(88%) contrast(95%)';
const GRAY_ICON_FILTER = 'brightness(0) saturate(100%) invert(48%) sepia(97%) saturate(2446%) hue-rotate(347deg) brightness(102%) contrast(105%)';

// Per-color config — all hex values match tailwind.config.js exactly
// badgeBg/badgeBorder removed — badge uses Tailwind bg-jj-gray-01 / border-jj-gray-02 instead
const COLORS = {
  red:    { hex: '#eb1700', filter: RED_ICON_FILTER },
  blue:   { hex: '#0f68b2', filter: BLUE_ICON_FILTER },
  violet: { hex: '#8c3bbb', filter: VIOLET_ICON_FILTER },
  green:  { hex: '#328714', filter: GREEN_ICON_FILTER },
  gray: { hex: '#6e6259', filter: GRAY_ICON_FILTER },
  dgray: { hex: '#312c2a', filter: GRAY_ICON_FILTER },
};

// jj-gray-02 hex — used only in JS event handlers where Tailwind classes can't apply
const BORDER_DEFAULT = '#e8e6e3'; // = jj-gray-02 in tailwind.config.js

// ─── Welcome Banner ────────────────────────────────────────────────────────────
const WelcomeBanner = ({ displayName, displayEmail, currentTime, profilePhoto, photoLoading }) => {
  const getGreeting = () => {
    const hour = currentTime.getHours();
    if (hour < 12) return 'Good morning';
    if (hour < 18) return 'Good afternoon';
    return 'Good evening';
  };

  return (
    <div className="bg-white rounded-xl overflow-hidden shadow-sm border border-jj-gray-02">
      <div className="flex items-center justify-between px-8 py-6">
        {/* Left: avatar + greeting */}
        <div className="flex items-center gap-6 min-w-0">
          <div className="flex-shrink-0 relative">
            {photoLoading ? (
              <div className="w-16 h-16 rounded-full bg-jj-gray-02 animate-pulse" />
            ) : profilePhoto ? (
              <img src={profilePhoto} alt={displayName}
                className="w-16 h-16 rounded-full object-cover border-2 border-jj-gray-04" />
            ) : (
              <div className="w-16 h-16 rounded-full flex items-center justify-center bg-jj-gray-01 border-2 border-jj-red">
                <Icon src={userIcon} alt="User" className="w-9 h-9" style={{ filter: RED_ICON_FILTER }} />
              </div>
            )}
            <span className="absolute bottom-0.5 right-0.5 w-3 h-3 rounded-full bg-jj-green-03 border-2 border-white" />
          </div>

          <div className="min-w-0">
            <p className="text-xs font-johnson-text text-jj-gray-05 uppercase tracking-widest mb-0.5">
              {getGreeting()}
            </p>
            <h1 className="font-johnson-display text-jj-gray-08 truncate"
              style={{ fontSize: '1.75rem', lineHeight: '115%', fontWeight: 400 }}>
              {displayName}
            </h1>
            <p className="text-sm font-johnson-text text-jj-gray-05 mt-0.5 truncate">{displayEmail}</p>
          </div>
        </div>

        {/* Right: date block */}
        <div className="flex-shrink-0 text-right hidden sm:block">
          <div className="inline-flex flex-col items-end gap-1 pl-8 border-l border-jj-gray-03">
            <div className="flex items-center gap-2 text-jj-gray-05">
              <Icon src={calendarIcon} alt="Calendar" className="w-4 h-4" style={{ filter: RED_ICON_FILTER }} />
              <span className="text-xs font-johnson-text uppercase tracking-widest">
                {currentTime.toLocaleDateString('en-US', { weekday: 'long' })}
              </span>
            </div>
            <p className="font-johnson-display text-jj-gray-08 text-2xl" style={{ fontWeight: 300 }}>
              {currentTime.toLocaleDateString('en-US', { month: 'long', day: 'numeric', year: 'numeric' })}
            </p>
          </div>
        </div>
      </div>
    </div>
  );
};

// ─── Status Strip ──────────────────────────────────────────────────────────────
const StatusStrip = ({ currentTime, sessionStart }) => {
  const getCurrentTime = () =>
    currentTime.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: true });

  const getSessionDuration = () => {
    const duration = Math.floor((currentTime - sessionStart) / 60000);
    if (duration < 1) return 'Just started';
    if (duration === 1) return '1 min';
    if (duration < 60) return `${duration} min`;
    const hours = Math.floor(duration / 60);
    return hours === 1 ? '1 hr' : `${hours} hrs`;
  };

  const stats = [
    { icon: timeIcon,    label: 'Current time',    value: getCurrentTime() },
    { icon: sessionIcon, label: 'Session duration', value: getSessionDuration() },
    { icon: statusIcon,  label: 'Account status',   value: 'Active' },
  ];

  return (
    <div className="bg-white rounded-xl border border-jj-gray-02 shadow-sm overflow-hidden">
      <div className="grid grid-cols-1 md:grid-cols-3 divide-y md:divide-y-0 md:divide-x divide-jj-gray-02">
        {stats.map((stat, i) => (
          <div key={i} className="flex items-center gap-4 px-6 py-5">
            <div className="w-10 h-10 rounded-lg flex items-center justify-center flex-shrink-0 bg-jj-gray-01">
              <Icon src={stat.icon} alt={stat.label} className="w-5 h-5" style={{ filter: RED_ICON_FILTER }} />
            </div>
            <div>
              <p className="text-xs font-johnson-text text-jj-gray-05 uppercase tracking-wider mb-0.5">
                {stat.label}
              </p>
              <p className="font-johnson-display text-jj-gray-08 text-lg" style={{ fontWeight: 600, lineHeight: '120%' }}>
                {stat.value}
              </p>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};

// ─── Section Header ────────────────────────────────────────────────────────────
const SectionHeader = ({ title, subtitle }) => (
  <div className="flex items-start gap-4">
    <div className="w-1 self-stretch rounded-full bg-jj-red flex-shrink-0" style={{ minHeight: '2.5rem' }} />
    <div>
      <h2 className="font-johnson-display text-jj-gray-08"
        style={{ fontSize: '1.35rem', fontWeight: 400, lineHeight: '115%' }}>
        {title}
      </h2>
      {subtitle && (
        <p className="font-johnson-text text-jj-gray-05 text-sm mt-1" style={{ lineHeight: '145%' }}>
          {subtitle}
        </p>
      )}
    </div>
  </div>
);

// ─── Tool Card ─────────────────────────────────────────────────────────────────
const ToolCard = ({ title, description, icon, onClick, badge, meta, colorKey }) => {
  const c = COLORS[colorKey] || COLORS.red;
  return (
    <div
      onClick={onClick}
      className="group bg-white rounded-xl border border-jj-gray-02 shadow-sm hover:shadow-md cursor-pointer overflow-hidden relative h-full flex flex-col"
      style={{ transition: 'box-shadow 0.2s ease, border-color 0.2s ease' }}
      onMouseEnter={e => e.currentTarget.style.borderColor = c.hex}
      onMouseLeave={e => e.currentTarget.style.borderColor = BORDER_DEFAULT}
    >
      <div className="p-6 flex flex-col flex-1">
        {/* Header row */}
        <div className="flex items-start justify-between mb-4">
          <div className="w-12 h-12 rounded-lg flex items-center justify-center flex-shrink-0 shadow-sm"
            style={{ background: c.hex }}>
            <Icon src={icon} alt={title} className="w-6 h-6" style={{ filter: WHITE_ICON_FILTER }} />
          </div>
          {badge && (
            <span className="text-xs font-johnson-text font-bold px-2.5 py-1 rounded-full bg-jj-gray-01 border border-jj-gray-02"
              style={{ color: c.hex }}>
              {badge}
            </span>
          )}
        </div>

        {/* Title */}
        <h3 className="font-johnson-display text-jj-gray-08 mb-2 group-hover:text-jj-red transition-colors duration-200"
          style={{ fontSize: '1.1rem', fontWeight: 400, lineHeight: '120%' }}>
          {title}
        </h3>

        {/* Description */}
        <p className="font-johnson-text text-jj-gray-06 text-sm mb-4" style={{ lineHeight: '155%' }}>
          {description}
        </p>

        {/* Meta tags */}
        {/*{meta && (*/}
        {/*  <div className="flex flex-wrap gap-2 mb-4">*/}
        {/*    {meta.map((tag, i) => (*/}
        {/*      <span key={i} className="text-xs font-johnson-text text-jj-gray-06 bg-jj-gray-01 px-2.5 py-1 rounded-full">*/}
        {/*        {tag}*/}
        {/*      </span>*/}
        {/*    ))}*/}
        {/*  </div>*/}
        {/*)}*/}

        {/* Spacer pushes CTA to bottom */}
        <div className="flex-1" />

        {/* CTA */}
        <div className="flex items-center gap-1 font-johnson-text font-semibold text-sm group-hover:gap-1.5 transition-all duration-200"
          style={{ color: c.hex }}>
          <span>Launch</span>
          <Icon src={arrowIcon} alt="Arrow" className="w-4 h-4" style={{ filter: c.filter }} />
        </div>
      </div>
    </div>
  );
};

// ─── Main Dashboard ────────────────────────────────────────────────────────────
export default function Dashboard() {
  const navigate = useNavigate();
  const { user, logout } = useAuth();
  const { hasPermission } = useRBAC();
  const [currentTime, setCurrentTime] = useState(new Date());
  const [sessionStart] = useState(new Date());
  const { profilePhoto, photoLoading } = useProfilePhoto(user, logout);

  useEffect(() => {
    const timer = setInterval(() => setCurrentTime(new Date()), 60000);
    return () => clearInterval(timer);
  }, []);

  const rawName      = user?.name || user?.username?.split('@')[0] || 'User';
  const displayName  = rawName.replace(/\s*\[.*?\]\s*/g, '').trim();
  const displayEmail = user?.email || user?.username || '';

  const allTools = [
    {
      id: 'dqe',
      title: 'Data Quality Engine',
      description: 'Monitor and validate data migration with real-time quality checks, comprehensive validation reports, and detailed analytics.',
      icon: dqIcon, badge: 'NEW', path: '/dqe',
      meta: ['Validation', 'Analytics', 'Reporting'],
      permission: 'viewDQE', colorKey: 'red',
    },
    {
      id: 'rag',
      title: 'RAG (Retrieval-Augmented Generation)',
      description: 'Retrieve and generate answers from various document repositories using AI-powered retrieval-augmented generation.',
      icon: networkIcon, badge: null, path: '/rag',
      meta: ['EPOD', 'RIMDOCS', 'AI Search'],
      permission: 'viewRAG', colorKey: 'blue',
    },
    {
      id: 'fuzzy-match',
      title: 'Fuzzy Match',
      description: 'Find records across datasets using intelligent fuzzy matching and configurable similarity thresholds.',
      icon: searchIcon, badge: null, path: '/fuzzy-match',
      meta: ['Matching', 'Deduplication', 'Search'],
      permission: 'viewFuzzyMatch', colorKey: 'dgray',
    },
    {
      id: 'ai-chatbot',
      title: 'AI Assistant',
      description: 'Ask questions about your data, generate insights, and get instant answers.',
      icon: aiIcon, badge: null, path: '/ai-chatbot',
      meta: ['AI', 'Chat', 'Insights'],
      permission: 'viewAIChatbot', colorKey: 'violet',
    },
    {
      id: 'data-validation',
      title: 'Data Validation',
      description: 'Run and monitor datatypes across your pipelines. Catch schema issues, null violations, etc. before they impact downstream processes.',
      icon: data_validation, badge: null, path: '/data-validation',
      meta: ['Schema', 'Null Checks', 'Custom Rules'],
      permission: 'viewDataValidation', colorKey: 'gray',
    },
    {
      id: 'user-management',
      title: 'User Management',
      description: 'Manage user roles, permissions, and access controls across the platform. Assign AD group mappings and configure role-based access.',
      icon: gearIcon, badge: null, path: '/user-management',
      meta: ['Roles', 'Permissions', 'Access'],
      permission: 'viewUserManagement', colorKey: 'green',
    },
  ];

  const tools = allTools.filter(t => hasPermission(t.permission));

  return (
    <div className="h-full flex flex-col overflow-auto bg-jj-gray-01">
      <div className="flex flex-col px-8 py-6 gap-5">

        {/* Welcome Banner */}
        <div className="flex-shrink-0">
          <WelcomeBanner
            displayName={displayName}
            displayEmail={displayEmail}
            currentTime={currentTime}
            profilePhoto={profilePhoto}
            photoLoading={photoLoading}
          />
        </div>

        {/* Status Strip */}
        <div className="flex-shrink-0">
          <StatusStrip currentTime={currentTime} sessionStart={sessionStart} />
        </div>

        <div className="flex-shrink-0 border-t border-jj-gray-02" />

        {/* Section header */}
        <div className="flex-shrink-0">
          <SectionHeader
            title="Applications"
            subtitle="Select a tool below to get started"
          />
        </div>

        {/* Tool cards */}
        <div>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-5">
            {tools.map(tool => (
              <ToolCard
                key={tool.id}
                title={tool.title}
                description={tool.description}
                icon={tool.icon}
                badge={tool.badge}
                meta={tool.meta}
                colorKey={tool.colorKey}
                onClick={() => navigate(tool.path)}
              />
            ))}
          </div>
        </div>

        {/* Footer */}
        <div className="flex-shrink-0 border-t border-jj-gray-02 pt-3 flex items-center justify-center">
          <p className="text-xs font-johnson-text text-jj-gray-04">
            © {new Date().getFullYear()} RDI · Johnson &amp; Johnson
          </p>
        </div>

      </div>
    </div>
  );
}