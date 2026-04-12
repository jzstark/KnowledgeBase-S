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

interface BriefingNode {
  id: string;
  title: string;
  summary: string;
  tags: string[];
  edge_count: number;
  created_at: string;
}

interface BriefingGroup {
  name: string;
  nodes: BriefingNode[];
}

interface Briefing {
  date: string;
  groups: BriefingGroup[];
  generated: boolean;
  created_at?: string;
}

// ── 可拖拽卡片 ────────────────────────────────────────────────────────────────

function SortableCard({
  node,
  onRemove,
}: {
  node: BriefingNode;
  onRemove: (id: string) => void;
}) {
  const { attributes, listeners, setNodeRef, transform, transition } =
    useSortable({ id: node.id });

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
        <p className="text-sm font-medium text-gray-900 truncate">{node.title || "（无标题）"}</p>
        <p className="text-xs text-gray-400 mt-0.5">{node.tags?.slice(0, 3).join(" · ")}</p>
      </div>
      <button
        onClick={() => onRemove(node.id)}
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

  const [selected, setSelected] = useState<BriefingNode[]>([]);
  const [skipped, setSkipped] = useState<Set<string>>(new Set());

  const sensors = useSensors(
    useSensor(PointerSensor),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
    })
  );

  // 拉取今日简报
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

  // 立即生成简报
  async function handleGenerate() {
    setGenerating(true);
    setStatusMsg("⏳ 正在生成简报...");
    try {
      const res = await fetch("/api/briefing/generate", { method: "POST" });
      if (!res.ok) throw new Error(await res.text());
      const data: Briefing = await res.json();
      setBriefing(data);
      setSelected([]);
      setSkipped(new Set());
      const total = data.groups.reduce((s, g) => s + g.nodes.length, 0);
      setStatusMsg(`✅ 完成，共 ${total} 条，${data.groups.length} 个分组`);
    } catch (e: unknown) {
      setStatusMsg(`❌ 生成失败: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setGenerating(false);
    }
  }

  function selectNode(node: BriefingNode) {
    if (selected.some((n) => n.id === node.id)) return;
    setSelected((prev) => [...prev, node]);
  }

  function skipNode(id: string) {
    setSkipped((prev) => new Set([...prev, id]));
  }

  function removeSelected(id: string) {
    setSelected((prev) => prev.filter((n) => n.id !== id));
  }

  function handleDragEnd(event: DragEndEvent) {
    const { active, over } = event;
    if (over && active.id !== over.id) {
      setSelected((items) => {
        const oldIndex = items.findIndex((n) => n.id === active.id);
        const newIndex = items.findIndex((n) => n.id === over.id);
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
          {generating ? "生成中..." : "立即生成简报"}
        </button>
      </header>

      {/* 三栏布局 */}
      <div className="flex h-[calc(100vh-57px)]">

        {/* 左栏：今日文章 */}
        <div className="w-80 border-r border-gray-200 bg-white flex flex-col overflow-hidden">
          <div className="px-4 py-3 border-b border-gray-100">
            <p className="text-xs font-medium text-gray-500 uppercase tracking-wide">今日文章</p>
          </div>
          <div className="flex-1 overflow-y-auto p-3 space-y-4">
            {loading ? (
              <p className="text-sm text-gray-400 text-center pt-8">加载中...</p>
            ) : !briefing?.generated ? (
              <div className="pt-8 text-center">
                <p className="text-sm text-gray-400 mb-3">暂无今日简报</p>
                <button
                  onClick={handleGenerate}
                  disabled={generating}
                  className="text-xs text-gray-600 underline"
                >
                  点击生成
                </button>
              </div>
            ) : briefing.groups.length === 0 ? (
              <p className="text-sm text-gray-400 text-center pt-8">今日暂无新内容</p>
            ) : (
              briefing.groups.map((group) => (
                <div key={group.name}>
                  <p className="text-xs font-semibold text-gray-400 mb-2 px-1">{group.name}</p>
                  <div className="space-y-2">
                    {group.nodes.map((node) => {
                      const isSelected = selected.some((n) => n.id === node.id);
                      const isSkipped = skipped.has(node.id);
                      return (
                        <div
                          key={node.id}
                          className={`rounded-lg border p-3 transition-opacity ${
                            isSkipped ? "opacity-30" : "opacity-100"
                          } ${isSelected ? "border-gray-900 bg-gray-50" : "border-gray-200 bg-white"}`}
                        >
                          <p className="text-sm font-medium text-gray-900 leading-snug mb-1">
                            {node.title || "（无标题）"}
                          </p>
                          <p className="text-xs text-gray-500 line-clamp-2 mb-2">{node.summary}</p>
                          <div className="flex items-center justify-between">
                            <div className="flex gap-1 flex-wrap">
                              {node.tags?.slice(0, 3).map((t) => (
                                <span key={t} className="text-xs bg-gray-100 text-gray-500 px-1.5 py-0.5 rounded">
                                  {t}
                                </span>
                              ))}
                              {node.edge_count > 0 && (
                                <span className="text-xs text-blue-400">
                                  {node.edge_count} 关联
                                </span>
                              )}
                            </div>
                            {!isSkipped && !isSelected && (
                              <div className="flex gap-1 shrink-0">
                                <button
                                  onClick={() => selectNode(node)}
                                  className="text-xs px-2 py-0.5 bg-gray-900 text-white rounded hover:bg-gray-700"
                                >
                                  选入
                                </button>
                                <button
                                  onClick={() => skipNode(node.id)}
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
                    })}
                  </div>
                </div>
              ))
            )}
          </div>
        </div>

        {/* 中栏：已选选题 */}
        <div className="w-72 border-r border-gray-200 bg-white flex flex-col overflow-hidden">
          <div className="px-4 py-3 border-b border-gray-100 flex items-center justify-between">
            <p className="text-xs font-medium text-gray-500 uppercase tracking-wide">已选选题</p>
            <span className="text-xs text-gray-400">{selected.length} 篇</span>
          </div>
          <div className="flex-1 overflow-y-auto p-3">
            {selected.length === 0 ? (
              <p className="text-sm text-gray-400 text-center pt-8">
                从左侧选入文章
              </p>
            ) : (
              <DndContext
                sensors={sensors}
                collisionDetection={closestCenter}
                onDragEnd={handleDragEnd}
              >
                <SortableContext
                  items={selected.map((n) => n.id)}
                  strategy={verticalListSortingStrategy}
                >
                  <div className="space-y-2">
                    {selected.map((node) => (
                      <SortableCard
                        key={node.id}
                        node={node}
                        onRemove={removeSelected}
                      />
                    ))}
                  </div>
                </SortableContext>
              </DndContext>
            )}
          </div>
        </div>

        {/* 右栏：生成草稿（第五步实现） */}
        <div className="flex-1 bg-white flex flex-col overflow-hidden">
          <div className="px-4 py-3 border-b border-gray-100">
            <p className="text-xs font-medium text-gray-500 uppercase tracking-wide">生成草稿</p>
          </div>
          <div className="flex-1 flex flex-col items-center justify-center gap-4 p-6">
            {selected.length === 0 ? (
              <p className="text-sm text-gray-400">请先在左侧选择选题</p>
            ) : (
              <>
                <p className="text-sm text-gray-600">
                  已选 <span className="font-semibold text-gray-900">{selected.length}</span> 篇文章
                </p>
                <p className="text-xs text-gray-400">草稿生成功能将在第五步实现</p>
              </>
            )}
          </div>
        </div>

      </div>
    </div>
  );
}
