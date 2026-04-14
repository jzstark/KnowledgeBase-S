"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

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

  // 定稿反馈状态
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
    <main className="min-h-screen bg-gray-50">
      <div className="max-w-6xl mx-auto px-6 py-8">
        <div className="flex items-center justify-between mb-6">
          <h1 className="text-2xl font-semibold text-gray-900">草稿历史</h1>
        </div>

        {loading ? (
          <p className="text-gray-400 text-sm">加载中…</p>
        ) : drafts.length === 0 ? (
          <p className="text-gray-400 text-sm">暂无草稿记录</p>
        ) : (
          <div className="flex gap-6">
            {/* 草稿列表 */}
            <div className="w-80 shrink-0 space-y-2">
              {drafts.map((d) => (
                <button
                  key={d.id}
                  onClick={() => openDraft(d.id)}
                  className={`w-full text-left rounded-lg border p-3 transition-colors ${
                    selected?.id === d.id
                      ? "border-blue-500 bg-blue-50"
                      : "border-gray-200 bg-white hover:border-gray-300"
                  }`}
                >
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-xs font-medium text-blue-600 bg-blue-50 px-2 py-0.5 rounded-full">
                      {d.template_name || "default"}
                    </span>
                    <span className="text-xs text-gray-400">
                      {d.created_at ? formatDate(d.created_at) : ""}
                    </span>
                  </div>
                  <p className="text-sm text-gray-700 line-clamp-2">
                    {d.preview || "（无预览）"}
                  </p>
                  <p className="text-xs text-gray-400 mt-1">
                    {(d.selected_node_ids || []).length} 篇素材
                  </p>
                </button>
              ))}
            </div>

            {/* 草稿详情 */}
            <div className="flex-1 min-w-0">
              {selected ? (
                <div className="bg-white rounded-lg border border-gray-200 p-5">
                  {/* 头部工具栏 */}
                  <div className="flex items-center justify-between mb-3">
                    <div className="flex items-center gap-2">
                      <span className="text-xs font-medium text-blue-600 bg-blue-50 px-2 py-0.5 rounded-full">
                        {selected.template_name || "default"}
                      </span>
                      <span className="text-xs text-gray-400">
                        {selected.created_at ? formatDate(selected.created_at) : ""}
                      </span>
                      {selected.final_content && (
                        <span className="text-xs text-green-600 bg-green-50 px-2 py-0.5 rounded-full">
                          已有定稿
                        </span>
                      )}
                    </div>
                    <button
                      onClick={copy}
                      className="text-sm px-3 py-1 rounded-md border border-gray-200 hover:bg-gray-50 transition-colors"
                    >
                      {copied ? "已复制" : "复制"}
                    </button>
                  </div>

                  {/* 草稿正文编辑区 */}
                  <textarea
                    value={selected.draft_content}
                    onChange={(e) =>
                      setSelected({ ...selected, draft_content: e.target.value })
                    }
                    className="w-full h-[55vh] text-sm text-gray-800 leading-relaxed resize-none border-0 outline-none"
                    spellCheck={false}
                  />

                  {/* 定稿反馈区 */}
                  <div className="mt-3 border-t border-gray-100 pt-3">
                    {feedbackResult && (
                      <div className="mb-2 text-sm text-green-700 bg-green-50 border border-green-200 rounded-lg px-3 py-2">
                        {feedbackResult}
                      </div>
                    )}

                    {!showFeedback ? (
                      <button
                        onClick={() => { setShowFeedback(true); setFeedbackResult(null); }}
                        className="text-sm text-gray-500 hover:text-gray-700 flex items-center gap-1"
                      >
                        <span>+</span>
                        <span>提交定稿，让系统学习你的写作偏好</span>
                      </button>
                    ) : (
                      <div className="space-y-2">
                        <p className="text-xs text-gray-500">
                          将你修改后的最终版本粘贴到下方，系统将对比草稿并提炼偏好规则：
                        </p>
                        <textarea
                          value={finalContent}
                          onChange={(e) => setFinalContent(e.target.value)}
                          placeholder="粘贴你的定稿内容…"
                          className="w-full h-[30vh] text-sm text-gray-800 leading-relaxed resize-none border border-gray-200 rounded-lg p-3 outline-none focus:border-blue-400"
                          spellCheck={false}
                        />
                        <div className="flex items-center gap-2">
                          <button
                            onClick={submitFeedback}
                            disabled={submitting || !finalContent.trim()}
                            className="text-sm px-4 py-1.5 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                          >
                            {submitting ? "分析中…" : "提交定稿"}
                          </button>
                          <button
                            onClick={() => { setShowFeedback(false); setFinalContent(""); }}
                            className="text-sm text-gray-400 hover:text-gray-600"
                          >
                            取消
                          </button>
                        </div>
                      </div>
                    )}
                  </div>
                </div>
              ) : (
                <div className="bg-white rounded-lg border border-gray-200 p-8 text-center text-gray-400 text-sm">
                  点击左侧草稿查看详情
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </main>
  );
}
