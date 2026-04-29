"use client";

import { useEffect, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { AlertTriangle } from "lucide-react";

// ── Types ─────────────────────────────────────────────────────────────────────

interface Template {
  name: string;
  content: string;
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
  const [topicsRegenStatus, setTopicsRegenStatus] = useState<"idle" | "generating" | "done">("idle");
  const [topicsRegenCount, setTopicsRegenCount] = useState(0);

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
        setTopicsRegenStatus("idle");
      }
    } finally {
      setTopicsSaving(false);
    }
  }

  async function regenTopics() {
    setTopicsRegenStatus("generating");
    try {
      const r = await fetch("/api/briefing/generate?force=true", {
        method: "POST",
        credentials: "include",
      });
      if (r.ok) {
        const data = await r.json();
        setTopicsRegenCount(data.topics?.length ?? 0);
        setTopicsRegenStatus("done");
      } else {
        setTopicsRegenStatus("idle");
      }
    } catch {
      setTopicsRegenStatus("idle");
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
    <main className="min-h-screen bg-background">
      <div className="max-w-2xl mx-auto px-6 py-8 space-y-5">

        <h1 className="text-2xl font-semibold">指令设置</h1>

        {/* ① 选题方向 */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-sm font-semibold">选题方向</CardTitle>
            <p className="text-xs text-muted-foreground">用自然语言描述你关注的领域，用于每日简报分类和草稿生成。</p>
          </CardHeader>
          <CardContent>
            {topicsLoading ? (
              <div className="h-16 bg-muted animate-pulse rounded-lg" />
            ) : topicsEditing ? (
              <div className="space-y-2">
                <Textarea
                  value={topicsDraft}
                  onChange={(e) => setTopicsDraft(e.target.value)}
                  rows={4}
                  autoFocus
                  placeholder="例如：AI 行业动态、创业融资、产品设计"
                  className="resize-none text-sm"
                />
                <div className="flex items-center gap-2">
                  <Button size="sm" onClick={saveTopics} disabled={topicsSaving}>
                    {topicsSaving ? "保存中…" : "保存"}
                  </Button>
                  <Button size="sm" variant="ghost" className="text-muted-foreground" onClick={() => setTopicsEditing(false)}>
                    取消
                  </Button>
                </div>
              </div>
            ) : (
              <div>
                <div className="bg-muted/50 rounded-lg px-4 py-3 text-sm whitespace-pre-wrap leading-relaxed min-h-[3rem]">
                  {topics || <span className="text-muted-foreground italic">（未设置）</span>}
                </div>
                <div className="mt-2 flex items-center gap-3 flex-wrap">
                  <Button
                    size="sm"
                    variant="ghost"
                    className="h-7 text-xs text-muted-foreground"
                    onClick={() => { setTopicsDraft(topics); setTopicsEditing(true); }}
                  >
                    编辑
                  </Button>
                  {topicsSaved && (
                    <>
                      <span className="text-xs text-green-600">已保存</span>
                      <Button
                        size="sm"
                        variant="secondary"
                        className="h-7 text-xs"
                        onClick={regenTopics}
                        disabled={topicsRegenStatus === "generating"}
                      >
                        {topicsRegenStatus === "generating"
                          ? "重新生成中…"
                          : topicsRegenStatus === "done"
                          ? `✓ 已生成 ${topicsRegenCount} 条新选题`
                          : "用新方向重新生成今日选题"}
                      </Button>
                    </>
                  )}
                </div>
              </div>
            )}
          </CardContent>
        </Card>

        {/* ② 写作模板 */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-sm font-semibold">写作模板</CardTitle>
            <p className="text-xs text-muted-foreground">模板是纯自然语言描述，告诉 AI「你想要什么样的文章」。</p>
          </CardHeader>
          <CardContent>
            {templatesLoading ? (
              <div className="h-24 bg-muted animate-pulse rounded-lg" />
            ) : (
              <div className="space-y-3">
                {templateList.length === 0 && !newTplMode && (
                  <p className="text-sm text-muted-foreground">暂无模板。</p>
                )}

                {templateList.map((tpl) => (
                  <div key={tpl.name} className="border border-border rounded-lg overflow-hidden">
                    <div className="flex items-center justify-between px-4 py-2.5 bg-muted/30 border-b border-border">
                      <span className="text-sm font-medium">{tpl.name}</span>
                      <div className="flex items-center gap-2">
                        {editingTpl !== tpl.name && (
                          <Button
                            size="sm"
                            variant="ghost"
                            className="h-6 text-xs"
                            onClick={() => startEditTpl(tpl.name, tpl.content)}
                          >
                            编辑
                          </Button>
                        )}
                        <Button
                          size="sm"
                          variant="ghost"
                          className="h-6 text-xs text-muted-foreground hover:text-destructive"
                          onClick={() => deleteTpl(tpl.name)}
                        >
                          删除
                        </Button>
                      </div>
                    </div>

                    {editingTpl === tpl.name ? (
                      <div className="p-3 space-y-2">
                        <Textarea
                          value={editContent}
                          onChange={(e) => setEditContent(e.target.value)}
                          rows={7}
                          autoFocus
                          className="text-sm font-mono resize-y"
                        />
                        <div className="flex items-center gap-2">
                          <Button size="sm" onClick={saveEditTpl} disabled={tplSaving}>
                            {tplSaving ? "保存中…" : "保存"}
                          </Button>
                          <Button size="sm" variant="ghost" className="text-muted-foreground" onClick={() => setEditingTpl(null)}>
                            取消
                          </Button>
                          {tplMsg && <span className="text-xs text-destructive">{tplMsg}</span>}
                        </div>
                      </div>
                    ) : (
                      <div className="px-4 py-3 text-sm text-muted-foreground whitespace-pre-wrap leading-relaxed font-mono max-h-48 overflow-y-auto">
                        {tpl.content || <span className="italic">（内容为空）</span>}
                      </div>
                    )}
                  </div>
                ))}

                {newTplMode && (
                  <div className="border border-border rounded-lg overflow-hidden">
                    <div className="px-4 py-2.5 bg-muted/30 border-b border-border">
                      <Input
                        type="text"
                        value={newTplName}
                        onChange={(e) => setNewTplName(e.target.value)}
                        placeholder="模板名称（如：公众号推文）"
                        autoFocus
                        className="border-0 shadow-none focus-visible:ring-0 p-0 bg-transparent text-sm font-medium h-auto"
                      />
                    </div>
                    <div className="p-3 space-y-2">
                      <Textarea
                        value={newTplContent}
                        onChange={(e) => setNewTplContent(e.target.value)}
                        rows={7}
                        className="text-sm font-mono resize-y"
                        placeholder="用自然语言描述你想要的文章风格、结构、长度等…"
                      />
                      <div className="flex items-center gap-2">
                        <Button size="sm" onClick={saveNewTpl} disabled={tplSaving}>
                          {tplSaving ? "保存中…" : "保存"}
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          className="text-muted-foreground"
                          onClick={() => {
                            setNewTplMode(false);
                            setNewTplName("");
                            setNewTplContent("");
                            setTplMsg("");
                          }}
                        >
                          取消
                        </Button>
                        {tplMsg && <span className="text-xs text-destructive">{tplMsg}</span>}
                      </div>
                    </div>
                  </div>
                )}
              </div>
            )}

            {!newTplMode && !templatesLoading && (
              <Button
                size="sm"
                variant="ghost"
                className="mt-3 h-7 text-xs text-muted-foreground"
                onClick={() => { setNewTplMode(true); setEditingTpl(null); setTplMsg(""); }}
              >
                + 新建模板
              </Button>
            )}
          </CardContent>
        </Card>

        {/* ③ 知识库宪法（Schema） */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-sm font-semibold">知识库宪法（Schema）</CardTitle>
          </CardHeader>
          <CardContent>
            <Alert className="mb-4 border-amber-200 bg-amber-50 dark:bg-amber-950/30 dark:border-amber-800">
              <AlertTriangle className="h-4 w-4 text-amber-600 dark:text-amber-400" />
              <AlertDescription className="text-xs text-amber-800 dark:text-amber-300">
                此文件定义系统如何理解和处理所有内容，修改后只影响
                <strong>新入库</strong>内容，不影响已有节点。
                建议保留各节标题，只修改具体描述；若内容缺失，系统将使用内置默认行为，不会崩溃。
              </AlertDescription>
            </Alert>

            {schemaLoading ? (
              <div className="h-32 bg-muted animate-pulse rounded-lg" />
            ) : schemaEditing ? (
              <div className="space-y-2">
                <Textarea
                  value={schemaDraft}
                  onChange={(e) => setSchemaDraft(e.target.value)}
                  rows={22}
                  autoFocus
                  className="text-sm font-mono resize-y"
                />
                <div className="flex items-center gap-2">
                  <Button size="sm" onClick={saveSchema} disabled={schemaSaving}>
                    {schemaSaving ? "保存中…" : "保存"}
                  </Button>
                  <Button size="sm" variant="ghost" className="text-muted-foreground" onClick={() => setSchemaEditing(false)}>
                    取消
                  </Button>
                  {schemaSaved && <span className="text-xs text-green-600">已保存 ✓</span>}
                </div>
              </div>
            ) : (
              <div>
                <div className="bg-muted/50 rounded-lg px-4 py-3 text-sm whitespace-pre-wrap leading-relaxed font-mono max-h-96 overflow-y-auto">
                  {schema || <span className="text-muted-foreground italic">（未设置）</span>}
                </div>
                <Button
                  size="sm"
                  variant="ghost"
                  className="mt-2 h-7 text-xs text-muted-foreground"
                  onClick={() => { setSchemaDraft(schema); setSchemaEditing(true); }}
                >
                  编辑
                </Button>
              </div>
            )}
          </CardContent>
        </Card>

      </div>
    </main>
  );
}
