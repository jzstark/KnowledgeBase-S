"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";

// ── Types ─────────────────────────────────────────────────────────────────────

interface Source {
  id: string;
  name: string;
  type: string;
  fetch_mode: string;
  config: Record<string, unknown> | string | null;
  api_token: string | null;
  is_primary: boolean;
  last_fetched_at: string | null;
  created_at: string;
  article_count: number;
}

const TYPE_LABELS: Record<string, string> = {
  rss: "RSS", wechat: "微信", url: "URL",
  pdf: "PDF", image: "图片", plaintext: "文本", word: "Word",
  epub: "电子书",
};
const TYPE_COLORS: Record<string, string> = {
  rss: "bg-orange-100 text-orange-700",
  wechat: "bg-green-100 text-green-700",
  url: "bg-blue-100 text-blue-700",
  pdf: "bg-red-100 text-red-700",
  image: "bg-purple-100 text-purple-700",
  plaintext: "bg-gray-100 text-gray-700",
  word: "bg-indigo-100 text-indigo-700",
  epub: "bg-teal-100 text-teal-700",
};

function parseCfg(s: Source): Record<string, unknown> {
  if (!s.config) return {};
  if (typeof s.config === "string") {
    try { return JSON.parse(s.config); } catch { return {}; }
  }
  return s.config as Record<string, unknown>;
}

// ── 可复制行 ──────────────────────────────────────────────────────────────────

function CopyRow({ label, value, mono = true }: { label: string; value: string; mono?: boolean }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    await navigator.clipboard.writeText(value);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }
  return (
    <div className="flex items-center gap-3 py-2 border-b border-gray-100 last:border-0">
      <span className="text-sm text-gray-500 w-24 shrink-0">{label}</span>
      <span className={`flex-1 text-sm text-gray-800 truncate ${mono ? "font-mono" : ""}`}>{value}</span>
      <button
        onClick={copy}
        className="shrink-0 text-xs px-2 py-1 border border-gray-200 rounded hover:bg-gray-50 transition-colors"
      >
        {copied ? "已复制 ✓" : "复制"}
      </button>
    </div>
  );
}

// ── Section 容器 ──────────────────────────────────────────────────────────────

function Section({ title, children, badge }: { title: string; children: React.ReactNode; badge?: string }) {
  return (
    <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
      <div className="px-5 py-3 border-b border-gray-100 flex items-center gap-2">
        <h2 className="text-sm font-semibold text-gray-700">{title}</h2>
        {badge && (
          <span className="text-xs px-2 py-0.5 bg-gray-100 text-gray-500 rounded-full">{badge}</span>
        )}
      </div>
      <div className="px-5 py-4">{children}</div>
    </div>
  );
}

// ── 微信专属：连接配置 ─────────────────────────────────────────────────────────

function WechatConfig({ source, onSourceUpdated }: { source: Source; onSourceUpdated: (source: Source) => void }) {
  const cfg = parseCfg(source);
  const [fetching, setFetching] = useState(false);
  const [saving, setSaving] = useState(false);
  const [feedId, setFeedId] = useState(typeof cfg.feed_id === "string" ? cfg.feed_id : "");
  const [message, setMessage] = useState("");

  async function saveFeedId() {
    const nextFeedId = feedId.trim();
    if (!nextFeedId) {
      setMessage("Feed ID 不能为空。");
      return;
    }
    setSaving(true);
    setMessage("");
    try {
      const res = await fetch(`/api/sources/${source.id}`, {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          config: {
            ...cfg,
            feed_id: nextFeedId,
          },
        }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        setMessage(body.detail || `保存失败 (${res.status})`);
        return;
      }
      onSourceUpdated({ ...source, ...(body as Source) });
      setFeedId(nextFeedId);
      setMessage("Feed ID 已保存。");
    } finally {
      setSaving(false);
    }
  }

  async function triggerFetch() {
    setFetching(true);
    setMessage("");
    try {
      const res = await fetch(`/api/sources/${source.id}/fetch`, {
        method: "POST",
        credentials: "include",
      });
      if (res.ok) setMessage("已触发抓取，ingestion-worker 将按 RSS 流程处理。");
      else {
        const body = await res.json().catch(() => ({}));
        setMessage(body.detail || `触发失败 (${res.status})`);
      }
    } finally {
      setFetching(false);
    }
  }

  return (
    <>
      <Section title="Wechat2RSS 订阅">
        <CopyRow label="Source ID" value={source.id} />
        <div className="flex items-center gap-3 py-2 border-b border-gray-100">
          <label htmlFor="feed-id" className="text-sm text-gray-500 w-24 shrink-0">Feed ID</label>
          <input
            id="feed-id"
            value={feedId}
            onChange={(event) => setFeedId(event.target.value)}
            className="min-w-0 flex-1 rounded border border-gray-200 px-2 py-1 text-sm font-mono text-gray-800 outline-none focus:border-green-400 focus:ring-2 focus:ring-green-100"
          />
          <button
            onClick={saveFeedId}
            disabled={saving || feedId.trim() === (typeof cfg.feed_id === "string" ? cfg.feed_id : "")}
            className="shrink-0 text-xs px-2 py-1 border border-green-200 text-green-700 rounded hover:bg-green-50 disabled:opacity-50"
          >
            {saving ? "保存中…" : "保存"}
          </button>
        </div>
        {typeof cfg.name === "string" && (
          <CopyRow label="公众号" value={cfg.name} mono={false} />
        )}
        <div className="mt-4 flex items-center gap-3">
          <button
            onClick={triggerFetch}
            disabled={fetching}
            className="text-xs px-3 py-1.5 border border-green-200 text-green-700 rounded hover:bg-green-50 disabled:opacity-50"
          >
            {fetching ? "抓取中…" : "立即抓取"}
          </button>
          {message && <span className="text-xs text-gray-500">{message}</span>}
        </div>
      </Section>
    </>
  );
}

// ── 主页面 ────────────────────────────────────────────────────────────────────

export default function SourceDetailPage() {
  const params = useParams();
  const id = params.id as string;

  const [source, setSource] = useState<Source | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    fetch(`/api/sources/${id}`, { credentials: "include" })
      .then((r) => {
        if (!r.ok) throw new Error(`${r.status}`);
        return r.json();
      })
      .then(setSource)
      .catch(() => setError("加载失败，source 可能不存在"))
      .finally(() => setLoading(false));
  }, [id]);

  if (loading) {
    return (
      <main className="min-h-screen bg-gray-50 flex items-center justify-center">
        <p className="text-sm text-gray-400">加载中…</p>
      </main>
    );
  }

  if (error || !source) {
    return (
      <main className="min-h-screen bg-gray-50 flex flex-col items-center justify-center gap-3">
        <p className="text-sm text-gray-500">{error || "source 不存在"}</p>
        <Link href="/sources" className="text-sm text-blue-600 hover:underline">← 返回 Source 列表</Link>
      </main>
    );
  }

  const cfg = parseCfg(source);

  return (
    <main className="min-h-screen bg-gray-50">
      <div className="max-w-2xl mx-auto px-6 py-8">

        {/* 顶部导航 */}
        <div className="flex items-center gap-3 mb-6">
          <Link href="/sources" className="text-sm text-gray-400 hover:text-gray-600 transition-colors">
            ← 订阅源
          </Link>
          <span className="text-gray-300">/</span>
          <span className="text-sm text-gray-700 font-medium">{source.name}</span>
        </div>

        {/* Source 信息头 */}
        <div className="bg-white rounded-xl border border-gray-200 p-5 mb-5">
          <div className="flex items-start justify-between">
            <div className="flex items-center gap-3">
              <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${TYPE_COLORS[source.type] || "bg-gray-100 text-gray-600"}`}>
                {TYPE_LABELS[source.type] || source.type}
              </span>
              <h1 className="text-lg font-semibold text-gray-900">{source.name}</h1>
            </div>
            <span className={`text-xs px-2 py-0.5 rounded-full border ${
              source.is_primary
                ? "border-blue-300 bg-blue-50 text-blue-700"
                : "border-gray-200 bg-gray-50 text-gray-500"
            }`}>
              {source.is_primary ? "主要" : "参考"}
            </span>
          </div>
          <div className="mt-3 flex gap-4 text-xs text-gray-400">
            <span>{source.article_count} 篇文章</span>
            <span>创建于 {new Date(source.created_at).toLocaleDateString("zh-CN")}</span>
            {source.last_fetched_at && (
              <span>最近处理 {new Date(source.last_fetched_at).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })}</span>
            )}
            {typeof cfg.url === "string" && (
              <span className="truncate max-w-xs">{cfg.url}</span>
            )}
          </div>
        </div>

        {/* 类型专属内容 */}
        <div className="space-y-4">
          {source.type === "wechat" && <WechatConfig source={source} onSourceUpdated={setSource} />}

          {source.type !== "wechat" && (
            <Section title="基本信息">
              <p className="text-sm text-gray-400">此 source 类型暂无额外配置项。</p>
            </Section>
          )}
        </div>
      </div>
    </main>
  );
}
