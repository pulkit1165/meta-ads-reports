// Studd Muffyn design system — mirrors studdmuffyn.com's live Shopify theme:
// white background, #121212 text, warm gold #c79353 accent, cream #fcf4ee
// header, red sale badges/prices. Values pulled from the site's CSS variables.
import { Dimensions, Platform } from 'react-native';

// On web the app renders inside a centered phone-width frame, so all
// width math must use the frame width, not the browser window width.
export const SCREEN_W = Platform.OS === 'web'
  ? Math.min(Dimensions.get('window').width, 430)
  : Dimensions.get('window').width;

export const colors = {
  bg: '#ffffff',                       // --color-background
  surface: '#f7f7f7',                  // --color-background-meta
  surfaceHi: '#eaeaea',                // --color-background-darker-meta
  card: '#ffffff',
  header: '#fcf4ee',                   // --color-background-header
  line: '#e4ddd9',                     // from --color-border, lightened
  text: '#121212',                     // --color-text
  textDim: 'rgba(18,18,18,0.65)',
  textFaint: 'rgba(18,18,18,0.42)',
  gold: '#c79353',                     // --color-button-primary-background / accent
  goldSoft: '#ba823c',                 // --color-button-primary-background-hover
  goldDeep: '#a9773a',
  cream: '#fcf4ee',                    // --color-button-primary-text
  sale: '#de0f2b',                     // --color-products-sale-price
  saleBadge: '#e40e47',                // --color-background-sale-badge
  danger: '#D02F2E',
  success: '#0d944b',
  overlay: 'rgba(0,0,0,0.8)',
  chip: 'rgba(199,147,83,0.12)',
};

export const goldGradient = ['#d3a262', '#c79353', '#ba823c'] as const;
export const darkGradient = ['rgba(0,0,0,0)', 'rgba(0,0,0,0.65)'] as const;

export const radius = { sm: 10, md: 16, lg: 22, xl: 30, pill: 999 };

export const spacing = (n: number) => n * 4;

export const type = {
  // Site pairs a classic serif for headings ("New York"/Baskerville stack)
  // with a clean sans body — Georgia is the closest built-in match.
  display: { fontFamily: 'Georgia', letterSpacing: 0.2 },
  body: { fontFamily: 'System' },
};

export const shadow = {
  card: {
    shadowColor: '#000',
    shadowOpacity: 0.08,
    shadowRadius: 12,
    shadowOffset: { width: 0, height: 4 },
    elevation: 3,
  },
  glow: {
    shadowColor: colors.gold,
    shadowOpacity: 0.35,
    shadowRadius: 12,
    shadowOffset: { width: 0, height: 3 },
    elevation: 6,
  },
};

export const INR = (v: number | string) => {
  const n = typeof v === 'string' ? parseFloat(v) : v;
  if (Number.isNaN(n)) return '';
  return '₹' + n.toLocaleString('en-IN', { maximumFractionDigits: 0 });
};

export const pctOff = (price: number, compareAt?: number | null) => {
  if (!compareAt || compareAt <= price) return 0;
  return Math.round(((compareAt - price) / compareAt) * 100);
};
