// Portal brands this app can be skinned as.
//
// The app is embedded as an iframe on the /voice-agents page of the agency
// portal, which is NetEnroll by default and white-labelled for some agencies
// (Life Leads Plus). The portal tells the frame which brand it is showing via
// `?brand=<key>` on the iframe src. Because the app's own "/" page server-
// redirects (dropping the query string), the middleware also persists the key
// in the `ai_voice_brand` cookie, and the inline script in app/layout.tsx
// mirrors it to localStorage. The resolved key ends up as `data-brand` on
// <html>, which is what the palette blocks in globals.css and <BrandLogo>
// answer to. Nothing here is security-sensitive: a brand is a skin.

export const BRAND_KEYS = ["netenroll", "life-leads-plus"] as const;
export type BrandKey = (typeof BRAND_KEYS)[number];

export const DEFAULT_BRAND: BrandKey = "netenroll";
export const BRAND_COOKIE = "ai_voice_brand";
export const BRAND_STORAGE_KEY = "ai_voice_brand";
export const BRAND_QUERY_PARAM = "brand";

export interface Brand {
  key: BrandKey;
  /** The product name as the portal says it. */
  name: string;
  /** Horizontal wordmark for light grounds. */
  wordmark: string;
  /** Wordmark reversed out for dark grounds. */
  wordmarkOnDark: string;
  /** Square icon for collapsed / icon-sized slots. */
  mark: string;
  favicon: string;
  appleTouchIcon: string;
}

export const BRANDS: Record<BrandKey, Brand> = {
  netenroll: {
    key: "netenroll",
    name: "NetEnroll",
    wordmark: "/brands/netenroll/wordmark.png",
    // NetEnroll has no reversed lockup ("net" is black); the dark brand panel
    // carries the wordmark on a white plate instead.
    wordmarkOnDark: "/brands/netenroll/wordmark.png",
    mark: "/brands/netenroll/mark.svg",
    favicon: "/brands/netenroll/favicon-32.png",
    appleTouchIcon: "/brands/netenroll/apple-touch-icon.png",
  },
  "life-leads-plus": {
    key: "life-leads-plus",
    name: "Life Leads Plus",
    wordmark: "/brands/life-leads-plus/wordmark.png",
    wordmarkOnDark: "/brands/life-leads-plus/wordmark-on-dark.png",
    mark: "/brands/life-leads-plus/mark-128.png",
    favicon: "/brands/life-leads-plus/favicon-32.png",
    appleTouchIcon: "/brands/life-leads-plus/apple-touch-icon.png",
  },
};

export function isBrandKey(value: unknown): value is BrandKey {
  return typeof value === "string" && (BRAND_KEYS as readonly string[]).includes(value);
}

/** The brand currently applied to the document (client only; default on the server). */
export function getActiveBrand(): Brand {
  if (typeof document === "undefined") return BRANDS[DEFAULT_BRAND];
  const key = document.documentElement.getAttribute("data-brand");
  return BRANDS[isBrandKey(key) ? key : DEFAULT_BRAND];
}

/**
 * Runs in <head> before first paint (see app/layout.tsx): resolves the brand
 * from ?brand=, then the cookie, then localStorage, sets `data-brand`, and
 * points the favicon at the brand's. Kept as a string because it is inlined.
 */
export const BRAND_BOOT_SCRIPT = `(function(){try{
var B=${JSON.stringify(
  Object.fromEntries(
    BRAND_KEYS.map((k) => [k, { f: BRANDS[k].favicon, a: BRANDS[k].appleTouchIcon, n: BRANDS[k].name }])
  )
)};
var k=null;
try{var q=new URLSearchParams(location.search).get(${JSON.stringify(BRAND_QUERY_PARAM)});if(q&&B[q])k=q;}catch(e){}
if(!k){var m=document.cookie.match(/(?:^|; )${BRAND_COOKIE}=([^;]+)/);if(m&&B[m[1]])k=m[1];}
if(!k){try{var s=localStorage.getItem(${JSON.stringify(BRAND_STORAGE_KEY)});if(s&&B[s])k=s;}catch(e){}}
if(!k)k=${JSON.stringify(DEFAULT_BRAND)};
try{localStorage.setItem(${JSON.stringify(BRAND_STORAGE_KEY)},k);}catch(e){}
var d=document.documentElement;d.setAttribute('data-brand',k);
function L(r,h){var l=document.createElement('link');l.rel=r;l.href=h;l.setAttribute('data-brand-icon','');document.head.appendChild(l);}
L('icon',B[k].f);L('apple-touch-icon',B[k].a);
}catch(e){document.documentElement.setAttribute('data-brand',${JSON.stringify(DEFAULT_BRAND)});}})();`;
