import React, { createContext, useContext, useEffect, useRef, useState, useCallback } from 'react';
import ReactDOM from 'react-dom';
import { useAuth } from './AuthContext';
import alertSrc from "/icons/JJ_Icon_Alert_RGB.svg";
import timerSrc from "/icons/JJ_Icon_Timer_RGB.svg";
import { registerActivityCallback, unregisterActivityCallback } from './apiActivityTracker';

const SessionTimeoutContext = createContext();

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
    // Non-critical
  }
};

// Production Configuration
const INACTIVITY_TIMEOUT = 15 * 60 * 1000; // 15 minutes
const WARNING_TIME = 13 * 120 * 1000; // 13 minutes (2 minute warning)
const WARNING_DURATION = 120; // 120 seconds warning

/**
 * SessionTimeoutProvider
 * Automatically logs out users after 15 minutes of inactivity
 * Shows a 120-second warning before logout
 */
function SessionTimeoutProvider({ children }) {
  const { logout, authenticated, user } = useAuth();
  const [modalVisible, setModalVisible] = useState(false);
  const [countdown, setCountdown] = useState(WARNING_DURATION);

  const timeoutRef = useRef(null);
  const warningTimeoutRef = useRef(null);
  const countdownIntervalRef = useRef(null);
  const isWarningShowing = useRef(false);

  const clearAllTimers = useCallback(() => {
    if (timeoutRef.current) {
      clearTimeout(timeoutRef.current);
      timeoutRef.current = null;
    }
    if (warningTimeoutRef.current) {
      clearTimeout(warningTimeoutRef.current);
      warningTimeoutRef.current = null;
    }
    if (countdownIntervalRef.current) {
      clearInterval(countdownIntervalRef.current);
      countdownIntervalRef.current = null;
    }
  }, []);

  const startTimers = useCallback(() => {
    if (!authenticated) return;

    clearAllTimers();
    isWarningShowing.current = false;

    // Warning timer (14 minutes)
    warningTimeoutRef.current = setTimeout(() => {
      isWarningShowing.current = true;
      setModalVisible(true);
      setCountdown(WARNING_DURATION);

      // Countdown timer
      let remaining = WARNING_DURATION;
      countdownIntervalRef.current = setInterval(() => {
        remaining--;
        setCountdown(remaining);
        if (remaining <= 0) {
          clearInterval(countdownIntervalRef.current);
        }
      }, 1000);
    }, WARNING_TIME);

    // Logout timer (15 minutes)
    timeoutRef.current = setTimeout(() => {
      doLogout();
    }, INACTIVITY_TIMEOUT);
  }, [authenticated, clearAllTimers]);

  const doLogout = useCallback(async () => {
    clearAllTimers();
    setModalVisible(false);
    isWarningShowing.current = false;

    try {
      await logAuditEvent('session_timeout', user?.email || user?.username || '', 'Session timed out due to inactivity');
      await logout();
    } catch (error) {
      console.error('Session timeout logout error:', error);
    }
  }, [logout, clearAllTimers, user]);

  const handleStayLoggedIn = useCallback(() => {
    setModalVisible(false);
    isWarningShowing.current = false;
    startTimers();
  }, [startTimers]);

  const handleActivity = useCallback(() => {
    if (!isWarningShowing.current) {
      startTimers();
    }
  }, [startTimers]);

  // Setup activity tracking
  useEffect(() => {
    if (!authenticated) {
      clearAllTimers();
      setModalVisible(false);
      isWarningShowing.current = false;
      unregisterActivityCallback();
      return;
    }

    // Start timers
    startTimers();

    // Register API activity callback
    registerActivityCallback(handleActivity);

    // Register DOM event listeners
    const events = ['mousedown', 'mousemove', 'keypress', 'keydown', 'click', 'scroll', 'touchstart'];
    events.forEach(event => {
      window.addEventListener(event, handleActivity, { passive: true });
    });

    // Cleanup
    return () => {
      events.forEach(event => {
        window.removeEventListener(event, handleActivity);
      });
      unregisterActivityCallback();
      clearAllTimers();
    };
  }, [authenticated, startTimers, handleActivity, clearAllTimers]);

  const value = {
    resetTimers: startTimers,
  };

  return (
    <SessionTimeoutContext.Provider value={value}>
      {children}
      {modalVisible && authenticated && (
        <SessionTimeoutModalPortal
          countdown={countdown}
          onStayLoggedIn={handleStayLoggedIn}
        />
      )}
    </SessionTimeoutContext.Provider>
  );
}

/**
 * Portal wrapper to render modal at document.body level
 */
function SessionTimeoutModalPortal({ countdown, onStayLoggedIn }) {
  return ReactDOM.createPortal(
    <SessionTimeoutModal countdown={countdown} onStayLoggedIn={onStayLoggedIn} />,
    document.body
  );
}

/**
 * Session Timeout Warning Modal
 */
function SessionTimeoutModal({ countdown, onStayLoggedIn }) {
  return (
    <div
      data-session-timeout-modal
      style={{
        position: 'fixed',
        top: 0,
        left: 0,
        right: 0,
        bottom: 0,
        zIndex: 999999,
        backgroundColor: 'rgba(0, 0, 0, 0.7)',
        backdropFilter: 'blur(4px)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '1rem'
      }}
    >
      <div
        style={{
          backgroundColor: 'white',
          borderRadius: '1rem',
          maxWidth: '28rem',
          width: '100%',
          boxShadow: '0 25px 50px -12px rgba(0, 0, 0, 0.5)',
          border: '4px solid #fbe058',
          overflow: 'hidden',
          animation: 'pulse-border 2s ease-in-out infinite'
        }}
      >
        {/* Header */}
        <div
          style={{
            background: 'linear-gradient(to right, #fbe058, #ff6017)',
            padding: '1.5rem',
            display: 'flex',
            alignItems: 'center',
            gap: '0.75rem'
          }}
        >
          <div
            style={{
              width: '3rem',
              height: '3rem',
              backgroundColor: 'white',
              borderRadius: '50%',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              animation: 'pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite'
            }}
          >
            <img src={alertSrc} alt="" style={{ width: '1.75rem', height: '1.75rem', filter: 'brightness(0) saturate(100%) invert(48%) sepia(97%) saturate(2446%) hue-rotate(347deg) brightness(102%) contrast(105%)' }} />
          </div>
          <h2 style={{
            fontSize: '1.25rem',
            fontWeight: 'bold',
            fontFamily: 'JohnsonDisplay, Arial, sans-serif',
            color: '#312c2a',
            lineHeight: '110%',
            margin: 0
          }}>
            Session Timeout Warning
          </h2>
        </div>

        {/* Body */}
        <div style={{ padding: '1.5rem' }}>
          {/* Countdown Circle */}
          <div style={{ display: 'flex', justifyContent: 'center', marginBottom: '1.5rem' }}>
            <div style={{ position: 'relative', width: '8rem', height: '8rem' }}>
              <svg style={{ transform: 'rotate(-90deg)', width: '8rem', height: '8rem' }}>
                <circle cx="64" cy="64" r="56" stroke="#f1efed" strokeWidth="8" fill="none" />
                <circle
                  cx="64"
                  cy="64"
                  r="56"
                  stroke="#eb1700"
                  strokeWidth="8"
                  fill="none"
                  strokeDasharray={`${2 * Math.PI * 56}`}
                  strokeDashoffset={`${2 * Math.PI * 56 * (1 - countdown / WARNING_DURATION)}`}
                  strokeLinecap="round"
                  style={{ transition: 'stroke-dashoffset 1s linear' }}
                />
              </svg>
              <div style={{
                position: 'absolute',
                inset: 0,
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                justifyContent: 'center'
              }}>
                <img src={timerSrc} alt="" style={{ width: '2rem', height: '2rem', marginBottom: '0.25rem', animation: 'pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite', filter: 'brightness(0) saturate(100%) invert(17%) sepia(100%) saturate(7426%) hue-rotate(3deg) brightness(92%) contrast(118%)' }} />
                <span style={{
                  fontSize: '3rem',
                  fontWeight: 'bold',
                  color: '#eb1700',
                  fontFamily: 'JohnsonDisplay, Arial, sans-serif',
                  lineHeight: 1
                }}>
                  {countdown}
                </span>
                <span style={{
                  fontSize: '0.75rem',
                  color: '#6e6259',
                  fontFamily: 'JohnsonText, Arial, sans-serif',
                  fontWeight: '500'
                }}>
                  seconds
                </span>
              </div>
            </div>
          </div>

          {/* Message */}
          <div style={{ textAlign: 'center', marginBottom: '1.5rem' }}>
            <p style={{
              fontSize: '1rem',
              color: '#312c2a',
              fontFamily: 'JohnsonText, Arial, sans-serif',
              lineHeight: '1.625',
              margin: '0 0 0.5rem 0'
            }}>
              You'll be logged out in <span style={{ fontWeight: 'bold', color: '#eb1700' }}>{countdown} seconds</span> due to inactivity.
            </p>
            <p style={{
              fontSize: '0.875rem',
              color: '#6e6259',
              fontFamily: 'JohnsonText, Arial, sans-serif',
              margin: 0
            }}>
              Click on "Stay Logged In" button to extend your session.
            </p>
          </div>

          {/* Action Button */}
          <button
            onClick={onStayLoggedIn}
            style={{
              width: '100%',
              padding: '1rem 1.5rem',
              background: 'linear-gradient(to right, #eb1700, #9e0000)',
              color: 'white',
              borderRadius: '0.75rem',
              fontWeight: 'bold',
              fontFamily: 'JohnsonText, Arial, sans-serif',
              fontSize: '1.125rem',
              border: 'none',
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              gap: '0.75rem',
              boxShadow: '0 10px 15px -3px rgba(0, 0, 0, 0.1)',
              transition: 'all 0.3s'
            }}
            onMouseOver={(e) => {
              e.currentTarget.style.transform = 'scale(1.05)';
              e.currentTarget.style.boxShadow = '0 20px 25px -5px rgba(0, 0, 0, 0.1)';
            }}
            onMouseOut={(e) => {
              e.currentTarget.style.transform = 'scale(1)';
              e.currentTarget.style.boxShadow = '0 10px 15px -3px rgba(0, 0, 0, 0.1)';
            }}
          >
            <img src={alertSrc} alt="" style={{ width: '1.5rem', height: '1.5rem', filter: 'brightness(0) invert(1)' }} />
            Stay Logged In
          </button>

          {/* Info Text */}
          <p style={{
            fontSize: '0.75rem',
            color: '#a39992',
            fontFamily: 'JohnsonText, Arial, sans-serif',
            textAlign: 'center',
            marginTop: '1rem',
            marginBottom: 0
          }}>
            Your session will automatically extend for another 15 minutes
          </p>
        </div>
      </div>

      {/* Animations */}
      <style dangerouslySetInnerHTML={{__html: `
        @keyframes pulse-border {
          0%, 100% { border-color: #fbe058; }
          50% { border-color: #ff6017; }
        }
        @keyframes pulse {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.5; }
        }
      `}} />
    </div>
  );
}

/**
 * Hook to access session timeout context
 */
export function useSessionTimeout() {
  const context = useContext(SessionTimeoutContext);
  if (!context) {
    throw new Error('useSessionTimeout must be used within a SessionTimeoutProvider');
  }
  return context;
}

// Export provider
export { SessionTimeoutProvider };