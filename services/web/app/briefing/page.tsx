"use client";

import { useEffect, useState, useCallback } from "react";
import {
  DndContext,
  closestCenter,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  DragEndEvent,
} from "@dnd-kit/core";
import {
  arrayMove,
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

// ── 类型 ──────────────────────────────────────────────────────────────────────

interface Topic {
  id: string;
  title: string;
  description: string;
  source_count: number;
  source_node_ids: string[];
  status: string;
  created_at?: string;
}

interface Briefing {
  date: string;
  topics: Topic[];
  generated: boolean;
  created_at?: string;
}

// ── 可拖拽卡片（中栏） ────────────────────────────────────────────────────────

function SortableCard({
  topic,
  onRemove,
}: {
  topic: Topic;
  onRemove: (id: string) => void;
}) {
  const { attributes, listeners, setNodeRef, transform, transition } =
    useSortable({ id: topic.id });

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
  };

  return (
    <div
      ref={setNodeRef}
      style={style}
      className="bg-background border border-border rounded-lg p-3 flex items-start gap-2 cursor-grab active:cursor-grabbing"
    >
      <span
        {...attributes}
        {...listeners}
        className="text-muted-foreground/40 hover:text-muted-foreground mt-0.5 text-lg leading-none select-none"
      >
        ⠿
      </span>
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium truncate">{topic.title}</p>
        {topic.description && (
          <p className="text-xs text-muted-foreground mt-0.5 line-clamp-2">{topic.description}</p>
        )}
      </div>
      <button
        onClick={() => onRemove(topic.id)}
        className="text-muted-foreground/40 hover:text-destructive text-xs shrink-0"
      >
        ✕
      </button>
    </div>
  );
}

// ── 可展开的选题卡片 ──────────────────────────────────────────────────────────

function TopicCard({
  topic,
  isSelected,
  isSkipped,
  onSelect,
  onSkip,
}: {
  topic: Topic;
  isSelected: boolean;
  isSkipped: boolean;
  onSelect: () => void;
  onSkip: () => void;
}) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div
      className={cn(
        "rounded-lg border p-3 transition-opacity",
        isSkipped ? "opacity-30" : "opacity-100",
        isSelected ? "border-primary bg-accent" : "border-border bg-background"
      )}
    >
      <button
        className="w-full text-left"
        onClick={() => setExpanded((v) => !v)}
      >
        <p className="text-sm font-medium leading-snug mb-1">
          {topic.title}
          <span className="ml-1 text-xs text-muted-foreground">{expanded ? "▲" : "▼"}</span>
        </p>
        {topic.description && (
          <p className={`text-xs text-muted-foreground mb-2 ${expanded ? "" : "line-clamp-2"}`}>
            {topic.description}
          </p>
        )}
      </button>
      <div className="flex items-center justify-between">
        <span className="text-xs text-muted-foreground/50">{topic.source_count} 篇来源</span>
        {!isSkipped && !isSelected && (
          <div className="flex gap-1 shrink-0">
            <Button size="sm" className="h-6 text-xs px-2" onClick={onSelect}>选入</Button>
            <Button size="sm" variant="outline" className="h-6 text-xs px-2" onClick={onSkip}>跳过</Button>
          </div>
        )}
        {isSelected && <span className="text-xs text-muted-foreground">已选</span>}
      </div>
    </div>
  );
}

// ── 主页面 ────────────────────────────────────────────────────────────────────

export default function BriefingPage() {
  const [briefing, setBriefing] = useState<Briefing | null>(null);
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [statusMsg, setStatusMsg] = useState("");

  const [selected, setSelected] = useState<Topic[]>([]);
  const [skipped, setSkipped] = useState<Set<string>>(new Set());

  const sensors = useSensors(
    useSensor(PointerSensor),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
    })
  );

  const fetchBriefing = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch("/api/briefing");
      const data: Briefing = await res.json();
      setBriefing(data);
    } catch {
      setStatusMsg("加载失败，请检查服务状态");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchBriefing(); }, [fetchBriefing]);

  async function handleGenerate() {
    setGenerating(true);
    setStatusMsg("⏳ 正在生成选题...");
    try {
      const res = await fetch("/api/briefing/generate", { method: "POST" });
      if (!res.ok) throw new Error(await res.text());
      const data: Briefing = await res.json();
      setBriefing(data);
      setSelected([]);
      setSkipped(new Set());
      setStatusMsg(`✅ 完成，共 ${data.topics.length} 个选题`);
    } catch (e: unknown) {
      setStatusMsg(`❌ 生成失败: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setGenerating(false);
    }
  }

  function selectTopic(topic: Topic) {
    if (selected.some((t) => t.id === topic.id)) return;
    setSelected((prev) => [...prev, topic]);
  }

  function skipTopic(id: string) {
    setSkipped((prev) => new Set([...prev, id]));
  }

  function removeSelected(id: string) {
    setSelected((prev) => prev.filter((t) => t.id !== id));
  }

  function handleDragEnd(event: DragEndEvent) {
    const { active, over } = event;
    if (over && active.id !== over.id) {
      setSelected((items) => {
        const oldIndex = items.findIndex((t) => t.id === active.id);
        const newIndex = items.findIndex((t) => t.id === over.id);
        return arrayMove(items, oldIndex, newIndex);
      });
    }
  }

  return (
    <div className="h-[calc(100vh-52px)] bg-background p-4">
      <div className="h-full flex flex-col rounded-xl border border-border overflow-hidden shadow-sm">
      {/* 顶部状态栏 */}
      <header className="bg-muted/50 border-b border-border px-6 py-3 flex items-center justify-between shrink-0">
        <div className="flex items-center gap-4">
          <h1 className="text-base font-semibold">今日简报</h1>
          {briefing?.created_at && (
            <span className="text-xs text-muted-foreground">
              更新于 {new Date(briefing.created_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}
            </span>
          )}
          {statusMsg && (
            <span className="text-xs text-muted-foreground">{statusMsg}</span>
          )}
        </div>
        <Button size="sm" onClick={handleGenerate} disabled={generating}>
          {generating ? "生成中..." : "立即生成选题"}
        </Button>
      </header>

      {/* 三栏布局 */}
      <div className="flex flex-1 min-h-0">

        {/* 左栏：今日选题 */}
        <div className="w-80 border-r border-border bg-muted/30 flex flex-col overflow-hidden">
          <div className="px-4 py-3 border-b border-border bg-muted/40">
            <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide">今日选题</p>
          </div>
          <div className="flex-1 overflow-y-auto p-3 space-y-2">
            {loading ? (
              <p className="text-sm text-muted-foreground text-center pt-8">加载中...</p>
            ) : !briefing?.generated ? (
              <div className="pt-8 text-center">
                <p className="text-sm text-muted-foreground mb-3">暂无今日选题</p>
                <Button variant="ghost" size="sm" onClick={handleGenerate} disabled={generating}>
                  点击生成
                </Button>
              </div>
            ) : briefing.topics.length === 0 ? (
              <p className="text-sm text-muted-foreground text-center pt-8">今日暂无新内容</p>
            ) : (
              briefing.topics.map((topic) => (
                <TopicCard
                  key={topic.id}
                  topic={topic}
                  isSelected={selected.some((t) => t.id === topic.id)}
                  isSkipped={skipped.has(topic.id)}
                  onSelect={() => selectTopic(topic)}
                  onSkip={() => skipTopic(topic.id)}
                />
              ))
            )}
          </div>
        </div>

        {/* 中栏：已选选题 */}
        <div className="w-72 border-r border-border bg-card flex flex-col overflow-hidden">
          <div className="px-4 py-3 border-b border-border bg-muted/30 flex items-center justify-between">
            <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide">已选选题</p>
            <span className="text-xs text-muted-foreground">{selected.length} 个</span>
          </div>
          <div className="flex-1 overflow-y-auto p-3">
            {selected.length === 0 ? (
              <p className="text-sm text-muted-foreground text-center pt-8">
                从左侧选入选题
              </p>
            ) : (
              <DndContext
                sensors={sensors}
                collisionDetection={closestCenter}
                onDragEnd={handleDragEnd}
              >
                <SortableContext
                  items={selected.map((t) => t.id)}
                  strategy={verticalListSortingStrategy}
                >
                  <div className="space-y-2">
                    {selected.map((topic) => (
                      <SortableCard
                        key={topic.id}
                        topic={topic}
                        onRemove={removeSelected}
                      />
                    ))}
                  </div>
                </SortableContext>
              </DndContext>
            )}
          </div>
        </div>

        {/* 右栏：生成草稿 */}
        <DraftPanel selected={selected} />

      </div>
      </div>
    </div>
  );
}

// ── 右栏：草稿生成面板 ─────────────────────────────────────────────────────────

function DraftPanel({ selected }: { selected: Topic[] }) {
  const [template, setTemplate] = useState("default");
  const [templates, setTemplates] = useState<{ value: string; label: string }[]>([
    { value: "default", label: "默认模板" },
  ]);

  useEffect(() => {
    fetch("/api/files/tree")
      .then((r) => r.json())
      .then((data) => {
        const custom = (data.config as { name: string; rel_path: string }[]).map((f) => {
          const name = f.name.replace(/\.(md|txt)$/, "");
          return { value: name, label: name };
        });
        setTemplates([{ value: "default", label: "默认模板" }, ...custom]);
      })
      .catch(() => {});
  }, []);

  const [drafting, setDrafting] = useState(false);
  const [draftStatus, setDraftStatus] = useState("");
  const [draft, setDraft] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  async function handleGenerate() {
    if (selected.length === 0) return;
    setDrafting(true);
    setDraft(null);
    setCopied(false);
    setDraftStatus("⏳ 正在检索知识库...");

    try {
      setDraftStatus("⏳ 正在生成草稿...");
      const res = await fetch("/api/drafts/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          selected_topic_ids: selected.map((t) => t.id),
          template_name: template,
        }),
      });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      setDraft(data.draft_content);
      setDraftStatus(`✅ 完成（参考了 ${data.knowledge_count} 条知识）`);
    } catch (e: unknown) {
      setDraftStatus(`❌ 生成失败: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setDrafting(false);
    }
  }

  async function handleCopy() {
    if (!draft) return;
    await navigator.clipboard.writeText(draft);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  return (
    <div className="flex-1 bg-muted/20 flex flex-col overflow-hidden">
      <div className="px-4 py-3 border-b border-border bg-muted/30 flex items-center gap-3">
        <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide shrink-0">
          生成草稿
        </p>
        <select
          value={template}
          onChange={(e) => setTemplate(e.target.value)}
          disabled={drafting}
          className="text-xs border border-input rounded-md px-2 py-1 bg-background focus:outline-none focus:ring-1 focus:ring-ring disabled:opacity-40"
        >
          {templates.map((t) => (
            <option key={t.value} value={t.value}>{t.label}</option>
          ))}
        </select>
        <Button
          size="sm"
          className="ml-auto text-xs shrink-0"
          onClick={handleGenerate}
          disabled={drafting || selected.length === 0}
        >
          {drafting ? "生成中..." : "生成草稿"}
        </Button>
      </div>

      {draftStatus && (
        <div className="px-4 py-2 border-b border-border flex items-center justify-between">
          <p className="text-xs text-muted-foreground">{draftStatus}</p>
          {draft && (
            <Button variant="outline" size="sm" className="h-6 text-xs" onClick={handleCopy}>
              {copied ? "已复制 ✓" : "复制全文"}
            </Button>
          )}
        </div>
      )}

      <div className="flex-1 overflow-y-auto p-4">
        {selected.length === 0 ? (
          <p className="text-sm text-muted-foreground text-center pt-12">请先在左侧选择选题</p>
        ) : !draft && !drafting ? (
          <div className="pt-12 text-center">
            <p className="text-sm text-muted-foreground mb-1">
              已选 <span className="font-semibold">{selected.length}</span> 个选题
            </p>
            <p className="text-xs text-muted-foreground/50">点击"生成草稿"开始</p>
          </div>
        ) : drafting ? (
          <div className="pt-12 text-center">
            <p className="text-sm text-muted-foreground">{draftStatus}</p>
          </div>
        ) : (
          <div className="space-y-3">
            <Textarea
              value={draft ?? ""}
              onChange={(e) => setDraft(e.target.value)}
              className="h-[calc(100vh-220px)] text-sm leading-relaxed resize-none"
            />
            <p className="text-xs text-muted-foreground/50 text-center">
              定稿后可提交反馈以改善未来草稿质量（第八步功能）
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
