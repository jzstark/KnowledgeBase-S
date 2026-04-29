"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import * as d3 from "d3";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

// ── 类型 ──────────────────────────────────────────────────────────────────────

interface KBNode {
  id: string;
  title: string;
  source_type: string;
  tags: string[];
  degree?: number;
  abstract?: string;
  object_type?: string;
  created_at?: string;
}

interface KBEdge {
  id: number;
  from_node_id: string;
  to_node_id: string;
  relation_type: string;
  weight: number;
}

interface NodeDetail extends KBNode {
  abstract: string;
  wiki_body?: string;
  edges: KBEdge[];
}

interface GraphData {
  nodes: KBNode[];
  edges: KBEdge[];
}

type SimNode = KBNode & d3.SimulationNodeDatum;
type SimLink = {
  source: string | SimNode;
  target: string | SimNode;
  relation_type: string;
  weight: number;
  id: number;
};

interface RawFile {
  name: string;
  rel_path: string;
  size: number;
  node_id: string | null;
}

interface MdFile {
  name: string;
  rel_path: string;
}

interface WikiSection {
  articles: MdFile[];
  entities: MdFile[];
  summaries: MdFile[];
  indices: MdFile[];
  index: boolean;
}

interface FileTree {
  raw: Record<string, RawFile[]>;
  wiki: WikiSection;
  config: MdFile[];
}

interface OpenFile {
  rel_path: string;
  name: string;
  writable: boolean;
}

// ── 常量 ──────────────────────────────────────────────────────────────────────

const EDGE_COLORS: Record<string, string> = {
  similar_to: "#60a5fa",
  background_of: "#f59e0b",
  extends: "#34d399",
  wikilink: "#a78bfa",
  mentions: "#a78bfa",
  contradicts: "#f87171",
  part_of: "#8b5cf6",
  summarizes: "#fbbf24",
  co_occurs_with: "#4ade80",
};

const OBJECT_TYPE_COLORS: Record<string, string> = {
  article: "#3b82f6",
  entity: "#10b981",
  summary: "#f59e0b",
  index: "#8b5cf6",
};

const SOURCE_TYPE_BADGE: Record<string, string> = {
  rss: "bg-orange-100 text-orange-700 dark:bg-orange-900/30 dark:text-orange-400",
  wechat: "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400",
  manual: "bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-400",
};

function nodeSymbolPath(d: SimNode, selected: boolean): string {
  const base = Math.min(8 + (d.degree || 0) * 1.5, 22);
  const r = selected ? base + 6 : base;
  const area = Math.PI * r * r;
  const type =
    d.object_type === "index" ? d3.symbolDiamond
    : d.object_type === "summary" ? d3.symbolTriangle
    : d3.symbolCircle;
  return d3.symbol().type(type).size(area)() ?? "";
}

// ── 可拖拽分隔线（保持不变） ──────────────────────────────────────────────────

function ResizeHandle({ direction, onMouseDown }: {
  direction: "h" | "v";
  onMouseDown: (e: React.MouseEvent) => void;
}) {
  return (
    <div
      onMouseDown={onMouseDown}
      className={cn(
        "shrink-0 bg-border hover:bg-primary/20 active:bg-primary/40 transition-colors z-10",
        direction === "h" ? "w-1 cursor-col-resize" : "h-1 cursor-row-resize"
      )}
    />
  );
}

function startDrag(
  e: React.MouseEvent,
  direction: "h" | "v",
  currentSize: number,
  sign: 1 | -1,
  setSize: (s: number) => void,
  min: number,
  max: number,
) {
  e.preventDefault();
  const origin = direction === "h" ? e.clientX : e.clientY;
  const cursor = direction === "h" ? "col-resize" : "row-resize";
  document.body.style.cursor = cursor;
  document.body.style.userSelect = "none";

  function onMove(ev: MouseEvent) {
    const delta = ((direction === "h" ? ev.clientX : ev.clientY) - origin) * sign;
    setSize(Math.max(min, Math.min(max, currentSize + delta)));
  }
  function onUp() {
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    document.removeEventListener("mousemove", onMove);
    document.removeEventListener("mouseup", onUp);
  }
  document.addEventListener("mousemove", onMove);
  document.addEventListener("mouseup", onUp);
}

// ── 节点卡片 ──────────────────────────────────────────────────────────────────

function NodeCard({
  node,
  selected,
  onClick,
}: {
  node: KBNode;
  selected: boolean;
  onClick: () => void;
}) {
  return (
    <button
      data-node-id={node.id}
      onClick={onClick}
      className={cn(
        "w-full text-left rounded-lg border p-3 transition-colors",
        selected
          ? "border-primary bg-accent"
          : "border-border bg-card hover:border-muted-foreground/40"
      )}
    >
      <div className="flex items-center gap-2 mb-1">
        <span className={cn(
          "text-xs px-1.5 py-0.5 rounded font-medium",
          SOURCE_TYPE_BADGE[node.source_type] || "bg-muted text-muted-foreground"
        )}>
          {node.source_type || "unknown"}
        </span>
        {node.created_at && (
          <span className="text-xs text-muted-foreground">
            {new Date(node.created_at).toLocaleDateString("zh-CN", {
              month: "2-digit",
              day: "2-digit",
            })}
          </span>
        )}
      </div>
      <p className="text-sm font-medium line-clamp-2 mb-1">
        {node.title || node.id}
      </p>
      {node.abstract && (
        <p className="text-xs text-muted-foreground line-clamp-2">{node.abstract}</p>
      )}
      {(node.tags || []).length > 0 && (
        <div className="flex flex-wrap gap-1 mt-1.5">
          {(node.tags || []).slice(0, 3).map((t) => (
            <Badge key={t} variant="secondary" className="text-xs px-1.5 py-0">
              {t}
            </Badge>
          ))}
          {(node.tags || []).length > 3 && (
            <span className="text-xs text-muted-foreground">+{node.tags.length - 3}</span>
          )}
        </div>
      )}
    </button>
  );
}

// ── 列表面板 ──────────────────────────────────────────────────────────────────

function ListPanel({
  onSelectNode,
  selectedId,
  refreshToken,
}: {
  onSelectNode: (id: string) => void;
  selectedId?: string;
  refreshToken?: number;
}) {
  const [nodes, setNodes] = useState<KBNode[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState("");
  const [tagFilter, setTagFilter] = useState("");
  const [offset, setOffset] = useState(0);
  const LIMIT = 50;
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  const loadNodes = useCallback(async (searchQ: string, tagF: string, off: number) => {
    setLoading(true);
    try {
      const params = new URLSearchParams({ limit: String(LIMIT), offset: String(off) });
      if (searchQ.trim()) params.set("q", searchQ.trim());
      if (tagF.trim()) params.set("tags", tagF.trim());
      const r = await fetch(`/api/kb/nodes?${params}`, { credentials: "include" });
      if (r.ok) {
        const data = await r.json();
        setNodes(data.nodes || []);
        setTotal(data.total || 0);
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadNodes(q, tagFilter, offset);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [offset]);

  useEffect(() => { loadNodes("", "", 0); }, [loadNodes]);

  useEffect(() => {
    if (refreshToken === undefined || refreshToken === 0) return;
    loadNodes(q, tagFilter, offset);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshToken]);

  useEffect(() => {
    if (!selectedId || !containerRef.current) return;
    const el = containerRef.current.querySelector(`[data-node-id="${selectedId}"]`);
    el?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [selectedId]);

  function handleQChange(val: string) {
    setQ(val);
    setOffset(0);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => loadNodes(val, tagFilter, 0), 500);
  }

  function handleTagChange(val: string) {
    setTagFilter(val);
    setOffset(0);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => loadNodes(q, val, 0), 500);
  }

  return (
    <div className="flex flex-col h-full">
      <div className="p-3 border-b border-border space-y-2 shrink-0">
        <Input
          type="text"
          value={q}
          onChange={(e) => handleQChange(e.target.value)}
          placeholder="搜索标题或 abstract…"
          className="text-sm h-8"
        />
        <div className="flex items-center gap-2">
          <Input
            type="text"
            value={tagFilter}
            onChange={(e) => handleTagChange(e.target.value)}
            placeholder="按标签过滤"
            className="flex-1 text-sm h-8"
          />
          <span className="text-xs text-muted-foreground shrink-0">{total}</span>
        </div>
      </div>

      <div ref={containerRef} className="flex-1 overflow-auto p-3 space-y-2">
        {loading ? (
          <p className="text-sm text-muted-foreground">加载中…</p>
        ) : nodes.length === 0 ? (
          <p className="text-sm text-muted-foreground">暂无节点</p>
        ) : (
          nodes.map((n) => (
            <NodeCard
              key={n.id}
              node={n}
              selected={n.id === selectedId}
              onClick={() => onSelectNode(n.id)}
            />
          ))
        )}
      </div>

      {total > LIMIT && (
        <div className="px-3 py-2 border-t border-border flex items-center justify-between shrink-0">
          <Button
            variant="outline"
            size="sm"
            className="h-7 text-xs"
            onClick={() => setOffset((o) => Math.max(0, o - LIMIT))}
            disabled={offset === 0}
          >
            上一页
          </Button>
          <span className="text-xs text-muted-foreground">
            {Math.floor(offset / LIMIT) + 1} / {Math.ceil(total / LIMIT)}
          </span>
          <Button
            variant="outline"
            size="sm"
            className="h-7 text-xs"
            onClick={() => setOffset((o) => o + LIMIT)}
            disabled={offset + LIMIT >= total}
          >
            下一页
          </Button>
        </div>
      )}
    </div>
  );
}

// ── 资源管理器面板 ────────────────────────────────────────────────────────────

const RAW_TYPE_LABELS: Record<string, string> = {
  pdf: "PDF",
  image: "图片",
  wechat: "微信",
  plaintext: "文本",
  word: "Word",
};

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

const WIKI_SECTION_LABELS: Record<string, string> = {
  articles: "文章",
  entities: "实体",
  summaries: "摘要",
  indices: "目录",
};

function ExplorerPanel({
  onOpenFile,
  onSelectNode,
  onDeleted,
  selectedNodeId,
}: {
  onOpenFile: (f: OpenFile) => void;
  onSelectNode: (id: string) => void;
  onDeleted: (deletedNodeId?: string) => void;
  selectedNodeId?: string | null;
}) {
  const [tree, setTree] = useState<FileTree | null>(null);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState<Set<string>>(
    new Set(["raw", "wiki", "wiki-articles", "wiki-entities", "wiki-indices", "config"])
  );
  const [deleting, setDeleting] = useState<string | null>(null);

  async function loadTree() {
    setLoading(true);
    try {
      const r = await fetch("/api/files/tree", { credentials: "include" });
      if (r.ok) setTree(await r.json());
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { loadTree(); }, []);

  function toggle(key: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });
  }

  async function handleDeleteNode(nodeId: string, name: string) {
    if (!confirm(`确认删除「${name}」及其知识节点？原始文件也会一并删除，此操作不可撤销。`)) return;
    setDeleting(nodeId);
    try {
      const r = await fetch(`/api/kb/nodes/${nodeId}`, {
        method: "DELETE",
        credentials: "include",
      });
      if (r.ok) { await loadTree(); onDeleted(nodeId); }
    } finally {
      setDeleting(null);
    }
  }

  async function handleDeleteWiki(nodeId: string, name: string) {
    if (!confirm(`确认删除「${name}」？对应的原始文件和知识节点也会一并删除，此操作不可撤销。`)) return;
    setDeleting(nodeId);
    try {
      const r = await fetch(`/api/kb/nodes/${nodeId}`, {
        method: "DELETE",
        credentials: "include",
      });
      if (r.ok) { await loadTree(); onDeleted(nodeId); }
    } finally {
      setDeleting(null);
    }
  }

  async function handleDeleteConfig(relPath: string, name: string) {
    if (!confirm(`确认删除配置文件「${name}」？此操作不可撤销。`)) return;
    setDeleting(relPath);
    try {
      const r = await fetch(
        `/api/files/content?rel_path=${encodeURIComponent(relPath)}`,
        { method: "DELETE", credentials: "include" },
      );
      if (r.ok) { await loadTree(); onDeleted(); }
    } finally {
      setDeleting(null);
    }
  }

  if (loading) return <div className="p-4 text-sm text-muted-foreground">加载中…</div>;
  if (!tree) return <div className="p-4 text-sm text-destructive">加载失败</div>;

  const chevron = (key: string) => (
    <span className="text-muted-foreground/50 text-xs mr-1">{expanded.has(key) ? "▾" : "▸"}</span>
  );

  return (
    <div className="h-full overflow-auto p-2 text-sm select-none">
      {/* 原始文件 */}
      <div>
        <button
          onClick={() => toggle("raw")}
          className="flex items-center w-full text-left font-medium text-foreground/80 py-1 hover:text-foreground"
        >
          {chevron("raw")} 原始文件
        </button>
        {expanded.has("raw") && (
          <div className="ml-3">
            {Object.entries(tree.raw).map(([type, files]) => {
              if (files.length === 0) return null;
              const key = `raw-${type}`;
              return (
                <div key={type}>
                  <button
                    onClick={() => toggle(key)}
                    className="flex items-center w-full text-left text-muted-foreground py-0.5 hover:text-foreground"
                  >
                    {chevron(key)}
                    <span className="text-xs">{RAW_TYPE_LABELS[type] ?? type}</span>
                    <span className="ml-1 text-xs text-muted-foreground/40">({files.length})</span>
                  </button>
                  {expanded.has(key) && (
                    <div className="ml-3 space-y-0.5">
                      {files.map((f) => (
                        <div key={f.rel_path} className="flex items-center gap-1 group py-0.5">
                          <span className="flex-1 text-xs text-muted-foreground truncate" title={f.name}>
                            {f.name}
                          </span>
                          <span className="text-xs text-muted-foreground/40 shrink-0">{formatBytes(f.size)}</span>
                          {f.node_id && (
                            <button
                              onClick={() => handleDeleteNode(f.node_id!, f.name)}
                              disabled={deleting === f.node_id}
                              className="shrink-0 text-muted-foreground/30 hover:text-destructive transition-colors disabled:opacity-40 ml-1"
                              title="删除节点"
                            >
                              {deleting === f.node_id ? "…" : "✕"}
                            </button>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>

      <div className="border-t border-border my-2" />

      {/* Wiki */}
      <div>
        <button
          onClick={() => toggle("wiki")}
          className="flex items-center w-full text-left font-medium text-foreground/80 py-1 hover:text-foreground"
        >
          {chevron("wiki")} Wiki
          <span className="ml-1 text-xs text-muted-foreground/40">
            ({(tree.wiki.articles?.length ?? 0) + (tree.wiki.entities?.length ?? 0) + (tree.wiki.summaries?.length ?? 0) + (tree.wiki.indices?.length ?? 0)})
          </span>
        </button>
        {expanded.has("wiki") && (
          <div className="ml-3">
            {(["articles", "entities", "summaries", "indices"] as const).map((subdir) => {
              const files = tree.wiki[subdir] ?? [];
              if (files.length === 0) return null;
              const key = `wiki-${subdir}`;
              return (
                <div key={subdir}>
                  <button
                    onClick={() => toggle(key)}
                    className="flex items-center w-full text-left text-muted-foreground py-0.5 hover:text-foreground"
                  >
                    {chevron(key)}
                    <span className="text-xs">{WIKI_SECTION_LABELS[subdir]}</span>
                    <span className="ml-1 text-xs text-muted-foreground/40">({files.length})</span>
                  </button>
                  {expanded.has(key) && (
                    <div className="ml-3 space-y-0.5">
                      {files.map((f: MdFile) => {
                        const nodeId = f.name.replace(/\.md$/, "");
                        const isSelected = nodeId === selectedNodeId;
                        return (
                          <div key={f.rel_path} className="flex items-center group gap-1 py-0.5">
                            <button
                              onClick={() => {
                                onOpenFile({ rel_path: f.rel_path, name: f.name, writable: true });
                                onSelectNode(nodeId);
                              }}
                              className={cn(
                                "flex-1 min-w-0 text-left text-xs truncate rounded px-1 transition-colors",
                                isSelected ? "bg-accent text-primary" : "text-blue-600 dark:text-blue-400 hover:text-blue-800"
                              )}
                              title={f.name}
                            >
                              📄 {f.name}
                            </button>
                            <button
                              onClick={() => handleDeleteWiki(nodeId, f.name)}
                              disabled={deleting === nodeId}
                              className="shrink-0 text-muted-foreground/30 hover:text-destructive transition-colors disabled:opacity-40 opacity-0 group-hover:opacity-100"
                              title="删除节点"
                            >
                              {deleting === nodeId ? "…" : "✕"}
                            </button>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>

      <div className="border-t border-border my-2" />

      {/* 配置模板 */}
      <div>
        <button
          onClick={() => toggle("config")}
          className="flex items-center w-full text-left font-medium text-foreground/80 py-1 hover:text-foreground"
        >
          {chevron("config")} 配置模板
          <span className="ml-1 text-xs text-muted-foreground/40">({tree.config.length})</span>
        </button>
        {expanded.has("config") && (
          <div className="ml-3 space-y-0.5">
            {tree.config.map((f) => (
              <div key={f.rel_path} className="flex items-center group gap-1 py-0.5">
                <button
                  onClick={() => onOpenFile({ rel_path: f.rel_path, name: f.name, writable: true })}
                  className="flex-1 min-w-0 text-left text-xs text-blue-600 dark:text-blue-400 hover:text-blue-800 truncate rounded px-1"
                  title={f.name}
                >
                  📄 {f.name}
                </button>
                <button
                  onClick={() => handleDeleteConfig(f.rel_path, f.name)}
                  disabled={deleting === f.rel_path}
                  className="shrink-0 text-muted-foreground/30 hover:text-destructive transition-colors disabled:opacity-40 opacity-0 group-hover:opacity-100"
                  title="删除配置文件"
                >
                  {deleting === f.rel_path ? "…" : "✕"}
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ── 文件内容面板 ──────────────────────────────────────────────────────────────

function FilePanel({ file, onClose }: { file: OpenFile; onClose: () => void }) {
  const [content, setContent] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState("");

  useEffect(() => {
    setLoading(true);
    setEditing(false);
    setSaveMsg("");
    fetch(`/api/files/content?rel_path=${encodeURIComponent(file.rel_path)}`, {
      credentials: "include",
    })
      .then((r) => r.json())
      .then((d) => setContent(d.content ?? ""))
      .catch(() => setContent(null))
      .finally(() => setLoading(false));
  }, [file.rel_path]);

  async function handleSave() {
    setSaving(true);
    try {
      const r = await fetch("/api/files/content", {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rel_path: file.rel_path, content: draft }),
      });
      if (r.ok) {
        setContent(draft);
        setEditing(false);
        setSaveMsg("已保存");
        setTimeout(() => setSaveMsg(""), 2000);
      }
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-2 px-4 py-2 border-b border-border shrink-0 bg-muted/30">
        <span className="flex-1 text-xs font-medium truncate" title={file.name}>
          {file.name}
        </span>
        {saveMsg && <span className="text-xs text-green-500">{saveMsg}</span>}
        {file.writable && !editing && (
          <Button
            variant="outline"
            size="sm"
            className="h-6 text-xs"
            onClick={() => { setDraft(content ?? ""); setEditing(true); }}
          >
            编辑
          </Button>
        )}
        {editing && (
          <>
            <Button size="sm" className="h-6 text-xs" onClick={handleSave} disabled={saving}>
              {saving ? "保存中…" : "保存"}
            </Button>
            <Button variant="ghost" size="sm" className="h-6 text-xs" onClick={() => setEditing(false)}>
              取消
            </Button>
          </>
        )}
        <button onClick={onClose} className="text-muted-foreground hover:text-foreground text-lg leading-none ml-1">
          ×
        </button>
      </div>
      <div className="flex-1 overflow-auto">
        {loading ? (
          <div className="p-4 text-sm text-muted-foreground">加载中…</div>
        ) : content === null ? (
          <div className="p-4 text-sm text-destructive">加载失败</div>
        ) : editing ? (
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            className="w-full h-full p-4 text-xs font-mono bg-background text-foreground resize-none outline-none"
            spellCheck={false}
          />
        ) : (
          <pre className="p-4 text-xs text-muted-foreground whitespace-pre-wrap font-mono leading-relaxed">
            {content}
          </pre>
        )}
      </div>
    </div>
  );
}

// ── Wiki 内容面板 ─────────────────────────────────────────────────────────────

function WikiPanel({
  detail,
  detailLoading,
  openFile,
  onCloseFile,
  onOpenFile,
  onSummaryCreated,
}: {
  detail: NodeDetail | null;
  detailLoading: boolean;
  openFile: OpenFile | null;
  onCloseFile: () => void;
  onOpenFile: (f: OpenFile) => void;
  onSummaryCreated?: () => void;
}) {
  const [sumFormOpen, setSumFormOpen] = useState(false);
  const [perspInput, setPerspInput] = useState("");
  const [sumLoading, setSumLoading] = useState(false);
  const [sumMsg, setSumMsg] = useState("");

  async function handleCreateSummary() {
    if (!detail) return;
    setSumLoading(true);
    setSumMsg("");
    try {
      const r = await fetch(`/api/kb/nodes/${detail.id}/create_summary`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ perspective: perspInput.trim() || null }),
      });
      if (r.ok) {
        const data = await r.json();
        setSumMsg(`已生成：${data.title}`);
        setPerspInput("");
        setSumFormOpen(false);
        onSummaryCreated?.();
      } else {
        const err = await r.json().catch(() => ({}));
        setSumMsg(`生成失败：${err.detail || r.status}`);
      }
    } catch {
      setSumMsg("网络错误");
    } finally {
      setSumLoading(false);
    }
  }

  if (openFile) return <FilePanel file={openFile} onClose={onCloseFile} />;

  if (detailLoading) {
    return (
      <div className="flex items-center justify-center h-full text-sm text-muted-foreground">加载中…</div>
    );
  }

  if (!detail) {
    return (
      <div className="flex items-center justify-center h-full text-sm text-muted-foreground">
        从右侧列表或图谱中选择一个节点
      </div>
    );
  }

  const objectType = detail.object_type || "article";
  const wikiSubdir = objectType === "entity" ? "entities"
    : objectType === "summary" ? "summaries"
    : objectType === "index" ? "indices"
    : "articles";
  const canCreateSummary = objectType === "article" || objectType === "index";

  return (
    <div className="flex flex-col h-full">
      {/* 节点元数据头部 */}
      <div className="px-5 py-3 border-b border-border bg-muted/20 shrink-0">
        <div className="flex items-start gap-3">
          <div className="flex-1 min-w-0">
            <h2 className="text-sm font-semibold leading-snug">
              {detail.title || detail.id}
            </h2>
            <div className="flex items-center gap-2 mt-1 flex-wrap">
              <span
                className="text-xs px-1.5 py-0.5 rounded font-medium text-white"
                style={{ background: OBJECT_TYPE_COLORS[objectType] || "#6b7280" }}
              >
                {objectType}
              </span>
              <span className={cn(
                "text-xs px-1.5 py-0.5 rounded font-medium",
                SOURCE_TYPE_BADGE[detail.source_type] || "bg-muted text-muted-foreground"
              )}>
                {detail.source_type || "unknown"}
              </span>
              {detail.created_at && (
                <span className="text-xs text-muted-foreground">
                  {new Date(detail.created_at).toLocaleDateString("zh-CN")}
                </span>
              )}
              {(detail.tags || []).map((t) => (
                <Badge key={t} variant="secondary" className="text-xs px-1.5 py-0">
                  {t}
                </Badge>
              ))}
            </div>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            {canCreateSummary && (
              <Button
                variant="outline"
                size="sm"
                className="h-7 text-xs"
                onClick={() => { setSumFormOpen((v) => !v); setSumMsg(""); }}
              >
                ＋ 摘要
              </Button>
            )}
            <Button
              variant="outline"
              size="sm"
              className="h-7 text-xs"
              onClick={() =>
                onOpenFile({
                  rel_path: `wiki/${wikiSubdir}/${detail.id}.md`,
                  name: `${detail.title || detail.id}.md`,
                  writable: true,
                })
              }
            >
              在编辑器中打开
            </Button>
          </div>
        </div>
        {detail.abstract && (
          <p className="text-xs text-muted-foreground mt-2 leading-relaxed">{detail.abstract}</p>
        )}

        {/* 摘要生成内联表单 */}
        {sumFormOpen && (
          <div className="mt-3 pt-3 border-t border-border flex flex-col gap-2">
            <Input
              type="text"
              value={perspInput}
              onChange={(e) => setPerspInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") handleCreateSummary(); }}
              placeholder='视角（可选，如"人物关系"、"技术架构"）'
              className="text-xs h-7"
              disabled={sumLoading}
            />
            <div className="flex items-center gap-2">
              <Button size="sm" className="h-6 text-xs" onClick={handleCreateSummary} disabled={sumLoading}>
                {sumLoading ? "生成中…" : "生成"}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                className="h-6 text-xs text-muted-foreground"
                onClick={() => { setSumFormOpen(false); setSumMsg(""); }}
              >
                取消
              </Button>
              {sumMsg && (
                <span className={cn(
                  "text-xs",
                  sumMsg.startsWith("生成失败") || sumMsg.startsWith("网络") ? "text-destructive" : "text-green-600"
                )}>
                  {sumMsg}
                </span>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Wiki 正文 */}
      <div className="flex-1 overflow-auto p-5">
        {detail.wiki_body ? (
          <pre className="text-xs text-muted-foreground whitespace-pre-wrap font-mono leading-relaxed">
            {detail.wiki_body}
          </pre>
        ) : (
          <p className="text-sm text-muted-foreground">暂无 Wiki 内容</p>
        )}

        {(detail.edges || []).length > 0 && (
          <div className="mt-6 pt-4 border-t border-border">
            <p className="text-xs font-medium text-muted-foreground mb-2">
              关联 {detail.edges.length} 条边
            </p>
            <div className="space-y-1">
              {detail.edges.slice(0, 10).map((e) => (
                <div key={e.id} className="flex items-center gap-2 text-xs text-muted-foreground">
                  <span
                    className="w-1.5 h-1.5 rounded-full shrink-0"
                    style={{ background: EDGE_COLORS[e.relation_type] || "#d1d5db" }}
                  />
                  <span>{e.relation_type}</span>
                  <span className="text-muted-foreground/40">{e.from_node_id === detail.id ? "→" : "←"}</span>
                  <span className="text-muted-foreground/60">{(e.weight * 100).toFixed(0)}%</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── 主页面 ────────────────────────────────────────────────────────────────────

export default function KnowledgePage() {
  const [maintenanceMsg, setMaintenanceMsg] = useState("");

  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);

  const [detail, setDetail] = useState<NodeDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const [graphData, setGraphData] = useState<GraphData | null>(null);
  const [graphLoading, setGraphLoading] = useState(false);
  const svgRef = useRef<SVGSVGElement>(null);
  const zoomRef = useRef<d3.ZoomBehavior<SVGSVGElement, unknown> | null>(null);
  const simNodesRef = useRef<SimNode[]>([]);

  const [openFile, setOpenFile] = useState<OpenFile | null>(null);
  const [explorerKey, setExplorerKey] = useState(0);
  const [listRefreshToken, setListRefreshToken] = useState(0);

  const [visibleNodeTypes, setVisibleNodeTypes] = useState<Set<string>>(
    () => new Set(["article", "entity", "summary", "index"]),
  );
  const [visibleEdgeTypes, setVisibleEdgeTypes] = useState<Set<string>>(
    () => new Set(Object.keys(EDGE_COLORS)),
  );
  const [filterOpen, setFilterOpen] = useState(false);

  const [leftWidth, setLeftWidth] = useState(208);
  const [rightWidth, setRightWidth] = useState(288);
  const [graphHeight, setGraphHeight] = useState(256);

  useEffect(() => { loadGraph(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") clearSelection();
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  async function loadGraph() {
    setGraphLoading(true);
    try {
      const r = await fetch("/api/kb/graph/all", { credentials: "include" });
      if (r.ok) setGraphData(await r.json());
    } finally {
      setGraphLoading(false);
    }
  }

  function toggleNodeType(t: string) {
    setVisibleNodeTypes((prev) => {
      const next = new Set(prev);
      next.has(t) ? next.delete(t) : next.add(t);
      return next;
    });
  }

  function toggleEdgeType(t: string) {
    setVisibleEdgeTypes((prev) => {
      const next = new Set(prev);
      next.has(t) ? next.delete(t) : next.add(t);
      return next;
    });
  }

  function clearSelection() {
    setSelectedNodeId(null);
    setDetail(null);
    setOpenFile(null);
  }

  async function selectNode(nodeId: string) {
    if (selectedNodeId === nodeId) return;
    setSelectedNodeId(nodeId);
    setOpenFile(null);
    setDetailLoading(true);
    try {
      const r = await fetch(`/api/kb/node/${nodeId}`, { credentials: "include" });
      if (r.ok) setDetail(await r.json());
    } finally {
      setDetailLoading(false);
    }
  }

  useEffect(() => {
    if (!graphData || !svgRef.current) return;
    renderGraph(graphData);
  }, [graphData]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!svgRef.current) return;

    const neighborIds = new Set<string>();
    if (selectedNodeId && graphData) {
      graphData.edges.forEach((e) => {
        if (e.from_node_id === selectedNodeId) neighborIds.add(e.to_node_id);
        if (e.to_node_id === selectedNodeId) neighborIds.add(e.from_node_id);
      });
    }

    const svgSel = d3.select(svgRef.current);

    svgSel
      .selectAll<SVGPathElement, SimNode>("path")
      .attr("d", (d) => nodeSymbolPath(d, d.id === selectedNodeId))
      .attr("fill", (d) => {
        if (d.id === selectedNodeId) return "#ef4444";
        if (neighborIds.has(d.id)) return "#f97316";
        return OBJECT_TYPE_COLORS[d.object_type || "article"] || "#3b82f6";
      })
      .attr("fill-opacity", (d) => {
        if (!selectedNodeId) return 0.85;
        if (d.id === selectedNodeId || neighborIds.has(d.id)) return 1;
        return 0.18;
      })
      .attr("stroke", "#fff")
      .attr("stroke-width", (d) => d.id === selectedNodeId ? 3 : 1.5);

    svgSel
      .selectAll<SVGLineElement, SimLink>("line")
      .attr("stroke-opacity", (d) => {
        if (!selectedNodeId) return 0.6;
        const srcId =
          typeof d.source === "string" ? d.source : (d.source as SimNode).id;
        const tgtId =
          typeof d.target === "string" ? d.target : (d.target as SimNode).id;
        return srcId === selectedNodeId || tgtId === selectedNodeId ? 0.9 : 0.08;
      });

    if (selectedNodeId && zoomRef.current) {
      const target = simNodesRef.current.find((n) => n.id === selectedNodeId);
      if (target && target.x != null && target.y != null) {
        const svgEl = svgRef.current;
        const w = svgEl.clientWidth || 600;
        const h = svgEl.clientHeight || 256;
        const scale = 2;
        const tx = w / 2 - target.x * scale;
        const ty = h / 2 - target.y * scale;
        svgSel
          .transition()
          .duration(600)
          .call(
            zoomRef.current.transform,
            d3.zoomIdentity.translate(tx, ty).scale(scale),
          );
      }
    }
  }, [selectedNodeId, graphData]);

  useEffect(() => {
    if (!svgRef.current || !graphData) return;
    const nodeTypeMap = new Map(
      graphData.nodes.map((n) => [n.id, n.object_type || "article"]),
    );
    const svgSel = d3.select(svgRef.current);
    svgSel
      .selectAll<SVGPathElement, SimNode>("path")
      .style("display", (d) =>
        !d || visibleNodeTypes.has(d.object_type || "article") ? null : "none",
      );
    svgSel
      .selectAll<SVGTextElement, SimNode>("text")
      .style("display", (d) =>
        !d || visibleNodeTypes.has(d.object_type || "article") ? null : "none",
      );
    svgSel.selectAll<SVGLineElement, SimLink>("line").style("display", (d) => {
      const srcId =
        typeof d.source === "string" ? d.source : (d.source as SimNode).id;
      const tgtId =
        typeof d.target === "string" ? d.target : (d.target as SimNode).id;
      if (!visibleNodeTypes.has(nodeTypeMap.get(srcId) ?? "article")) return "none";
      if (!visibleNodeTypes.has(nodeTypeMap.get(tgtId) ?? "article")) return "none";
      if (!visibleEdgeTypes.has(d.relation_type)) return "none";
      return null;
    });
  }, [visibleNodeTypes, visibleEdgeTypes, graphData]);

  function renderGraph(data: GraphData) {
    const svgEl = svgRef.current!;
    const svg = d3.select(svgEl);
    svg.selectAll("*").remove();

    const width = svgEl.clientWidth || 600;
    const height = svgEl.clientHeight || 256;

    const g = svg.append("g");

    const zoom = d3
      .zoom<SVGSVGElement, unknown>()
      .scaleExtent([0.1, 6])
      .on("zoom", (e) => g.attr("transform", e.transform));
    svg.call(zoom);
    zoomRef.current = zoom;

    if (data.nodes.length === 0) {
      svg
        .append("text")
        .attr("x", width / 2).attr("y", height / 2)
        .attr("text-anchor", "middle")
        .attr("fill", "#9ca3af").attr("font-size", 14)
        .text("暂无知识节点");
      return;
    }

    const links: SimLink[] = data.edges.map((e) => ({
      source: e.from_node_id,
      target: e.to_node_id,
      relation_type: e.relation_type,
      weight: e.weight,
      id: e.id,
    }));

    const nodes: SimNode[] = data.nodes.map((n) => ({ ...n }));
    simNodesRef.current = nodes;

    const link = g
      .append("g")
      .selectAll<SVGLineElement, SimLink>("line")
      .data(links).enter().append("line")
      .attr("stroke", (d) => EDGE_COLORS[d.relation_type] || "#d1d5db")
      .attr("stroke-width", 1.5)
      .attr("stroke-opacity", 0.6);

    const node = g
      .append("g")
      .selectAll<SVGPathElement, SimNode>("path")
      .data(nodes).enter().append("path")
      .attr("d", (d) => nodeSymbolPath(d, false))
      .attr("fill", (d) => OBJECT_TYPE_COLORS[d.object_type || "article"] || "#3b82f6")
      .attr("fill-opacity", 0.85)
      .attr("stroke", "#fff")
      .attr("stroke-width", 1.5)
      .attr("cursor", "pointer")
      .on("click", (_, d) => selectNode(d.id))
      .call(
        d3
          .drag<SVGPathElement, SimNode>()
          .on("start", (e, d) => {
            if (!e.active) simulation.alphaTarget(0.3).restart();
            d.fx = d.x; d.fy = d.y;
          })
          .on("drag", (e, d) => { d.fx = e.x; d.fy = e.y; })
          .on("end", (e, d) => {
            if (!e.active) simulation.alphaTarget(0);
            d.fx = null; d.fy = null;
          }),
      );

    const label = g
      .append("g")
      .selectAll<SVGTextElement, SimNode>("text")
      .data(nodes).enter().append("text")
      .text((d) => (d.title || d.id).slice(0, 12))
      .attr("font-size", 9)
      .attr("fill", "#374151")
      .attr("pointer-events", "none");

    const simulation = d3
      .forceSimulation<SimNode>(nodes)
      .force("link", d3.forceLink<SimNode, SimLink>(links).id((d) => d.id).distance(60))
      .force("charge", d3.forceManyBody<SimNode>().strength(-150))
      .force("center", d3.forceCenter(width / 2, height / 2))
      .alphaDecay(0.03)
      .on("tick", () => {
        link
          .attr("x1", (d) => (d.source as SimNode).x ?? 0)
          .attr("y1", (d) => (d.source as SimNode).y ?? 0)
          .attr("x2", (d) => (d.target as SimNode).x ?? 0)
          .attr("y2", (d) => (d.target as SimNode).y ?? 0);
        node.attr("transform", (d) => `translate(${d.x ?? 0},${d.y ?? 0})`);
        label.attr("x", (d) => (d.x ?? 0) + 14).attr("y", (d) => (d.y ?? 0) + 4);
      });

    return () => simulation.stop();
  }

  async function handleMaintenance() {
    setMaintenanceMsg("维护中…");
    try {
      const r = await fetch("/api/kb/maintenance/run", { method: "POST", credentials: "include" });
      setMaintenanceMsg(r.ok ? "维护已触发，后台运行中" : "触发失败，请重试");
    } catch {
      setMaintenanceMsg("网络错误");
    }
    setTimeout(() => setMaintenanceMsg(""), 4000);
  }

  return (
    <main className="h-screen bg-background flex flex-col">
      {/* 顶部工具栏 */}
      <header className="bg-card border-b border-border px-5 py-2.5 flex items-center justify-between shrink-0">
        <h1 className="text-base font-semibold">知识库</h1>
        <Button variant="outline" size="sm" onClick={handleMaintenance}>
          {maintenanceMsg || "立即运行维护"}
        </Button>
      </header>

      {/* 四面板主体 */}
      <div className="flex flex-1 min-h-0">

        {/* 左：资源管理器 */}
        <div style={{ width: leftWidth }} className="shrink-0 border-r border-border bg-card overflow-hidden flex flex-col">
          <div className="px-3 py-2 border-b border-border shrink-0">
            <span className="text-xs font-medium text-muted-foreground uppercase tracking-wide">资源管理器</span>
          </div>
          <div className="flex-1 overflow-hidden">
            <ExplorerPanel
              key={explorerKey}
              selectedNodeId={selectedNodeId}
              onOpenFile={(f) => setOpenFile(f)}
              onSelectNode={(id) => selectNode(id)}
              onDeleted={(deletedNodeId) => {
                setExplorerKey((k) => k + 1);
                if (deletedNodeId) {
                  clearSelection();
                  setListRefreshToken((t) => t + 1);
                  loadGraph();
                }
              }}
            />
          </div>
        </div>

        <ResizeHandle
          direction="h"
          onMouseDown={(e) => startDrag(e, "h", leftWidth, 1, setLeftWidth, 140, 480)}
        />

        {/* 中：Wiki 查看器（上）+ 图谱（下） */}
        <div className="flex-1 min-w-0 flex flex-col">
          <div className="flex-1 min-h-0 bg-card overflow-hidden">
            <WikiPanel
              detail={detail}
              detailLoading={detailLoading}
              openFile={openFile}
              onCloseFile={() => setOpenFile(null)}
              onOpenFile={(f) => setOpenFile(f)}
              onSummaryCreated={() => {
                setExplorerKey((k) => k + 1);
                setListRefreshToken((t) => t + 1);
                loadGraph();
              }}
            />
          </div>

          <ResizeHandle
            direction="v"
            onMouseDown={(e) => startDrag(e, "v", graphHeight, -1, setGraphHeight, 100, 560)}
          />

          {/* 下：图谱 */}
          <div style={{ height: graphHeight }} className="shrink-0 relative bg-card border-t border-border">
            <div className="absolute top-2 left-3 z-10 flex items-center gap-2 flex-wrap">
              <span className="text-xs font-medium text-muted-foreground uppercase tracking-wide">图谱</span>
              {(["article", "entity", "summary", "index"] as const).map((t) => (
                <button
                  key={t}
                  onClick={() => toggleNodeType(t)}
                  title={visibleNodeTypes.has(t) ? `隐藏 ${t}` : `显示 ${t}`}
                  className={cn(
                    "flex items-center gap-1 text-xs rounded px-1 py-0.5 transition-opacity",
                    visibleNodeTypes.has(t) ? "text-foreground/70" : "text-muted-foreground/30 line-through"
                  )}
                >
                  <span
                    className="w-2 h-2 rounded-full inline-block shrink-0"
                    style={{ background: OBJECT_TYPE_COLORS[t] }}
                  />
                  {t}
                </button>
              ))}
              <div className="relative">
                <button
                  onClick={() => setFilterOpen((v) => !v)}
                  className="text-xs text-muted-foreground hover:text-foreground border border-border rounded px-2 py-0.5 bg-card/90"
                >
                  边类型
                </button>
                {filterOpen && (
                  <div className="absolute top-6 left-0 z-20 bg-card border border-border rounded-lg shadow-md p-2 space-y-0.5 min-w-[9rem]">
                    {Object.entries(EDGE_COLORS).map(([type, color]) => (
                      <button
                        key={type}
                        onClick={() => toggleEdgeType(type)}
                        className={cn(
                          "flex items-center gap-2 w-full text-left text-xs py-0.5 px-1 rounded hover:bg-muted",
                          visibleEdgeTypes.has(type) ? "text-foreground/80" : "text-muted-foreground/30 line-through"
                        )}
                      >
                        <span
                          className="w-2 h-2 rounded-full shrink-0"
                          style={{ background: color }}
                        />
                        {type}
                      </button>
                    ))}
                  </div>
                )}
              </div>
              {selectedNodeId && (
                <span className="text-xs text-muted-foreground">
                  · <kbd className="font-mono bg-muted px-1 rounded">Esc</kbd> 取消
                </span>
              )}
            </div>
            {graphLoading && (
              <div className="absolute inset-0 flex items-center justify-center text-muted-foreground text-sm z-10 bg-card/70">
                加载图谱中…
              </div>
            )}
            <svg ref={svgRef} className="w-full h-full" />
          </div>
        </div>

        <ResizeHandle
          direction="h"
          onMouseDown={(e) => startDrag(e, "h", rightWidth, -1, setRightWidth, 200, 520)}
        />

        {/* 右：列表 */}
        <div style={{ width: rightWidth }} className="shrink-0 border-l border-border bg-card overflow-hidden flex flex-col">
          <div className="px-3 py-2 border-b border-border shrink-0">
            <span className="text-xs font-medium text-muted-foreground uppercase tracking-wide">列表</span>
          </div>
          <div className="flex-1 overflow-hidden">
            <ListPanel
              selectedId={selectedNodeId ?? undefined}
              onSelectNode={(id) => selectNode(id)}
              refreshToken={listRefreshToken}
            />
          </div>
        </div>

      </div>
    </main>
  );
}
