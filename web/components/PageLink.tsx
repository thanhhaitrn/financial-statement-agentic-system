"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { ComponentProps, MouseEvent } from "react";
import { go } from "@/lib/transition";

export default function PageLink({ onClick, href, ...rest }: ComponentProps<typeof Link>) {
  const router = useRouter();

  function handle(e: MouseEvent<HTMLAnchorElement>) {
    onClick?.(e);
    if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
    if (typeof href !== "string" || !href.startsWith("/")) return;
    e.preventDefault();
    go(router, href);
  }

  return <Link href={href} onClick={handle} {...rest} />;
}
