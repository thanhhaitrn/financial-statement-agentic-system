"use client";

import { ReactNode, useEffect } from "react";
import { endLeave } from "@/lib/transition";

export default function Template({ children }: { children: ReactNode }) {
  useEffect(() => {
    endLeave();
  }, []);
  return <div className="page-enter">{children}</div>;
}
