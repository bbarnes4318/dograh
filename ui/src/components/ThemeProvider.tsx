"use client";

// Thin wrapper around next-themes so the root (server) layout can mount a theme
// provider without pulling client-only code into the server module graph. The
// theme is forced light in app/layout.tsx to match the portal this app is
// embedded in; the system preference is intentionally not consulted.

import { ThemeProvider as NextThemesProvider } from "next-themes";
import type { ComponentProps } from "react";

export function ThemeProvider({ children, ...props }: ComponentProps<typeof NextThemesProvider>) {
  return <NextThemesProvider {...props}>{children}</NextThemesProvider>;
}
