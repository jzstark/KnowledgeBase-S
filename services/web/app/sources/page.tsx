"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";

// ── Types ─────────────────────────────────────────────────────────────────────

interface Source {
  id: string;
  name: string;
  type: string;
  fetch_mode: string;
  config: Record<string, unknown> | string;
  api_token: string | null;
  is_primary: boolean;
  default_doc_kind: string | null;
  last_fetched_at: string | null;
  created_at: string;
  article_count: number;
}

// ── doc_kind 下拉（共享）──────────────────────────────────────────────────────

interface DocKindConfig {
  values: string[];
  default: string;
}

const DOC_KIND_LABELS: Record<string, string> = {
  regulation: "法规 / 规章",
  case: "判例 / 裁判文书",
  news: "新闻 / 动态",
  memo: "备忘录 / 内部说明",
  contract: "合同 / 协议",
  analysis: "评论 / 分析",
  other: "其他",
};

function useDocKindConfig(): DocKindConfig | null {
  const [cfg, setCfg] = useState<DocKindConfig | null>(null);
  useEffect(() => {
    fetch("/api/config/doc_kind", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => d && setCfg(d))
      .catch(() => {});
  }, []);
  return cfg;
}

function DocKindSelect({
  value, onChange, required = false, includeEmpty = true,
  emptyLabel = "继承 source 默认", id,
}: {
  value: string;
  onChange: (v: string) => void;
  required?: boolean;
  includeEmpty?: boolean;
  emptyLabel?: string;
  id?: string;
}) {
  const cfg = useDocKindConfig();
  return (
    <select
      id={id}
      required={required}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="flex h-9 w-full rounded-md border border-input bg-background px-2 py-1 text-sm shadow-sm focus:outline-none focus:ring-1 focus:ring-ring"
    >
      {includeEmpty && <option value="">{emptyLabel}</option>}
      {(cfg?.values ?? []).map((v) => (
        <option key={v} value={v}>
          {DOC_KIND_LABELS[v] ?? v}
        </option>
      ))}
    </select>
  );
}

interface WechatSubscription {
  feed_id: string;
  name: string;
  enabled: boolean;
  source_id: string | null;
  source_name: string | null;
}

type TabType = "auto" | "manual";

const AUTO_TYPES = ["rss", "wechat"];
const MANUAL_TYPES = ["url", "pdf", "image", "plaintext", "word", "epub"];
const FILE_TYPES = ["pdf", "image", "plaintext", "word", "epub"];
const FILE_ACCEPT: Record<string, string> = {
  pdf: ".pdf",
  image: ".jpg,.jpeg,.png,.gif,.webp",
  plaintext: ".txt,.md",
  word: ".doc,.docx",
  epub: ".epub,.mobi,.azw3",
};
const TYPE_LABELS: Record<string, string> = {
  rss: "RSS", wechat: "微信", url: "URL",
  pdf: "PDF", image: "图片", plaintext: "文本", word: "Word", epub: "电子书",
};
const TYPE_VARIANT: Record<string, string> = {
  rss: "bg-orange-100 text-orange-700 dark:bg-orange-900/30 dark:text-orange-400",
  wechat: "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400",
  url: "bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400",
  pdf: "bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400",
  image: "bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-400",
  plaintext: "bg-muted text-muted-foreground",
  word: "bg-indigo-100 text-indigo-700 dark:bg-indigo-900/30 dark:text-indigo-400",
  epub: "bg-teal-100 text-teal-700 dark:bg-teal-900/30 dark:text-teal-400",
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
    if (!confirm(`停用「${name}」？该来源将从列表隐藏，已入库文章继续保留。`)) return;
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
    if (AUTO_TYPES.includes(source.type)) setTab("auto");
    else setTab("manual");
  }

  const autoSources = sources.filter((s) => AUTO_TYPES.includes(s.type));
  const manualSources = sources.filter((s) => MANUAL_TYPES.includes(s.type));

  return (
    <main className="min-h-screen bg-background">
      <div className="max-w-4xl mx-auto px-6 py-8">
        <div className="flex items-center justify-between mb-6">
          <h1 className="text-2xl font-semibold">订阅源管理</h1>
          <Button
            size="sm"
            variant={showAdd ? "outline" : "default"}
            onClick={() => setShowAdd((v) => !v)}
          >
            {showAdd ? "取消" : "+ 新建 Source"}
          </Button>
        </div>

        {showAdd && <AddForm onCreated={handleCreated} />}

        <Tabs value={tab} onValueChange={(v) => setTab(v as TabType)}>
          <TabsList className="mb-4">
            <TabsTrigger value="auto">自动抓取型 ({autoSources.length})</TabsTrigger>
            <TabsTrigger value="manual">手动管理型 ({manualSources.length})</TabsTrigger>
          </TabsList>

          <TabsContent value="auto">
            <p className="text-xs text-muted-foreground mb-4">系统定时自动抓取，无需手动操作。</p>
            {loading ? (
              <p className="text-sm text-muted-foreground py-8 text-center">加载中…</p>
            ) : autoSources.length === 0 ? (
              <p className="text-sm text-muted-foreground py-8 text-center">暂无 source，点击"新建 Source"添加</p>
            ) : (
              <div className="space-y-3">
                {autoSources.map((s) => (
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
          </TabsContent>

          <TabsContent value="manual">
            <p className="text-xs text-muted-foreground mb-4">
              持久渠道 — 可随时向其中追加 URL 或上传文件，每次处理后知识入库。
            </p>
            {loading ? (
              <p className="text-sm text-muted-foreground py-8 text-center">加载中…</p>
            ) : manualSources.length === 0 ? (
              <p className="text-sm text-muted-foreground py-8 text-center">暂无 source，点击"新建 Source"添加</p>
            ) : (
              <div className="space-y-3">
                {manualSources.map((s) => (
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
          </TabsContent>
        </Tabs>
      </div>

      <UploadModal
        source={uploadTarget}
        onClose={() => setUploadTarget(null)}
        onDone={() => { setUploadTarget(null); setTimeout(loadSources, 2000); }}
      />

      <AddUrlModal
        source={addUrlTarget}
        onClose={() => setAddUrlTarget(null)}
        onDone={() => { setAddUrlTarget(null); setTimeout(loadSources, 2000); }}
      />
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
  const cfg = parseCfg(source);

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
    <Card>
      <CardContent className="p-4">
        <div className="flex items-start justify-between gap-4">
          <div className="flex items-center gap-3 min-w-0">
            <span className={cn(
              "text-xs font-medium px-2 py-0.5 rounded-full shrink-0",
              TYPE_VARIANT[source.type] || "bg-muted text-muted-foreground"
            )}>
              {TYPE_LABELS[source.type] || source.type}
            </span>
            <div className="min-w-0">
              <p className="font-medium truncate">{source.name}</p>
              {typeof cfg.url === "string" && (
                <p className="text-xs text-muted-foreground truncate mt-0.5">{cfg.url}</p>
              )}
              {source.type === "wechat" && typeof cfg.feed_id === "string" && (
                <p className="text-xs text-muted-foreground truncate mt-0.5">Feed ID：{cfg.feed_id}</p>
              )}
              {FILE_TYPES.includes(source.type) && (
                <p className="text-xs text-muted-foreground mt-0.5">
                  {uploadCount > 0 ? `${uploadCount} 次上传批次` : "暂无上传记录"}
                </p>
              )}
            </div>
          </div>

          <div className="flex items-center gap-2 shrink-0 flex-wrap justify-end">
            <Button
              variant="outline"
              size="sm"
              className="h-7 text-xs"
              onClick={onTogglePrimary}
              title={source.is_primary ? "点击切换为参考型" : "点击切换为主要型"}
            >
              {source.is_primary ? "主要" : "参考"}
            </Button>
            <span className="text-xs text-muted-foreground">{source.article_count} 篇</span>

            {(source.type === "rss" || source.type === "wechat") && (
              <Button variant="outline" size="sm" className="h-7 text-xs" onClick={onFetch} disabled={fetching}>
                {fetching ? "抓取中…" : "立即抓取"}
              </Button>
            )}

            {source.type === "wechat" && (
              <Button variant="outline" size="sm" className="h-7 text-xs text-green-700 border-green-200" asChild>
                <Link href={`/sources/${source.id}`}>查看配置</Link>
              </Button>
            )}

            {source.type === "url" && (
              <Button variant="outline" size="sm" className="h-7 text-xs" onClick={onAddUrl}>
                添加 URL
              </Button>
            )}

            {FILE_TYPES.includes(source.type) && (
              <Button variant="outline" size="sm" className="h-7 text-xs" onClick={onUpload}>
                上传文件
              </Button>
            )}

            <Button variant="outline" size="sm" className="h-7 text-xs text-destructive border-destructive/30 hover:bg-destructive/10" onClick={onDelete}>
              停用
            </Button>
          </div>
        </div>

        <div className="mt-2 flex items-center gap-4 text-xs text-muted-foreground">
          {source.fetch_mode === "subscription" && (
            <span>上次抓取：{fmtDate(source.last_fetched_at)}</span>
          )}
          <span>创建：{fmtDate(source.created_at)}</span>
          {source.default_doc_kind && (
            <span>默认类型：{DOC_KIND_LABELS[source.default_doc_kind] ?? source.default_doc_kind}</span>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

// ── Add Form ─────────────────────────────────────────────────────────────────

function AddForm({ onCreated }: { onCreated: (s: Source) => void }) {
  const [type, setType] = useState("rss");
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [defaultDocKind, setDefaultDocKind] = useState("");
  const [wechatSubscriptions, setWechatSubscriptions] = useState<WechatSubscription[]>([]);
  const [loadingWechat, setLoadingWechat] = useState(false);
  const [selectedFeedId, setSelectedFeedId] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (type !== "wechat") return;
    setError("");
    setLoadingWechat(true);
    fetch("/api/sources/wechat2rss/subscriptions", { credentials: "include" })
      .then(async (res) => {
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          throw new Error(body.detail || `加载公众号失败 (${res.status})`);
        }
        return res.json();
      })
      .then((data) => {
        const subscriptions = data.subscriptions || [];
        setWechatSubscriptions(subscriptions);
        const firstAvailable = subscriptions.find((s: WechatSubscription) => !s.enabled);
        setSelectedFeedId(firstAvailable?.feed_id || "");
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : "加载公众号失败"))
      .finally(() => setLoadingWechat(false));
  }, [type]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (type !== "wechat" && !name.trim()) { setError("请填写名称"); return; }
    if ((type === "rss" || type === "url") && !url.trim()) { setError("请填写 URL"); return; }
    if (type === "wechat" && !selectedFeedId) { setError("请选择公众号"); return; }

    setSaving(true);
    try {
      let res: Response;
      if (type === "wechat") {
        const selected = wechatSubscriptions.find((s) => s.feed_id === selectedFeedId);
        res = await fetch("/api/sources/wechat2rss/sources", {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            feed_id: selectedFeedId,
            name: selected?.name || selectedFeedId,
          }),
        });
      } else {
        const config: Record<string, string> = {};
        if (type === "rss" || type === "url") config.url = url.trim();

        res = await fetch("/api/sources", {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name: name.trim(),
            type,
            config,
            default_doc_kind: defaultDocKind || null,
          }),
        });
      }
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setError(body.detail || `创建失败 (${res.status})`);
        return;
      }
      onCreated(await res.json());
      setName(""); setUrl(""); setType("rss"); setSelectedFeedId(""); setDefaultDocKind("");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "请求失败");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card className="mb-6">
      <CardContent className="p-4">
        <form onSubmit={handleSubmit} className="space-y-3">
          <p className="text-sm font-medium">新建 Source 渠道</p>

          <div className="flex gap-3 items-center">
            <Label className="text-xs w-14 shrink-0">类型</Label>
            <select
              value={type}
              onChange={(e) => { setType(e.target.value); setUrl(""); setName(""); setError(""); }}
              className="text-sm border border-input rounded-md px-2 py-1.5 bg-background focus:outline-none focus:ring-1 focus:ring-ring"
            >
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
                <option value="epub">电子书 (EPUB/MOBI)</option>
              </optgroup>
            </select>
          </div>

          {type !== "wechat" && (
            <div className="flex gap-3 items-center">
              <Label className="text-xs w-14 shrink-0">名称</Label>
              <Input
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder={type === "pdf" ? "例：有趣的 Paper" : "例：科技早报"}
                className="flex-1 text-sm"
              />
            </div>
          )}

          {(type === "rss" || type === "url") && (
            <div className="flex gap-3 items-center">
              <Label className="text-xs w-14 shrink-0">{type === "rss" ? "Feed URL" : "初始 URL"}</Label>
              <Input
                type="url"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder={type === "rss" ? "https://example.com/feed.xml" : "https://example.com/article"}
                className="flex-1 text-sm"
              />
            </div>
          )}

          {FILE_TYPES.includes(type) && (
            <p className="text-xs text-muted-foreground pl-[4.5rem]">
              创建后点击 Source 卡片上的"上传文件"按钮，支持随时批量追加。
            </p>
          )}

          {type !== "wechat" && (
            <div className="flex gap-3 items-start">
              <Label className="text-xs w-14 shrink-0 pt-2">默认类型</Label>
              <div className="flex-1">
                <DocKindSelect
                  value={defaultDocKind}
                  onChange={setDefaultDocKind}
                  includeEmpty
                  emptyLabel="（不预设）"
                />
                <p className="text-xs text-muted-foreground mt-1">
                  source 级 doc_kind 默认值，会自动应用到本 source 下所有未单独标注的 item。
                </p>
              </div>
            </div>
          )}

          {type === "wechat" && (
            <div className="space-y-2 pl-[4.5rem]">
              {loadingWechat ? (
                <p className="text-xs text-muted-foreground">正在加载 Wechat2RSS 公众号列表…</p>
              ) : wechatSubscriptions.length === 0 ? (
                <p className="text-xs text-muted-foreground">
                  暂无可选公众号。请先进入 Wechat2RSS 管理界面添加订阅。
                </p>
              ) : (
                <div className="space-y-2">
                  <select
                    value={selectedFeedId}
                    onChange={(e) => setSelectedFeedId(e.target.value)}
                    className="w-full text-sm border border-input rounded-md px-2 py-1.5 bg-background focus:outline-none focus:ring-1 focus:ring-ring"
                  >
                    {!selectedFeedId && (
                      <option value="" disabled>
                        没有未追踪的公众号
                      </option>
                    )}
                    {wechatSubscriptions.map((s) => (
                      <option key={s.feed_id} value={s.feed_id} disabled={s.enabled}>
                        {s.name} ({s.feed_id}){s.enabled ? " - 已追踪" : ""}
                      </option>
                    ))}
                  </select>
                  <p className="text-xs text-muted-foreground">
                    公众号列表来自 Wechat2RSS，创建后按 RSS 订阅自动抓取。
                  </p>
                </div>
              )}
            </div>
          )}

          {error && <p className="text-xs text-destructive">{error}</p>}

          <div className="flex justify-end">
            <Button type="submit" size="sm" disabled={saving || loadingWechat || (type === "wechat" && !selectedFeedId)}>
              {saving ? "创建中…" : "创建渠道"}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

// ── Upload Files Modal ────────────────────────────────────────────────────────

function UploadModal({ source, onClose, onDone }: { source: Source | null; onClose: () => void; onDone: () => void }) {
  const [files, setFiles] = useState<FileList | null>(null);
  const [capturedAt, setCapturedAt] = useState("");
  const [effectiveAt, setEffectiveAt] = useState("");
  const [docKind, setDocKind] = useState("");
  const [uploading, setUploading] = useState(false);
  const [result, setResult] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  // 上传必填 doc_kind（设计要求）；source 已有 default 时该字段视为预填
  useEffect(() => {
    if (source?.default_doc_kind) setDocKind(source.default_doc_kind);
  }, [source]);

  async function handleUpload(e: React.FormEvent) {
    e.preventDefault();
    if (!files || files.length === 0 || !source) return;
    if (!docKind) { setResult("请选择文件类型 (doc_kind)"); return; }
    setUploading(true);
    try {
      const fd = new FormData();
      for (const f of Array.from(files)) fd.append("files", f);
      if (capturedAt) fd.append("captured_at", new Date(capturedAt).toISOString());
      if (effectiveAt) fd.append("effective_at", new Date(effectiveAt).toISOString());
      fd.append("doc_kind", docKind);
      const res = await fetch(`/api/sources/${source.id}/upload`, {
        method: "POST", credentials: "include", body: fd,
      });
      if (res.ok) {
        const data = await res.json();
        setResult(`已上传 ${data.files_saved} 个文件，ingestion-worker 正在处理中…`);
        setTimeout(onDone, 2000);
      } else {
        const err = await res.json().catch(() => ({}));
        setResult(`上传失败：${err.detail || "请重试"}`);
      }
    } finally {
      setUploading(false);
    }
  }

  return (
    <Dialog open={!!source} onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>向「{source?.name}」上传文件</DialogTitle>
        </DialogHeader>
        <form onSubmit={handleUpload} className="space-y-4">
          <input
            ref={fileRef}
            type="file"
            multiple
            accept={source ? (FILE_ACCEPT[source.type] || "*") : "*"}
            onChange={(e) => setFiles(e.target.files)}
            className="text-sm text-muted-foreground w-full"
          />
          <p className="text-xs text-muted-foreground">可一次选择多个文件，每个文件独立处理后进入知识库。</p>
          <div className="space-y-1.5">
            <Label htmlFor="upload-doc-kind">文件类型 (doc_kind) <span className="text-red-500">*</span></Label>
            <DocKindSelect
              id="upload-doc-kind"
              value={docKind}
              onChange={setDocKind}
              required
              includeEmpty
              emptyLabel="（请选择）"
            />
            <p className="text-xs text-muted-foreground">用于知识库内的结构化过滤；上传后可在节点元数据中逐条修改。</p>
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="captured-at">保存时间</Label>
              <Input
                id="captured-at"
                type="datetime-local"
                value={capturedAt}
                onChange={(e) => setCapturedAt(e.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="effective-at">内容时间</Label>
              <Input
                id="effective-at"
                type="datetime-local"
                value={effectiveAt}
                onChange={(e) => setEffectiveAt(e.target.value)}
              />
            </div>
          </div>
          {result && <p className="text-sm">{result}</p>}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" size="sm" onClick={onClose}>取消</Button>
            <Button type="submit" size="sm" disabled={uploading || !files?.length || !docKind}>
              {uploading ? "上传中…" : `上传${files?.length ? ` (${files.length})` : ""}`}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

// ── Add URL Modal ─────────────────────────────────────────────────────────────

function AddUrlModal({ source, onClose, onDone }: { source: Source | null; onClose: () => void; onDone: () => void }) {
  const [text, setText] = useState("");
  // 设计：URL 默认 news；source 配置了 default_doc_kind 时以其为初值
  const [docKind, setDocKind] = useState("news");
  const [saving, setSaving] = useState(false);
  const [result, setResult] = useState("");

  useEffect(() => {
    if (source?.default_doc_kind) setDocKind(source.default_doc_kind);
  }, [source]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!source) return;
    const urls = text.split("\n").map((u) => u.trim()).filter(Boolean);
    if (!urls.length) return;
    setSaving(true);
    try {
      const res = await fetch(`/api/sources/${source.id}/add-url`, {
        method: "POST", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ urls, doc_kind: docKind || null }),
      });
      if (res.ok) {
        const data = await res.json();
        setResult(`已加入队列 ${data.urls_queued} 条 URL，ingestion-worker 正在处理中…`);
        setTimeout(onDone, 2000);
      } else {
        const err = await res.json().catch(() => ({}));
        setResult(`操作失败：${err.detail || "请重试"}`);
      }
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={!!source} onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>向「{source?.name}」添加 URL</DialogTitle>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-4">
          <Textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={"https://example.com/article-1\nhttps://example.com/article-2"}
            rows={5}
            className="text-sm resize-none"
          />
          <p className="text-xs text-muted-foreground">每行一个 URL，可批量添加。</p>
          <div className="space-y-1.5">
            <Label htmlFor="add-url-doc-kind">文件类型 (doc_kind)</Label>
            <DocKindSelect
              id="add-url-doc-kind"
              value={docKind}
              onChange={setDocKind}
              includeEmpty
              emptyLabel="继承 source 默认"
            />
          </div>
          {result && <p className="text-sm">{result}</p>}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" size="sm" onClick={onClose}>取消</Button>
            <Button type="submit" size="sm" disabled={saving || !text.trim()}>
              {saving ? "处理中…" : "添加"}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
