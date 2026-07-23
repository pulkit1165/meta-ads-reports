// Per-product extras scraped from the website's own product pages:
// Judge.me review data, Pairs-well-with, and theme detail sections.
// Served as static JSON from the app's Vercel deployment.
import { useEffect, useState } from 'react';

const EXTRAS_BASE = 'https://studd-muffyn-app.vercel.app/extras';

export interface Review {
  score: number;
  author: string;
  date: string;
  title: string;
  body: string;
}

export interface ProductExtras {
  handle: string;
  rating: number;
  reviewCount: number;
  reviews: Review[];
  pairsWith: string[];
  sections: { heading: string; text: string }[];
}

const cache = new Map<string, ProductExtras | null>();

export function useProductExtras(handle?: string): ProductExtras | null {
  const [extras, setExtras] = useState<ProductExtras | null>(
    handle && cache.has(handle) ? cache.get(handle)! : null
  );

  useEffect(() => {
    if (!handle) return;
    if (cache.has(handle)) {
      setExtras(cache.get(handle)!);
      return;
    }
    let alive = true;
    (async () => {
      try {
        const ctrl = new AbortController();
        const t = setTimeout(() => ctrl.abort(), 7000);
        const r = await fetch(`${EXTRAS_BASE}/${handle}.json`, { signal: ctrl.signal });
        clearTimeout(t);
        if (!r.ok) {
          cache.set(handle, null);
          return;
        }
        const j = (await r.json()) as ProductExtras;
        cache.set(handle, j);
        if (alive) setExtras(j);
      } catch {
        cache.set(handle, null);
      }
    })();
    return () => {
      alive = false;
    };
  }, [handle]);

  return extras;
}
