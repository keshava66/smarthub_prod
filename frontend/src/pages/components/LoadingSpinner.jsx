import React from 'react';
import spinnerVideo from '/spinner.mp4'; // adjust the path as needed

export default function LoadingSpinner() {
  return (
    <div className="flex items-center justify-center">
      <div className="w-32 h-32 rounded-full overflow-hidden">
        <video
          src={spinnerVideo}
          autoPlay
          loop
          muted
          className="w-full h-full object-cover"
        />
      </div>
    </div>
  );
}
