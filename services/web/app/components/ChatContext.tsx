"use client";

import { createContext, useContext, useState, useEffect, useCallback } from "react";

interface ChatContextValue {
  isOpen: boolean;
  toggleChat: () => void;
  activeSessionId: string | null;
  setActiveSessionId: (id: string | null) => void;
}

const ChatContext = createContext<ChatContextValue | null>(null);

export function ChatProvider({ children }: { children: React.ReactNode }) {
  const [isOpen, setIsOpen] = useState(false);
  const [activeSessionId, setActiveSessionIdState] = useState<string | null>(null);

  useEffect(() => {
    const saved = localStorage.getItem("chat_session_id");
    if (saved) setActiveSessionIdState(saved);
  }, []);

  const setActiveSessionId = useCallback((id: string | null) => {
    setActiveSessionIdState(id);
    if (id) localStorage.setItem("chat_session_id", id);
    else localStorage.removeItem("chat_session_id");
  }, []);

  const toggleChat = useCallback(() => setIsOpen((v) => !v), []);

  return (
    <ChatContext.Provider value={{ isOpen, toggleChat, activeSessionId, setActiveSessionId }}>
      {children}
    </ChatContext.Provider>
  );
}

export function useChatContext() {
  const ctx = useContext(ChatContext);
  if (!ctx) throw new Error("useChatContext must be used within ChatProvider");
  return ctx;
}
