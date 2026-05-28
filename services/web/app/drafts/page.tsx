"use client";

import { useEffect, useState } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { MarkdownView } from "../components/MarkdownView";

interface DraftSummary {
  id: string;
  template_name: string;
  selected_node_ids: string[];
  preview: string;
  created_at: string;
}

interface DraftDetail extends DraftSummary {
  draft_content: string;
  final_content: string | null;
}

export default function DraftsPage() {
  const [drafts, setDrafts] = useState<DraftSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<DraftDetail | null>(null);
  const [copied, setCopied] = useState(false);
  const [editingDraft, setEditingDraft] = useState(false);

  useEffect(() => {
    fetch("/api/drafts", { credentials: "include" })
      .then((r) => r.json())
      .then((data) => {
        if (Array.isArray(data)) setDrafts(data);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  async function openDraft(id: string) {
    const r = await fetch(`/api/drafts/${id}`, { credentials: "include" });
    if (r.ok) {
      const data = await r.json();
      setSelected(data);
      setEditingDraft(false);
    }
  }

  function formatDate(iso: string) {
    return new Date(iso).toLocaleString("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  async function copy() {
    if (!selected) return;
    await navigator.clipboard.writeText(selected.draft_content);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  return (
    <main className="min-h-screen bg-background">
      <div className="max-w-6xl mx-auto px-6 py-8">
        <h1 className="mb-6 text-2xl font-semibold">工作室 &gt; 草稿历史</h1>

        {loading ? (
          <p className="text-muted-foreground text-sm">加载中…</p>
        ) : drafts.length === 0 ? (
          <p className="text-muted-foreground text-sm">暂无草稿记录</p>
        ) : (
          <div className="flex gap-6">
            {/* 草稿列表 */}
            <div className="w-80 shrink-0 space-y-2">
              {drafts.map((d) => (
                <button
                  key={d.id}
                  onClick={() => openDraft(d.id)}
                  className={cn(
                    "w-full text-left rounded-lg border p-3 transition-colors",
                    selected?.id === d.id
                      ? "border-primary bg-accent"
                      : "border-border bg-card hover:border-muted-foreground/40"
                  )}
                >
                  <div className="flex items-center justify-between mb-1">
                    <Badge variant="secondary" className="text-xs">
                      {d.template_name || "default"}
                    </Badge>
                    <span className="text-xs text-muted-foreground">
                      {d.created_at ? formatDate(d.created_at) : ""}
                    </span>
                  </div>
                  <p className="text-sm line-clamp-2">
                    {d.preview || "（无预览）"}
                  </p>
                  <p className="text-xs text-muted-foreground mt-1">
                    {(d.selected_node_ids || []).length} 篇素材
                  </p>
                </button>
              ))}
            </div>

            {/* 草稿详情 */}
            <div className="flex-1 min-w-0">
              {selected ? (
                <Card>
                  <CardContent className="p-5">
                    {/* 头部工具栏 */}
                    <div className="flex items-center justify-between mb-3">
                      <div className="flex items-center gap-2">
                        <Badge variant="secondary" className="text-xs">
                          {selected.template_name || "default"}
                        </Badge>
                        <span className="text-xs text-muted-foreground">
                          {selected.created_at ? formatDate(selected.created_at) : ""}
                        </span>
                      </div>
                      <div className="flex items-center gap-2">
                        <Button variant="ghost" size="sm" onClick={() => setEditingDraft((v) => !v)}>
                          {editingDraft ? "预览" : "编辑"}
                        </Button>
                        <Button variant="outline" size="sm" onClick={copy}>
                          {copied ? "已复制 ✓" : "复制"}
                        </Button>
                      </div>
                    </div>

                    {/* 草稿正文编辑区 */}
                    {editingDraft ? (
                      <Textarea
                        value={selected.draft_content}
                        onChange={(e) =>
                          setSelected({ ...selected, draft_content: e.target.value })
                        }
                        className="h-[65vh] text-sm leading-relaxed resize-none border-0 shadow-none focus-visible:ring-0 p-0"
                        spellCheck={false}
                      />
                    ) : (
                      <div className="max-h-[65vh] overflow-y-auto rounded-md border border-border bg-background p-4">
                        <MarkdownView content={selected.draft_content} />
                      </div>
                    )}
                  </CardContent>
                </Card>
              ) : (
                <Card>
                  <CardContent className="p-8 text-center text-muted-foreground text-sm">
                    点击左侧草稿查看详情
                  </CardContent>
                </Card>
              )}
            </div>
          </div>
        )}
      </div>
    </main>
  );
}
