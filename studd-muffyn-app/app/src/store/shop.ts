// Cart / wishlist / recently-viewed — persisted locally, checkout via Shopify.
import { create } from 'zustand';
import { persist, createJSONStorage } from 'zustand/middleware';
import AsyncStorage from '@react-native-async-storage/async-storage';

export interface CartLine {
  handle: string;
  variantId: number;
  variantTitle: string;
  title: string;
  image?: string;
  price: number;
  compareAt: number | null;
  qty: number;
}

interface ShopState {
  cart: CartLine[];
  wishlist: string[]; // product handles
  recents: string[]; // product handles, newest first
  searches: string[];
  onboarded: boolean;
  setOnboarded: () => void;
  addToCart: (line: Omit<CartLine, 'qty'>, qty?: number) => void;
  setQty: (variantId: number, qty: number) => void;
  removeLine: (variantId: number) => void;
  clearCart: () => void;
  toggleWish: (handle: string) => void;
  pushRecent: (handle: string) => void;
  pushSearch: (q: string) => void;
}

export const useShop = create<ShopState>()(
  persist(
    (set, get) => ({
      cart: [],
      wishlist: [],
      recents: [],
      searches: [],
      onboarded: false,
      setOnboarded: () => set({ onboarded: true }),
      addToCart: (line, qty = 1) =>
        set((s) => {
          const existing = s.cart.find((l) => l.variantId === line.variantId);
          if (existing) {
            return {
              cart: s.cart.map((l) =>
                l.variantId === line.variantId ? { ...l, qty: l.qty + qty } : l
              ),
            };
          }
          return { cart: [...s.cart, { ...line, qty }] };
        }),
      setQty: (variantId, qty) =>
        set((s) => ({
          cart:
            qty <= 0
              ? s.cart.filter((l) => l.variantId !== variantId)
              : s.cart.map((l) => (l.variantId === variantId ? { ...l, qty } : l)),
        })),
      removeLine: (variantId) =>
        set((s) => ({ cart: s.cart.filter((l) => l.variantId !== variantId) })),
      clearCart: () => set({ cart: [] }),
      toggleWish: (handle) =>
        set((s) => ({
          wishlist: s.wishlist.includes(handle)
            ? s.wishlist.filter((h) => h !== handle)
            : [handle, ...s.wishlist],
        })),
      pushRecent: (handle) =>
        set((s) => ({
          recents: [handle, ...s.recents.filter((h) => h !== handle)].slice(0, 40),
        })),
      pushSearch: (q) =>
        set((s) => ({
          searches: [q, ...s.searches.filter((x) => x !== q)].slice(0, 10),
        })),
    }),
    { name: 'sm-shop', storage: createJSONStorage(() => AsyncStorage) }
  )
);

export const cartCount = (s: ShopState) => s.cart.reduce((n, l) => n + l.qty, 0);
export const cartTotal = (s: ShopState) => s.cart.reduce((n, l) => n + l.qty * l.price, 0);
export const cartSavings = (s: ShopState) =>
  s.cart.reduce((n, l) => n + l.qty * Math.max(0, (l.compareAt ?? l.price) - l.price), 0);
