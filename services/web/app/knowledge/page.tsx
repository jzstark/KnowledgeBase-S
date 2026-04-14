"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
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
  edges: KBEdge[];
}

interface GraphData {
  nodes: KBNode[];
  edges: KBEdge[];
}

// D3 simulation types
type SimNode = KBNode & d3.SimulationNodeDatum;
type SimLink = {
  source: string | SimNode;
  target: string | SimNode;
  relation_type: string;
  weight: number;
  id: number;
};

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

// ── 子组件：节点卡片 ──────────────────────────────────────────────────────────

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
            <span
              key={t}
              className="text-xs bg-blue-50 text-blue-600 px-1.5 py-0.5 rounded"
            >
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

// ── 子组件：列表面板 ──────────────────────────────────────────────────────────

function ListPanel({
  onSelectNode,
  selectedId,
}: {
  onSelectNode: (id: string) => void;
  selectedId?: string;
}) {
  const [nodes, setNodes] = useState<KBNode[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState("");
  const [tagFilter, setTagFilter] = useState("");
  const [offset, setOffset] = useState(0);
  const LIMIT = 50;
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

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

  // initial load + offset changes
  useEffect(() => {
    loadNodes(q, tagFilter, offset);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [offset]);

  // first mount
  useEffect(() => {
    loadNodes("", "", 0);
  }, [loadNodes]);

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
      {/* 搜索栏 */}
      <div className="p-4 border-b border-gray-200 bg-white space-y-2 shrink-0">
        <input
          type="text"
          value={q}
          onChange={(e) => handleQChange(e.target.value)}
          placeholder="搜索标题或摘要…"
          className="w-full text-sm border border-gray-200 rounded-lg px-3 py-1.5 outline-none focus:border-blue-400"
        />
        <div className="flex items-center gap-3">
          <input
            type="text"
            value={tagFilter}
            onChange={(e) => handleTagChange(e.target.value)}
            placeholder="按标签过滤（逗号分隔）"
            className="flex-1 text-sm border border-gray-200 rounded-lg px-3 py-1.5 outline-none focus:border-blue-400"
          />
          <span className="text-xs text-gray-400 shrink-0">共 {total} 个节点</span>
        </div>
      </div>

      {/* 节点网格 */}
      <div className="flex-1 overflow-auto p-4">
        {loading ? (
          <p className="text-sm text-gray-400">加载中…</p>
        ) : nodes.length === 0 ? (
          <p className="text-sm text-gray-400">暂无节点</p>
        ) : (
          <div className="grid grid-cols-2 gap-3">
            {nodes.map((n) => (
              <NodeCard
                key={n.id}
                node={n}
                selected={n.id === selectedId}
                onClick={() => onSelectNode(n.id)}
              />
            ))}
          </div>
        )}
      </div>

      {/* 分页 */}
      {total > LIMIT && (
        <div className="px-4 py-3 border-t border-gray-200 bg-white flex items-center justify-between shrink-0">
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

// ── 主页面 ────────────────────────────────────────────────────────────────────

export default function KnowledgePage() {
  const [view, setView] = useState<"list" | "graph">("list");
  const [maintenanceMsg, setMaintenanceMsg] = useState("");

  // 图谱状态
  const [graphData, setGraphData] = useState<GraphData | null>(null);
  const [graphLoading, setGraphLoading] = useState(false);
  const svgRef = useRef<SVGSVGElement>(null);

  // 侧边栏详情
  const [detail, setDetail] = useState<NodeDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  // 切到图谱视图时加载数据
  useEffect(() => {
    if (view === "graph" && !graphData) {
      loadGraph();
    }
  }, [view]); // eslint-disable-line react-hooks/exhaustive-deps

  async function loadGraph() {
    setGraphLoading(true);
    try {
      const r = await fetch("/api/kb/graph/all", { credentials: "include" });
      if (r.ok) setGraphData(await r.json());
    } finally {
      setGraphLoading(false);
    }
  }

  async function loadDetail(nodeId: string) {
    if (detail?.id === nodeId) {
      setDetail(null);
      return;
    }
    setDetailLoading(true);
    try {
      const r = await fetch(`/api/kb/node/${nodeId}`, { credentials: "include" });
      if (r.ok) setDetail(await r.json());
    } finally {
      setDetailLoading(false);
    }
  }

  // D3 渲染
  useEffect(() => {
    if (view !== "graph" || !graphData || !svgRef.current) return;
    renderGraph(graphData);
  }, [view, graphData]); // eslint-disable-line react-hooks/exhaustive-deps

  function renderGraph(data: GraphData) {
    const svgEl = svgRef.current!;
    const svg = d3.select(svgEl);
    svg.selectAll("*").remove();

    const width = svgEl.clientWidth || 800;
    const height = svgEl.clientHeight || 600;

    // 可缩放容器
    const g = svg.append("g");
    svg.call(
      d3
        .zoom<SVGSVGElement, unknown>()
        .scaleExtent([0.1, 4])
        .on("zoom", (e) => g.attr("transform", e.transform))
    );

    if (data.nodes.length === 0) {
      svg
        .append("text")
        .attr("x", width / 2)
        .attr("y", height / 2)
        .attr("text-anchor", "middle")
        .attr("fill", "#9ca3af")
        .attr("font-size", 14)
        .text("暂无知识节点");
      return;
    }

    // 转换 edges 为 d3 link 格式
    const links: SimLink[] = data.edges.map((e) => ({
      source: e.from_node_id,
      target: e.to_node_id,
      relation_type: e.relation_type,
      weight: e.weight,
      id: e.id,
    }));

    const nodes: SimNode[] = data.nodes.map((n) => ({ ...n }));

    // edges
    const link = g
      .append("g")
      .selectAll<SVGLineElement, SimLink>("line")
      .data(links)
      .enter()
      .append("line")
      .attr("stroke", (d) => EDGE_COLORS[d.relation_type] || "#d1d5db")
      .attr("stroke-width", 1.5)
      .attr("stroke-opacity", 0.6);

    // nodes
    const node = g
      .append("g")
      .selectAll<SVGCircleElement, SimNode>("circle")
      .data(nodes)
      .enter()
      .append("circle")
      .attr("r", (d) => Math.min(8 + (d.degree || 0) * 1.5, 22))
      .attr("fill", "#3b82f6")
      .attr("fill-opacity", 0.8)
      .attr("stroke", "#fff")
      .attr("stroke-width", 1.5)
      .attr("cursor", "pointer")
      .on("click", (_, d) => loadDetail(d.id))
      .call(
        d3
          .drag<SVGCircleElement, SimNode>()
          .on("start", (e, d) => {
            if (!e.active) simulation.alphaTarget(0.3).restart();
            d.fx = d.x;
            d.fy = d.y;
          })
          .on("drag", (e, d) => {
            d.fx = e.x;
            d.fy = e.y;
          })
          .on("end", (e, d) => {
            if (!e.active) simulation.alphaTarget(0);
            d.fx = null;
            d.fy = null;
          })
      );

    // labels
    const label = g
      .append("g")
      .selectAll<SVGTextElement, SimNode>("text")
      .data(nodes)
      .enter()
      .append("text")
      .text((d) => (d.title || d.id).slice(0, 14))
      .attr("font-size", 10)
      .attr("fill", "#374151")
      .attr("pointer-events", "none");

    // simulation
    const simulation = d3
      .forceSimulation<SimNode>(nodes)
      .force(
        "link",
        d3
          .forceLink<SimNode, SimLink>(links)
          .id((d) => d.id)
          .distance(80)
      )
      .force("charge", d3.forceManyBody<SimNode>().strength(-200))
      .force("center", d3.forceCenter(width / 2, height / 2))
      .alphaDecay(0.03)
      .on("tick", () => {
        link
          .attr("x1", (d) => (d.source as SimNode).x ?? 0)
          .attr("y1", (d) => (d.source as SimNode).y ?? 0)
          .attr("x2", (d) => (d.target as SimNode).x ?? 0)
          .attr("y2", (d) => (d.target as SimNode).y ?? 0);
        node.attr("cx", (d) => d.x ?? 0).attr("cy", (d) => d.y ?? 0);
        label
          .attr("x", (d) => (d.x ?? 0) + 12)
          .attr("y", (d) => (d.y ?? 0) + 4);
      });

    // 清理
    return () => simulation.stop();
  }

  async function handleMaintenance() {
    setMaintenanceMsg("维护中…");
    try {
      const r = await fetch("/api/kb/maintenance/run", {
        method: "POST",
        credentials: "include",
      });
      if (r.ok) {
        setMaintenanceMsg("维护已触发，后台运行中");
      } else {
        setMaintenanceMsg("触发失败，请重试");
      }
    } catch {
      setMaintenanceMsg("网络错误");
    }
    setTimeout(() => setMaintenanceMsg(""), 4000);
  }

  return (
    <main className="h-screen bg-gray-50 flex flex-col">
      {/* 顶部工具栏 */}
      <header className="bg-white border-b border-gray-200 px-6 py-3 flex items-center justify-between shrink-0">
        <div className="flex items-center gap-4">
          <Link href="/" className="text-sm text-blue-600 hover:underline">
            ← 首页
          </Link>
          <h1 className="text-base font-semibold text-gray-900">知识库</h1>
          <div className="flex rounded-lg border border-gray-200 overflow-hidden text-xs">
            <button
              onClick={() => setView("list")}
              className={
                view === "list"
                  ? "px-3 py-1 bg-gray-900 text-white"
                  : "px-3 py-1 text-gray-600 hover:bg-gray-50"
              }
            >
              列表
            </button>
            <button
              onClick={() => setView("graph")}
              className={
                view === "graph"
                  ? "px-3 py-1 bg-gray-900 text-white"
                  : "px-3 py-1 text-gray-600 hover:bg-gray-50"
              }
            >
              图谱
            </button>
          </div>
        </div>
        <button
          onClick={handleMaintenance}
          className="text-xs px-3 py-1.5 border border-gray-200 rounded-lg text-gray-600 hover:bg-gray-50 transition-colors"
        >
          {maintenanceMsg || "立即运行维护"}
        </button>
      </header>

      {/* 主体：内容区 + 详情侧边栏 */}
      <div className="flex flex-1 min-h-0">
        {/* 内容区 */}
        <div className="flex-1 overflow-hidden">
          {view === "list" ? (
            <ListPanel onSelectNode={loadDetail} selectedId={detail?.id} />
          ) : (
            <div className="relative h-full">
              {graphLoading && (
                <div className="absolute inset-0 flex items-center justify-center text-gray-400 text-sm z-10 bg-white/60">
                  加载图谱中…
                </div>
              )}
              <svg ref={svgRef} className="w-full h-full" />
            </div>
          )}
        </div>

        {/* 详情侧边栏 */}
        {(detail || detailLoading) && (
          <aside className="w-80 shrink-0 border-l border-gray-200 bg-white overflow-auto">
            {detailLoading ? (
              <div className="p-5 text-sm text-gray-400">加载中…</div>
            ) : detail ? (
              <div className="p-5 space-y-3">
                <div className="flex items-start justify-between gap-2">
                  <h2 className="text-sm font-semibold text-gray-900 leading-snug">
                    {detail.title || detail.id}
                  </h2>
                  <button
                    onClick={() => setDetail(null)}
                    className="shrink-0 text-gray-400 hover:text-gray-600 text-xl leading-none"
                  >
                    ×
                  </button>
                </div>

                <div className="flex items-center gap-2 flex-wrap">
                  <span
                    className={`text-xs px-1.5 py-0.5 rounded font-medium ${
                      SOURCE_TYPE_COLORS[detail.source_type] || "bg-gray-50 text-gray-600"
                    }`}
                  >
                    {detail.source_type || "unknown"}
                  </span>
                  {detail.created_at && (
                    <span className="text-xs text-gray-400">
                      {new Date(detail.created_at).toLocaleDateString("zh-CN")}
                    </span>
                  )}
                </div>

                {(detail.tags || []).length > 0 && (
                  <div className="flex flex-wrap gap-1">
                    {(detail.tags || []).map((t) => (
                      <span
                        key={t}
                        className="text-xs bg-blue-50 text-blue-600 px-1.5 py-0.5 rounded"
                      >
                        {t}
                      </span>
                    ))}
                  </div>
                )}

                {detail.summary && (
                  <p className="text-sm text-gray-700 leading-relaxed">{detail.summary}</p>
                )}

                {(detail.edges || []).length > 0 && (
                  <div>
                    <p className="text-xs font-medium text-gray-500 mb-2">
                      关联 {detail.edges.length} 条边
                    </p>
                    <div className="space-y-1">
                      {detail.edges.slice(0, 10).map((e) => (
                        <div
                          key={e.id}
                          className="flex items-center gap-2 text-xs text-gray-500"
                        >
                          <span
                            className="w-1.5 h-1.5 rounded-full shrink-0"
                            style={{
                              background: EDGE_COLORS[e.relation_type] || "#d1d5db",
                            }}
                          />
                          <span>{e.relation_type}</span>
                          <span className="text-gray-300">
                            {e.from_node_id === detail.id ? "→" : "←"}
                          </span>
                          <span className="text-gray-400">
                            {(e.weight * 100).toFixed(0)}%
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            ) : null}
          </aside>
        )}
      </div>
    </main>
  );
}
