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
      className="bg-white border border-gray-200 rounded-lg p-3 flex items-start gap-2 cursor-grab active:cursor-grabbing"
    >
      <span
        {...attributes}
        {...listeners}
        className="text-gray-300 hover:text-gray-500 mt-0.5 text-lg leading-none select-none"
      >
        ⠿
      </span>
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium text-gray-900 truncate">{topic.title}</p>
        {topic.description && (
          <p className="text-xs text-gray-400 mt-0.5 line-clamp-2">{topic.description}</p>
        )}
      </div>
      <button
        onClick={() => onRemove(topic.id)}
        className="text-gray-300 hover:text-red-400 text-xs shrink-0"
      >
        ✕
      </button>
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

  // ── 渲染 ──────────────────────────────────────────────────────────────────

  return (
    <div className="min-h-screen bg-gray-50">
      {/* 顶部状态栏 */}
      <header className="bg-white border-b border-gray-200 px-6 py-3 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <h1 className="text-base font-semibold text-gray-900">今日简报</h1>
          {briefing?.created_at && (
            <span className="text-xs text-gray-400">
              更新于 {new Date(briefing.created_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}
            </span>
          )}
          {statusMsg && (
            <span className="text-xs text-gray-500">{statusMsg}</span>
          )}
        </div>
        <button
          onClick={handleGenerate}
          disabled={generating}
          className="px-3 py-1.5 bg-gray-900 text-white text-xs rounded-lg
                     hover:bg-gray-700 disabled:opacity-40 transition-colors"
        >
          {generating ? "生成中..." : "立即生成选题"}
        </button>
      </header>

      {/* 三栏布局 */}
      <div className="flex h-[calc(100vh-57px)]">

        {/* 左栏：今日选题 */}
        <div className="w-80 border-r border-gray-200 bg-white flex flex-col overflow-hidden">
          <div className="px-4 py-3 border-b border-gray-100">
            <p className="text-xs font-medium text-gray-500 uppercase tracking-wide">今日选题</p>
          </div>
          <div className="flex-1 overflow-y-auto p-3 space-y-2">
            {loading ? (
              <p className="text-sm text-gray-400 text-center pt-8">加载中...</p>
            ) : !briefing?.generated ? (
              <div className="pt-8 text-center">
                <p className="text-sm text-gray-400 mb-3">暂无今日选题</p>
                <button
                  onClick={handleGenerate}
                  disabled={generating}
                  className="text-xs text-gray-600 underline"
                >
                  点击生成
                </button>
              </div>
            ) : briefing.topics.length === 0 ? (
              <p className="text-sm text-gray-400 text-center pt-8">今日暂无新内容</p>
            ) : (
              briefing.topics.map((topic) => {
                const isSelected = selected.some((t) => t.id === topic.id);
                const isSkipped = skipped.has(topic.id);
                return (
                  <div
                    key={topic.id}
                    className={`rounded-lg border p-3 transition-opacity ${
                      isSkipped ? "opacity-30" : "opacity-100"
                    } ${isSelected ? "border-gray-900 bg-gray-50" : "border-gray-200 bg-white"}`}
                  >
                    <p className="text-sm font-medium text-gray-900 leading-snug mb-1">
                      {topic.title}
                    </p>
                    {topic.description && (
                      <p className="text-xs text-gray-500 line-clamp-2 mb-2">{topic.description}</p>
                    )}
                    <div className="flex items-center justify-between">
                      <span className="text-xs text-gray-300">
                        {topic.source_count} 篇来源
                      </span>
                      {!isSkipped && !isSelected && (
                        <div className="flex gap-1 shrink-0">
                          <button
                            onClick={() => selectTopic(topic)}
                            className="text-xs px-2 py-0.5 bg-gray-900 text-white rounded hover:bg-gray-700"
                          >
                            选入
                          </button>
                          <button
                            onClick={() => skipTopic(topic.id)}
                            className="text-xs px-2 py-0.5 border border-gray-300 text-gray-500 rounded hover:bg-gray-50"
                          >
                            跳过
                          </button>
                        </div>
                      )}
                      {isSelected && (
                        <span className="text-xs text-gray-400">已选</span>
                      )}
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </div>

        {/* 中栏：已选选题 */}
        <div className="w-72 border-r border-gray-200 bg-white flex flex-col overflow-hidden">
          <div className="px-4 py-3 border-b border-gray-100 flex items-center justify-between">
            <p className="text-xs font-medium text-gray-500 uppercase tracking-wide">已选选题</p>
            <span className="text-xs text-gray-400">{selected.length} 个</span>
          </div>
          <div className="flex-1 overflow-y-auto p-3">
            {selected.length === 0 ? (
              <p className="text-sm text-gray-400 text-center pt-8">
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
  );
}

// ── 右栏：草稿生成面板 ─────────────────────────────────────────────────────────

const TEMPLATES = [
  { value: "default", label: "默认模板" },
  { value: "公众号推文", label: "公众号推文" },
  { value: "周报", label: "周报" },
];

function DraftPanel({ selected }: { selected: Topic[] }) {
  const [template, setTemplate] = useState("default");
  const [drafting, setDrafting] = useState(false);
  const [draftStatus, setDraftStatus] = useState("");
  const [draft, setDraft] = useState<string | null>(null);
  const [draftId, setDraftId] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  async function handleGenerate() {
    if (selected.length === 0) return;
    setDrafting(true);
    setDraft(null);
    setDraftId(null);
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
      setDraftId(data.id);
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
    <div className="flex-1 bg-white flex flex-col overflow-hidden">
      <div className="px-4 py-3 border-b border-gray-100 flex items-center gap-3">
        <p className="text-xs font-medium text-gray-500 uppercase tracking-wide shrink-0">
          生成草稿
        </p>
        <select
          value={template}
          onChange={(e) => setTemplate(e.target.value)}
          disabled={drafting}
          className="text-xs border border-gray-200 rounded px-2 py-1 text-gray-700
                     focus:outline-none focus:ring-1 focus:ring-gray-400 disabled:opacity-40"
        >
          {TEMPLATES.map((t) => (
            <option key={t.value} value={t.value}>{t.label}</option>
          ))}
        </select>
        <button
          onClick={handleGenerate}
          disabled={drafting || selected.length === 0}
          className="ml-auto px-3 py-1.5 bg-gray-900 text-white text-xs rounded-lg
                     hover:bg-gray-700 disabled:opacity-40 transition-colors shrink-0"
        >
          {drafting ? "生成中..." : "生成草稿"}
        </button>
      </div>

      {draftStatus && (
        <div className="px-4 py-2 border-b border-gray-100 flex items-center justify-between">
          <p className="text-xs text-gray-500">{draftStatus}</p>
          {draft && (
            <button
              onClick={handleCopy}
              className="text-xs px-2 py-1 border border-gray-300 rounded hover:bg-gray-50"
            >
              {copied ? "已复制 ✓" : "复制全文"}
            </button>
          )}
        </div>
      )}

      <div className="flex-1 overflow-y-auto p-4">
        {selected.length === 0 ? (
          <p className="text-sm text-gray-400 text-center pt-12">请先在左侧选择选题</p>
        ) : !draft && !drafting ? (
          <div className="pt-12 text-center">
            <p className="text-sm text-gray-400 mb-1">
              已选 <span className="font-semibold text-gray-700">{selected.length}</span> 个选题
            </p>
            <p className="text-xs text-gray-300">点击"生成草稿"开始</p>
          </div>
        ) : drafting ? (
          <div className="pt-12 text-center">
            <p className="text-sm text-gray-400">{draftStatus}</p>
          </div>
        ) : (
          <div className="space-y-3">
            <textarea
              value={draft ?? ""}
              onChange={(e) => setDraft(e.target.value)}
              className="w-full h-[calc(100vh-220px)] text-sm text-gray-800 leading-relaxed
                         border border-gray-200 rounded-lg p-3 resize-none
                         focus:outline-none focus:ring-1 focus:ring-gray-400"
            />
            <p className="text-xs text-gray-300 text-center">
              定稿后可提交反馈以改善未来草稿质量（第八步功能）
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
