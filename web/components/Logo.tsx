import PageLink from "@/components/PageLink";
import { BRAND } from "@/lib/site";

export function LogoMark({ size = 28 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" fill="none" aria-hidden="true">
      <rect width="32" height="32" rx="8" fill="currentColor" />
      <rect x="8" y="16" width="4" height="8" rx="1" fill="var(--logo-fg, #fff)" />
      <rect x="14" y="11" width="4" height="13" rx="1" fill="var(--logo-fg, #fff)" />
      <rect x="20" y="7" width="4" height="17" rx="1" fill="var(--logo-fg, #fff)" />
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
