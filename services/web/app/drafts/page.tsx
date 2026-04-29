"use client";

import { useEffect, useState } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { cn } from "@/lib/utils";

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

  const [showFeedback, setShowFeedback] = useState(false);
  const [finalContent, setFinalContent] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [feedbackResult, setFeedbackResult] = useState<string | null>(null);

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
      setShowFeedback(false);
      setFinalContent("");
      setFeedbackResult(null);
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

  async function submitFeedback() {
    if (!selected || !finalContent.trim()) return;
    setSubmitting(true);
    try {
      const r = await fetch(`/api/drafts/${selected.id}/feedback`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ final_content: finalContent }),
      });
      if (r.ok) {
        const data = await r.json();
        const n = data.rules_extracted ?? 0;
        setFeedbackResult(
          n > 0 ? `已学习 ${n} 条偏好规则` : "已保存定稿（未提取到新规则）"
        );
        setShowFeedback(false);
        setFinalContent("");
      } else {
        setFeedbackResult("提交失败，请重试");
      }
    } catch {
      setFeedbackResult("提交失败，请重试");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="min-h-screen bg-background">
      <div className="max-w-6xl mx-auto px-6 py-8">
        <h1 className="text-2xl font-semibold mb-6">草稿历史</h1>

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
                        {selected.final_content && (
                          <Badge variant="outline" className="text-xs text-green-600 border-green-200">
                            已有定稿
                          </Badge>
                        )}
                      </div>
                      <Button variant="outline" size="sm" onClick={copy}>
                        {copied ? "已复制 ✓" : "复制"}
                      </Button>
                    </div>

                    {/* 草稿正文编辑区 */}
                    <Textarea
                      value={selected.draft_content}
                      onChange={(e) =>
                        setSelected({ ...selected, draft_content: e.target.value })
                      }
                      className="h-[55vh] text-sm leading-relaxed resize-none border-0 shadow-none focus-visible:ring-0 p-0"
                      spellCheck={false}
                    />

                    {/* 定稿反馈区 */}
                    <div className="mt-3 pt-3">
                      <Separator className="mb-3" />
                      {feedbackResult && (
                        <div className="mb-3 text-sm text-green-700 bg-green-50 dark:bg-green-950 dark:text-green-400 border border-green-200 dark:border-green-800 rounded-lg px-3 py-2">
                          {feedbackResult}
                        </div>
                      )}

                      {!showFeedback ? (
                        <Button
                          variant="ghost"
                          size="sm"
                          className="text-muted-foreground"
                          onClick={() => { setShowFeedback(true); setFeedbackResult(null); }}
                        >
                          + 提交定稿，让系统学习你的写作偏好
                        </Button>
                      ) : (
                        <div className="space-y-2">
                          <p className="text-xs text-muted-foreground">
                            将你修改后的最终版本粘贴到下方，系统将对比草稿并提炼偏好规则：
                          </p>
                          <Textarea
                            value={finalContent}
                            onChange={(e) => setFinalContent(e.target.value)}
                            placeholder="粘贴你的定稿内容…"
                            className="h-[30vh] text-sm leading-relaxed resize-none"
                            spellCheck={false}
                          />
                          <div className="flex items-center gap-2">
                            <Button
                              size="sm"
                              onClick={submitFeedback}
                              disabled={submitting || !finalContent.trim()}
                            >
                              {submitting ? "分析中…" : "提交定稿"}
                            </Button>
                            <Button
                              variant="ghost"
                              size="sm"
                              className="text-muted-foreground"
                              onClick={() => { setShowFeedback(false); setFinalContent(""); }}
                            >
                              取消
                            </Button>
                          </div>
                        </div>
                      )}
                    </div>
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
