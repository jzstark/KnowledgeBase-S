"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

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

  if (pathname === "/login") return null;

  return (
    <nav className="bg-white border-b border-gray-200 px-6 py-2.5 flex items-center gap-4">
      {links.map(({ href, label }) => {
        const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
        return (
          <Link
            key={href}
            href={href}
            className={`text-sm transition-colors ${
              active
                ? "text-gray-900 font-medium"
                : "text-gray-400 hover:text-gray-700"
            }`}
          >
            {label}
          </Link>
        );
      })}
    </nav>
  );
}
