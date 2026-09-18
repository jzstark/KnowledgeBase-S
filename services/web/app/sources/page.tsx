"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { cn } from "@/lib/utils";

// ── Types ─────────────────────────────────────────────────────────────────────

interface Folder {
  id: string;
  name: string;
  kind: "normal" | "stream";
  status: "active" | "archived";
  parent_id: string | null;
  item_count: number;
  created_at: string;
  updated_at: string;
}

interface DocumentInstance {
  id: string;
  folder_id: string;
  display_name: string | null;
  origin_ref: string | null;
  origin_ref_type: string | null;
  doc_kind: string | null;
  article_doc_kind: string | null;
  status: string;
  mime_type: string | null;
  size: number | null;
  article_id: string | null;
  article_title: string | null;
  article_titles: string[] | null;
  created_at: string;
  updated_at: string;
}

interface Connector {
  id: string;
  folder_id: string;
  folder_name: string;
  type: "rss" | "wechat";
  config: Record<string, unknown>;
  status: "active" | "inactive";
  last_fetched_at: string | null;
}

interface FolderContents {
  folder: Folder;
  subfolders: Folder[];
  items: DocumentInstance[];
  connector: Connector | null;
}

interface DocKindConfig {
  values: string[];
  default: string;
}

interface BatchArchiveResult {
  id: string;
  status: "archived" | "skipped" | "failed";
  detail: string;
}

interface BatchArchiveResponse {
  archived: number;
  skipped: number;
  failed: number;
  results: BatchArchiveResult[];
}

interface BatchDocKindResult {
  id: string;
  status: "updated" | "failed";
  detail: string;
  wiki_warnings: string[];
}

interface BatchDocKindResponse {
  updated: number;
  failed: number;
  wiki_warnings: string[];
  results: BatchDocKindResult[];
}

// ── Constants ─────────────────────────────────────────────────────────────────

const DOC_KIND_LABELS: Record<string, string> = {
  regulation: "法规",
  case: "判例",
  news: "新闻",
  memo: "备忘录",
  contract: "合同",
  analysis: "分析",
  other: "其他",
};

const STATUS_LABELS: Record<string, string> = {
  pending: "待处理",
  processing: "处理中",
  succeeded: "已入库",
  failed: "失败",
  ignored: "已归档",
};

const STATUS_COLORS: Record<string, string> = {
  pending: "bg-yellow-100 text-yellow-700",
  processing: "bg-blue-100 text-blue-700",
  succeeded: "bg-green-100 text-green-700",
  failed: "bg-red-100 text-red-700",
  ignored: "bg-muted text-muted-foreground",
};

function fmtDate(iso: string | null) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("zh-CN", {
    month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit",
  });
}

function fmtSize(bytes: number | null) {
  if (!bytes) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

// ── doc_kind hook ─────────────────────────────────────────────────────────────

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
  value, onChange, id, includeEmpty = true, emptyLabel = "（不预设）",
}: {
  value: string; onChange: (v: string) => void;
  id?: string; includeEmpty?: boolean; emptyLabel?: string;
}) {
  const cfg = useDocKindConfig();
  return (
    <select
      id={id}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="flex h-9 w-full rounded-md border border-input bg-background px-2 py-1 text-sm shadow-sm focus:outline-none focus:ring-1 focus:ring-ring"
    >
      {includeEmpty && <option value="">{emptyLabel}</option>}
      {(cfg?.values ?? []).map((v) => (
        <option key={v} value={v}>{DOC_KIND_LABELS[v] ?? v}</option>
      ))}
    </select>
  );
}

function DocKindChangeDialog({
  open, folderId, ids, initialValue, onClose, onDone,
}: {
  open: boolean;
  folderId: string;
  ids: string[];
  initialValue?: string | null;
  onClose: () => void;
  onDone: (result: BatchDocKindResponse, docKind: string) => void | Promise<void>;
}) {
  const cfg = useDocKindConfig();
  const [docKind, setDocKind] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open || !cfg) return;
    setDocKind(
      initialValue && cfg.values.includes(initialValue)
        ? initialValue
        : cfg.default || cfg.values[0] || "",
    );
    setError("");
  }, [open, cfg, initialValue]);

  async function submit() {
    if (!docKind || ids.length === 0) return;
    setSaving(true);
    setError("");
    try {
      const response = await fetch("/api/document-instances/batch/doc-kind", {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ folder_id: folderId, ids, doc_kind: docKind }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail || "修改内容类型失败");
      await onDone(body as BatchDocKindResponse, docKind);
      onClose();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "修改内容类型失败");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !next && !saving && onClose()}>
      <DialogContent>
        <DialogHeader><DialogTitle>修改内容类型</DialogTitle></DialogHeader>
        <div className="space-y-4">
          <p className="text-sm text-muted-foreground">
            将选中的 {ids.length} 篇文档设为指定类型。此操作不会重新生成文章，也不会调用模型。
          </p>
          <div className="space-y-1.5">
            <Label htmlFor="change-doc-kind">目标类型</Label>
            <select
              id="change-doc-kind"
              value={docKind}
              onChange={(event) => setDocKind(event.target.value)}
              className="flex h-9 w-full rounded-md border border-input bg-background px-2 py-1 text-sm shadow-sm focus:outline-none focus:ring-1 focus:ring-ring"
            >
              {(cfg?.values ?? []).map((value) => (
                <option key={value} value={value}>{DOC_KIND_LABELS[value] ?? value}</option>
              ))}
            </select>
          </div>
          {error && <p className="text-sm text-destructive">{error}</p>}
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={onClose} disabled={saving}>取消</Button>
            <Button onClick={submit} disabled={saving || !docKind}>
              {saving ? "保存中…" : `确认修改 ${ids.length} 篇`}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

// ── Main Page ─────────────────────────────────────────────────────────────────

export default function FoldersPage() {
  const [folders, setFolders] = useState<Folder[]>([]);
  const [activeFolderId, setActiveFolderId] = useState<string | null>(null);
  const [contents, setContents] = useState<FolderContents | null>(null);
  const [selectedItem, setSelectedItem] = useState<DocumentInstance | null>(null);
  const [loading, setLoading] = useState(true);
  const [contentsLoading, setContentsLoading] = useState(false);
  const [contentsError, setContentsError] = useState("");
  const [showArchived, setShowArchived] = useState(false);
  const activeFolderIdRef = useRef<string | null>(activeFolderId);
  const showArchivedRef = useRef(showArchived);
  const contentsRequestRef = useRef(0);
  activeFolderIdRef.current = activeFolderId;
  showArchivedRef.current = showArchived;

  // Modal states
  const [showNewFolder, setShowNewFolder] = useState(false);
  const [newFolderParent, setNewFolderParent] = useState<string | null>(null);
  const [uploadTarget, setUploadTarget] = useState<string | null>(null);
  const [addUrlTarget, setAddUrlTarget] = useState<string | null>(null);
  const [newStreamTarget, setNewStreamTarget] = useState(false);
  const [renameTarget, setRenameTarget] = useState<Folder | null>(null);

  async function loadFolders() {
    try {
      const r = await fetch("/api/folders", { credentials: "include" });
      if (r.ok) setFolders(await r.json());
    } finally {
      setLoading(false);
    }
  }

  async function loadContents(folderId: string, showLoading = true) {
    if (activeFolderIdRef.current !== folderId) return;
    const requestId = ++contentsRequestRef.current;
    if (showLoading) setContentsLoading(true);
    setContentsError("");
    setSelectedItem(null);
    try {
      const includeArchived = showArchivedRef.current ? "?include_archived=true" : "";
      const r = await fetch(`/api/folders/${folderId}/contents${includeArchived}`, { credentials: "include" });
      if (requestId !== contentsRequestRef.current || activeFolderIdRef.current !== folderId) return;
      if (!r.ok) {
        setContents(null);
        setContentsError("加载资料夹内容失败，请重试");
        return;
      }
      const nextContents = await r.json();
      if (requestId !== contentsRequestRef.current || activeFolderIdRef.current !== folderId) return;
      setContents(nextContents);
    } catch {
      if (requestId === contentsRequestRef.current && activeFolderIdRef.current === folderId) {
        setContents(null);
        setContentsError("加载资料夹内容失败，请重试");
      }
    } finally {
      if (requestId === contentsRequestRef.current && activeFolderIdRef.current === folderId) {
        setContentsLoading(false);
      }
    }
  }

  useEffect(() => { loadFolders(); }, []);

  useEffect(() => {
    if (activeFolderId) loadContents(activeFolderId);
    else {
      contentsRequestRef.current += 1;
      setContents(null);
      setContentsError("");
      setContentsLoading(false);
      setSelectedItem(null);
    }
  }, [activeFolderId, showArchived]);

  async function refreshCurrentFolder() {
    if (!activeFolderId) return;
    await Promise.all([loadContents(activeFolderId, false), loadFolders()]);
  }

  async function handleArchiveFolder(id: string) {
    if (!confirm("归档该资料夹？内容保留，不再显示在主列表中。")) return;
    const r = await fetch(`/api/folders/${id}`, {
      method: "PATCH", credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: "archived" }),
    });
    if (r.ok) {
      setFolders((p) => p.filter((f) => f.id !== id));
      if (activeFolderId === id) { setActiveFolderId(null); setContents(null); }
    }
  }

  async function handleSync(folderId: string, connectorId: string) {
    await fetch(`/api/connectors/${connectorId}/sync`, {
      method: "POST", credentials: "include",
    });
    setTimeout(() => loadContents(folderId), 1500);
  }

  async function handleDeleteItem(di: DocumentInstance) {
    if (!confirm(`归档「${di.display_name || di.origin_ref}」？`)) return;
    const r = await fetch(`/api/document-instances/${di.id}`, {
      method: "DELETE", credentials: "include",
    });
    if (r.ok || r.status === 204) {
      await refreshCurrentFolder();
      if (selectedItem?.id === di.id) setSelectedItem(null);
    } else {
      const body = await r.json().catch(() => ({}));
      alert(body.detail || "归档失败");
    }
  }

  async function handleHardDeleteItem(di: DocumentInstance) {
    if (!confirm(`彻底删除「${di.display_name || di.origin_ref}」及其所有摘要？此操作不可恢复。`)) return;
    const r = await fetch(`/api/document-instances/${di.id}?hard=true`, {
      method: "DELETE", credentials: "include",
    });
    if (r.ok || r.status === 204) {
      if (activeFolderId) loadContents(activeFolderId);
      if (selectedItem?.id === di.id) setSelectedItem(null);
    }
  }

  async function handleReprocess(di: DocumentInstance) {
    await fetch(`/api/document-instances/${di.id}/reprocess`, {
      method: "POST", credentials: "include",
    });
    if (activeFolderId) setTimeout(() => loadContents(activeFolderId), 1000);
  }

  async function handleSingleDocKindChanged(docKind: string) {
    if (!selectedItem) return;
    const updatedItem = {
      ...selectedItem,
      doc_kind: docKind,
      article_doc_kind: docKind,
    };
    await refreshCurrentFolder();
    setSelectedItem(updatedItem);
  }

  // Build folder tree (top-level only for simplicity)
  const rootFolders = folders.filter((f) => !f.parent_id && f.status === "active");

  return (
    <main className="flex h-full min-h-0 flex-col overflow-hidden bg-background">
      <div className="flex min-h-0 flex-1 overflow-hidden">
        {/* Left: Folder Tree */}
        <aside className="flex min-h-0 w-64 shrink-0 flex-col overflow-hidden border-r bg-muted/30">
          <div className="flex shrink-0 items-center justify-between border-b p-3">
            <span className="text-sm font-semibold">资料夹</span>
            <div className="flex gap-1">
              <Button
                size="sm"
                variant="ghost"
                className="h-6 px-1.5 text-xs"
                title="新建资料夹"
                onClick={() => { setNewFolderParent(null); setShowNewFolder(true); }}
              >＋</Button>
              <Button
                size="sm"
                variant="ghost"
                className="h-6 px-1.5 text-xs"
                title="新建订阅资料夹"
                onClick={() => setNewStreamTarget(true)}
              >⚡</Button>
            </div>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto">
            {loading ? (
              <p className="p-3 text-xs text-muted-foreground">加载中…</p>
            ) : rootFolders.length === 0 ? (
              <p className="p-3 text-xs text-muted-foreground">暂无资料夹</p>
            ) : (
              <div className="py-1">
                {rootFolders.map((f) => (
                  <FolderTreeItem
                    key={f.id}
                    folder={f}
                    active={activeFolderId === f.id}
                    onClick={() => setActiveFolderId(f.id)}
                    onRename={() => setRenameTarget(f)}
                    onArchive={() => handleArchiveFolder(f.id)}
                    onNewSub={() => { setNewFolderParent(f.id); setShowNewFolder(true); }}
                  />
                ))}
              </div>
            )}
          </div>
        </aside>

        {/* Center: Contents */}
        <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
          {!activeFolderId ? (
            <div className="flex-1 flex items-center justify-center text-sm text-muted-foreground">
              选择左侧资料夹查看内容
            </div>
          ) : contentsLoading ? (
            <div className="flex-1 flex items-center justify-center text-sm text-muted-foreground">
              加载中…
            </div>
          ) : contentsError ? (
            <div className="flex flex-1 flex-col items-center justify-center gap-3 text-sm text-muted-foreground">
              <p>{contentsError}</p>
              <Button size="sm" variant="outline" onClick={() => loadContents(activeFolderId)}>
                重试
              </Button>
            </div>
          ) : contents ? (
            <FolderContentsPanel
              contents={contents}
              selectedItem={selectedItem}
              onSelectItem={setSelectedItem}
              onUpload={() => setUploadTarget(activeFolderId)}
              onAddUrl={() => setAddUrlTarget(activeFolderId)}
              onSync={handleSync}
              onDelete={handleDeleteItem}
              onHardDelete={handleHardDeleteItem}
              onReprocess={handleReprocess}
              showArchived={showArchived}
              onShowArchivedChange={setShowArchived}
              onRefresh={refreshCurrentFolder}
            />
          ) : null}
        </div>

        {/* Right: Detail Drawer */}
        {selectedItem && (
          <DetailDrawer
            item={selectedItem}
            onClose={() => setSelectedItem(null)}
            onDelete={() => handleDeleteItem(selectedItem)}
            onHardDelete={() => handleHardDeleteItem(selectedItem)}
            onReprocess={() => handleReprocess(selectedItem)}
            onDocKindChanged={handleSingleDocKindChanged}
          />
        )}
      </div>

      {/* Modals */}
      <NewFolderModal
        open={showNewFolder}
        parentId={newFolderParent}
        onClose={() => setShowNewFolder(false)}
        onCreated={(f) => { setFolders((p) => [...p, f]); setShowNewFolder(false); setActiveFolderId(f.id); }}
      />
      <NewStreamFolderModal
        open={newStreamTarget}
        onClose={() => setNewStreamTarget(false)}
        onCreated={() => { setNewStreamTarget(false); loadFolders(); }}
      />
      <UploadModal
        folderId={uploadTarget}
        onClose={() => setUploadTarget(null)}
        onDone={() => { setUploadTarget(null); if (activeFolderId) setTimeout(() => loadContents(activeFolderId), 1000); }}
      />
      <AddUrlModal
        folderId={addUrlTarget}
        onClose={() => setAddUrlTarget(null)}
        onDone={() => { setAddUrlTarget(null); if (activeFolderId) setTimeout(() => loadContents(activeFolderId), 1000); }}
      />
      {renameTarget && (
        <RenameFolderModal
          folder={renameTarget}
          onClose={() => setRenameTarget(null)}
          onRenamed={(f) => {
            setFolders((p) => p.map((x) => x.id === f.id ? f : x));
            if (contents?.folder.id === f.id) setContents((c) => c ? { ...c, folder: f } : c);
            setRenameTarget(null);
          }}
        />
      )}
    </main>
  );
}

// ── Folder Tree Item ──────────────────────────────────────────────────────────

function FolderTreeItem({
  folder, active, onClick, onRename, onArchive, onNewSub,
}: {
  folder: Folder; active: boolean;
  onClick: () => void;
  onRename: () => void;
  onArchive: () => void;
  onNewSub: () => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  return (
    <div
      className={cn(
        "group flex items-center gap-2 px-3 py-1.5 cursor-pointer text-sm select-none",
        active ? "bg-accent text-accent-foreground" : "hover:bg-muted/60",
      )}
      onClick={onClick}
    >
      <span className="text-base leading-none shrink-0">
        {folder.kind === "stream" ? "⚡" : "📁"}
      </span>
      <span className="flex-1 truncate">{folder.name}</span>
      <span className="text-xs text-muted-foreground shrink-0">{folder.item_count}</span>
      <div className="relative" onClick={(e) => e.stopPropagation()}>
        <button
          className="opacity-0 group-hover:opacity-100 text-muted-foreground hover:text-foreground p-0.5 rounded text-xs"
          onClick={() => setMenuOpen((v) => !v)}
        >⋯</button>
        {menuOpen && (
          <div
            className="absolute right-0 top-5 z-50 bg-popover border rounded-md shadow-md py-1 min-w-[120px]"
            onMouseLeave={() => setMenuOpen(false)}
          >
            <button
              className="w-full px-3 py-1.5 text-xs text-left hover:bg-accent"
              onClick={() => { onRename(); setMenuOpen(false); }}
            >重命名</button>
            <button
              className="w-full px-3 py-1.5 text-xs text-left hover:bg-accent"
              onClick={() => { onNewSub(); setMenuOpen(false); }}
            >新建子资料夹</button>
            <Separator className="my-1" />
            <button
              className="w-full px-3 py-1.5 text-xs text-left hover:bg-accent text-destructive"
              onClick={() => { onArchive(); setMenuOpen(false); }}
            >归档</button>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Folder Contents Panel ─────────────────────────────────────────────────────

function FolderContentsPanel({
  contents, selectedItem, onSelectItem,
  onUpload, onAddUrl, onSync, onDelete, onHardDelete, onReprocess,
  showArchived, onShowArchivedChange, onRefresh,
}: {
  contents: FolderContents;
  selectedItem: DocumentInstance | null;
  onSelectItem: (item: DocumentInstance | null) => void;
  onUpload: () => void;
  onAddUrl: () => void;
  onSync: (folderId: string, connectorId: string) => void;
  onDelete: (item: DocumentInstance) => void;
  onHardDelete: (item: DocumentInstance) => void;
  onReprocess: (item: DocumentInstance) => void;
  showArchived: boolean;
  onShowArchivedChange: (show: boolean) => void;
  onRefresh: () => Promise<void>;
}) {
  const { folder, items, connector } = contents;
  const [query, setQuery] = useState("");
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [batching, setBatching] = useState(false);
  const [batchNotice, setBatchNotice] = useState<{ text: string; error: boolean } | null>(null);
  const [showDocKindDialog, setShowDocKindDialog] = useState(false);
  const selectAllRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    setQuery("");
    setSelectedIds(new Set());
    setBatchNotice(null);
    setShowDocKindDialog(false);
  }, [folder.id]);

  const searchTerms = useMemo(
    () => query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean),
    [query],
  );
  const filteredItems = useMemo(() => {
    if (searchTerms.length === 0) return items;
    return items.filter((item) => {
      const searchable = [
        item.display_name,
        item.article_title,
        ...(item.article_titles ?? []),
        item.origin_ref,
      ]
        .filter(Boolean)
        .join("\n")
        .toLocaleLowerCase();
      return searchTerms.every((term) => searchable.includes(term));
    });
  }, [items, searchTerms]);
  const selectableItems = useMemo(
    () => filteredItems.filter((item) => !["ignored", "deleted", "processing"].includes(item.status)),
    [filteredItems],
  );
  const allFilteredSelected = selectableItems.length > 0
    && selectableItems.every((item) => selectedIds.has(item.id));
  const someFilteredSelected = selectableItems.some((item) => selectedIds.has(item.id));

  useEffect(() => {
    const visibleIds = new Set(selectableItems.map((item) => item.id));
    setSelectedIds((previous) => {
      const next = new Set([...previous].filter((id) => visibleIds.has(id)));
      return next.size === previous.size ? previous : next;
    });
  }, [selectableItems]);

  useEffect(() => {
    if (selectAllRef.current) {
      selectAllRef.current.indeterminate = someFilteredSelected && !allFilteredSelected;
    }
  }, [allFilteredSelected, someFilteredSelected]);

  function toggleSelectAll() {
    if (allFilteredSelected) {
      setSelectedIds(new Set());
      return;
    }
    setSelectedIds(new Set(selectableItems.map((item) => item.id)));
  }

  function toggleSelected(id: string) {
    setSelectedIds((previous) => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function archiveSelected() {
    const ids = [...selectedIds];
    if (ids.length === 0) return;
    if (!confirm(`归档选中的 ${ids.length} 篇文章？知识文章和原始材料会保留。`)) return;

    setBatching(true);
    setBatchNotice(null);
    try {
      const response = await fetch("/api/document-instances/batch/archive", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ folder_id: folder.id, ids }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || "批量归档失败");
      }
      const result: BatchArchiveResponse = await response.json();
      const completed = new Set(
        result.results
          .filter((item) => item.status !== "failed")
          .map((item) => item.id),
      );
      setSelectedIds((previous) => new Set([...previous].filter((id) => !completed.has(id))));
      const summary = [`已归档 ${result.archived} 篇`];
      if (result.skipped) summary.push(`跳过 ${result.skipped} 篇`);
      if (result.failed) summary.push(`失败 ${result.failed} 篇`);
      const failedDetails = result.results
        .filter((item) => item.status === "failed")
        .map((item) => item.detail);
      setBatchNotice({
        text: failedDetails.length > 0
          ? `${summary.join("，")}：${[...new Set(failedDetails)].join("；")}`
          : summary.join("，"),
        error: result.failed > 0,
      });
      await onRefresh();
    } catch (error) {
      setBatchNotice({
        text: error instanceof Error ? error.message : "批量归档失败",
        error: true,
      });
    } finally {
      setBatching(false);
    }
  }

  async function handleDocKindChanged(result: BatchDocKindResponse) {
    const completed = new Set(
      result.results
        .filter((item) => item.status === "updated")
        .map((item) => item.id),
    );
    setSelectedIds((previous) => new Set([...previous].filter((id) => !completed.has(id))));
    const summary = [`已修改 ${result.updated} 篇`];
    if (result.failed) summary.push(`失败 ${result.failed} 篇`);
    if (result.wiki_warnings.length > 0) {
      summary.push(`Wiki 元数据警告 ${result.wiki_warnings.length} 项（写入失败可重试；文件缺失需先恢复）`);
    }
    const failedDetails = result.results
      .filter((item) => item.status === "failed")
      .map((item) => item.detail);
    setBatchNotice({
      text: failedDetails.length > 0
        ? `${summary.join("，")}：${[...new Set(failedDetails)].join("；")}`
        : summary.join("，"),
      error: result.failed > 0 || result.wiki_warnings.length > 0,
    });
    await onRefresh();
  }

  return (
    <div className="flex h-full min-h-0 min-w-0 flex-col overflow-hidden">
      {/* Toolbar */}
      <div className="px-4 py-2.5 border-b flex items-center gap-2 shrink-0">
        <h2 className="font-medium text-sm flex-1 truncate">
          {folder.kind === "stream" ? "⚡ " : "📁 "}{folder.name}
        </h2>
        {folder.kind === "normal" && (
          <>
            <Button size="sm" variant="outline" className="h-7 text-xs" onClick={onUpload}>
              上传文件
            </Button>
            <Button size="sm" variant="outline" className="h-7 text-xs" onClick={onAddUrl}>
              添加 URL
            </Button>
          </>
        )}
        {folder.kind === "stream" && connector && (
          <Button
            size="sm"
            variant="outline"
            className="h-7 text-xs"
            onClick={() => onSync(folder.id, connector.id)}
          >
            立即同步
          </Button>
        )}
        <Button size="sm" variant="ghost" className="h-7 text-xs" onClick={onRefresh}>
          刷新
        </Button>
      </div>

      {/* Connector info bar (stream folders) */}
      {connector && (
        <div className="flex shrink-0 gap-4 border-b bg-muted/30 px-4 py-1.5 text-xs text-muted-foreground">
          <span>{connector.type === "rss" ? "RSS" : "微信公众号"}</span>
          <span>状态：{connector.status === "active" ? "✅ 订阅中" : "⏸ 已暂停"}</span>
          <span>上次同步：{fmtDate(connector.last_fetched_at)}</span>
        </div>
      )}

      {/* Current-folder search */}
      <div className="flex shrink-0 items-center gap-3 border-b px-4 py-2">
        <Input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="搜索名称、文章标题或来源链接"
          aria-label="搜索当前资料夹"
          className="h-8 min-w-0 flex-1"
        />
        {query && (
          <Button
            size="sm"
            variant="ghost"
            className="h-8 shrink-0 px-2 text-xs"
            onClick={() => setQuery("")}
          >
            清除
          </Button>
        )}
        <label className="flex shrink-0 items-center gap-1.5 whitespace-nowrap text-xs text-muted-foreground">
          <input
            type="checkbox"
            checked={showArchived}
            onChange={(e) => onShowArchivedChange(e.target.checked)}
          />
          显示已归档
        </label>
        <span className="shrink-0 whitespace-nowrap text-xs text-muted-foreground">
          {searchTerms.length > 0
            ? `匹配 ${filteredItems.length} 条，共 ${items.length} 条`
            : `共 ${items.length} 条`}
        </span>
      </div>

      {(selectedIds.size > 0 || batchNotice) && (
        <div className="flex shrink-0 items-center gap-3 border-b bg-muted/30 px-4 py-2 text-xs">
          {selectedIds.size > 0 && (
            <>
              <span className="font-medium">已选择 {selectedIds.size} 篇</span>
              <Button size="sm" variant="outline" className="h-7 text-xs" disabled={batching} onClick={archiveSelected}>
                {batching ? "归档中…" : "归档"}
              </Button>
              <Button
                size="sm"
                variant="outline"
                className="h-7 text-xs"
                disabled={batching}
                onClick={() => setShowDocKindDialog(true)}
              >
                修改内容类型
              </Button>
              <Button size="sm" variant="ghost" className="h-7 text-xs" disabled={batching} onClick={() => setSelectedIds(new Set())}>
                取消选择
              </Button>
            </>
          )}
          {batchNotice && (
            <span className={cn("min-w-0 flex-1", batchNotice.error ? "text-destructive" : "text-muted-foreground")}>
              {batchNotice.text}
            </span>
          )}
        </div>
      )}

      {/* Items list */}
      <div className="min-h-0 min-w-0 flex-1 overflow-auto">
        {items.length === 0 ? (
          <p className="text-sm text-muted-foreground p-6 text-center">
            {folder.kind === "stream" ? "暂无条目，点击「立即同步」拉取内容" : "暂无文件，点击「上传文件」或「添加 URL」"}
          </p>
        ) : filteredItems.length === 0 ? (
          <div className="flex flex-col items-center gap-2 p-6 text-center text-sm text-muted-foreground">
            <p>没有匹配的文章</p>
            <Button size="sm" variant="outline" onClick={() => setQuery("")}>清除搜索</Button>
          </div>
        ) : (
          <table className="w-full min-w-[760px] table-fixed text-sm">
            <thead className="sticky top-0 z-10 bg-background">
              <tr className="border-b bg-muted/95 text-xs text-muted-foreground">
                <th className="w-12 px-4 py-2 text-left font-medium">
                  <input
                    ref={selectAllRef}
                    type="checkbox"
                    checked={allFilteredSelected}
                    disabled={selectableItems.length === 0}
                    aria-label="全选当前筛选结果"
                    onChange={toggleSelectAll}
                  />
                </th>
                <th className="px-4 py-2 text-left font-medium">名称</th>
                <th className="w-24 whitespace-nowrap px-4 py-2 text-left font-medium">类型</th>
                <th className="w-24 whitespace-nowrap px-4 py-2 text-left font-medium">状态</th>
                <th className="w-32 whitespace-nowrap px-4 py-2 text-left font-medium">时间</th>
                <th className="w-24 px-4 py-2" />
              </tr>
            </thead>
            <tbody>
              {filteredItems.map((item) => (
                <DocumentRow
                  key={item.id}
                  item={item}
                  selected={selectedItem?.id === item.id}
                  checked={selectedIds.has(item.id)}
                  selectable={!['ignored', 'deleted', 'processing'].includes(item.status)}
                  onClick={() => onSelectItem(selectedItem?.id === item.id ? null : item)}
                  onCheckedChange={() => toggleSelected(item.id)}
                  onDelete={() => onDelete(item)}
                  onHardDelete={() => onHardDelete(item)}
                  onReprocess={() => onReprocess(item)}
                />
              ))}
            </tbody>
          </table>
        )}
      </div>
      <DocKindChangeDialog
        open={showDocKindDialog}
        folderId={folder.id}
        ids={[...selectedIds]}
        onClose={() => setShowDocKindDialog(false)}
        onDone={handleDocKindChanged}
      />
    </div>
  );
}

// ── Document Row ──────────────────────────────────────────────────────────────

function DocumentRow({
  item, selected, checked, selectable, onClick, onCheckedChange,
  onDelete, onHardDelete, onReprocess,
}: {
  item: DocumentInstance;
  selected: boolean;
  checked: boolean;
  selectable: boolean;
  onClick: () => void;
  onCheckedChange: () => void;
  onDelete: () => void;
  onHardDelete: () => void;
  onReprocess: () => void;
}) {
  const name = item.display_name || item.origin_ref || item.id;
  const effectiveDocKind = item.doc_kind || item.article_doc_kind;
  return (
    <tr
      className={cn(
        "group border-b cursor-pointer hover:bg-muted/40 transition-colors",
        selected && "bg-accent",
      )}
      onClick={onClick}
    >
      <td className="w-12 px-4 py-2" onClick={(e) => e.stopPropagation()}>
        <input
          type="checkbox"
          checked={checked}
          disabled={!selectable}
          aria-label={`选择 ${name}`}
          title={selectable ? `选择 ${name}` : "处理中或已归档的文档不可选择"}
          onChange={onCheckedChange}
        />
      </td>
      <td className="min-w-0 px-4 py-2">
        <div className="flex items-center gap-2">
          <span className="shrink-0 text-base leading-none">{fileIcon(item.mime_type, item.origin_ref_type)}</span>
          <div className="min-w-0">
            <p className="truncate text-sm font-medium" title={name}>{name}</p>
            {item.article_title && item.article_title !== name && (
              <p className="truncate text-xs text-muted-foreground" title={item.article_title}>{item.article_title}</p>
            )}
          </div>
        </div>
      </td>
      <td className="whitespace-nowrap px-4 py-2">
        {effectiveDocKind && (
          <span className="text-xs text-muted-foreground">
            {DOC_KIND_LABELS[effectiveDocKind] ?? effectiveDocKind}
          </span>
        )}
      </td>
      <td className="whitespace-nowrap px-4 py-2">
        <span className={cn(
          "inline-flex whitespace-nowrap rounded-full px-1.5 py-0.5 text-xs",
          STATUS_COLORS[item.status] || "bg-muted text-muted-foreground",
        )}>
          {STATUS_LABELS[item.status] ?? item.status}
        </span>
      </td>
      <td className="px-4 py-2 text-xs text-muted-foreground whitespace-nowrap">
        {fmtDate(item.created_at)}
      </td>
      <td className="px-4 py-2" onClick={(e) => e.stopPropagation()}>
        <div className="flex gap-1 justify-end opacity-0 group-hover:opacity-100">
          {item.status === "failed" && (
            <button
              className="text-xs text-blue-600 hover:underline"
              onClick={onReprocess}
            >重试</button>
          )}
          {item.status !== "ignored" && (
            <button
              className="text-xs text-muted-foreground hover:underline disabled:cursor-not-allowed disabled:opacity-50"
              disabled={item.status === "processing"}
              onClick={onDelete}
            >归档</button>
          )}
          <button
            className="text-xs text-destructive hover:underline"
            onClick={onHardDelete}
          >删除</button>
        </div>
      </td>
    </tr>
  );
}

function fileIcon(mime: string | null, refType: string | null): string {
  if (refType === "url" || refType === "feed_entry") return "🔗";
  if (!mime) return "📄";
  if (mime.startsWith("image/")) return "🖼";
  if (mime === "application/pdf") return "📑";
  if (mime.includes("word")) return "📝";
  if (mime.includes("epub") || mime.includes("mobi")) return "📚";
  return "📄";
}

// ── Detail Drawer ─────────────────────────────────────────────────────────────

function DetailDrawer({
  item, onClose, onDelete, onHardDelete, onReprocess, onDocKindChanged,
}: {
  item: DocumentInstance;
  onClose: () => void;
  onDelete: () => void;
  onHardDelete: () => void;
  onReprocess: () => void;
  onDocKindChanged: (docKind: string) => void | Promise<void>;
}) {
  const [showDocKindDialog, setShowDocKindDialog] = useState(false);
  const [typeNotice, setTypeNotice] = useState("");
  const effectiveDocKind = item.doc_kind || item.article_doc_kind;

  async function handleChanged(result: BatchDocKindResponse, docKind: string) {
    if (result.updated === 0) {
      throw new Error(result.results[0]?.detail || "修改内容类型失败");
    }
    setTypeNotice(
      result.wiki_warnings.length > 0
        ? `数据库已更新，但有 ${result.wiki_warnings.length} 项 Wiki 元数据未同步；写入失败可重试，文件缺失需先恢复。`
        : "内容类型已更新。",
    );
    await onDocKindChanged(docKind);
  }

  return (
    <>
    <aside className="flex min-h-0 w-72 shrink-0 flex-col overflow-hidden border-l bg-background">
      <div className="flex shrink-0 items-center justify-between border-b p-3">
        <span className="text-sm font-medium truncate">{item.display_name || "详情"}</span>
        <button
          className="text-muted-foreground hover:text-foreground text-lg leading-none"
          onClick={onClose}
        >×</button>
      </div>
      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4 text-sm">
        <div>
          <p className="text-xs text-muted-foreground mb-0.5">名称</p>
          <p className="font-medium break-all">{item.display_name || item.origin_ref || item.id}</p>
        </div>
        <div>
          <p className="text-xs text-muted-foreground mb-0.5">状态</p>
          <span className={cn(
            "text-xs px-1.5 py-0.5 rounded-full",
            STATUS_COLORS[item.status] || "bg-muted text-muted-foreground",
          )}>
            {STATUS_LABELS[item.status] ?? item.status}
          </span>
        </div>
        <div>
          <p className="text-xs text-muted-foreground mb-0.5">内容类型</p>
          <p>{effectiveDocKind ? (DOC_KIND_LABELS[effectiveDocKind] ?? effectiveDocKind) : "尚未确定"}</p>
          <p className="mt-0.5 text-xs text-muted-foreground">
            {item.doc_kind
              ? "已显式设置；后续重新处理将沿用此类型"
              : effectiveDocKind
                ? "当前文章类型来自入库规则，尚未设置覆盖"
                : "尚未设置覆盖；入库时将继承来源默认类型"}
          </p>
        </div>
        {item.mime_type && (
          <div>
            <p className="text-xs text-muted-foreground mb-0.5">文件类型</p>
            <p className="text-muted-foreground">{item.mime_type}</p>
          </div>
        )}
        {item.origin_ref && (
          <div>
            <p className="text-xs text-muted-foreground mb-0.5">来源</p>
            <p className="break-all text-xs text-muted-foreground">{item.origin_ref}</p>
          </div>
        )}
        <div>
          <p className="text-xs text-muted-foreground mb-0.5">创建时间</p>
          <p className="text-muted-foreground">{fmtDate(item.created_at)}</p>
        </div>

        {item.article_id && (
          <>
            <Separator />
            <div>
              <p className="text-xs text-muted-foreground mb-1">知识文章</p>
              <Link
                href={`/kb/node/${item.article_id}`}
                className="text-sm text-blue-600 hover:underline break-all"
              >
                {item.article_title || item.article_id}
              </Link>
            </div>
          </>
        )}

        <Separator />
        <div className="flex flex-col gap-2">
          <Button
            size="sm"
            variant="outline"
            className="w-full"
            disabled={item.status === "processing"}
            onClick={() => setShowDocKindDialog(true)}
          >
            修改内容类型
          </Button>
          {typeNotice && (
            <p className="text-xs text-muted-foreground">{typeNotice}</p>
          )}
          {item.status === "failed" && (
            <Button size="sm" variant="outline" className="w-full" onClick={onReprocess}>
              重新处理
            </Button>
          )}
          {item.article_id && (
            <Button size="sm" variant="outline" className="w-full" onClick={onReprocess}>
              重新生成 Article
            </Button>
          )}
          {item.status !== "ignored" && (
            <Button
              size="sm"
              variant="outline"
              className="w-full"
              disabled={item.status === "processing"}
              onClick={onDelete}
            >
              归档
            </Button>
          )}
          <Button
            size="sm"
            variant="outline"
            className="w-full text-destructive border-destructive/30 hover:bg-destructive/10"
            onClick={onHardDelete}
          >
            删除（含摘要，不可恢复）
          </Button>
        </div>
      </div>
    </aside>
    <DocKindChangeDialog
      open={showDocKindDialog}
      folderId={item.folder_id}
      ids={[item.id]}
      initialValue={effectiveDocKind}
      onClose={() => setShowDocKindDialog(false)}
      onDone={handleChanged}
    />
    </>
  );
}

// ── Modals ────────────────────────────────────────────────────────────────────

function NewFolderModal({
  open, parentId, onClose, onCreated,
}: {
  open: boolean; parentId: string | null;
  onClose: () => void; onCreated: (f: Folder) => void;
}) {
  const [name, setName] = useState("");
  const [defaultDocKind, setDefaultDocKind] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) { setError("请填写名称"); return; }
    setSaving(true);
    setError("");
    try {
      const r = await fetch("/api/folders", {
        method: "POST", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name.trim(), parent_id: parentId, kind: "normal", default_doc_kind: defaultDocKind || null }),
      });
      if (!r.ok) { const b = await r.json().catch(() => ({})); setError(b.detail || "创建失败"); return; }
      onCreated(await r.json());
      setName(""); setDefaultDocKind("");
    } finally { setSaving(false); }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader><DialogTitle>新建资料夹{parentId ? "（子级）" : ""}</DialogTitle></DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-3">
          <div className="space-y-1.5">
            <Label>名称</Label>
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="例：合同档案" autoFocus />
          </div>
          <div className="space-y-1.5">
            <Label>默认内容类型</Label>
            <DocKindSelect value={defaultDocKind} onChange={setDefaultDocKind} />
          </div>
          {error && <p className="text-xs text-destructive">{error}</p>}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" size="sm" onClick={onClose}>取消</Button>
            <Button type="submit" size="sm" disabled={saving}>{saving ? "创建中…" : "创建"}</Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

interface WechatSubscription {
  feed_id: string;
  name: string;
  enabled: boolean;
}

function NewStreamFolderModal({
  open, onClose, onCreated,
}: {
  open: boolean; onClose: () => void; onCreated: () => void;
}) {
  const [type, setType] = useState<"rss" | "wechat">("rss");
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  // WeChat-specific
  const [subscriptions, setSubscriptions] = useState<WechatSubscription[]>([]);
  const [loadingWechat, setLoadingWechat] = useState(false);
  const [selectedFeedId, setSelectedFeedId] = useState("");

  useEffect(() => {
    if (type !== "wechat") return;
    setError("");
    setLoadingWechat(true);
    fetch("/api/sources/wechat2rss/subscriptions", { credentials: "include" })
      .then(async (res) => {
        if (!res.ok) {
          const b = await res.json().catch(() => ({}));
          throw new Error(b.detail || `加载公众号失败 (${res.status})`);
        }
        return res.json();
      })
      .then((data) => {
        const subs: WechatSubscription[] = data.subscriptions ?? [];
        setSubscriptions(subs);
        const first = subs.find((s) => !s.enabled);
        setSelectedFeedId(first?.feed_id ?? "");
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : "加载公众号失败"))
      .finally(() => setLoadingWechat(false));
  }, [type]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (type === "rss") {
      if (!name.trim()) { setError("请填写名称"); return; }
      if (!url.trim()) { setError("请填写 Feed URL"); return; }
    }
    if (type === "wechat" && !selectedFeedId) { setError("请选择公众号"); return; }
    setSaving(true); setError("");
    try {
      let config: Record<string, string> = {};
      let folderName = name.trim();
      if (type === "rss") {
        config = { url: url.trim() };
      } else {
        const selected = subscriptions.find((s) => s.feed_id === selectedFeedId);
        config = { provider: "wechat2rss", feed_id: selectedFeedId };
        if (!folderName) folderName = selected?.name ?? selectedFeedId;
      }
      const r = await fetch("/api/connectors", {
        method: "POST", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ folder_name: folderName, type, config }),
      });
      if (!r.ok) { const b = await r.json().catch(() => ({})); setError(b.detail || "创建失败"); return; }
      onCreated();
      setName(""); setUrl(""); setType("rss"); setSelectedFeedId("");
    } finally { setSaving(false); }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader><DialogTitle>⚡ 新建订阅资料夹</DialogTitle></DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-3">
          <div className="space-y-1.5">
            <Label>类型</Label>
            <select
              value={type}
              onChange={(e) => { setType(e.target.value as "rss" | "wechat"); setError(""); }}
              className="flex h-9 w-full rounded-md border border-input bg-background px-2 py-1 text-sm shadow-sm focus:outline-none focus:ring-1 focus:ring-ring"
            >
              <option value="rss">RSS</option>
              <option value="wechat">微信公众号</option>
            </select>
          </div>

          {type === "rss" && (
            <>
              <div className="space-y-1.5">
                <Label>名称</Label>
                <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="例：科技早报" autoFocus />
              </div>
              <div className="space-y-1.5">
                <Label>Feed URL</Label>
                <Input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://example.com/feed.xml" type="url" />
              </div>
            </>
          )}

          {type === "wechat" && (
            <>
              {loadingWechat ? (
                <p className="text-xs text-muted-foreground">正在加载公众号列表…</p>
              ) : subscriptions.length === 0 && !error ? (
                <p className="text-xs text-muted-foreground">暂无可选公众号，请先在 Wechat2RSS 管理界面添加订阅。</p>
              ) : (
                <div className="space-y-1.5">
                  <Label>选择公众号</Label>
                  <select
                    value={selectedFeedId}
                    onChange={(e) => setSelectedFeedId(e.target.value)}
                    className="flex h-9 w-full rounded-md border border-input bg-background px-2 py-1 text-sm shadow-sm focus:outline-none focus:ring-1 focus:ring-ring"
                  >
                    {!selectedFeedId && <option value="" disabled>请选择…</option>}
                    {subscriptions.map((s) => (
                      <option key={s.feed_id} value={s.feed_id} disabled={s.enabled}>
                        {s.name}（{s.feed_id}）{s.enabled ? " — 已追踪" : ""}
                      </option>
                    ))}
                  </select>
                  <p className="text-xs text-muted-foreground">资料夹名称默认与公众号同名，可在下方自定义。</p>
                </div>
              )}
              <div className="space-y-1.5">
                <Label>资料夹名称（可选）</Label>
                <Input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder={subscriptions.find((s) => s.feed_id === selectedFeedId)?.name ?? "留空则使用公众号名称"}
                />
              </div>
            </>
          )}

          {error && <p className="text-xs text-destructive">{error}</p>}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" size="sm" onClick={onClose}>取消</Button>
            <Button
              type="submit"
              size="sm"
              disabled={saving || loadingWechat || (type === "wechat" && !selectedFeedId)}
            >{saving ? "创建中…" : "创建"}</Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function UploadModal({
  folderId, onClose, onDone,
}: {
  folderId: string | null; onClose: () => void; onDone: () => void;
}) {
  const [files, setFiles] = useState<FileList | null>(null);
  const [capturedAt, setCapturedAt] = useState("");
  const [effectiveAt, setEffectiveAt] = useState("");
  const [docKind, setDocKind] = useState("");
  const [uploading, setUploading] = useState(false);
  const [result, setResult] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!folderId) return;
    const d = new Date();
    const pad = (n: number) => String(n).padStart(2, "0");
    const now = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
    setCapturedAt(now); setEffectiveAt(now); setResult(""); setFiles(null); setDocKind("");
  }, [folderId]);

  async function handleUpload(e: React.FormEvent) {
    e.preventDefault();
    if (!files || files.length === 0 || !folderId) return;
    setUploading(true);
    try {
      const fd = new FormData();
      for (const f of Array.from(files)) fd.append("files", f);
      if (capturedAt) fd.append("captured_at", new Date(capturedAt).toISOString());
      if (effectiveAt) fd.append("effective_at", new Date(effectiveAt).toISOString());
      if (docKind) fd.append("doc_kind", docKind);
      const r = await fetch(`/api/folders/${folderId}/upload`, {
        method: "POST", credentials: "include", body: fd,
      });
      if (r.ok) {
        const data = await r.json();
        setResult(`已上传 ${data.files_saved} 个文件，处理中…`);
        setTimeout(onDone, 1500);
      } else {
        const err = await r.json().catch(() => ({}));
        setResult(`上传失败：${err.detail || "请重试"}`);
      }
    } finally { setUploading(false); }
  }

  return (
    <Dialog open={!!folderId} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader><DialogTitle>上传文件</DialogTitle></DialogHeader>
        <form onSubmit={handleUpload} className="space-y-4">
          <input
            ref={fileRef}
            type="file"
            multiple
            accept=".pdf,.jpg,.jpeg,.png,.gif,.webp,.txt,.md,.doc,.docx,.epub,.mobi"
            onChange={(e) => setFiles(e.target.files)}
            className="text-sm text-muted-foreground w-full"
          />
          <div className="space-y-1.5">
            <Label htmlFor="up-doc-kind">内容类型</Label>
            <DocKindSelect id="up-doc-kind" value={docKind} onChange={setDocKind} includeEmpty emptyLabel="（不预设）" />
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label>保存时间</Label>
              <Input type="datetime-local" value={capturedAt} onChange={(e) => setCapturedAt(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label>内容时间</Label>
              <Input type="datetime-local" value={effectiveAt} onChange={(e) => setEffectiveAt(e.target.value)} />
            </div>
          </div>
          {result && <p className="text-sm">{result}</p>}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" size="sm" onClick={onClose}>取消</Button>
            <Button type="submit" size="sm" disabled={uploading || !files?.length}>
              {uploading ? "上传中…" : `上传${files?.length ? ` (${files.length})` : ""}`}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function AddUrlModal({
  folderId, onClose, onDone,
}: {
  folderId: string | null; onClose: () => void; onDone: () => void;
}) {
  const [text, setText] = useState("");
  const [docKind, setDocKind] = useState("news");
  const [saving, setSaving] = useState(false);
  const [result, setResult] = useState("");

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!folderId) return;
    const urls = text.split("\n").map((u) => u.trim()).filter(Boolean);
    if (!urls.length) return;
    setSaving(true);
    try {
      const r = await fetch(`/api/folders/${folderId}/add-url`, {
        method: "POST", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ urls, doc_kind: docKind || null }),
      });
      if (r.ok) {
        const data = await r.json();
        setResult(`已加入 ${data.urls_queued} 条 URL，处理中…`);
        setTimeout(onDone, 1500);
      } else {
        const err = await r.json().catch(() => ({}));
        setResult(`操作失败：${err.detail || "请重试"}`);
      }
    } finally { setSaving(false); }
  }

  return (
    <Dialog open={!!folderId} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader><DialogTitle>添加 URL</DialogTitle></DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-4">
          <Textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={"https://example.com/article-1\nhttps://example.com/article-2"}
            rows={5}
            className="text-sm resize-none"
          />
          <p className="text-xs text-muted-foreground">每行一个 URL。</p>
          <div className="space-y-1.5">
            <Label>内容类型</Label>
            <DocKindSelect value={docKind} onChange={setDocKind} includeEmpty emptyLabel="继承资料夹默认" />
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

function RenameFolderModal({
  folder, onClose, onRenamed,
}: {
  folder: Folder; onClose: () => void; onRenamed: (f: Folder) => void;
}) {
  const [name, setName] = useState(folder.name);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) { setError("请填写名称"); return; }
    setSaving(true); setError("");
    try {
      const r = await fetch(`/api/folders/${folder.id}`, {
        method: "PATCH", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name.trim() }),
      });
      if (!r.ok) { const b = await r.json().catch(() => ({})); setError(b.detail || "操作失败"); return; }
      onRenamed(await r.json());
    } finally { setSaving(false); }
  }

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader><DialogTitle>重命名资料夹</DialogTitle></DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-3">
          <Input value={name} onChange={(e) => setName(e.target.value)} autoFocus />
          {error && <p className="text-xs text-destructive">{error}</p>}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" size="sm" onClick={onClose}>取消</Button>
            <Button type="submit" size="sm" disabled={saving}>{saving ? "保存中…" : "保存"}</Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
