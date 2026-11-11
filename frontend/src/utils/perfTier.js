import { useEffect, useState } from 'react';

export const TIERS = ['full', 'lite', 'min'];

let tier = null;
let measured = false; // measurement only ever lowers, and only once
const subscribers = new Set();

function probe() {
  if (typeof window === 'undefined') return 'full';

  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    return 'lite';
  }
  return probeHardware();
}

let hardware = null;
/** The machine's tier alone, ignoring the OS "reduce motion" setting. */
export function hardwareTier() {
  if (!hardware) hardware = probeHardware();
  return hardware;
}

function probeHardware() {
  if (typeof window === 'undefined') return 'full';

  const cores = navigator.hardwareConcurrency || 8;
  const mem = navigator.deviceMemory || 8;

  // Software rasterizers on GPU-less VMs usually name themselves.
  let softwareGpu = false;
  let noGpu = false;
  try {
    const c = document.createElement('canvas');
    const gl = c.getContext('webgl') || c.getContext('experimental-webgl');
    if (!gl) {
      noGpu = true;
    } else {
      const dbg = gl.getExtension('WEBGL_debug_renderer_info');
      const r = dbg ? String(gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL)) : String(gl.getParameter(gl.RENDERER) || '');
      softwareGpu = /swiftshader|llvmpipe|softpipe|software|basic render|basic display|remote display|remotefx|virgl|paravirtual|hyper-v|vmware|virtualbox|citrix|parallels|qxl|vmsvga/i.test(r);
      // the browser itself says it would be rendering on the CPU
      if (!softwareGpu) {
        const c2 = document.createElement('canvas');
        const strict = c2.getContext('webgl', { failIfMajorPerformanceCaveat: true });
        if (!strict) softwareGpu = true;
        else strict.getExtension('WEBGL_lose_context')?.loseContext();
      }
      gl.getExtension('WEBGL_lose_context')?.loseContext();
    }
  } catch {
    noGpu = true;
  }

  const weak = cores <= 4 || mem <= 4;
  return noGpu || softwareGpu || weak ? 'lite' : 'full';
}

export function getTier() {
  if (!tier) tier = probe();
  return tier;
}

function setTier(next) {
  if (next === tier) return;
  tier = next;
  subscribers.forEach((fn) => {
    try {
      fn(tier);
    } catch {
      /* a bad subscriber shouldn't take the rest down */
    }
  });
}

export function subscribeTier(fn) {
  subscribers.add(fn);
  return () => subscribers.delete(fn);
}

/*
 * Count frames in one-second windows while something heavy is on screen.
 * The first window is thrown away — mount, fonts and the entrance animations
 * all land there and would slander a machine that's actually fine.
 */
export function measureTier(seconds = 5) {
  if (measured || typeof window === 'undefined') return () => {};
  measured = true;

  let frames = 0;
  let windowStart = performance.now();
  let windowsSeen = 0;
  let slowWindows = 0;
  let raf = requestAnimationFrame(step);

  function stop() {
    cancelAnimationFrame(raf);
  }

  function step(now) {
    frames += 1;
    const elapsed = now - windowStart;
    if (elapsed >= 1000) {
      const fps = (frames * 1000) / elapsed;
      windowsSeen += 1;
      frames = 0;
      windowStart = now;

      if (windowsSeen > 1) {
        if (fps < 24) {
          // Struggling badly — go straight to the cheapest tier.
          setTier('min');
          return stop();
        }
        if (fps < 46) slowWindows += 1;
        if (slowWindows >= 2) {
          setTier(getTier() === 'full' ? 'lite' : 'min');
          return stop();
        }
      }

      if (windowsSeen >= seconds) return stop();
    }
    raf = requestAnimationFrame(step);
  }

  return stop;
}

export function usePerfTier() {
  const [current, setCurrent] = useState(getTier);
  useEffect(() => subscribeTier(setCurrent), []);
  return current;
}