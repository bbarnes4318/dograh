// Shared dark two-column auth shell, used by BOTH the Stack Auth handler
// (/handler/[...stack], cloud) and the local/OSS auth pages (/auth/login,
// /auth/signup). LEFT: a centered card that wraps the auth form (`children`).
// RIGHT (lg+ only): a brand/value panel with the portal's logo (NetEnroll or the
// agency's white label, see lib/brand.ts), proof points, and an enterprise CTA
// block at the bottom (passed in as `enterpriseSlot`). Mobile collapses to the
// single card column. The form column scrolls and stays centered so tall
// (sign-up) forms never clip on short viewports. The panel is the brand's dark
// ground (--brand-panel-image) with its accent.

import type { ReactNode } from "react";

import { BrandLogo } from "@/components/BrandLogo";

const HIGHLIGHTS = [
  "Inbound & outbound",
  "Live transfer to your agents",
  "Every call recorded",
];

export function AuthShell({
  children,
  enterpriseSlot,
}: {
  children: ReactNode;
  enterpriseSlot?: ReactNode;
}) {
  return (
    <div className="grid min-h-screen w-full bg-background lg:grid-cols-[55%_45%]">
      {/* Form column (LEFT) — scrolls and stays centered so tall forms never
          clip. */}
      <main className="auth-imprint flex min-h-screen flex-col overflow-y-auto">
        <div className="flex min-h-full items-center justify-center p-6 sm:p-10">
          <div className="w-full max-w-md space-y-6 rounded-xl border border-border bg-card p-6 shadow-[0_1px_2px_rgba(16,24,40,0.04),0_16px_40px_-16px_rgba(16,24,40,0.14)] sm:p-8">
            {/* Mobile-only wordmark (brand panel is hidden) */}
            <div className="lg:hidden">
              <BrandLogo className="h-7" />
            </div>
            {children}
          </div>
        </div>
      </main>

      {/* Brand / value panel (RIGHT) — hidden on mobile */}
      <aside
        className="relative hidden flex-col justify-between overflow-hidden p-10 lg:flex xl:p-14"
        style={{ background: "var(--brand-panel-image)", backgroundColor: "var(--brand-panel)" }}
      >
        {/* Ambient depth: soft radial glow behind the content */}
        <div
          aria-hidden
          className="pointer-events-none absolute -right-24 top-1/3 size-[28rem] rounded-full opacity-20 blur-3xl"
          style={{ background: "radial-gradient(circle, var(--brand-panel-accent), transparent 70%)" }}
        />

        <div className="relative">
          <BrandLogo inverse className="h-9" />
        </div>

        <div className="relative max-w-md space-y-5">
          <h1 className="text-3xl font-semibold leading-tight tracking-tight text-zinc-50 xl:text-4xl">
            AI voice agents that answer, qualify and transfer your callers.
          </h1>
          <ul className="flex flex-wrap gap-2">
            {HIGHLIGHTS.map((point) => (
              <li
                key={point}
                className="rounded-full border border-white/10 bg-white/[0.04] px-3 py-1 text-xs font-medium text-zinc-300"
              >
                {point}
              </li>
            ))}
          </ul>
        </div>

        {/* Enterprise CTA block (Bland-style) — bottom margin lifts it off the
            viewport edge while justify-between keeps the column layout */}
        <div className="relative mb-12 max-w-md space-y-3 rounded-xl border border-white/10 bg-white/[0.03] p-5 xl:mb-16">
          <h2 className="text-sm font-semibold text-zinc-100">
            Need on-prem, data residency &amp; a data perimeter?
          </h2>
          <p className="text-sm text-zinc-400">
            We can deploy your voice agents inside your own environment for
            regulated and high-scale teams.
          </p>
          {enterpriseSlot}
        </div>
      </aside>
    </div>
  );
}
