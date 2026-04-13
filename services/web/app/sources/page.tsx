"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";

// ── Types ─────────────────────────────────────────────────────────────────────

interface Source {
  id: string;
  name: string;
  type: string;
  fetch_mode: string;
  config: Record<string, unknown> | string;
  api_token: string | null;
  is_primary: boolean;
  last_fetched_at: string | null;
  created_at: string;
  article_count: number;
}

type TabType = "auto" | "manual";

const AUTO_TYPES = ["rss", "wechat"];
const MANUAL_TYPES = ["url", "pdf", "image", "plaintext", "word"];
const FILE_TYPES = ["pdf", "image", "plaintext", "word"];
const FILE_ACCEPT: Record<string, string> = {
  pdf: ".pdf",
  image: ".jpg,.jpeg,.png,.gif,.webp",
  plaintext: ".txt,.md",
  word: ".doc,.docx",
};
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

// ── Main Page ─────────────────────────────────────────────────────────────────

export default function SourcesPage() {
  const [sources, setSources] = useState<Source[]>([]);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState<TabType>("auto");
  const [showAdd, setShowAdd] = useState(false);
  const [fetching, setFetching] = useState<string | null>(null);
  const [newlyCreatedWechat, setNewlyCreatedWechat] = useState<Source | null>(null);
  // upload/add-url modal state
  const [uploadTarget, setUploadTarget] = useState<Source | null>(null);
  const [addUrlTarget, setAddUrlTarget] = useState<Source | null>(null);

  async function loadSources() {
    try {
      const r = await fetch("/api/sources", { credentials: "include" });
      if (r.ok) setSources(await r.json());
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { loadSources(); }, []);

  async function handleDelete(id: string, name: string) {
    if (!confirm(`确定删除「${name}」？此操作不可恢复。`)) return;
    const r = await fetch(`/api/sources/${id}`, {
      method: "DELETE", credentials: "include",
    });
    if (r.ok || r.status === 204) setSources((p) => p.filter((s) => s.id !== id));
  }

  async function handleFetch(id: string) {
    setFetching(id);
    try {
      await fetch(`/api/sources/${id}/fetch`, { method: "POST", credentials: "include" });
    } finally {
      setFetching(null);
      setTimeout(loadSources, 2000);
    }
  }

  async function handleTogglePrimary(id: string, current: boolean) {
    const r = await fetch(`/api/sources/${id}`, {
      method: "PUT",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ is_primary: !current }),
    });
    if (r.ok) {
      setSources((p) => p.map((s) => s.id === id ? { ...s, is_primary: !current } : s));
    }
  }

  function handleCreated(source: Source) {
    setSources((p) => [source, ...p]);
    setShowAdd(false);
    if (source.type === "wechat") setNewlyCreatedWechat(source);
    if (AUTO_TYPES.includes(source.type)) setTab("auto");
    else setTab("manual");
  }

  const autoSources = sources.filter((s) => AUTO_TYPES.includes(s.type));
  const manualSources = sources.filter((s) => MANUAL_TYPES.includes(s.type));
  const displayed = tab === "auto" ? autoSources : manualSources;

  return (
    <main className="min-h-screen bg-gray-50">
      <div className="max-w-4xl mx-auto px-6 py-8">
        {/* 顶部 */}
        <div className="flex items-center justify-between mb-6">
          <h1 className="text-2xl font-semibold text-gray-900">订阅源管理</h1>
          <div className="flex items-center gap-3">
            <Link href="/" className="text-sm text-blue-600 hover:underline">← 返回首页</Link>
            <button
              onClick={() => { setShowAdd((v) => !v); setNewlyCreatedWechat(null); }}
              className="px-3 py-1.5 bg-gray-900 text-white text-sm rounded-lg hover:bg-gray-700 transition-colors"
            >
              {showAdd ? "取消" : "+ 新建 Source"}
            </button>
          </div>
        </div>

        {/* 微信创建后 token 展示 */}
        {newlyCreatedWechat && (
          <WechatInfo source={newlyCreatedWechat} onClose={() => setNewlyCreatedWechat(null)} />
        )}

        {/* 添加表单 */}
        {showAdd && <AddForm onCreated={handleCreated} />}

        {/* Tab */}
        <div className="flex gap-1 mb-4 border-b border-gray-200">
          {(["auto", "manual"] as TabType[]).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`px-4 py-2 text-sm font-medium transition-colors border-b-2 -mb-px ${
                tab === t
                  ? "border-gray-900 text-gray-900"
                  : "border-transparent text-gray-500 hover:text-gray-700"
              }`}
            >
              {t === "auto"
                ? `自动抓取型 (${autoSources.length})`
                : `手动管理型 (${manualSources.length})`}
            </button>
          ))}
        </div>

        {/* 类型说明 */}
        <p className="text-xs text-gray-400 mb-4">
          {tab === "auto"
            ? "系统定时自动抓取，无需手动操作。"
            : "持久渠道 — 可随时向其中追加 URL 或上传文件，每次处理后知识入库。"}
        </p>

        {/* 列表 */}
        {loading ? (
          <p className="text-sm text-gray-400 py-8 text-center">加载中…</p>
        ) : displayed.length === 0 ? (
          <p className="text-sm text-gray-400 py-8 text-center">暂无 source，点击"新建 Source"添加</p>
        ) : (
          <div className="space-y-3">
            {displayed.map((s) => (
              <SourceCard
                key={s.id}
                source={s}
                fetching={fetching === s.id}
                onFetch={() => handleFetch(s.id)}
                onDelete={() => handleDelete(s.id, s.name)}
                onUpload={() => setUploadTarget(s)}
                onAddUrl={() => setAddUrlTarget(s)}
                onTogglePrimary={() => handleTogglePrimary(s.id, s.is_primary)}
              />
            ))}
          </div>
        )}
      </div>

      {/* 上传文件 Modal */}
      {uploadTarget && (
        <UploadModal
          source={uploadTarget}
          onClose={() => setUploadTarget(null)}
          onDone={() => { setUploadTarget(null); setTimeout(loadSources, 2000); }}
        />
      )}

      {/* 添加 URL Modal */}
      {addUrlTarget && (
        <AddUrlModal
          source={addUrlTarget}
          onClose={() => setAddUrlTarget(null)}
          onDone={() => { setAddUrlTarget(null); setTimeout(loadSources, 2000); }}
        />
      )}
    </main>
  );
}

// ── Source Card ───────────────────────────────────────────────────────────────

function SourceCard({
  source, fetching, onFetch, onDelete, onUpload, onAddUrl, onTogglePrimary,
}: {
  source: Source;
  fetching: boolean;
  onFetch: () => void;
  onDelete: () => void;
  onUpload: () => void;
  onAddUrl: () => void;
  onTogglePrimary: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const cfg = parseCfg(source);

  async function copyToken() {
    if (!source.api_token) return;
    await navigator.clipboard.writeText(source.api_token);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  function fmtDate(iso: string | null) {
    if (!iso) return "从未";
    return new Date(iso).toLocaleString("zh-CN", {
      month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    });
  }

  const uploadCount = Array.isArray((cfg.uploads as unknown[]))
    ? (cfg.uploads as unknown[]).length
    : 0;

  return (
    <div className="bg-white rounded-lg border border-gray-200 p-4">
      <div className="flex items-start justify-between gap-4">
        <div className="flex items-center gap-3 min-w-0">
          <span className={`text-xs font-medium px-2 py-0.5 rounded-full shrink-0 ${TYPE_COLORS[source.type] || "bg-gray-100 text-gray-600"}`}>
            {TYPE_LABELS[source.type] || source.type}
          </span>
          <div className="min-w-0">
            <p className="font-medium text-gray-900 truncate">{source.name}</p>
            {typeof cfg.url === "string" && (
              <p className="text-xs text-gray-400 truncate mt-0.5">{cfg.url}</p>
            )}
            {FILE_TYPES.includes(source.type) && (
              <p className="text-xs text-gray-400 mt-0.5">
                {uploadCount > 0 ? `${uploadCount} 次上传批次` : "暂无上传记录"}
              </p>
            )}
          </div>
        </div>

        <div className="flex items-center gap-2 shrink-0 flex-wrap justify-end">
          {/* is_primary 徽章 + 切换 */}
          <button
            onClick={onTogglePrimary}
            title={source.is_primary ? "点击切换为参考型（不出现在简报）" : "点击切换为主要型（出现在简报）"}
            className={`text-xs px-2 py-0.5 rounded-full border transition-colors ${
              source.is_primary
                ? "border-blue-300 bg-blue-50 text-blue-700 hover:bg-blue-100"
                : "border-gray-200 bg-gray-50 text-gray-500 hover:bg-gray-100"
            }`}
          >
            {source.is_primary ? "主要" : "参考"}
          </button>
          <span className="text-xs text-gray-400">{source.article_count} 篇</span>

          {/* 自动型：立即抓取 */}
          {source.type === "rss" && (
            <button onClick={onFetch} disabled={fetching}
              className="text-xs px-2 py-1 rounded border border-gray-200 hover:bg-gray-50 disabled:opacity-40 transition-colors">
              {fetching ? "抓取中…" : "立即抓取"}
            </button>
          )}

          {/* WeChat：查看配置 + 复制 token */}
          {source.type === "wechat" && (
            <>
              <Link
                href={`/sources/${source.id}`}
                className="text-xs px-2 py-1 rounded border border-green-200 text-green-700 hover:bg-green-50 transition-colors"
              >
                查看配置
              </Link>
              {source.api_token && (
                <button onClick={copyToken}
                  className="text-xs px-2 py-1 rounded border border-gray-200 hover:bg-gray-50 transition-colors">
                  {copied ? "已复制 ✓" : "复制 Token"}
                </button>
              )}
            </>
          )}

          {/* URL：添加 URL */}
          {source.type === "url" && (
            <button onClick={onAddUrl}
              className="text-xs px-2 py-1 rounded border border-blue-200 text-blue-600 hover:bg-blue-50 transition-colors">
              添加 URL
            </button>
          )}

          {/* 文件型：上传文件 */}
          {FILE_TYPES.includes(source.type) && (
            <button onClick={onUpload}
              className="text-xs px-2 py-1 rounded border border-blue-200 text-blue-600 hover:bg-blue-50 transition-colors">
              上传文件
            </button>
          )}

          <button onClick={onDelete}
            className="text-xs px-2 py-1 rounded border border-red-100 text-red-500 hover:bg-red-50 transition-colors">
            删除
          </button>
        </div>
      </div>

      <div className="mt-2 flex items-center gap-4 text-xs text-gray-400">
        {source.fetch_mode === "subscription" && (
          <span>上次抓取：{fmtDate(source.last_fetched_at)}</span>
        )}
        <span>创建：{fmtDate(source.created_at)}</span>
      </div>
    </div>
  );
}

// ── WeChat Info Panel ─────────────────────────────────────────────────────────

function WechatInfo({ source, onClose }: { source: Source; onClose: () => void }) {
  const [copied, setCopied] = useState<string | null>(null);
  const [showGuide, setShowGuide] = useState(false);
  const pushUrl = `${typeof window !== "undefined" ? window.location.origin : ""}/api/sources/wechat/ingest`;

  async function copy(text: string, key: string) {
    await navigator.clipboard.writeText(text);
    setCopied(key);
    setTimeout(() => setCopied(null), 2000);
  }

  const bodyTemplate = JSON.stringify({
    source_id: source.id,
    title: "文章标题",
    content: "正文全文",
    url: "原文链接",
  }, null, 2);

  return (
    <div className="mb-6 bg-green-50 border border-green-200 rounded-lg p-4">
      <div className="flex justify-between items-start mb-3">
        <p className="text-sm font-medium text-green-800">微信公众号 Source 已创建 — 配置快捷指令：</p>
        <button onClick={onClose} className="text-green-600 hover:text-green-800 text-xs">关闭</button>
      </div>

      {/* 三个可复制字段 */}
      <div className="space-y-2 text-xs">
        <InfoRow label="推送地址" value={pushUrl} onCopy={() => copy(pushUrl, "url")} copied={copied === "url"} />
        <InfoRow label="Source ID" value={source.id} onCopy={() => copy(source.id, "id")} copied={copied === "id"} />
        {source.api_token && (
          <InfoRow label="API Token" value={source.api_token} onCopy={() => copy(source.api_token!, "token")} copied={copied === "token"} />
        )}
      </div>

      {/* 快捷指令配置指南 */}
      <div className="mt-3">
        <button
          onClick={() => setShowGuide((v) => !v)}
          className="text-xs text-green-700 hover:text-green-900 flex items-center gap-1"
        >
          <span>{showGuide ? "▼" : "▶"}</span>
          <span>iPhone 快捷指令配置方法</span>
        </button>

        {showGuide && (
          <div className="mt-2 space-y-2 text-xs text-green-800 bg-white border border-green-100 rounded p-3">
            <p className="font-medium">在「快捷指令」App 中新建快捷指令，添加以下操作：</p>
            <ol className="list-decimal list-inside space-y-1.5 text-green-700">
              <li>操作：<strong>获取 URL 的内容</strong></li>
              <li>URL 填写上方「推送地址」</li>
              <li>方法：<strong>POST</strong></li>
              <li>
                标头添加一项：
                <code className="mx-1 bg-green-50 border border-green-200 px-1 rounded">X-API-Token</code>
                值填写上方「API Token」
              </li>
              <li>
                请求体选 <strong>JSON</strong>，内容参考下方模板
                （title / content / url 替换为快捷指令变量）
              </li>
              <li>将快捷指令加入「共享表单」，在微信 / Safari 分享文章时触发</li>
            </ol>

            {/* 请求体模板 */}
            <div className="mt-2">
              <div className="flex items-center justify-between mb-1">
                <span className="font-medium text-green-700">请求体模板（source_id 已预填）</span>
                <button
                  onClick={() => copy(bodyTemplate, "body")}
                  className="px-2 py-0.5 border border-green-200 rounded bg-green-50 hover:bg-green-100 text-green-700"
                >
                  {copied === "body" ? "已复制 ✓" : "复制"}
                </button>
              </div>
              <pre className="bg-gray-50 border border-gray-200 rounded p-2 text-gray-600 overflow-x-auto text-xs leading-relaxed">
                {bodyTemplate}
              </pre>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function InfoRow({ label, value, onCopy, copied }: { label: string; value: string; onCopy: () => void; copied: boolean }) {
  return (
    <div className="flex items-center gap-2">
      <span className="text-green-700 w-20 shrink-0">{label}：</span>
      <code className="flex-1 bg-white border border-green-200 rounded px-2 py-1 text-gray-700 truncate">{value}</code>
      <button onClick={onCopy} className="shrink-0 text-green-700 hover:text-green-900 px-2 py-1 border border-green-200 rounded bg-white">
        {copied ? "✓" : "复制"}
      </button>
    </div>
  );
}

// ── Add Form (create source channel, no file) ────────────────────────────────

function AddForm({ onCreated }: { onCreated: (s: Source) => void }) {
  const [type, setType] = useState("rss");
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (!name.trim()) { setError("请填写名称"); return; }
    if ((type === "rss" || type === "url") && !url.trim()) { setError("请填写 URL"); return; }

    setSaving(true);
    try {
      const config: Record<string, string> = {};
      if (type === "rss" || type === "url") config.url = url.trim();

      const res = await fetch("/api/sources", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name.trim(), type, config }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setError(body.detail || `创建失败 (${res.status})`);
        return;
      }
      onCreated(await res.json());
      setName(""); setUrl(""); setType("rss");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "请求失败");
    } finally {
      setSaving(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="mb-6 bg-white border border-gray-200 rounded-lg p-4 space-y-3">
      <p className="text-sm font-medium text-gray-700">新建 Source 渠道</p>

      <div className="flex gap-3 items-center">
        <label className="text-xs text-gray-500 w-14 shrink-0">类型</label>
        <select value={type} onChange={(e) => { setType(e.target.value); setUrl(""); }}
          className="text-sm border border-gray-200 rounded px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-gray-400">
          <optgroup label="自动抓取型">
            <option value="rss">RSS</option>
            <option value="wechat">微信公众号</option>
          </optgroup>
          <optgroup label="手动管理型">
            <option value="url">URL</option>
            <option value="pdf">PDF</option>
            <option value="image">图片</option>
            <option value="plaintext">纯文本</option>
            <option value="word">Word</option>
          </optgroup>
        </select>
      </div>

      <div className="flex gap-3 items-center">
        <label className="text-xs text-gray-500 w-14 shrink-0">名称</label>
        <input type="text" value={name} onChange={(e) => setName(e.target.value)}
          placeholder={type === "pdf" ? "例：有趣的 Paper" : "例：科技早报"}
          className="flex-1 text-sm border border-gray-200 rounded px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-gray-400" />
      </div>

      {(type === "rss" || type === "url") && (
        <div className="flex gap-3 items-center">
          <label className="text-xs text-gray-500 w-14 shrink-0">{type === "rss" ? "Feed URL" : "初始 URL"}</label>
          <input type="url" value={url} onChange={(e) => setUrl(e.target.value)}
            placeholder={type === "rss" ? "https://example.com/feed.xml" : "https://example.com/article"}
            className="flex-1 text-sm border border-gray-200 rounded px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-gray-400" />
        </div>
      )}

      {FILE_TYPES.includes(type) && (
        <p className="text-xs text-gray-400 pl-[4.5rem]">
          创建后点击 Source 卡片上的"上传文件"按钮，支持随时批量追加。
        </p>
      )}

      {type === "wechat" && (
        <p className="text-xs text-gray-400 pl-[4.5rem]">
          创建后自动生成专属 API Token，配置到 iPhone 快捷指令使用。
        </p>
      )}

      {error && <p className="text-xs text-red-500">{error}</p>}

      <div className="flex justify-end">
        <button type="submit" disabled={saving}
          className="px-4 py-1.5 bg-gray-900 text-white text-sm rounded-lg hover:bg-gray-700 disabled:opacity-40 transition-colors">
          {saving ? "创建中…" : "创建渠道"}
        </button>
      </div>
    </form>
  );
}

// ── Upload Files Modal ────────────────────────────────────────────────────────

function UploadModal({ source, onClose, onDone }: { source: Source; onClose: () => void; onDone: () => void }) {
  const [files, setFiles] = useState<FileList | null>(null);
  const [uploading, setUploading] = useState(false);
  const [result, setResult] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  async function handleUpload(e: React.FormEvent) {
    e.preventDefault();
    if (!files || files.length === 0) return;
    setUploading(true);
    try {
      const fd = new FormData();
      for (const f of Array.from(files)) fd.append("files", f);
      const res = await fetch(`/api/sources/${source.id}/upload`, {
        method: "POST", credentials: "include", body: fd,
      });
      if (res.ok) {
        const data = await res.json();
        setResult(`✅ 已上传 ${data.files_saved} 个文件，ingestion-worker 正在处理中…`);
        setTimeout(onDone, 2000);
      } else {
        const err = await res.json().catch(() => ({}));
        setResult(`❌ ${err.detail || "上传失败"}`);
      }
    } finally {
      setUploading(false);
    }
  }

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
      <div className="bg-white rounded-xl shadow-xl p-6 w-full max-w-md">
        <div className="flex justify-between items-center mb-4">
          <p className="font-medium text-gray-900">向「{source.name}」上传文件</p>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 text-xl leading-none">×</button>
        </div>
        <form onSubmit={handleUpload} className="space-y-4">
          <input
            ref={fileRef}
            type="file"
            multiple
            accept={FILE_ACCEPT[source.type] || "*"}
            onChange={(e) => setFiles(e.target.files)}
            className="text-sm text-gray-600 w-full"
          />
          <p className="text-xs text-gray-400">可一次选择多个文件，每个文件独立处理后进入知识库。</p>
          {result && <p className="text-sm">{result}</p>}
          <div className="flex justify-end gap-2">
            <button type="button" onClick={onClose}
              className="px-3 py-1.5 text-sm border border-gray-200 rounded-lg hover:bg-gray-50">
              取消
            </button>
            <button type="submit" disabled={uploading || !files?.length}
              className="px-4 py-1.5 bg-gray-900 text-white text-sm rounded-lg hover:bg-gray-700 disabled:opacity-40 transition-colors">
              {uploading ? "上传中…" : `上传${files?.length ? ` (${files.length})` : ""}`}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ── Add URL Modal ─────────────────────────────────────────────────────────────

function AddUrlModal({ source, onClose, onDone }: { source: Source; onClose: () => void; onDone: () => void }) {
  const [text, setText] = useState("");
  const [saving, setSaving] = useState(false);
  const [result, setResult] = useState("");

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const urls = text.split("\n").map((u) => u.trim()).filter(Boolean);
    if (!urls.length) return;
    setSaving(true);
    try {
      const res = await fetch(`/api/sources/${source.id}/add-url`, {
        method: "POST", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ urls }),
      });
      if (res.ok) {
        const data = await res.json();
        setResult(`✅ 已加入队列 ${data.urls_queued} 条 URL，ingestion-worker 正在处理中…`);
        setTimeout(onDone, 2000);
      } else {
        const err = await res.json().catch(() => ({}));
        setResult(`❌ ${err.detail || "操作失败"}`);
      }
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
      <div className="bg-white rounded-xl shadow-xl p-6 w-full max-w-md">
        <div className="flex justify-between items-center mb-4">
          <p className="font-medium text-gray-900">向「{source.name}」添加 URL</p>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 text-xl leading-none">×</button>
        </div>
        <form onSubmit={handleSubmit} className="space-y-4">
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={"https://example.com/article-1\nhttps://example.com/article-2"}
            rows={5}
            className="w-full text-sm border border-gray-200 rounded-lg px-3 py-2 resize-none
                       focus:outline-none focus:ring-1 focus:ring-gray-400"
          />
          <p className="text-xs text-gray-400">每行一个 URL，可批量添加。</p>
          {result && <p className="text-sm">{result}</p>}
          <div className="flex justify-end gap-2">
            <button type="button" onClick={onClose}
              className="px-3 py-1.5 text-sm border border-gray-200 rounded-lg hover:bg-gray-50">
              取消
            </button>
            <button type="submit" disabled={saving || !text.trim()}
              className="px-4 py-1.5 bg-gray-900 text-white text-sm rounded-lg hover:bg-gray-700 disabled:opacity-40 transition-colors">
              {saving ? "处理中…" : "添加"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
