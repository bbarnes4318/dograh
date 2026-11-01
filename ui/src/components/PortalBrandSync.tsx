"use client";

import { useEffect } from "react";

import { activePortalBrand } from "@/lib/portalBrand";

/** Names the tab and swaps the favicon for the active portal brand, if any. */
export function PortalBrandSync() {
  useEffect(() => {
    const active = activePortalBrand();
    if (!active) return;
    const { brand } = active;
    const retitle = () => {
      if (document.title.includes("Dograh")) {
        document.title = document.title.replace(/Dograh/g, brand.name);
      }
    };
    retitle();
    const observer = new MutationObserver(retitle);
    const title = document.querySelector("title");
    if (title) observer.observe(title, { childList: true });
    let icon = document.querySelector<HTMLLinkElement>("link[rel~='icon']");
    if (!icon) {
      icon = document.createElement("link");
      icon.rel = "icon";
      document.head.appendChild(icon);
    }
    icon.href = brand.favicon;
    return () => observer.disconnect();
  }, []);
  return null;
}
