"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

// ── Types ─────────────────────────────────────────────────────────────────────

interface Template {
  name: string;
  content: string;
}

// ── Section 容器 ──────────────────────────────────────────────────────────────

function Section({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
      <div className="px-5 py-3 border-b border-gray-100">
        <h2 className="text-sm font-semibold text-gray-700">{title}</h2>
      </div>
      <div className="px-5 py-4">
        {description && (
          <p className="text-xs text-gray-400 mb-4">{description}</p>
        )}
        {children}
      </div>
    </div>
  );
}

// ── 主页面 ────────────────────────────────────────────────────────────────────

export default function InstructionsPage() {
  // ── 选题方向 ─────────────────────────────────────────────────────────────────
  const [topics, setTopics] = useState("");
  const [topicsLoading, setTopicsLoading] = useState(true);
  const [topicsEditing, setTopicsEditing] = useState(false);
  const [topicsDraft, setTopicsDraft] = useState("");
  const [topicsSaving, setTopicsSaving] = useState(false);
  const [topicsSaved, setTopicsSaved] = useState(false);

  // ── 写作模板 ─────────────────────────────────────────────────────────────────
  const [templateList, setTemplateList] = useState<Template[]>([]);
  const [templatesLoading, setTemplatesLoading] = useState(true);
  const [editingTpl, setEditingTpl] = useState<string | null>(null);
  const [editContent, setEditContent] = useState("");
  const [newTplMode, setNewTplMode] = useState(false);
  const [newTplName, setNewTplName] = useState("");
  const [newTplContent, setNewTplContent] = useState("");
  const [tplSaving, setTplSaving] = useState(false);
  const [tplMsg, setTplMsg] = useState("");

  // ── Schema ───────────────────────────────────────────────────────────────────
  const [schema, setSchema] = useState("");
  const [schemaLoading, setSchemaLoading] = useState(true);
  const [schemaEditing, setSchemaEditing] = useState(false);
  const [schemaDraft, setSchemaDraft] = useState("");
  const [schemaSaving, setSchemaSaving] = useState(false);
  const [schemaSaved, setSchemaSaved] = useState(false);

  // ── 初始加载 ────────────────────────────────────────────────────────────────
  useEffect(() => {
    fetch("/api/settings/topics", { credentials: "include" })
      .then((r) => r.json())
      .then((d) => setTopics(d.content || ""))
      .catch(() => {})
      .finally(() => setTopicsLoading(false));

    loadAllTemplates();

    fetch("/api/settings/schema", { credentials: "include" })
      .then((r) => r.json())
      .then((d) => setSchema(d.content || ""))
      .catch(() => {})
      .finally(() => setSchemaLoading(false));
  }, []);

  // ── 选题方向 ─────────────────────────────────────────────────────────────────
  async function saveTopics() {
    setTopicsSaving(true);
    try {
      const r = await fetch("/api/settings/topics", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ content: topicsDraft }),
      });
      if (r.ok) {
        setTopics(topicsDraft);
        setTopicsEditing(false);
        setTopicsSaved(true);
        setTimeout(() => setTopicsSaved(false), 2000);
      }
    } finally {
      setTopicsSaving(false);
    }
  }

  // ── 写作模板 ─────────────────────────────────────────────────────────────────
  async function loadAllTemplates() {
    setTemplatesLoading(true);
    try {
      const r = await fetch("/api/settings/templates", { credentials: "include" });
      if (!r.ok) return;
      const names: string[] = await r.json();
      const loaded = await Promise.all(
        names.map(async (name) => {
          const tr = await fetch(
            `/api/settings/templates/${encodeURIComponent(name)}`,
            { credentials: "include" }
          );
          const d = tr.ok ? await tr.json() : {};
          return { name, content: d.content || "" } as Template;
        })
      );
      setTemplateList(loaded);
    } finally {
      setTemplatesLoading(false);
    }
  }

  function startEditTpl(name: string, content: string) {
    setEditingTpl(name);
    setEditContent(content);
    setTplMsg("");
    setNewTplMode(false);
  }

  async function saveEditTpl() {
    if (!editingTpl) return;
    setTplSaving(true);
    setTplMsg("");
    try {
      const r = await fetch(
        `/api/settings/templates/${encodeURIComponent(editingTpl)}`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          credentials: "include",
          body: JSON.stringify({ content: editContent }),
        }
      );
      if (r.ok) {
        setTemplateList((prev) =>
          prev.map((t) => (t.name === editingTpl ? { ...t, content: editContent } : t))
        );
        setEditingTpl(null);
      } else {
        setTplMsg("保存失败");
      }
    } finally {
      setTplSaving(false);
    }
  }

  async function deleteTpl(name: string) {
    if (!confirm(`确认删除模板「${name}」？`)) return;
    await fetch(`/api/settings/templates/${encodeURIComponent(name)}`, {
      method: "DELETE",
      credentials: "include",
    });
    setTemplateList((prev) => prev.filter((t) => t.name !== name));
    if (editingTpl === name) setEditingTpl(null);
  }

  async function saveNewTpl() {
    const name = newTplName.trim();
    if (!name) { setTplMsg("请输入模板名称"); return; }
    setTplSaving(true);
    setTplMsg("");
    try {
      const r = await fetch(
        `/api/settings/templates/${encodeURIComponent(name)}`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          credentials: "include",
          body: JSON.stringify({ content: newTplContent }),
        }
      );
      if (r.ok) {
        setTemplateList((prev) =>
          [...prev, { name, content: newTplContent }].sort((a, b) =>
            a.name.localeCompare(b.name)
          )
        );
        setNewTplMode(false);
        setNewTplName("");
        setNewTplContent("");
      } else {
        const d = await r.json();
        setTplMsg(d.detail || "保存失败");
      }
    } finally {
      setTplSaving(false);
    }
  }

  // ── Schema ───────────────────────────────────────────────────────────────────
  async function saveSchema() {
    setSchemaSaving(true);
    try {
      const r = await fetch("/api/settings/schema", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ content: schemaDraft }),
      });
      if (r.ok) {
        setSchema(schemaDraft);
        setSchemaEditing(false);
        setSchemaSaved(true);
        setTimeout(() => setSchemaSaved(false), 2000);
      }
    } finally {
      setSchemaSaving(false);
    }
  }

  // ── 渲染 ──────────────────────────────────────────────────────────────────────

  return (
    <main className="min-h-screen bg-gray-50">
      <div className="max-w-2xl mx-auto px-6 py-8 space-y-5">

        {/* 顶部导航 */}
        <div className="flex items-center justify-between">
          <h1 className="text-2xl font-semibold text-gray-900">指令设置</h1>
          <Link href="/" className="text-sm text-blue-600 hover:underline">← 返回首页</Link>
        </div>

        {/* ① 选题方向 */}
        <Section
          title="选题方向"
          description="用自然语言描述你关注的领域，用于每日简报分类和草稿生成。"
        >
          {topicsLoading ? (
            <div className="h-16 bg-gray-100 animate-pulse rounded-lg" />
          ) : topicsEditing ? (
            <div className="space-y-2">
              <textarea
                value={topicsDraft}
                onChange={(e) => setTopicsDraft(e.target.value)}
                rows={4}
                autoFocus
                className="w-full text-sm border border-blue-300 rounded-lg px-3 py-2 outline-none focus:border-blue-400 resize-none"
                placeholder="例如：AI 行业动态、创业融资、产品设计"
              />
              <div className="flex items-center gap-2">
                <button
                  onClick={saveTopics}
                  disabled={topicsSaving}
                  className="text-sm px-4 py-1.5 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors"
                >
                  {topicsSaving ? "保存中…" : "保存"}
                </button>
                <button
                  onClick={() => setTopicsEditing(false)}
                  className="text-sm text-gray-400 hover:text-gray-600"
                >
                  取消
                </button>
                {topicsSaved && <span className="text-xs text-green-600">已保存</span>}
              </div>
            </div>
          ) : (
            <div>
              <div className="bg-gray-50 rounded-lg px-4 py-3 text-sm text-gray-700 whitespace-pre-wrap leading-relaxed min-h-[3rem]">
                {topics || <span className="text-gray-400 italic">（未设置）</span>}
              </div>
              <button
                onClick={() => { setTopicsDraft(topics); setTopicsEditing(true); }}
                className="mt-2 text-xs text-blue-600 hover:text-blue-800"
              >
                编辑
              </button>
            </div>
          )}
        </Section>

        {/* ② 写作模板 */}
        <Section
          title="写作模板"
          description="模板是纯自然语言描述，告诉 AI「你想要什么样的文章」。"
        >
          {templatesLoading ? (
            <div className="h-24 bg-gray-100 animate-pulse rounded-lg" />
          ) : (
            <div className="space-y-3">
              {templateList.length === 0 && !newTplMode && (
                <p className="text-sm text-gray-400">暂无模板。</p>
              )}

              {templateList.map((tpl) => (
                <div key={tpl.name} className="border border-gray-200 rounded-lg overflow-hidden">
                  <div className="flex items-center justify-between px-4 py-2.5 bg-gray-50 border-b border-gray-200">
                    <span className="text-sm font-medium text-gray-800">{tpl.name}</span>
                    <div className="flex items-center gap-3">
                      {editingTpl !== tpl.name && (
                        <button
                          onClick={() => startEditTpl(tpl.name, tpl.content)}
                          className="text-xs text-blue-600 hover:text-blue-800"
                        >
                          编辑
                        </button>
                      )}
                      <button
                        onClick={() => deleteTpl(tpl.name)}
                        className="text-xs text-gray-400 hover:text-red-500"
                      >
                        删除
                      </button>
                    </div>
                  </div>

                  {editingTpl === tpl.name ? (
                    <div className="p-3 space-y-2">
                      <textarea
                        value={editContent}
                        onChange={(e) => setEditContent(e.target.value)}
                        rows={7}
                        autoFocus
                        className="w-full text-sm border border-blue-300 rounded-lg px-3 py-2 outline-none focus:border-blue-400 resize-y font-mono"
                      />
                      <div className="flex items-center gap-2">
                        <button
                          onClick={saveEditTpl}
                          disabled={tplSaving}
                          className="text-sm px-3 py-1 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50"
                        >
                          {tplSaving ? "保存中…" : "保存"}
                        </button>
                        <button
                          onClick={() => setEditingTpl(null)}
                          className="text-sm text-gray-400 hover:text-gray-600"
                        >
                          取消
                        </button>
                        {tplMsg && <span className="text-xs text-red-500">{tplMsg}</span>}
                      </div>
                    </div>
                  ) : (
                    <div className="px-4 py-3 text-sm text-gray-600 whitespace-pre-wrap leading-relaxed font-mono max-h-48 overflow-y-auto">
                      {tpl.content || <span className="text-gray-400 italic">（内容为空）</span>}
                    </div>
                  )}
                </div>
              ))}

              {newTplMode && (
                <div className="border border-blue-200 rounded-lg overflow-hidden">
                  <div className="px-4 py-2.5 bg-blue-50 border-b border-blue-200">
                    <input
                      type="text"
                      value={newTplName}
                      onChange={(e) => setNewTplName(e.target.value)}
                      placeholder="模板名称（如：公众号推文）"
                      autoFocus
                      className="w-full text-sm bg-transparent outline-none font-medium text-gray-800 placeholder:text-gray-400"
                    />
                  </div>
                  <div className="p-3 space-y-2">
                    <textarea
                      value={newTplContent}
                      onChange={(e) => setNewTplContent(e.target.value)}
                      rows={7}
                      className="w-full text-sm border border-gray-200 rounded-lg px-3 py-2 outline-none focus:border-blue-400 resize-y font-mono"
                      placeholder="用自然语言描述你想要的文章风格、结构、长度等…"
                    />
                    <div className="flex items-center gap-2">
                      <button
                        onClick={saveNewTpl}
                        disabled={tplSaving}
                        className="text-sm px-3 py-1 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50"
                      >
                        {tplSaving ? "保存中…" : "保存"}
                      </button>
                      <button
                        onClick={() => {
                          setNewTplMode(false);
                          setNewTplName("");
                          setNewTplContent("");
                          setTplMsg("");
                        }}
                        className="text-sm text-gray-400 hover:text-gray-600"
                      >
                        取消
                      </button>
                      {tplMsg && <span className="text-xs text-red-500">{tplMsg}</span>}
                    </div>
                  </div>
                </div>
              )}
            </div>
          )}

          {!newTplMode && !templatesLoading && (
            <button
              onClick={() => { setNewTplMode(true); setEditingTpl(null); setTplMsg(""); }}
              className="mt-3 text-sm text-blue-600 hover:text-blue-800"
            >
              + 新建模板
            </button>
          )}
        </Section>

        {/* ③ 知识库宪法（Schema） */}
        <Section title="知识库宪法（Schema）">
          {/* 警告横幅 */}
          <div className="flex items-start gap-2 bg-amber-50 border border-amber-200 rounded-lg px-4 py-3 mb-4 text-xs text-amber-800">
            <span className="shrink-0 mt-0.5">⚠️</span>
            <span>
              此文件定义系统如何理解和处理所有内容，修改后只影响
              <strong>新入库</strong>内容，不影响已有节点。
              建议保留各节标题，只修改具体描述；若内容缺失，系统将使用内置默认行为，不会崩溃。
            </span>
          </div>

          {schemaLoading ? (
            <div className="h-32 bg-gray-100 animate-pulse rounded-lg" />
          ) : schemaEditing ? (
            <div className="space-y-2">
              <textarea
                value={schemaDraft}
                onChange={(e) => setSchemaDraft(e.target.value)}
                rows={22}
                autoFocus
                className="w-full text-sm border border-blue-300 rounded-lg px-3 py-2 outline-none focus:border-blue-400 resize-y font-mono"
              />
              <div className="flex items-center gap-2">
                <button
                  onClick={saveSchema}
                  disabled={schemaSaving}
                  className="text-sm px-4 py-1.5 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors"
                >
                  {schemaSaving ? "保存中…" : "保存"}
                </button>
                <button
                  onClick={() => setSchemaEditing(false)}
                  className="text-sm text-gray-400 hover:text-gray-600"
                >
                  取消
                </button>
                {schemaSaved && <span className="text-xs text-green-600">已保存</span>}
              </div>
            </div>
          ) : (
            <div>
              <div className="bg-gray-50 rounded-lg px-4 py-3 text-sm text-gray-700 whitespace-pre-wrap leading-relaxed font-mono max-h-96 overflow-y-auto">
                {schema || <span className="text-gray-400 italic">（未设置）</span>}
              </div>
              <button
                onClick={() => { setSchemaDraft(schema); setSchemaEditing(true); }}
                className="mt-2 text-xs text-blue-600 hover:text-blue-800"
              >
                编辑
              </button>
            </div>
          )}
        </Section>

      </div>
    </main>
  );
}
