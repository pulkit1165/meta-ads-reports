// Remote home config — the app's home screen mirrors studdmuffyn.com.
// Order of truth: fresh fetch → last good copy (AsyncStorage) → bundled JSON.
import { useEffect, useState } from 'react';
import AsyncStorage from '@react-native-async-storage/async-storage';
import bundled from '../config/home.json';

export const CONFIG_URL = 'https://studd-muffyn-app.vercel.app/api/home-config';
const STORE_KEY = 'sm-home-config-v1';

export interface HomeConfig {
  announcement: { messages: string[] };
  sections: any[];
  source?: string;
  generatedAt?: string;
}

function isValid(cfg: any): cfg is HomeConfig {
  return (
    !!cfg &&
    Array.isArray(cfg.sections) &&
    cfg.sections.length >= 4 &&
    !!cfg.announcement &&
    Array.isArray(cfg.announcement.messages)
  );
}

async function fetchWithTimeout(url: string, ms: number): Promise<Response> {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), ms);
  try {
    return await fetch(url, { signal: ctrl.signal });
  } finally {
    clearTimeout(t);
  }
}

export function useHomeConfig(): HomeConfig {
  const [config, setConfig] = useState<HomeConfig>(bundled as unknown as HomeConfig);

  useEffect(() => {
    let alive = true;
    (async () => {
      // 1. last good copy renders immediately while we refresh
      try {
        const raw = await AsyncStorage.getItem(STORE_KEY);
        if (raw) {
          const cached = JSON.parse(raw);
          if (alive && isValid(cached)) setConfig(cached);
        }
      } catch {}
      // 2. live config from the website mirror
      try {
        const r = await fetchWithTimeout(CONFIG_URL, 8000);
        if (!r.ok) return;
        const fresh = await r.json();
        if (isValid(fresh)) {
          if (alive) setConfig(fresh);
          AsyncStorage.setItem(STORE_KEY, JSON.stringify(fresh)).catch(() => {});
        }
      } catch {}
    })();
    return () => {
      alive = false;
    };
  }, []);

  return config;
}
