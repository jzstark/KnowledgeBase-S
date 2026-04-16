"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import * as d3 from "d3";

// ── 类型 ──────────────────────────────────────────────────────────────────────

interface KBNode {
  id: string;
  title: string;
  source_type: string;
  tags: string[];
  degree?: number;
  summary?: string;
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
  summary: string;
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

interface FileTree {
  raw: Record<string, RawFile[]>;
  wiki: MdFile[];
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
};

const SOURCE_TYPE_COLORS: Record<string, string> = {
  rss: "bg-orange-50 text-orange-600",
  wechat: "bg-green-50 text-green-600",
  manual: "bg-purple-50 text-purple-600",
};

// ── 可拖拽分隔线 ──────────────────────────────────────────────────────────────

function ResizeHandle({ direction, onMouseDown }: {
  direction: "h" | "v";
  onMouseDown: (e: React.MouseEvent) => void;
}) {
  return (
    <div
      onMouseDown={onMouseDown}
      className={`shrink-0 ${
        direction === "h"
          ? "w-1 cursor-col-resize"
          : "h-1 cursor-row-resize"
      } bg-gray-200 hover:bg-blue-400 active:bg-blue-500 transition-colors z-10`}
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
  const colorClass = SOURCE_TYPE_COLORS[node.source_type] || "bg-gray-50 text-gray-600";
  return (
    <button
      data-node-id={node.id}
      onClick={onClick}
      className={`w-full text-left rounded-lg border p-3 transition-colors ${
        selected
          ? "border-blue-500 bg-blue-50"
          : "border-gray-200 bg-white hover:border-gray-300"
      }`}
    >
      <div className="flex items-center gap-2 mb-1">
        <span className={`text-xs px-1.5 py-0.5 rounded font-medium ${colorClass}`}>
          {node.source_type || "unknown"}
        </span>
        {node.created_at && (
          <span className="text-xs text-gray-400">
            {new Date(node.created_at).toLocaleDateString("zh-CN", {
              month: "2-digit",
              day: "2-digit",
            })}
          </span>
        )}
      </div>
      <p className="text-sm font-medium text-gray-900 line-clamp-2 mb-1">
        {node.title || node.id}
      </p>
      {node.summary && (
        <p className="text-xs text-gray-500 line-clamp-2">{node.summary}</p>
      )}
      {(node.tags || []).length > 0 && (
        <div className="flex flex-wrap gap-1 mt-1.5">
          {(node.tags || []).slice(0, 3).map((t) => (
            <span key={t} className="text-xs bg-blue-50 text-blue-600 px-1.5 py-0.5 rounded">
              {t}
            </span>
          ))}
          {(node.tags || []).length > 3 && (
            <span className="text-xs text-gray-400">+{node.tags.length - 3}</span>
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

  // 外部触发刷新（如节点删除后），保留当前搜索条件重新拉取
  useEffect(() => {
    if (refreshToken === undefined || refreshToken === 0) return;
    loadNodes(q, tagFilter, offset);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshToken]);

  // 选中节点时自动滚动到对应卡片
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
      <div className="p-3 border-b border-gray-200 space-y-2 shrink-0">
        <input
          type="text"
          value={q}
          onChange={(e) => handleQChange(e.target.value)}
          placeholder="搜索标题或摘要…"
          className="w-full text-sm border border-gray-200 rounded-lg px-3 py-1.5 outline-none focus:border-blue-400"
        />
        <div className="flex items-center gap-2">
          <input
            type="text"
            value={tagFilter}
            onChange={(e) => handleTagChange(e.target.value)}
            placeholder="按标签过滤"
            className="flex-1 text-sm border border-gray-200 rounded-lg px-3 py-1.5 outline-none focus:border-blue-400"
          />
          <span className="text-xs text-gray-400 shrink-0">{total}</span>
        </div>
      </div>

      <div ref={containerRef} className="flex-1 overflow-auto p-3 space-y-2">
        {loading ? (
          <p className="text-sm text-gray-400">加载中…</p>
        ) : nodes.length === 0 ? (
          <p className="text-sm text-gray-400">暂无节点</p>
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
        <div className="px-3 py-2 border-t border-gray-200 flex items-center justify-between shrink-0">
          <button
            onClick={() => setOffset((o) => Math.max(0, o - LIMIT))}
            disabled={offset === 0}
            className="text-xs px-3 py-1 border border-gray-200 rounded disabled:opacity-40"
          >
            上一页
          </button>
          <span className="text-xs text-gray-500">
            {Math.floor(offset / LIMIT) + 1} / {Math.ceil(total / LIMIT)}
          </span>
          <button
            onClick={() => setOffset((o) => o + LIMIT)}
            disabled={offset + LIMIT >= total}
            className="text-xs px-3 py-1 border border-gray-200 rounded disabled:opacity-40"
          >
            下一页
          </button>
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
  const [expanded, setExpanded] = useState<Set<string>>(new Set(["raw", "wiki", "config"]));
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

  // 删除知识节点（raw 文件 + wiki 文件 + DB 记录）
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

  // 删除 wiki 节点文件（同时删除原始文件和 DB 节点记录）
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

  // 删除配置模板文件（仅删除文件）
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

  if (loading) return <div className="p-4 text-sm text-gray-400">加载中…</div>;
  if (!tree) return <div className="p-4 text-sm text-red-400">加载失败</div>;

  const chevron = (key: string) => (
    <span className="text-gray-400 text-xs mr-1">{expanded.has(key) ? "▾" : "▸"}</span>
  );

  return (
    <div className="h-full overflow-auto p-2 text-sm select-none">
      {/* 原始文件 */}
      <div>
        <button
          onClick={() => toggle("raw")}
          className="flex items-center w-full text-left font-medium text-gray-700 py-1 hover:text-gray-900"
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
                    className="flex items-center w-full text-left text-gray-500 py-0.5 hover:text-gray-700"
                  >
                    {chevron(key)}
                    <span className="text-xs">{RAW_TYPE_LABELS[type] ?? type}</span>
                    <span className="ml-1 text-xs text-gray-300">({files.length})</span>
                  </button>
                  {expanded.has(key) && (
                    <div className="ml-3 space-y-0.5">
                      {files.map((f) => (
                        <div key={f.rel_path} className="flex items-center gap-1 group py-0.5">
                          <span className="flex-1 text-xs text-gray-600 truncate" title={f.name}>
                            {f.name}
                          </span>
                          <span className="text-xs text-gray-300 shrink-0">{formatBytes(f.size)}</span>
                          {f.node_id && (
                            <button
                              onClick={() => handleDeleteNode(f.node_id!, f.name)}
                              disabled={deleting === f.node_id}
                              className="shrink-0 text-gray-300 hover:text-red-500 transition-colors disabled:opacity-40 ml-1"
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

      <div className="border-t border-gray-100 my-2" />

      {/* Wiki */}
      <div>
        <button
          onClick={() => toggle("wiki")}
          className="flex items-center w-full text-left font-medium text-gray-700 py-1 hover:text-gray-900"
        >
          {chevron("wiki")} Wiki
          <span className="ml-1 text-xs text-gray-300">({tree.wiki.length})</span>
        </button>
        {expanded.has("wiki") && (
          <div className="ml-3 space-y-0.5">
            {tree.wiki.map((f) => {
              const nodeId = f.name.replace(/\.md$/, "");
              const isSelected = nodeId === selectedNodeId;
              return (
                <div key={f.rel_path} className="flex items-center group gap-1 py-0.5">
                  <button
                    onClick={() => {
                      onOpenFile({ rel_path: f.rel_path, name: f.name, writable: true });
                      onSelectNode(nodeId);
                    }}
                    className={`flex-1 min-w-0 text-left text-xs truncate rounded px-1 transition-colors ${
                      isSelected ? "bg-blue-50 text-blue-700" : "text-blue-600 hover:text-blue-800"
                    }`}
                    title={f.name}
                  >
                    📄 {f.name}
                  </button>
                  <button
                    onClick={() => handleDeleteWiki(nodeId, f.name)}
                    disabled={deleting === nodeId}
                    className="shrink-0 text-gray-300 hover:text-red-500 transition-colors disabled:opacity-40 opacity-0 group-hover:opacity-100"
                    title="删除节点及原始文件"
                  >
                    {deleting === nodeId ? "…" : "✕"}
                  </button>
                </div>
              );
            })}
          </div>
        )}
      </div>

      <div className="border-t border-gray-100 my-2" />

      {/* 配置模板 */}
      <div>
        <button
          onClick={() => toggle("config")}
          className="flex items-center w-full text-left font-medium text-gray-700 py-1 hover:text-gray-900"
        >
          {chevron("config")} 配置模板
          <span className="ml-1 text-xs text-gray-300">({tree.config.length})</span>
        </button>
        {expanded.has("config") && (
          <div className="ml-3 space-y-0.5">
            {tree.config.map((f) => (
              <div key={f.rel_path} className="flex items-center group gap-1 py-0.5">
                <button
                  onClick={() => onOpenFile({ rel_path: f.rel_path, name: f.name, writable: true })}
                  className="flex-1 min-w-0 text-left text-xs text-blue-600 hover:text-blue-800 truncate rounded px-1"
                  title={f.name}
                >
                  📄 {f.name}
                </button>
                <button
                  onClick={() => handleDeleteConfig(f.rel_path, f.name)}
                  disabled={deleting === f.rel_path}
                  className="shrink-0 text-gray-300 hover:text-red-500 transition-colors disabled:opacity-40 opacity-0 group-hover:opacity-100"
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
      <div className="flex items-center gap-2 px-4 py-2 border-b border-gray-100 shrink-0 bg-gray-50">
        <span className="flex-1 text-xs font-medium text-gray-700 truncate" title={file.name}>
          {file.name}
        </span>
        {saveMsg && <span className="text-xs text-green-500">{saveMsg}</span>}
        {file.writable && !editing && (
          <button
            onClick={() => { setDraft(content ?? ""); setEditing(true); }}
            className="text-xs text-gray-500 hover:text-gray-800 border border-gray-200 rounded px-2 py-0.5 bg-white"
          >
            编辑
          </button>
        )}
        {editing && (
          <>
            <button
              onClick={handleSave}
              disabled={saving}
              className="text-xs text-white bg-gray-900 rounded px-2 py-0.5 disabled:opacity-40"
            >
              {saving ? "保存中…" : "保存"}
            </button>
            <button onClick={() => setEditing(false)} className="text-xs text-gray-500 hover:text-gray-800">
              取消
            </button>
          </>
        )}
        <button onClick={onClose} className="text-gray-400 hover:text-gray-600 text-lg leading-none ml-1">
          ×
        </button>
      </div>
      <div className="flex-1 overflow-auto">
        {loading ? (
          <div className="p-4 text-sm text-gray-400">加载中…</div>
        ) : content === null ? (
          <div className="p-4 text-sm text-red-400">加载失败</div>
        ) : editing ? (
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            className="w-full h-full p-4 text-xs font-mono text-gray-800 resize-none outline-none"
            spellCheck={false}
          />
        ) : (
          <pre className="p-4 text-xs text-gray-700 whitespace-pre-wrap font-mono leading-relaxed">
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
}: {
  detail: NodeDetail | null;
  detailLoading: boolean;
  openFile: OpenFile | null;
  onCloseFile: () => void;
  onOpenFile: (f: OpenFile) => void;
}) {
  if (openFile) return <FilePanel file={openFile} onClose={onCloseFile} />;

  if (detailLoading) {
    return (
      <div className="flex items-center justify-center h-full text-sm text-gray-400">加载中…</div>
    );
  }

  if (!detail) {
    return (
      <div className="flex items-center justify-center h-full text-sm text-gray-400">
        从右侧列表或图谱中选择一个节点
      </div>
    );
  }

  const colorClass = SOURCE_TYPE_COLORS[detail.source_type] || "bg-gray-50 text-gray-600";

  return (
    <div className="flex flex-col h-full">
      {/* 节点元数据头部 */}
      <div className="px-5 py-3 border-b border-gray-100 bg-gray-50 shrink-0">
        <div className="flex items-start gap-3">
          <div className="flex-1 min-w-0">
            <h2 className="text-sm font-semibold text-gray-900 leading-snug">
              {detail.title || detail.id}
            </h2>
            <div className="flex items-center gap-2 mt-1 flex-wrap">
              <span className={`text-xs px-1.5 py-0.5 rounded font-medium ${colorClass}`}>
                {detail.source_type || "unknown"}
              </span>
              {detail.created_at && (
                <span className="text-xs text-gray-400">
                  {new Date(detail.created_at).toLocaleDateString("zh-CN")}
                </span>
              )}
              {(detail.tags || []).map((t) => (
                <span key={t} className="text-xs bg-blue-50 text-blue-600 px-1.5 py-0.5 rounded">
                  {t}
                </span>
              ))}
            </div>
          </div>
          <button
            onClick={() =>
              onOpenFile({
                rel_path: `wiki/nodes/${detail.id}.md`,
                name: `${detail.title || detail.id}.md`,
                writable: true,
              })
            }
            className="shrink-0 text-xs text-gray-500 hover:text-gray-800 border border-gray-200 rounded px-2 py-1 bg-white"
          >
            在编辑器中打开
          </button>
        </div>
        {detail.summary && (
          <p className="text-xs text-gray-500 mt-2 leading-relaxed">{detail.summary}</p>
        )}
      </div>

      {/* Wiki 正文 */}
      <div className="flex-1 overflow-auto p-5">
        {detail.wiki_body ? (
          <pre className="text-xs text-gray-700 whitespace-pre-wrap font-mono leading-relaxed">
            {detail.wiki_body}
          </pre>
        ) : (
          <p className="text-sm text-gray-400">暂无 Wiki 内容</p>
        )}

        {(detail.edges || []).length > 0 && (
          <div className="mt-6 pt-4 border-t border-gray-100">
            <p className="text-xs font-medium text-gray-400 mb-2">
              关联 {detail.edges.length} 条边
            </p>
            <div className="space-y-1">
              {detail.edges.slice(0, 10).map((e) => (
                <div key={e.id} className="flex items-center gap-2 text-xs text-gray-500">
                  <span
                    className="w-1.5 h-1.5 rounded-full shrink-0"
                    style={{ background: EDGE_COLORS[e.relation_type] || "#d1d5db" }}
                  />
                  <span>{e.relation_type}</span>
                  <span className="text-gray-300">{e.from_node_id === detail.id ? "→" : "←"}</span>
                  <span className="text-gray-400">{(e.weight * 100).toFixed(0)}%</span>
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

  // 中央同步状态
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);

  // 节点详情
  const [detail, setDetail] = useState<NodeDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  // 图谱
  const [graphData, setGraphData] = useState<GraphData | null>(null);
  const [graphLoading, setGraphLoading] = useState(false);
  const svgRef = useRef<SVGSVGElement>(null);
  const zoomRef = useRef<d3.ZoomBehavior<SVGSVGElement, unknown> | null>(null);
  const simNodesRef = useRef<SimNode[]>([]);

  // 文件编辑器
  const [openFile, setOpenFile] = useState<OpenFile | null>(null);
  const [explorerKey, setExplorerKey] = useState(0);
  const [listRefreshToken, setListRefreshToken] = useState(0);

  // 面板尺寸（可拖拽）
  const [leftWidth, setLeftWidth] = useState(208);
  const [rightWidth, setRightWidth] = useState(288);
  const [graphHeight, setGraphHeight] = useState(256);

  // 挂载时加载图谱
  useEffect(() => { loadGraph(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Esc 取消选中
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

  // D3 渲染（graphData 变化时重绘）
  useEffect(() => {
    if (!graphData || !svgRef.current) return;
    renderGraph(graphData);
  }, [graphData]); // eslint-disable-line react-hooks/exhaustive-deps

  // 高亮选中节点及其邻居，并将其平移到图谱中心
  useEffect(() => {
    if (!svgRef.current) return;

    // 计算邻居节点 ID
    const neighborIds = new Set<string>();
    if (selectedNodeId && graphData) {
      graphData.edges.forEach((e) => {
        if (e.from_node_id === selectedNodeId) neighborIds.add(e.to_node_id);
        if (e.to_node_id === selectedNodeId) neighborIds.add(e.from_node_id);
      });
    }

    const svgSel = d3.select(svgRef.current);

    // 更新节点样式
    svgSel
      .selectAll<SVGCircleElement, SimNode>("circle")
      .attr("fill", (d) => {
        if (!selectedNodeId) return "#3b82f6";
        if (d.id === selectedNodeId) return "#ef4444";   // 红色：选中
        if (neighborIds.has(d.id)) return "#f59e0b";     // 琥珀色：邻居
        return "#3b82f6";
      })
      .attr("fill-opacity", (d) => {
        if (!selectedNodeId) return 0.8;
        if (d.id === selectedNodeId || neighborIds.has(d.id)) return 1;
        return 0.18; // 非相关节点变暗
      })
      .attr("r", (d) => {
        const base = Math.min(8 + (d.degree || 0) * 1.5, 22);
        return d.id === selectedNodeId ? base + 6 : base;
      })
      .attr("stroke", (d) => d.id === selectedNodeId ? "#fff" : "#fff")
      .attr("stroke-width", (d) => d.id === selectedNodeId ? 3 : 1.5);

    // 更新边样式
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

    // 将选中节点平移到图谱中心
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
      .selectAll<SVGCircleElement, SimNode>("circle")
      .data(nodes).enter().append("circle")
      .attr("r", (d) => Math.min(8 + (d.degree || 0) * 1.5, 22))
      .attr("fill", "#3b82f6")
      .attr("fill-opacity", 0.8)
      .attr("stroke", "#fff")
      .attr("stroke-width", 1.5)
      .attr("cursor", "pointer")
      .on("click", (_, d) => selectNode(d.id))
      .call(
        d3
          .drag<SVGCircleElement, SimNode>()
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
        node.attr("cx", (d) => d.x ?? 0).attr("cy", (d) => d.y ?? 0);
        label.attr("x", (d) => (d.x ?? 0) + 10).attr("y", (d) => (d.y ?? 0) + 3);
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
    <main className="h-screen bg-gray-50 flex flex-col">
      {/* 顶部工具栏 */}
      <header className="bg-white border-b border-gray-200 px-5 py-2.5 flex items-center justify-between shrink-0">
        <h1 className="text-base font-semibold text-gray-900">知识库</h1>
        <button
          onClick={handleMaintenance}
          className="text-xs px-3 py-1.5 border border-gray-200 rounded-lg text-gray-600 hover:bg-gray-50 transition-colors"
        >
          {maintenanceMsg || "立即运行维护"}
        </button>
      </header>

      {/* 四面板主体 */}
      <div className="flex flex-1 min-h-0">

        {/* 左：资源管理器 */}
        <div style={{ width: leftWidth }} className="shrink-0 border-r border-gray-200 bg-white overflow-hidden flex flex-col">
          <div className="px-3 py-2 border-b border-gray-100 shrink-0">
            <span className="text-xs font-medium text-gray-400 uppercase tracking-wide">资源管理器</span>
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

        {/* 左右分隔线 */}
        <ResizeHandle
          direction="h"
          onMouseDown={(e) => startDrag(e, "h", leftWidth, 1, setLeftWidth, 140, 480)}
        />

        {/* 中：Wiki 查看器（上）+ 图谱（下） */}
        <div className="flex-1 min-w-0 flex flex-col">
          {/* 上：Wiki / 文件查看器 */}
          <div className="flex-1 min-h-0 bg-white overflow-hidden">
            <WikiPanel
              detail={detail}
              detailLoading={detailLoading}
              openFile={openFile}
              onCloseFile={() => setOpenFile(null)}
              onOpenFile={(f) => setOpenFile(f)}
            />
          </div>

          {/* 上下分隔线 */}
          <ResizeHandle
            direction="v"
            onMouseDown={(e) => startDrag(e, "v", graphHeight, -1, setGraphHeight, 100, 560)}
          />

          {/* 下：图谱 */}
          <div style={{ height: graphHeight }} className="shrink-0 relative bg-white border-t border-gray-200">
            <div className="absolute top-2 left-3 z-10 flex items-center gap-2">
              <span className="text-xs font-medium text-gray-400 uppercase tracking-wide">图谱</span>
              {selectedNodeId && (
                <span className="text-xs text-gray-400">
                  · 按 <kbd className="font-mono bg-gray-100 px-1 rounded">Esc</kbd> 取消选中
                </span>
              )}
            </div>
            {graphLoading && (
              <div className="absolute inset-0 flex items-center justify-center text-gray-400 text-sm z-10 bg-white/70">
                加载图谱中…
              </div>
            )}
            <svg ref={svgRef} className="w-full h-full" />
          </div>
        </div>

        {/* 右左分隔线 */}
        <ResizeHandle
          direction="h"
          onMouseDown={(e) => startDrag(e, "h", rightWidth, -1, setRightWidth, 200, 520)}
        />

        {/* 右：列表 */}
        <div style={{ width: rightWidth }} className="shrink-0 border-l border-gray-200 bg-white overflow-hidden flex flex-col">
          <div className="px-3 py-2 border-b border-gray-100 shrink-0">
            <span className="text-xs font-medium text-gray-400 uppercase tracking-wide">列表</span>
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
