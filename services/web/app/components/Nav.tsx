"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTheme } from "next-themes";
import { Sun, Moon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const links = [
  { href: "/", label: "简报" },
  { href: "/knowledge", label: "知识库" },
  { href: "/drafts", label: "草稿" },
  { href: "/sources", label: "来源" },
  { href: "/instructions", label: "指令设置" },
  { href: "/settings", label: "设置" },
];

export default function Nav() {
  const pathname = usePathname();
  const { theme, setTheme } = useTheme();

  if (pathname === "/login") return null;

  return (
    <nav className="bg-background/90 backdrop-blur-sm border-b border-border px-6 py-2.5 flex items-center gap-1 sticky top-0 z-30">
      <Link href="/" className="shrink-0 mr-3">
        <img src="/logo.svg" alt="logo" className="h-6 w-6" />
      </Link>
      {links.map(({ href, label }) => {
        const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
        return (
          <Button
            key={href}
            variant="ghost"
            size="sm"
            asChild
            className={cn(
              "text-sm h-8 px-3",
              active
                ? "text-foreground font-medium bg-accent"
                : "text-muted-foreground"
            )}
          >
            <Link href={href}>{label}</Link>
          </Button>
        );
      })}
      <div className="ml-auto">
        <Button
          variant="ghost"
          size="icon"
          className="h-8 w-8 relative"
          onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
          aria-label="切换深色模式"
        >
          <Sun className="h-4 w-4 rotate-0 scale-100 transition-all dark:-rotate-90 dark:scale-0" />
          <Moon className="absolute h-4 w-4 rotate-90 scale-0 transition-all dark:rotate-0 dark:scale-100" />
        </Button>
      </div>
    </nav>
  );
}
