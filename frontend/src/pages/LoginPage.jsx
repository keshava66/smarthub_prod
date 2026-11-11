import React, { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMsal, useIsAuthenticated } from '@azure/msal-react';
import { loginRequest } from '../config/msalConfig';
import jnjlogo from "/jj-logo-stacked.png";
import jnjIcon from "/icons/JJ_Icon_Login_RGB.svg"; // Your custom JNJ icon

export default function LoginPage() {
  const navigate = useNavigate();
  const { instance, inProgress } = useMsal();
  const isAuthenticated = useIsAuthenticated();
  const [loginLoading, setLoginLoading] = useState(false);
  const [error, setError] = useState("");

  // Redirect to dashboard if already authenticated
  useEffect(() => {
    if (isAuthenticated && inProgress === "none") {
      console.log('User already authenticated, redirecting to dashboard');
      navigate('/dashboard', { replace: true });
    }
  }, [isAuthenticated, inProgress, navigate]);

  const handleLogin = async () => {
    setLoginLoading(true);
    setError("");

    try {
      // Initiate Microsoft login redirect
      await instance.loginRedirect(loginRequest);
    } catch (err) {
      console.error("Login error:", err);

      // Handle specific error types
      if (err.errorCode === 'user_cancelled') {
        setError("Login was cancelled. Please try again.");
      } else if (err.errorCode === 'interaction_in_progress') {
        setError("Another login is already in progress.");
      } else {
        setError("Unable to sign in. Please try again or contact support.");
      }

      setLoginLoading(false);
    }
  };

  // Show loading state during auth check or login redirect
  if (inProgress !== "none" || (isAuthenticated && !error)) {
    return (
      <div
        className="min-h-screen flex items-center justify-center"
        style={{
          backgroundImage: 'url(/background.png)',
          backgroundSize: 'cover',
          backgroundPosition: 'center',
          backgroundRepeat: 'no-repeat'
        }}
      >
        <div className="text-center">
          <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-jj-red mx-auto"></div>
          <p className="mt-4 text-white font-johnson-text bg-black bg-opacity-50 px-4 py-2 rounded">
            {inProgress === "login" ? "Signing in..." : "Loading..."}
          </p>
        </div>
      </div>
    );
  }

  return (
    <div
      className="min-h-screen flex items-center justify-center"
      style={{
        backgroundImage: 'url(/background.png)',
        backgroundSize: 'cover',
        backgroundPosition: 'center',
        backgroundRepeat: 'no-repeat'
      }}
    >
      <div>
        <div className="text-center mb-4">
          <img src={jnjlogo} alt="JnJ Logo" className="h-16 mx-auto mb-3" />
          <h2 className="text-2xl font-bold font-johnson-display text-jj-gray-08" style={{ lineHeight: '110%' }}>
            Welcome to Regulatory Data Intelligence
          </h2>
        </div>

        <div className="space-y-4">
          {/* JNJ SSO Sign In Button */}
          <div className="flex justify-center">
            <button
              onClick={handleLogin}
              disabled={loginLoading}
              className={`py-1.5 px-8 bg-jj-red text-white rounded-md hover:bg-jj-maroon-05 transition-all duration-200 font-johnson-text flex items-center justify-center space-x-2 shadow-md hover:shadow-lg ${
                loginLoading ? 'cursor-not-allowed opacity-60' : ''
              }`}
              style={{ outline: 'none' }}
            >
              {loginLoading ? (
                <>
                  <svg className="animate-spin h-5 w-5 text-white" fill="none" viewBox="0 0 24 24">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                  </svg>
                  <span>Signing in...</span>
                </>
              ) : (
                <>
                  <span className="font-semibold">Sign in with SSO</span>
                  <img
                    src={jnjIcon}
                    alt="JNJ"
                    className="w-5 h-5"
                    style={{ filter: 'brightness(0) invert(1)' }}
                  />
                </>
              )}
            </button>
          </div>

          {/* Error Message */}
          {error && (
            <div className="text-jj-red text-sm font-johnson-text bg-red-50 p-3 rounded-md border border-red-200" role="alert">
              {error}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="mt-4 pt-2 border-t border-jj-gray-02">
          <p className="text-xs text-jj-gray-05 font-johnson-text text-center">
            Having trouble signing in? Contact your IT administrator
          </p>
        </div>
      </div>
    </div>
  );
}