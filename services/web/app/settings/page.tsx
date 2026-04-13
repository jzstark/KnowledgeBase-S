"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

// ── Types ─────────────────────────────────────────────────────────────────────

interface Settings {
  topics: string;
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

// ── 保存按钮 ──────────────────────────────────────────────────────────────────

function SaveButton({
  onClick,
  saving,
  saved,
}: {
  onClick: () => void;
  saving: boolean;
  saved: boolean;
}) {
  return (
    <button
      onClick={onClick}
      disabled={saving}
      className="text-sm px-4 py-1.5 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors"
    >
      {saving ? "保存中…" : saved ? "已保存" : "保存"}
    </button>
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
    topics: "",
    briefing_hours_back: 24,
    briefing_time: "08:00",
    maintenance_frequency: "weekly",
  });
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [settingsSaved, setSettingsSaved] = useState(false);

  // ── 模板管理 ────────────────────────────────────────────────────────────────
  const [templates, setTemplates] = useState<string[]>([]);
  const [selectedTpl, setSelectedTpl] = useState<string>("");
  const [tplContent, setTplContent] = useState("");
  const [newTplName, setNewTplName] = useState("");
  const [tplSaving, setTplSaving] = useState(false);
  const [tplSaved, setTplSaved] = useState(false);
  const [tplMsg, setTplMsg] = useState("");

  // ── 偏好规则 ────────────────────────────────────────────────────────────────
  const [rules, setRules] = useState<MemoryRule[]>([]);
  const [rulesLoading, setRulesLoading] = useState(true);

  // ── 初始加载 ────────────────────────────────────────────────────────────────
  useEffect(() => {
    // 设置
    fetch("/api/settings", { credentials: "include" })
      .then((r) => r.json())
      .then((d) => {
        if (d && typeof d === "object") setSettings((prev) => ({ ...prev, ...d }));
      })
      .catch(() => {});

    // 模板列表
    fetch("/api/settings/templates", { credentials: "include" })
      .then((r) => r.json())
      .then((list) => {
        if (Array.isArray(list)) setTemplates(list);
      })
      .catch(() => {});

    // 偏好规则
    loadRules();
  }, []);

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

  // ── 加载模板内容 ─────────────────────────────────────────────────────────────
  async function loadTemplate(name: string) {
    setSelectedTpl(name);
    setTplMsg("");
    if (!name) { setTplContent(""); return; }
    try {
      const r = await fetch(`/api/settings/templates/${encodeURIComponent(name)}`, {
        credentials: "include",
      });
      if (r.ok) {
        const d = await r.json();
        setTplContent(d.content || "");
      }
    } catch { /* ignore */ }
  }

  // ── 保存设置 ────────────────────────────────────────────────────────────────
  async function saveSettings() {
    setSettingsSaving(true);
    setSettingsSaved(false);
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

  // ── 保存模板 ────────────────────────────────────────────────────────────────
  async function saveTemplate() {
    const name = selectedTpl || newTplName.trim();
    if (!name) { setTplMsg("请选择或输入模板名称"); return; }
    setTplSaving(true);
    setTplSaved(false);
    setTplMsg("");
    try {
      const r = await fetch(`/api/settings/templates/${encodeURIComponent(name)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ content: tplContent }),
      });
      if (r.ok) {
        setTplSaved(true);
        setTimeout(() => setTplSaved(false), 2000);
        if (!templates.includes(name)) {
          setTemplates((prev) => [...prev, name].sort());
          setSelectedTpl(name);
          setNewTplName("");
        }
      } else {
        const d = await r.json();
        setTplMsg(d.detail || "保存失败");
      }
    } finally {
      setTplSaving(false);
    }
  }

  // ── 删除模板 ────────────────────────────────────────────────────────────────
  async function deleteTemplate() {
    if (!selectedTpl) return;
    if (!confirm(`确认删除模板「${selectedTpl}」？`)) return;
    await fetch(`/api/settings/templates/${encodeURIComponent(selectedTpl)}`, {
      method: "DELETE",
      credentials: "include",
    });
    setTemplates((prev) => prev.filter((t) => t !== selectedTpl));
    setSelectedTpl("");
    setTplContent("");
  }

  // ── 删除偏好规则 ─────────────────────────────────────────────────────────────
  async function deleteRule(id: number) {
    await fetch(`/api/kb/memory/${id}`, {
      method: "DELETE",
      credentials: "include",
    });
    setRules((prev) => prev.filter((r) => r.id !== id));
  }

  return (
    <main className="min-h-screen bg-gray-50">
      <div className="max-w-2xl mx-auto px-6 py-8 space-y-5">

        {/* 顶部导航 */}
        <div className="flex items-center justify-between">
          <h1 className="text-2xl font-semibold text-gray-900">设置</h1>
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
              <SaveButton onClick={saveSettings} saving={settingsSaving} saved={settingsSaved} />
            </div>
          </div>
        </Section>

        {/* ② 选题方向 */}
        <Section title="选题方向">
          <p className="text-xs text-gray-400 mb-2">
            用自然语言描述你关注的领域，用于每日简报分类和草稿生成。
          </p>
          <textarea
            value={settings.topics}
            onChange={(e) => setSettings({ ...settings, topics: e.target.value })}
            rows={3}
            className="w-full text-sm border border-gray-200 rounded-lg px-3 py-2 outline-none focus:border-blue-400 resize-none"
            placeholder="例如：AI 行业动态、创业融资、产品设计"
          />
          <div className="mt-2">
            <SaveButton onClick={saveSettings} saving={settingsSaving} saved={settingsSaved} />
          </div>
        </Section>

        {/* ③ 写作模板 */}
        <Section title="写作模板">
          <p className="text-xs text-gray-400 mb-3">
            模板是纯自然语言描述，告诉 AI「你想要什么样的文章」。
          </p>

          {/* 模板选择 */}
          <div className="flex items-center gap-2 mb-3">
            <select
              value={selectedTpl}
              onChange={(e) => loadTemplate(e.target.value)}
              className="flex-1 text-sm border border-gray-200 rounded-lg px-3 py-1.5 outline-none focus:border-blue-400"
            >
              <option value="">-- 新建模板 --</option>
              {templates.map((t) => (
                <option key={t} value={t}>{t}</option>
              ))}
            </select>
            {selectedTpl && (
              <button
                onClick={deleteTemplate}
                className="text-sm text-red-500 hover:text-red-700 px-2"
              >
                删除
              </button>
            )}
          </div>

          {/* 新建时输入名称 */}
          {!selectedTpl && (
            <input
              type="text"
              value={newTplName}
              onChange={(e) => setNewTplName(e.target.value)}
              placeholder="新模板名称（如：公众号推文）"
              className="w-full text-sm border border-gray-200 rounded-lg px-3 py-1.5 outline-none focus:border-blue-400 mb-3"
            />
          )}

          {/* 模板内容 */}
          <textarea
            value={tplContent}
            onChange={(e) => setTplContent(e.target.value)}
            rows={6}
            className="w-full text-sm border border-gray-200 rounded-lg px-3 py-2 outline-none focus:border-blue-400 resize-none font-mono"
            placeholder="用自然语言描述你想要的文章风格、结构、长度等…"
          />

          <div className="mt-2 flex items-center gap-3">
            <SaveButton onClick={saveTemplate} saving={tplSaving} saved={tplSaved} />
            {tplMsg && <span className="text-xs text-red-500">{tplMsg}</span>}
          </div>
        </Section>

        {/* ④ 偏好规则 */}
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
                        <span className="text-xs text-blue-500">
                          {r.template_name}
                        </span>
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

      </div>
    </main>
  );
}
