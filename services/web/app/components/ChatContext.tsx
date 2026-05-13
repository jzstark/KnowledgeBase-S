"use client";

import { createContext, useContext, useState, useEffect, useCallback } from "react";

interface ChatContextValue {
  isOpen: boolean;
  toggleChat: () => void;
  width: number;
  setWidth: (width: number) => void;
  activeSessionId: string | null;
  setActiveSessionId: (id: string | null) => void;
}

const ChatContext = createContext<ChatContextValue | null>(null);

export function ChatProvider({ children }: { children: React.ReactNode }) {
  const [isOpen, setIsOpen] = useState(false);
  const [width, setWidthState] = useState(380);
  const [activeSessionId, setActiveSessionIdState] = useState<string | null>(null);

  useEffect(() => {
    const saved = localStorage.getItem("chat_session_id");
    if (saved) setActiveSessionIdState(saved);
    const savedWidth = Number(localStorage.getItem("chat_sidebar_width"));
    if (Number.isFinite(savedWidth) && savedWidth >= 320 && savedWidth <= 720) {
      setWidthState(savedWidth);
    }
  }, []);

  const setActiveSessionId = useCallback((id: string | null) => {
    setActiveSessionIdState(id);
    if (id) localStorage.setItem("chat_session_id", id);
    else localStorage.removeItem("chat_session_id");
  }, []);

  const toggleChat = useCallback(() => setIsOpen((v) => !v), []);
  const setWidth = useCallback((nextWidth: number) => {
    const clamped = Math.min(720, Math.max(320, Math.round(nextWidth)));
    setWidthState(clamped);
    localStorage.setItem("chat_sidebar_width", String(clamped));
  }, []);

  return (
    <ChatContext.Provider value={{ isOpen, toggleChat, width, setWidth, activeSessionId, setActiveSessionId }}>
      {children}
    </ChatContext.Provider>
  );
}

export function useChatContext() {
  const ctx = useContext(ChatContext);
  if (!ctx) throw new Error("useChatContext must be used within ChatProvider");
  return ctx;
}
