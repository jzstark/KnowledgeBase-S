"use client";

import { useEffect, useRef, useState, useCallback, type ReactNode } from "react";
import Link from "next/link";
import { ArrowUp, BookOpen, PenSquare, Search, Trash2, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { cn } from "@/lib/utils";
import { useChatContext } from "./ChatContext";

interface Session {
  id: string;
  title: string | null;
  updated_at: string;
}

interface Message {
  id?: number;
  role: "user" | "assistant";
  content: string;
  toolEvents?: ToolEvent[];
  references?: ToolReference[];
}

interface ToolReference {
  id: string;
  title?: string | null;
  object_type?: string | null;
  source_type?: string | null;
  score?: number | null;
}

interface ToolEvent {
  name: string;
  input?: Record<string, unknown>;
  result?: {
    results?: unknown[];
    node?: unknown;
    nodes?: unknown[];
    sources?: unknown[];
  };
}

function isSafeMarkdownHref(href: string) {
  return (href.startsWith("/") && !href.startsWith("//"))
    || href.startsWith("https://")
    || href.startsWith("http://")
    || href.startsWith("mailto:");
}

const KB_REF_PATTERN = String.raw`\[\s*(?:(?:art|ent|sum|idx)_[A-Za-z0-9_-]+)\s*\]`;
const INLINE_TOKEN_RE = new RegExp(
  `(\`[^\`]+\`|\\*\\*[^*]+\\*\\*|\\[[^\\]]+\\]\\([^)]+\\)|${KB_REF_PATTERN})`,
  "g",
);

function knowledgeNodeHref(nodeId: string) {
  return `/knowledge#node=${encodeURIComponent(nodeId)}`;
}

function findKnowledgeRefs(text: string) {
  return Array.from(
    text.matchAll(/\[\s*((?:art|ent|sum|idx)_[A-Za-z0-9_-]+)\s*\](?:\s*["“]([^"”]+)["”])?/g)
  );
}

function isCitationOnlyLine(text: string) {
  const withoutRefs = text
    .replace(/\[\s*(?:art|ent|sum|idx)_[A-Za-z0-9_-]+\s*\](?:\s*["“][^"”]+["”])?/g, "")
    .replace(/[,\s，、"“”]/g, "");
  return findKnowledgeRefs(text).length > 0 && withoutRefs === "";
}

function KnowledgeNodeLink({
  nodeId,
  children,
  className,
  title,
}: {
  nodeId: string;
  children: ReactNode;
  className?: string;
  title?: string;
}) {
  return (
    <a
      href={knowledgeNodeHref(nodeId)}
      className={className}
      title={title}
      onClick={() => {
        if (window.location.pathname !== "/knowledge") return;
        window.setTimeout(() => {
          window.dispatchEvent(new HashChangeEvent("hashchange"));
        }, 0);
      }}
    >
      {children}
    </a>
  );
}

function InlineMarkdown({ text }: { text: string }) {
  const parts = text.split(INLINE_TOKEN_RE);
  return (
    <>
      {parts.map((part, idx) => {
        if (!part) return null;
        if (part.startsWith("`") && part.endsWith("`")) {
          return (
            <code key={idx} className="rounded bg-background/70 px-1 py-0.5 font-mono text-[0.92em]">
              {part.slice(1, -1)}
            </code>
          );
        }
        if (part.startsWith("**") && part.endsWith("**")) {
          return <strong key={idx} className="font-semibold">{part.slice(2, -2)}</strong>;
        }
        const link = part.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
        if (link) {
          const [, label, href] = link;
          if (!isSafeMarkdownHref(href)) return <span key={idx}>{label}</span>;
          const isInternal = href.startsWith("/");
          if (isInternal) {
            return (
              <Link key={idx} href={href} className="font-medium text-primary underline underline-offset-2">
                {label}
              </Link>
            );
          }
          return (
            <a
              key={idx}
              href={href}
              target="_blank"
              rel="noreferrer"
              className="font-medium text-primary underline underline-offset-2"
            >
              {label}
            </a>
          );
        }
        const kbRef = part.match(/^\[\s*((?:art|ent|sum|idx)_[A-Za-z0-9_-]+)\s*\]$/);
        if (kbRef) {
          const nodeId = kbRef[1];
          return (
            <KnowledgeNodeLink
              key={idx}
              nodeId={nodeId}
              className="font-mono text-primary underline underline-offset-2"
            >
              [{nodeId}]
            </KnowledgeNodeLink>
          );
        }
        return <span key={idx}>{part}</span>;
      })}
    </>
  );
}

function SourceCitationLine({ text }: { text: string }) {
  const refs = findKnowledgeRefs(text);

  if (refs.length === 0) {
    return (
      <p className="text-xs text-muted-foreground">
        <InlineMarkdown text={text} />
      </p>
    );
  }

  return (
    <div className="flex flex-wrap items-center gap-1.5 rounded border border-border bg-background/70 px-2 py-1 text-[11px] text-muted-foreground">
      <BookOpen className="h-3 w-3 shrink-0" />
      <span>来源</span>
      {refs.map((ref, idx) => {
        const nodeId = ref[1];
        const title = ref[2];
        return (
          <KnowledgeNodeLink
            key={`${nodeId}-${idx}`}
            nodeId={nodeId}
            className="max-w-full truncate rounded bg-muted px-1.5 py-0.5 font-medium text-foreground hover:text-primary"
            title={title ? `${title} · ${nodeId}` : nodeId}
          >
            {title || nodeId}
          </KnowledgeNodeLink>
        );
      })}
    </div>
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
    <div className="overflow-x-auto rounded-md border border-border bg-background/70">
      <table className="w-full border-collapse text-left text-xs">
        <thead className="bg-muted">
          <tr>
            {headers.map((cell, idx) => (
              <th key={idx} className="border-b border-border px-2 py-1.5 font-semibold">
                <InlineMarkdown text={cell} />
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, rowIdx) => (
            <tr key={rowIdx} className="border-t border-border/60">
              {headers.map((_, cellIdx) => (
                <td key={cellIdx} className="px-2 py-1.5 align-top">
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

function MarkdownMessage({ content }: { content: string }) {
  const blocks = content.split(/(```[\s\S]*?```)/g);

  return (
    <div className="space-y-2">
      {blocks.map((block, blockIndex) => {
        if (!block) return null;
        if (block.startsWith("```") && block.endsWith("```")) {
          const code = block.replace(/^```[^\n]*\n?/, "").replace(/```$/, "");
          return (
            <pre
              key={blockIndex}
              className="overflow-x-auto rounded-md bg-background/80 p-2 text-xs leading-relaxed"
            >
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
            <ListTag key={key} className={cn("ml-4 space-y-1", ordered ? "list-decimal" : "list-disc")}>
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

          if (
            trimmed.includes("|")
            && idx + 1 < lines.length
            && isTableDivider(lines[idx + 1])
          ) {
            flushList(`list-${blockIndex}-${idx}`);
            const tableLines = [line, lines[idx + 1]];
            idx += 2;
            while (idx < lines.length && lines[idx].trim().includes("|")) {
              tableLines.push(lines[idx]);
              idx += 1;
            }
            idx -= 1;
            nodes.push(
              <MarkdownTable key={`table-${blockIndex}-${idx}`} lines={tableLines} />
            );
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
          const sourceLine = trimmed.match(/^\*?\(?\s*(来源|Sources)\s*[:：]\s*(.+?)\s*\)?\*?$/i);
          if (sourceLine) {
            nodes.push(
              <SourceCitationLine key={`source-${blockIndex}-${idx}`} text={sourceLine[2]} />
            );
            continue;
          }

          if (isCitationOnlyLine(trimmed)) {
            nodes.push(
              <SourceCitationLine key={`citation-${blockIndex}-${idx}`} text={trimmed} />
            );
            continue;
          }

          const heading = trimmed.match(/^(#{1,3})\s+(.+)$/);
          if (heading) {
            const HeadingTag = heading[1].length === 1 ? "h3" : "h4";
            nodes.push(
              <HeadingTag key={`heading-${blockIndex}-${idx}`} className="font-semibold leading-snug">
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

        return <div key={blockIndex} className="space-y-2">{nodes}</div>;
      })}
    </div>
  );
}

export default function ChatSidebar() {
  const { isOpen, toggleChat, width, setWidth, activeSessionId, setActiveSessionId } = useChatContext();

  const [sessions, setSessions] = useState<Session[]>([]);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);

  const bottomRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const resizeRef = useRef<{ startX: number; startWidth: number } | null>(null);

  const scrollToBottom = useCallback(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, []);

  // ── 会话列表 ──────────────────────────────────────────────────────────────

  const loadSessions = useCallback(async () => {
    try {
      const res = await fetch("/api/chat/sessions");
      if (!res.ok) return;
      const data: Session[] = await res.json();
      setSessions(data);
      return data;
    } catch {
      return undefined;
    }
  }, []);

  const createSession = useCallback(async () => {
    try {
      const res = await fetch("/api/chat/sessions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}) });
      if (!res.ok) return null;
      const data = await res.json();
      await loadSessions();
      return data.id as string;
    } catch {
      return null;
    }
  }, [loadSessions]);

  // ── 消息列表 ──────────────────────────────────────────────────────────────

  const loadMessages = useCallback(async (sid: string) => {
    try {
      const res = await fetch(`/api/chat/sessions/${sid}/messages`);
      if (!res.ok) return;
      const data: Message[] = await res.json();
      setMessages(data);
    } catch {}
  }, []);

  // ── 初始化 ────────────────────────────────────────────────────────────────

  useEffect(() => {
    if (!isOpen) return;
    (async () => {
      const data = await loadSessions();
      if (!data) return;

      if (activeSessionId) {
        const exists = data.some((s) => s.id === activeSessionId);
        if (exists) {
          await loadMessages(activeSessionId);
          return;
        }
      }
      // 没有有效会话：用现有第一个或新建
      if (data.length > 0) {
        setActiveSessionId(data[0].id);
        await loadMessages(data[0].id);
      } else {
        const newId = await createSession();
        if (newId) {
          setActiveSessionId(newId);
          setMessages([]);
        }
      }
    })();
  }, [isOpen]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    scrollToBottom();
  }, [messages, scrollToBottom]);

  // ── 切换会话 ──────────────────────────────────────────────────────────────

  const switchSession = useCallback(async (sid: string) => {
    setActiveSessionId(sid);
    setMessages([]);
    await loadMessages(sid);
  }, [setActiveSessionId, loadMessages]);

  // ── 新建会话 ──────────────────────────────────────────────────────────────

  const handleNewChat = useCallback(async () => {
    const newId = await createSession();
    if (newId) {
      setActiveSessionId(newId);
      setMessages([]);
    }
  }, [createSession, setActiveSessionId]);

  // ── 删除当前会话 ──────────────────────────────────────────────────────────

  const handleDeleteSession = useCallback(async () => {
    if (!activeSessionId) return;
    await fetch(`/api/chat/sessions/${activeSessionId}`, { method: "DELETE" });
    const data = await loadSessions();
    if (data && data.length > 0) {
      setActiveSessionId(data[0].id);
      await loadMessages(data[0].id);
    } else {
      const newId = await createSession();
      if (newId) {
        setActiveSessionId(newId);
        setMessages([]);
      }
    }
  }, [activeSessionId, loadSessions, loadMessages, createSession, setActiveSessionId]);

  // ── 发送消息 ──────────────────────────────────────────────────────────────

  const sendMessage = useCallback(async () => {
    const text = input.trim();
    if (!text || isStreaming || !activeSessionId) return;

    setInput("");
    setIsStreaming(true);
    setMessages((prev) => [
      ...prev,
      { role: "user", content: text },
      { role: "assistant", content: "" },
    ]);

    try {
      const res = await fetch(`/api/chat/sessions/${activeSessionId}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: text }),
      });

      if (!res.ok || !res.body) throw new Error("请求失败");

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";
        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          const payload = line.slice(6).trim();
          if (payload === "[DONE]") break;
          try {
            const { delta, tool_result, references } = JSON.parse(payload);
            setMessages((prev) => {
              const next = [...prev];
              const current = next[next.length - 1];
              const nextRefs = references ? mergeReferences(current.references ?? [], references) : current.references;
              const nextToolEvents = tool_result
                ? [...(current.toolEvents ?? []), tool_result]
                : current.toolEvents;
              next[next.length - 1] = {
                ...current,
                content: current.content + (delta ?? ""),
                toolEvents: nextToolEvents,
                references: nextRefs,
              };
              return next;
            });
          } catch {}
        }
      }
    } catch (e) {
      setMessages((prev) => {
        const next = [...prev];
        next[next.length - 1] = { ...next[next.length - 1], content: "（请求出错，请重试）" };
        return next;
      });
    } finally {
      setIsStreaming(false);
      await loadSessions();
      scrollToBottom();
    }
  }, [input, isStreaming, activeSessionId, loadSessions, scrollToBottom]);

  function mergeReferences(existing: ToolReference[], incoming: ToolReference[]) {
    const seen = new Set(existing.map((r) => r.id));
    const merged = [...existing];
    for (const ref of incoming) {
      if (!ref.id || seen.has(ref.id)) continue;
      merged.push(ref);
      seen.add(ref.id);
    }
    return merged.slice(0, 12);
  }

  function toolLabel(event: ToolEvent) {
    if (event.name === "kb_search") {
      const count = event.result?.results?.length ?? 0;
      return `搜索知识库 · ${count}`;
    }
    if (event.name === "kb_get_node") return "打开节点详情";
    if (event.name === "kb_get_neighbors") {
      const count = event.result?.nodes?.length ?? 0;
      return `查看邻居 · ${count}`;
    }
    if (event.name === "kb_get_sources") {
      const count = event.result?.sources?.length ?? 0;
      return `查看来源 · ${count}`;
    }
    return event.name;
  }

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
      }
    },
    [sendMessage]
  );

  const startResize = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      resizeRef.current = { startX: e.clientX, startWidth: width };
      e.currentTarget.setPointerCapture(e.pointerId);
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
    },
    [width]
  );

  const handleResize = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      const current = resizeRef.current;
      if (!current) return;
      setWidth(current.startWidth + current.startX - e.clientX);
    },
    [setWidth]
  );

  const stopResize = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    if (!resizeRef.current) return;
    resizeRef.current = null;
    e.currentTarget.releasePointerCapture(e.pointerId);
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
  }, []);

  // ── 渲染 ──────────────────────────────────────────────────────────────────

  if (!isOpen) return null;

  return (
    <div
      className="relative flex h-full shrink-0 flex-col border-l border-border bg-background"
      style={{ width: `min(${width}px, calc(100vw - 56px))` }}
    >
      <div
        className="absolute left-0 top-0 z-10 h-full w-2 -translate-x-1 cursor-col-resize touch-none"
        onPointerDown={startResize}
        onPointerMove={handleResize}
        onPointerUp={stopResize}
        onPointerCancel={stopResize}
        aria-label="调整对话栏宽度"
        role="separator"
      />

      {/* Header */}
      <div className="h-14 shrink-0 flex items-center gap-2 px-3 border-b border-border">
        <Select value={activeSessionId ?? ""} onValueChange={switchSession}>
          <SelectTrigger className="h-8 flex-1 text-xs">
            <SelectValue placeholder="选择会话" />
          </SelectTrigger>
          <SelectContent>
            {sessions.map((s) => (
              <SelectItem key={s.id} value={s.id} className="text-xs">
                {s.title || "新对话"}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <Button
          variant="ghost"
          size="icon"
          className="h-8 w-8 shrink-0"
          onClick={handleNewChat}
          aria-label="新建对话"
          title="新建对话"
        >
          <PenSquare className="h-4 w-4" />
        </Button>

        <Button
          variant="ghost"
          size="icon"
          className="h-8 w-8 shrink-0 text-muted-foreground hover:text-destructive"
          onClick={handleDeleteSession}
          aria-label="删除当前对话"
          title="删除当前对话"
          disabled={!activeSessionId}
        >
          <Trash2 className="h-4 w-4" />
        </Button>

        <Button
          variant="ghost"
          size="icon"
          className="h-8 w-8 shrink-0"
          onClick={toggleChat}
          aria-label="关闭对话"
        >
          <X className="h-4 w-4" />
        </Button>
      </div>

      {/* Messages */}
      <ScrollArea className="flex-1 px-3 py-3">
        <div className="flex flex-col gap-3">
          {messages.length === 0 && (
            <p className="text-xs text-muted-foreground text-center mt-8">
              开始一段新对话
            </p>
          )}
          {messages.map((msg, i) => (
            <div
              key={i}
              className={cn(
                "max-w-[90%] rounded-lg px-3 py-2 text-sm leading-relaxed break-words",
                msg.role === "user"
                  ? "self-end whitespace-pre-wrap bg-primary text-primary-foreground"
                  : "self-start bg-muted text-foreground"
              )}
            >
              {msg.role === "assistant" ? <MarkdownMessage content={msg.content} /> : msg.content}
              {msg.role === "assistant" && msg.toolEvents && msg.toolEvents.length > 0 && (
                <div className="mt-2 flex flex-wrap gap-1.5 whitespace-normal">
                  {msg.toolEvents.map((event, idx) => (
                    <span
                      key={`${event.name}-${idx}`}
                      className="inline-flex items-center gap-1 rounded border border-border bg-background px-1.5 py-0.5 text-[11px] text-muted-foreground"
                    >
                      <Search className="h-3 w-3" />
                      {toolLabel(event)}
                    </span>
                  ))}
                </div>
              )}
              {msg.role === "assistant" && msg.references && msg.references.length > 0 && (
                <div className="mt-2 space-y-1 whitespace-normal">
                  {msg.references.slice(0, 5).map((ref) => (
                    <KnowledgeNodeLink
                      key={ref.id}
                      nodeId={ref.id}
                      className="flex items-start gap-1.5 rounded border border-border bg-background px-2 py-1 text-[11px] leading-snug text-muted-foreground"
                    >
                      <BookOpen className="mt-0.5 h-3 w-3 shrink-0" />
                      <div className="min-w-0">
                        <div className="truncate text-foreground">{ref.title || ref.id}</div>
                        <div className="font-mono text-[10px]">{ref.object_type || "node"} · {ref.id}</div>
                      </div>
                    </KnowledgeNodeLink>
                  ))}
                </div>
              )}
              {msg.role === "assistant" && isStreaming && i === messages.length - 1 && (
                <span className="inline-block w-1 h-3 ml-0.5 bg-current animate-pulse align-middle" />
              )}
            </div>
          ))}
          <div ref={bottomRef} />
        </div>
      </ScrollArea>

      {/* Input */}
      <div className="shrink-0 p-3 border-t border-border flex gap-2 items-end">
        <Textarea
          ref={textareaRef}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="发送消息… (Enter 发送，Shift+Enter 换行)"
          className="resize-none text-sm min-h-[60px] max-h-[160px]"
          disabled={isStreaming}
          rows={2}
        />
        <Button
          size="icon"
          className="h-9 w-9 shrink-0"
          onClick={sendMessage}
          disabled={isStreaming || !input.trim()}
          aria-label="发送"
        >
          <ArrowUp className="h-4 w-4" />
        </Button>
      </div>
    </div>
  );
}
