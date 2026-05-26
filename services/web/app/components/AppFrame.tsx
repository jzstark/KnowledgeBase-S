"use client";

import { usePathname } from "next/navigation";
import Nav from "./Nav";

export default function AppFrame({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const barePage = pathname === "/" || pathname === "/login";

  if (barePage) return <>{children}</>;

  return (
    <div className="h-screen overflow-hidden bg-background text-foreground">
      <div className="flex h-full min-h-0">
        <Nav />
        <main className="min-w-0 flex-1 overflow-auto">{children}</main>
      </div>
    </div>
  );
}
