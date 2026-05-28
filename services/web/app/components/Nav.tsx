"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTheme } from "next-themes";
import {
  BookOpen,
  Bot,
  FileText,
  Moon,
  Newspaper,
  PanelLeftClose,
  PanelLeftOpen,
  PenSquare,
  Rss,
  Sun,
  UploadCloud,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const links = [
  { href: "/briefing", label: "简报", icon: Newspaper, key: "B", match: ["/briefing"] },
  { href: "/knowledge", label: "知识库", icon: BookOpen, key: "K", match: ["/knowledge"] },
  { href: "/drafts", label: "工作室", icon: PenSquare, key: "S", match: ["/drafts", "/instructions", "/settings"] },
  { href: "/sources", label: "来源", icon: UploadCloud, key: "O", match: ["/sources"] },
  { href: "https://chat.laughtale.co.uk/", label: "LibreChat", icon: Bot, key: "C", match: [] },
  { href: "https://rss.laughtale.co.uk/wechat-admin/", label: "Wechat2RSS", icon: Rss, key: "W", match: [] },
];

const studioLinks = [
  { href: "/drafts", label: "草稿历史", activePath: "/drafts" },
  { href: "/instructions#templates", label: "写作模板", activePath: "/instructions" },
  { href: "/settings", label: "系统设置", activePath: "/settings" },
];

const sidebarShortcutLabel = "⌘\\";

export default function Nav() {
  const pathname = usePathname();
  const { theme, setTheme } = useTheme();
  const [collapsed, setCollapsed] = useState(false);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.altKey || event.shiftKey) return;
      if (!(event.metaKey || event.ctrlKey)) return;
      if (event.key !== "\\" && event.code !== "Backslash") return;

      event.preventDefault();
      setCollapsed((value) => !value);
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  return (
    <aside
      className={cn(
        "flex h-full w-14 shrink-0 flex-col border-r border-border bg-sidebar text-sidebar-foreground transition-[width] duration-200",
        collapsed ? "md:w-20" : "md:w-64"
      )}
    >
      <div
        className={cn(
          "flex shrink-0 border-b border-sidebar-border px-2 md:px-3",
          collapsed
            ? "h-36 flex-col items-center justify-start gap-5 pt-6"
            : "h-16 items-center justify-center gap-2.5 md:justify-start"
        )}
      >
        <Link href="/" className="grid h-10 w-10 shrink-0 place-items-center" aria-label="返回主页">
          <img src="/logo.svg" alt="Swanny" className="h-9 w-9 dark:invert" />
        </Link>
        <div className={cn("hidden min-w-0 flex-1 md:block", collapsed && "md:hidden")}>
          <div className="truncate text-[15px] font-semibold leading-tight tracking-tight">Swanny</div>
          <div className="truncate text-[11px] leading-tight text-muted-foreground">工作台</div>
        </div>
        <div className={cn("hidden items-center gap-1 md:flex", collapsed && "mt-1")}>
          {!collapsed && (
            <kbd className="rounded border border-border bg-background px-1 text-[10px] text-muted-foreground">
              {sidebarShortcutLabel}
            </kbd>
          )}
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7 text-muted-foreground"
            onClick={() => setCollapsed((value) => !value)}
            aria-label={collapsed ? "展开侧栏" : "折叠侧栏"}
            title={collapsed ? `展开侧栏 (${sidebarShortcutLabel})` : `折叠侧栏 (${sidebarShortcutLabel})`}
          >
            {collapsed ? <PanelLeftOpen className="h-4 w-4" /> : <PanelLeftClose className="h-4 w-4" />}
          </Button>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-auto px-2 py-1 md:px-3">
        <div
          className={cn(
            "hidden px-2 pb-1.5 pt-3 text-[10px] font-medium uppercase tracking-wider text-muted-foreground md:block",
            collapsed && "md:hidden"
          )}
        >
          工作区
        </div>
        <nav className="space-y-0.5">
          {links.map(({ href, label, icon: Icon, key, match }) => {
            const active = match.some((prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`));
            const isExternal = href.startsWith("http");
            return (
              <div key={href}>
                <Button
                  variant="ghost"
                  size="sm"
                  asChild
                  className={cn(
                    "h-9 w-full justify-center gap-2 rounded-md px-0 text-[13px] font-medium md:h-8 md:px-2",
                    collapsed ? "md:justify-center" : "md:justify-start",
                    active
                      ? "bg-sidebar-accent text-sidebar-accent-foreground shadow-sm"
                      : "text-muted-foreground hover:bg-accent hover:text-foreground"
                  )}
                >
                  {isExternal ? (
                    <a href={href} target="_blank" rel="noreferrer">
                      <Icon className="h-3.5 w-3.5" />
                      <span className={cn("hidden flex-1 text-left md:block", collapsed && "md:hidden")}>{label}</span>
                      <kbd
                        className={cn(
                          "hidden rounded border border-border/80 bg-background/80 px-1 text-[10px] font-medium text-muted-foreground md:inline-flex",
                          collapsed && "md:hidden"
                        )}
                      >
                        {key}
                      </kbd>
                    </a>
                  ) : (
                  <Link href={href}>
                    <Icon className="h-3.5 w-3.5" />
                    <span className={cn("hidden flex-1 text-left md:block", collapsed && "md:hidden")}>{label}</span>
                    <kbd
                      className={cn(
                        "hidden rounded border border-border/80 bg-background/80 px-1 text-[10px] font-medium text-muted-foreground md:inline-flex",
                        collapsed && "md:hidden"
                      )}
                    >
                      {key}
                    </kbd>
                  </Link>
                  )}
                </Button>
                {label === "工作室" && active && !collapsed && (
                  <div className="ml-5 mt-1 hidden space-y-0.5 border-l border-sidebar-border pl-2 md:block">
                    {studioLinks.map((item) => {
                      const itemActive = item.activePath
                        ? pathname === item.activePath || pathname.startsWith(`${item.activePath}/`)
                        : false;
                      return (
                        <Link
                          key={item.href}
                          href={item.href}
                          className={cn(
                            "flex h-7 items-center gap-2 rounded-md px-2 text-[12px] transition-colors",
                            itemActive
                              ? "bg-accent text-foreground"
                              : "text-muted-foreground hover:bg-accent hover:text-foreground"
                          )}
                        >
                          <span className="flex-1 truncate">{item.label}</span>
                        </Link>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
        </nav>

        <div
          className={cn(
            "hidden px-2 pb-1.5 pt-5 text-[10px] font-medium uppercase tracking-wider text-muted-foreground md:block",
            collapsed && "md:hidden"
          )}
        >
          快捷入口
        </div>
        <nav className="mt-3 space-y-0.5 md:mt-0">
          <Link
            href="/knowledge"
            className={cn(
              "flex h-9 items-center justify-center gap-2 rounded-md px-0 text-[12px] text-muted-foreground transition-colors hover:bg-accent hover:text-foreground md:h-7 md:px-2",
              collapsed ? "md:justify-center" : "md:justify-start"
            )}
            aria-label="Wiki 与图谱"
          >
            <FileText className="h-3 w-3" />
            <span className={cn("hidden flex-1 truncate md:block", collapsed && "md:hidden")}>Wiki 与图谱</span>
          </Link>
        </nav>
      </div>

      <div
        className={cn(
          "flex items-center justify-center gap-1 border-t border-sidebar-border p-2",
          collapsed ? "md:flex-col" : "md:justify-start"
        )}
      >
        <div className={cn("hidden h-7 w-7 shrink-0 place-items-center rounded-full bg-muted text-[10px] font-medium md:grid", collapsed && "md:hidden")}>
          KB
        </div>
        <div className={cn("hidden min-w-0 flex-1 text-[12px] leading-tight md:block", collapsed && "md:hidden")}>
          <div className="truncate font-medium">KnowledgeBase-S</div>
          <div className="truncate text-[10px] text-muted-foreground">local workspace</div>
        </div>
        <Button
          variant="ghost"
          size="icon"
          className="relative h-7 w-7"
          onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
          aria-label="切换深色模式"
        >
          <Sun className="h-3.5 w-3.5 rotate-0 scale-100 transition-all dark:-rotate-90 dark:scale-0" />
          <Moon className="absolute h-3.5 w-3.5 rotate-90 scale-0 transition-all dark:rotate-0 dark:scale-100" />
        </Button>
      </div>
    </aside>
  );
}
