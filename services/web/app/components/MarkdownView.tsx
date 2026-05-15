"use client";

import Link from "next/link";
import { type ReactNode } from "react";
import { cn } from "@/lib/utils";

function isSafeHref(href: string) {
  return (href.startsWith("/") && !href.startsWith("//"))
    || href.startsWith("https://")
    || href.startsWith("http://")
    || href.startsWith("mailto:");
}

const INLINE_TOKEN_RE = /(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*|\[[^\]]+\]\([^)]+\)|\[\[[^\]]+\]\])/g;

function knowledgeHref(nodeId: string) {
  return `/knowledge#node=${encodeURIComponent(nodeId)}`;
}

function InlineMarkdown({ text }: { text: string }) {
  const parts = text.split(INLINE_TOKEN_RE);
  return (
    <>
      {parts.map((part, idx) => {
        if (!part) return null;
        if (part.startsWith("`") && part.endsWith("`")) {
          return (
            <code key={idx} className="rounded bg-muted px-1 py-0.5 font-mono text-[0.92em]">
              {part.slice(1, -1)}
            </code>
          );
        }
        if (part.startsWith("**") && part.endsWith("**")) {
          return <strong key={idx} className="font-semibold">{part.slice(2, -2)}</strong>;
        }
        if (part.startsWith("*") && part.endsWith("*")) {
          return <em key={idx}>{part.slice(1, -1)}</em>;
        }
        const wiki = part.match(/^\[\[([^|\]]+)(?:\|([^\]]+))?\]\]$/);
        if (wiki) {
          const nodeId = wiki[1].trim();
          const label = (wiki[2] || wiki[1]).trim();
          return (
            <Link key={idx} href={knowledgeHref(nodeId)} className="font-medium text-primary underline underline-offset-2">
              {label}
            </Link>
          );
        }
        const link = part.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
        if (link) {
          const [, label, href] = link;
          if (!isSafeHref(href)) return <span key={idx}>{label}</span>;
          if (href.startsWith("/")) {
            return (
              <Link key={idx} href={href} className="font-medium text-primary underline underline-offset-2">
                {label}
              </Link>
            );
          }
          return (
            <a key={idx} href={href} target="_blank" rel="noreferrer" className="font-medium text-primary underline underline-offset-2">
              {label}
            </a>
          );
        }
        return <span key={idx}>{part}</span>;
      })}
    </>
  );
}

function parseTableRow(line: string) {
  const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  return trimmed.split("|").map((cell) => cell.trim());
}

function isTableDivider(line: string) {
  return /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line);
}

function MarkdownTable({ lines }: { lines: string[] }) {
  const headers = parseTableRow(lines[0]);
  const rows = lines.slice(2).map(parseTableRow);
  return (
    <div className="overflow-x-auto rounded-md border border-border">
      <table className="w-full border-collapse text-left text-sm">
        <thead className="bg-muted/60">
          <tr>
            {headers.map((cell, idx) => (
              <th key={idx} className="border-b border-border px-3 py-2 font-semibold">
                <InlineMarkdown text={cell} />
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, rowIdx) => (
            <tr key={rowIdx} className="border-t border-border/60">
              {headers.map((_, cellIdx) => (
                <td key={cellIdx} className="px-3 py-2 align-top">
                  <InlineMarkdown text={row[cellIdx] ?? ""} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function MarkdownView({ content, className }: { content: string; className?: string }) {
  const blocks = content.split(/(```[\s\S]*?```)/g);

  return (
    <div className={cn("space-y-3 text-sm leading-7 text-foreground", className)}>
      {blocks.map((block, blockIndex) => {
        if (!block) return null;
        if (block.startsWith("```") && block.endsWith("```")) {
          const code = block.replace(/^```[^\n]*\n?/, "").replace(/```$/, "");
          return (
            <pre key={blockIndex} className="overflow-x-auto rounded-md bg-muted p-3 text-xs leading-relaxed">
              <code>{code}</code>
            </pre>
          );
        }

        const lines = block.split("\n");
        const nodes: ReactNode[] = [];
        let listItems: { text: string; ordered: boolean }[] = [];

        function flushList(key: string) {
          if (listItems.length === 0) return;
          const ordered = listItems[0].ordered;
          const ListTag = ordered ? "ol" : "ul";
          nodes.push(
            <ListTag key={key} className={cn("ml-5 space-y-1", ordered ? "list-decimal" : "list-disc")}>
              {listItems.map((item, idx) => (
                <li key={idx}>
                  <InlineMarkdown text={item.text} />
                </li>
              ))}
            </ListTag>
          );
          listItems = [];
        }

        for (let idx = 0; idx < lines.length; idx += 1) {
          const line = lines[idx];
          const trimmed = line.trim();
          if (!trimmed) {
            flushList(`list-${blockIndex}-${idx}`);
            continue;
          }

          if (trimmed.includes("|") && idx + 1 < lines.length && isTableDivider(lines[idx + 1])) {
            flushList(`list-${blockIndex}-${idx}`);
            const tableLines = [line, lines[idx + 1]];
            idx += 2;
            while (idx < lines.length && lines[idx].trim().includes("|")) {
              tableLines.push(lines[idx]);
              idx += 1;
            }
            idx -= 1;
            nodes.push(<MarkdownTable key={`table-${blockIndex}-${idx}`} lines={tableLines} />);
            continue;
          }

          const listMatch = trimmed.match(/^[-*]\s+(.+)$/);
          if (listMatch) {
            if (listItems.some((item) => item.ordered)) flushList(`list-${blockIndex}-${idx}`);
            listItems.push({ text: listMatch[1], ordered: false });
            continue;
          }

          const orderedListMatch = trimmed.match(/^\d+\.\s+(.+)$/);
          if (orderedListMatch) {
            if (listItems.some((item) => !item.ordered)) flushList(`list-${blockIndex}-${idx}`);
            listItems.push({ text: orderedListMatch[1], ordered: true });
            continue;
          }

          flushList(`list-${blockIndex}-${idx}`);

          if (/^---+$/.test(trimmed)) {
            nodes.push(<hr key={`hr-${blockIndex}-${idx}`} className="border-border" />);
            continue;
          }

          const quote = trimmed.match(/^>\s*(.+)$/);
          if (quote) {
            nodes.push(
              <blockquote key={`quote-${blockIndex}-${idx}`} className="border-l-2 border-border pl-3 text-muted-foreground">
                <InlineMarkdown text={quote[1]} />
              </blockquote>
            );
            continue;
          }

          const heading = trimmed.match(/^(#{1,4})\s+(.+)$/);
          if (heading) {
            const level = heading[1].length;
            const HeadingTag = level === 1 ? "h2" : level === 2 ? "h3" : "h4";
            nodes.push(
              <HeadingTag key={`heading-${blockIndex}-${idx}`} className={cn(
                "font-semibold leading-snug text-foreground",
                level === 1 ? "text-lg" : level === 2 ? "text-base" : "text-sm"
              )}>
                <InlineMarkdown text={heading[2]} />
              </HeadingTag>
            );
            continue;
          }

          nodes.push(
            <p key={`p-${blockIndex}-${idx}`}>
              <InlineMarkdown text={trimmed} />
            </p>
          );
        }
        flushList(`list-${blockIndex}-end`);

        return <div key={blockIndex} className="space-y-3">{nodes}</div>;
      })}
    </div>
  );
}
