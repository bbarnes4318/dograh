import { BRAND_KEYS, BRANDS } from "@/lib/brand";
import { cn } from "@/lib/utils";

// The portal's logo -- NetEnroll, or the white-label agency's (lib/brand.ts).
// Every brand's artwork is rendered and CSS shows the active one
// (`[data-brand-only]` in globals.css), so a server-rendered page never flashes
// the wrong brand before hydration.
//
//   default   the horizontal wordmark, for light grounds
//   inverse   the wordmark for a dark ground (the sign-in brand panel)
//   mark      the square icon (collapsed sidebar)
//
// Height is set by the caller via className (e.g. "h-7"); width stays auto so
// each lockup keeps its aspect ratio.
export function BrandLogo({
  className,
  inverse = false,
  mark = false,
}: {
  className?: string;
  inverse?: boolean;
  mark?: boolean;
}) {
  return (
    <>
      {BRAND_KEYS.map((key) => {
        const brand = BRANDS[key];
        const src = mark ? brand.mark : inverse ? brand.wordmarkOnDark : brand.wordmark;
        // NetEnroll has no reversed lockup ("net" is black), so on a dark
        // ground it sits on a white plate rather than disappearing.
        if (inverse && !mark && key === "netenroll") {
          return (
            <span key={key} data-brand-only={key} className="items-center rounded-md bg-white px-3 py-2">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={src} alt={brand.name} draggable={false} className={cn("w-auto select-none", className)} />
            </span>
          );
        }
        return (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            key={key}
            src={src}
            alt={brand.name}
            draggable={false}
            data-brand-only={key}
            className={cn("w-auto select-none", className)}
          />
        );
      })}
    </>
  );
}
