import PageLink from "@/components/PageLink";
import { BRAND } from "@/lib/site";

// The AgentFinX mark: a hexagon holding an X of four blocks around a square.
// Drawn around its centre; the hexagon takes the text colour, the X
// --logo-fg, so the mark follows the surface it sits on.
const HEXAGON = "M0 -145L125.6 -72.5V72.5L0 145L-125.6 72.5V-72.5Z";
const CROSS = "M-83 -83H-37.5V-54.5L-16 -33H16L37.5 -54.5V-83H83V-37.5H54.5L33 -16V16L54.5 37.5H83V83H37.5V54.5L16 33H-16L-37.5 54.5V83H-83V37.5H-54.5L-33 16V-16L-54.5 -37.5H-83ZM-17 -17V17H17V-17Z";

export function LogoMark({ size = 28 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="-145 -145 290 290" fill="none" aria-hidden="true">
      <path d={HEXAGON} fill="currentColor" />
      <path d={CROSS} fill="var(--logo-fg, #fff)" fillRule="evenodd" />
    </svg>
  );
}

export function Logo() {
  return (
    <PageLink href="/" className="logo" aria-label={BRAND}>
      <LogoMark />
      <span>{BRAND}</span>
    </PageLink>
  );
}
