"use client";

import { ArrowLeft } from "lucide-react";
import React, { useEffect, useState } from "react";

import { type Brand, getActiveBrand } from "@/lib/brand";
import { cn } from "@/lib/utils";

/**
 * "Back to <portal>" -- for brands whose portal opens this app full-window
 * (Brand.portalUrl). Leaves the whole window, even if the app is framed.
 * Read after mount: the brand is set on <html> before hydration, and the
 * server render cannot know it.
 */
export function PortalHomeLink({ compact = false, className }: { compact?: boolean; className?: string }) {
  const [brand, setBrand] = useState<Brand | null>(null);
  useEffect(() => setBrand(getActiveBrand()), []);
  if (!brand?.portalUrl) return null;
  return (
    <a
      href={brand.portalUrl}
      target="_top"
      title={`Back to ${brand.name}`}
      aria-label={`Back to ${brand.name} main menu`}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-md border border-sidebar-border bg-background px-2.5 py-1.5 text-xs font-medium text-foreground transition-colors hover:bg-accent",
        compact && "justify-center px-1.5",
        className
      )}
    >
      <ArrowLeft className="h-3.5 w-3.5 shrink-0" />
      {compact ? null : <span className="truncate">{brand.name} main menu</span>}
    </a>
  );
}
