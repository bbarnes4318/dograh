/**
 * The portal brand this app is skinned as, when it is embedded in a branded
 * portal.
 *
 * The embedding portal names its brand in the frame's URL (`?brand=<key>`).
 * A known key is remembered for this origin (localStorage) so in-app
 * navigation, which drops the query, keeps the skin; any other value clears
 * it and the app shows its default look. The choice is applied to
 * `<html data-brand>` before React hydrates (see app/layout.tsx), and the
 * palette lives in globals.css under `[data-brand='<key>']`.
 */

export const PORTAL_BRAND_STORAGE_KEY = "portal-brand";

export interface PortalBrand {
  /** The product name in the tab title and alt text. */
  name: string;
  /** Wordmark for light surfaces. */
  logo: string;
  /** Wordmark for dark surfaces. */
  logoInverse: string;
  /** Square mark, for compact slots such as the sidebar header. */
  mark: string;
  favicon: string;
}

export const PORTAL_BRANDS: Record<string, PortalBrand> = {
  "home-payouts": {
    name: "Home Payouts",
    logo: "/brands/home-payouts/wordmark.png",
    logoInverse: "/brands/home-payouts/wordmark-on-dark.png",
    mark: "/brands/home-payouts/mark.png",
    favicon: "/brands/home-payouts/favicon-32.png",
  },
};

/** The brand applied to this page, or null for the default look. Client only. */
export function activePortalBrand(): {
  key: string;
  brand: PortalBrand;
} | null {
  if (typeof document === "undefined") return null;
  const key = document.documentElement.dataset.brand;
  return key && PORTAL_BRANDS[key] ? { key, brand: PORTAL_BRANDS[key] } : null;
}

/**
 * The inline script for <head>: reads `?brand=`, remembers or clears it, and
 * sets `<html data-brand>` before first paint. Known keys only.
 */
export const PORTAL_BRAND_BOOT_SCRIPT = `
(function() {
  try {
    var known = ${JSON.stringify(Object.keys(PORTAL_BRANDS))};
    var param = new URLSearchParams(window.location.search).get('brand');
    if (param !== null) {
      if (known.indexOf(param) >= 0) localStorage.setItem('${PORTAL_BRAND_STORAGE_KEY}', param);
      else localStorage.removeItem('${PORTAL_BRAND_STORAGE_KEY}');
    }
    var stored = localStorage.getItem('${PORTAL_BRAND_STORAGE_KEY}');
    if (stored && known.indexOf(stored) >= 0) document.documentElement.setAttribute('data-brand', stored);
  } catch (e) {}
})();
`;
