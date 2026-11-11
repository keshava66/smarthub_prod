import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import { useProfilePhoto } from "../hooks/useProfilePhoto";
import userIcon    from "/icons/JJ_Icon_Profile_red.svg";
import logoutIcon  from "/icons/JJ_Icon_Log_Out_RGB.svg";         // Sign out
import chevronIcon from "/icons/JJ_Icon_Chevron_Down.svg";  // Dropdown chevron

const FILTER_RED  = 'brightness(0) saturate(100%) invert(17%) sepia(100%) saturate(7426%) hue-rotate(3deg) brightness(92%) contrast(118%)';
const FILTER_GRAY = 'brightness(0) saturate(100%) invert(42%) sepia(6%) saturate(656%) hue-rotate(346deg) brightness(92%) contrast(88%)';

export default function Header() {
  const [showDropdown, setShowDropdown] = useState(false);
  const { logout, user } = useAuth();
  const navigate = useNavigate();

  const { profilePhoto, photoLoading } = useProfilePhoto(user, logout);

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
    <header className="w-full h-16 flex items-center px-6 bg-white border-b-4 border-jj-red shadow-md sticky top-0 z-20 select-none">

      {/* Centered title */}
      <div className="absolute left-1/2 top-1/2 transform -translate-x-1/2 -translate-y-1/2 flex items-center pointer-events-none">
        <h1 className="font-johnson-display text-jj-gray-08">
          <span className="text-4xl">Regulatory Data Intelligence</span>
        </h1>
      </div>

      {/* Profile section on far right */}
      <div className="ml-auto relative">
        <button
          onClick={() => setShowDropdown(!showDropdown)}
          className="flex items-center space-x-3 text-jj-gray-07 hover:text-jj-red transition-colors duration-200 px-3 py-2 rounded-lg hover:bg-jj-gray-01 focus:outline-none focus:ring-2 focus:ring-jj-red focus:ring-opacity-20"
          aria-label="User menu"
        >
          <div className="flex items-center space-x-2">
            {photoLoading ? (
              <div className="w-8 h-8 rounded-full bg-jj-gray-02 animate-pulse"></div>
            ) : displayUser.avatar ? (
              <img
                src={displayUser.avatar}
                alt={displayUser.name}
                className="w-8 h-8 rounded-full object-cover border-2 border-jj-gray-02"
                onError={() => {}}
              />
            ) : (
              <div className="w-8 h-8 rounded-full flex items-center justify-center">
                <img src={userIcon} alt="User" className="w-10 h-10" />
              </div>
            )}

            <div className="hidden sm:block text-left">
              <p className="text-sm font-johnson-display text-jj-gray-08 leading-tight">
                {displayUser.name}
              </p>
              <p className="text-xs text-jj-gray-05 font-johnson-text leading-tight">
                {displayUser.email}
              </p>
            </div>
          </div>

          <img
            src={chevronIcon}
            alt=""
            className={`w-4 h-4 transition-transform duration-200 ${showDropdown ? 'rotate-180' : ''}`}
            style={{ filter: FILTER_GRAY }}
          />
        </button>

        {/* Dropdown Menu */}
        {showDropdown && (
          <div className="absolute right-0 mt-2 w-64 bg-white rounded-lg shadow-lg border border-jj-gray-03 py-2 z-30">
            <div className="px-4 py-3 border-b border-jj-gray-02">
              <div className="flex items-center space-x-3">
                {photoLoading ? (
                  <div className="w-10 h-10 rounded-full bg-jj-gray-02 animate-pulse"></div>
                ) : displayUser.avatar ? (
                  <img
                    src={displayUser.avatar}
                    alt={displayUser.name}
                    className="w-10 h-10 rounded-full object-cover border-2 border-jj-gray-02"
                  />
                ) : (
                  <div className="w-10 h-10 rounded-full flex items-center justify-center">
                    <img src={userIcon} alt="User" className="w-10 h-10" />
                  </div>
                )}
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-johnson-display text-jj-gray-08 truncate">
                    {displayUser.name}
                  </p>
                  <p className="text-xs text-jj-gray-05 font-johnson-text truncate">
                    {displayUser.email}
                  </p>
                </div>
              </div>
            </div>

            <div className="py-1">
              <button
                onClick={handleLogout}
                className="w-full flex items-center space-x-3 px-4 py-2 text-sm font-johnson-text text-jj-red hover:bg-red-50 transition-colors duration-200 text-left"
              >
                <img src={logoutIcon} alt="" className="w-4 h-4 flex-shrink-0" style={{ filter: FILTER_RED }} />
                <span>Sign out</span>
              </button>
            </div>
          </div>
        )}

        {/* Backdrop to close dropdown */}
        {showDropdown && (
          <div
            className="fixed inset-0 z-20"
            onClick={() => setShowDropdown(false)}
            aria-hidden="true"
          />
        )}
      </div>
    </header>
  );
}