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
}

export default function DraftsPage() {
  const [drafts, setDrafts] = useState<DraftSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<DraftDetail | null>(null);
  const [copied, setCopied] = useState(false);

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
    <main className="min-h-screen bg-gray-50">
      <div className="max-w-6xl mx-auto px-6 py-8">
        <div className="flex items-center justify-between mb-6">
          <h1 className="text-2xl font-semibold text-gray-900">草稿历史</h1>
          <Link href="/" className="text-sm text-blue-600 hover:underline">
            ← 返回首页
          </Link>
        </div>

        {loading ? (
          <p className="text-gray-400 text-sm">加载中…</p>
        ) : drafts.length === 0 ? (
          <p className="text-gray-400 text-sm">暂无草稿记录</p>
        ) : (
          <div className="flex gap-6">
            {/* 列表 */}
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

            {/* 详情 */}
            <div className="flex-1 min-w-0">
              {selected ? (
                <div className="bg-white rounded-lg border border-gray-200 p-5">
                  <div className="flex items-center justify-between mb-3">
                    <div className="flex items-center gap-2">
                      <span className="text-xs font-medium text-blue-600 bg-blue-50 px-2 py-0.5 rounded-full">
                        {selected.template_name || "default"}
                      </span>
                      <span className="text-xs text-gray-400">
                        {selected.created_at ? formatDate(selected.created_at) : ""}
                      </span>
                    </div>
                    <button
                      onClick={copy}
                      className="text-sm px-3 py-1 rounded-md border border-gray-200 hover:bg-gray-50 transition-colors"
                    >
                      {copied ? "✅ 已复制" : "复制"}
                    </button>
                  </div>
                  <textarea
                    value={selected.draft_content}
                    onChange={(e) =>
                      setSelected({ ...selected, draft_content: e.target.value })
                    }
                    className="w-full h-[70vh] text-sm text-gray-800 leading-relaxed resize-none border-0 outline-none"
                    spellCheck={false}
                  />
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
