"use client";

import { useEffect, useState } from "react";

import { type Brand, BRANDS, DEFAULT_BRAND, getActiveBrand } from "@/lib/brand";

/**
 * The brand applied to this document (`data-brand` on <html>, set before first
 * paint by the inline script in app/layout.tsx). Renders the default on the
 * server and the first client pass, then the real one -- so use it for text
 * (names, titles). Logos switch in CSS via <BrandLogo> and never flash.
 */
export function useBrand(): Brand {
  const [brand, setBrand] = useState<Brand>(BRANDS[DEFAULT_BRAND]);
  useEffect(() => {
    setBrand(getActiveBrand());
  }, []);
  return brand;
}
