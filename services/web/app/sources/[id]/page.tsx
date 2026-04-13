"use client";

import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
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
};
const TYPE_COLORS: Record<string, string> = {
  rss: "bg-orange-100 text-orange-700",
  wechat: "bg-green-100 text-green-700",
  url: "bg-blue-100 text-blue-700",
  pdf: "bg-red-100 text-red-700",
  image: "bg-purple-100 text-purple-700",
  plaintext: "bg-gray-100 text-gray-700",
  word: "bg-indigo-100 text-indigo-700",
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

function WechatConfig({ source }: { source: Source }) {
  const pushUrl = typeof window !== "undefined"
    ? `${window.location.origin}/api/sources/wechat/ingest`
    : "/api/sources/wechat/ingest";

  const bodyTemplate = JSON.stringify({
    source_id: source.id,
    title: "文章标题",
    content: "正文全文",
    url: "原文链接",
  }, null, 2);

  const [copiedBody, setCopiedBody] = useState(false);
  async function copyBody() {
    await navigator.clipboard.writeText(bodyTemplate);
    setCopiedBody(true);
    setTimeout(() => setCopiedBody(false), 2000);
  }

  return (
    <>
      {/* 连接配置 */}
      <Section title="连接配置">
        <CopyRow label="推送地址" value={pushUrl} />
        <CopyRow label="Source ID" value={source.id} />
        {source.api_token && (
          <CopyRow label="API Token" value={source.api_token} />
        )}
      </Section>

      {/* 快捷指令配置 */}
      <Section title="iPhone 快捷指令" badge="配置指南">
        <ol className="list-decimal list-inside space-y-2 text-sm text-gray-600">
          <li>打开「快捷指令」App，新建快捷指令</li>
          <li>
            添加操作：<strong className="text-gray-800">获取 URL 的内容</strong>
          </li>
          <li>
            URL 填入上方「推送地址」，方法选 <strong className="text-gray-800">POST</strong>
          </li>
          <li>
            标头添加一项：
            <code className="mx-1 text-xs bg-gray-100 border border-gray-200 px-1.5 py-0.5 rounded">X-API-Token</code>
            值填入上方「API Token」
          </li>
          <li>
            请求体选 <strong className="text-gray-800">JSON</strong>，参考下方模板
            （title / content / url 替换为快捷指令变量，如「询问输入」或共享内容）
          </li>
          <li>将快捷指令加入「共享表单」，在微信 / Safari 中分享文章时即可触发</li>
        </ol>

        <div className="mt-4">
          <div className="flex items-center justify-between mb-2">
            <span className="text-sm font-medium text-gray-700">请求体模板（source_id 已预填）</span>
            <button
              onClick={copyBody}
              className="text-xs px-2 py-1 border border-gray-200 rounded hover:bg-gray-50"
            >
              {copiedBody ? "已复制 ✓" : "复制"}
            </button>
          </div>
          <pre className="bg-gray-50 border border-gray-200 rounded-lg p-3 text-xs text-gray-600 overflow-x-auto leading-relaxed">
            {bodyTemplate}
          </pre>
        </div>
      </Section>

      {/* 预留扩展区域 */}
      <Section title="安装说明 / QR Code" badge="即将推出">
        <p className="text-sm text-gray-400">后续将在此提供一键导入的快捷指令文件与二维码。</p>
      </Section>
    </>
  );
}

// ── 主页面 ────────────────────────────────────────────────────────────────────

export default function SourceDetailPage() {
  const params = useParams();
  const router = useRouter();
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
          {source.type === "wechat" && <WechatConfig source={source} />}

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
