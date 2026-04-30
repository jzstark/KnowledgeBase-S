"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import { ArrowUp, PenSquare, X, Trash2 } from "lucide-react";
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
}

export default function ChatSidebar() {
  const { isOpen, toggleChat, activeSessionId, setActiveSessionId } = useChatContext();

  const [sessions, setSessions] = useState<Session[]>([]);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);

  const bottomRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

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
            const { delta } = JSON.parse(payload);
            setMessages((prev) => {
              const next = [...prev];
              next[next.length - 1] = {
                ...next[next.length - 1],
                content: next[next.length - 1].content + delta,
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

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
      }
    },
    [sendMessage]
  );

  // ── 渲染 ──────────────────────────────────────────────────────────────────

  return (
    <div
      className={cn(
        "fixed top-0 right-0 h-full w-[360px] z-40",
        "border-l border-border bg-background flex flex-col",
        "transition-transform duration-300 ease-in-out",
        isOpen ? "translate-x-0" : "translate-x-full"
      )}
    >
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
                "max-w-[90%] rounded-lg px-3 py-2 text-sm leading-relaxed whitespace-pre-wrap break-words",
                msg.role === "user"
                  ? "self-end bg-primary text-primary-foreground"
                  : "self-start bg-muted text-foreground"
              )}
            >
              {msg.content}
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
