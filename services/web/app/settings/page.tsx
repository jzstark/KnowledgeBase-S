"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

// ── Types ─────────────────────────────────────────────────────────────────────

interface Settings {
  briefing_hours_back: number;
  briefing_time: string;
  maintenance_frequency: string;
}

interface MemoryRule {
  id: number;
  template_name: string;
  rule: string;
  rule_type: string;
  confidence: number;
  count: number;
}

// ── Section 容器 ──────────────────────────────────────────────────────────────

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
      <div className="px-5 py-3 border-b border-gray-100">
        <h2 className="text-sm font-semibold text-gray-700">{title}</h2>
      </div>
      <div className="px-5 py-4">{children}</div>
    </div>
  );
}

// ── 置信度进度条 ──────────────────────────────────────────────────────────────

function ConfidenceBar({ value }: { value: number }) {
  const pct = Math.round(value * 100);
  const color =
    value >= 0.8 ? "bg-green-500" : value >= 0.5 ? "bg-blue-400" : "bg-gray-300";
  return (
    <div className="flex items-center gap-2">
      <div className="w-20 h-1.5 bg-gray-100 rounded-full overflow-hidden">
        <div className={`h-full rounded-full ${color}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs text-gray-500">{pct}%</span>
    </div>
  );
}

const RULE_TYPE_LABELS: Record<string, string> = {
  style: "风格",
  structure: "结构",
  content: "内容",
  tone: "语气",
};

// ── 主页面 ────────────────────────────────────────────────────────────────────

export default function SettingsPage() {
  // ── 基本设置 ────────────────────────────────────────────────────────────────
  const [settings, setSettings] = useState<Settings>({
    briefing_hours_back: 24,
    briefing_time: "08:00",
    maintenance_frequency: "weekly",
  });
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [settingsSaved, setSettingsSaved] = useState(false);

  // ── 偏好规则 ────────────────────────────────────────────────────────────────
  const [rules, setRules] = useState<MemoryRule[]>([]);
  const [rulesLoading, setRulesLoading] = useState(true);

  // ── Obsidian 同步 ────────────────────────────────────────────────────────────
  const [wikiStatus, setWikiStatus] = useState<{ synced_count: number; index_exists: boolean } | null>(null);
  const [wikiRebuilding, setWikiRebuilding] = useState(false);
  const [wikiMsg, setWikiMsg] = useState("");

  // ── 初始加载 ────────────────────────────────────────────────────────────────
  useEffect(() => {
    fetch("/api/settings", { credentials: "include" })
      .then((r) => r.json())
      .then((d) => {
        if (d && typeof d === "object") setSettings((prev) => ({ ...prev, ...d }));
      })
      .catch(() => {});

    loadRules();
    loadWikiStatus();
  }, []);

  // ── 流程节奏 ─────────────────────────────────────────────────────────────────
  async function saveSchedule() {
    setSettingsSaving(true);
    try {
      await fetch("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify(settings),
      });
      setSettingsSaved(true);
      setTimeout(() => setSettingsSaved(false), 2000);
    } finally {
      setSettingsSaving(false);
    }
  }

  // ── 偏好规则 ────────────────────────────────────────────────────────────────
  async function loadRules() {
    setRulesLoading(true);
    try {
      const r = await fetch("/api/kb/memory", { credentials: "include" });
      if (r.ok) {
        const data = await r.json();
        if (Array.isArray(data)) setRules(data);
      }
    } finally {
      setRulesLoading(false);
    }
  }

  async function deleteRule(id: number) {
    await fetch(`/api/kb/memory/${id}`, { method: "DELETE", credentials: "include" });
    setRules((prev) => prev.filter((r) => r.id !== id));
  }

  // ── Wiki 同步 ────────────────────────────────────────────────────────────────
  async function loadWikiStatus() {
    try {
      const r = await fetch("/api/kb/wiki/status");
      if (r.ok) setWikiStatus(await r.json());
    } catch { /* ignore */ }
  }

  async function rebuildWiki() {
    setWikiRebuilding(true);
    setWikiMsg("");
    try {
      const r = await fetch("/api/kb/wiki/rebuild", {
        method: "POST",
        credentials: "include",
      });
      if (r.ok) {
        setWikiMsg("重建已触发，后台运行中…");
        setTimeout(async () => {
          await loadWikiStatus();
          setWikiMsg("");
          setWikiRebuilding(false);
        }, 3000);
      } else {
        setWikiMsg("触发失败，请重试");
        setWikiRebuilding(false);
      }
    } catch {
      setWikiMsg("网络错误");
      setWikiRebuilding(false);
    }
  }

  // ── 渲染 ──────────────────────────────────────────────────────────────────────

  return (
    <main className="min-h-screen bg-gray-50">
      <div className="max-w-2xl mx-auto px-6 py-8 space-y-5">

        {/* 顶部导航 */}
        <div className="flex items-center justify-between">
          <h1 className="text-2xl font-semibold text-gray-900">系统设置</h1>
          <Link href="/" className="text-sm text-blue-600 hover:underline">← 返回首页</Link>
        </div>

        {/* ① 流程节奏 */}
        <Section title="流程节奏">
          <div className="space-y-3">
            <div className="flex items-center gap-3">
              <label className="text-sm text-gray-600 w-32 shrink-0">简报生成时间</label>
              <input
                type="time"
                value={settings.briefing_time}
                onChange={(e) => setSettings({ ...settings, briefing_time: e.target.value })}
                className="text-sm border border-gray-200 rounded-lg px-3 py-1.5 outline-none focus:border-blue-400"
              />
            </div>
            <div className="flex items-center gap-3">
              <label className="text-sm text-gray-600 w-32 shrink-0">覆盖最近（小时）</label>
              <input
                type="number"
                min={1}
                max={168}
                value={settings.briefing_hours_back}
                onChange={(e) =>
                  setSettings({ ...settings, briefing_hours_back: parseInt(e.target.value) || 24 })
                }
                className="text-sm border border-gray-200 rounded-lg px-3 py-1.5 outline-none focus:border-blue-400 w-20"
              />
            </div>
            <div className="pt-1">
              <button
                onClick={saveSchedule}
                disabled={settingsSaving}
                className="text-sm px-4 py-1.5 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors"
              >
                {settingsSaving ? "保存中…" : settingsSaved ? "已保存" : "保存"}
              </button>
            </div>
          </div>
        </Section>

        {/* ② 写作偏好规则 */}
        <Section title="写作偏好规则">
          <p className="text-xs text-gray-400 mb-3">
            由系统从你的定稿修改中自动学习。置信度 ≥ 80% 的规则会在生成草稿时自动应用。
          </p>

          {rulesLoading ? (
            <p className="text-sm text-gray-400">加载中…</p>
          ) : rules.length === 0 ? (
            <p className="text-sm text-gray-400">
              暂无学习到的偏好规则。在草稿历史页提交定稿后，系统会自动学习。
            </p>
          ) : (
            <div className="space-y-2">
              {rules.map((r) => (
                <div
                  key={r.id}
                  className="flex items-start gap-3 py-2 border-b border-gray-100 last:border-0"
                >
                  <div className="flex-1 min-w-0">
                    <p className="text-sm text-gray-800 leading-relaxed">{r.rule}</p>
                    <div className="flex items-center gap-3 mt-1">
                      <span className="text-xs text-gray-400 bg-gray-100 px-1.5 py-0.5 rounded">
                        {RULE_TYPE_LABELS[r.rule_type] || r.rule_type}
                      </span>
                      <ConfidenceBar value={r.confidence} />
                      <span className="text-xs text-gray-400">出现 {r.count} 次</span>
                      {r.template_name && (
                        <span className="text-xs text-blue-500">{r.template_name}</span>
                      )}
                    </div>
                  </div>
                  <button
                    onClick={() => deleteRule(r.id)}
                    className="shrink-0 text-xs text-gray-400 hover:text-red-500 transition-colors px-1"
                    title="删除此规则"
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>
          )}
        </Section>

        {/* ③ Obsidian 同步 */}
        <Section title="Obsidian 同步">
          <p className="text-xs text-gray-400 mb-3">
            单向同步：将知识库节点写入{" "}
            <code className="bg-gray-100 px-1 rounded">user_data/wiki/nodes/</code>
            ，可将该目录作为 Obsidian vault 打开（支持双链与图谱视图）。
            新节点入库时自动同步，此处可触发全量重建。
          </p>

          {wikiStatus && (
            <div className="flex items-center gap-4 mb-3 text-sm text-gray-600">
              <span>已同步节点：<strong>{wikiStatus.synced_count}</strong></span>
              <span className={wikiStatus.index_exists ? "text-green-600" : "text-gray-400"}>
                {wikiStatus.index_exists ? "✓ index.md 存在" : "× index.md 未生成"}
              </span>
            </div>
          )}

          <div className="flex items-center gap-3">
            <button
              onClick={rebuildWiki}
              disabled={wikiRebuilding}
              className="text-sm px-4 py-1.5 bg-gray-800 text-white rounded-lg hover:bg-gray-900 disabled:opacity-50 transition-colors"
            >
              {wikiRebuilding ? "重建中…" : "全量重建"}
            </button>
            {wikiMsg && <span className="text-xs text-blue-600">{wikiMsg}</span>}
          </div>
        </Section>

        {/* ④ 数据导出 */}
        <Section title="数据导出">
          <p className="text-xs text-gray-400 mb-3">
            打包下载 user_data/ 目录，包含 wiki 文件、原始内容、配置（选题方向、写作模板、Schema）。
            解压后 wiki/ 目录可直接作为 Obsidian vault 打开。
          </p>
          <a
            href="/api/settings/export"
            className="inline-block text-sm px-4 py-1.5 bg-gray-800 text-white rounded-lg hover:bg-gray-900 transition-colors"
          >
            下载数据包
          </a>
        </Section>

      </div>
    </main>
  );
}
