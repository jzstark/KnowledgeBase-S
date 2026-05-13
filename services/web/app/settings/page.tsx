"use client";

import { useEffect, useState } from "react";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

// ── Types ─────────────────────────────────────────────────────────────────────

interface Settings {
  briefing_hours_back: number;
  briefing_time: string;
  maintenance_frequency: string;
}

// ── 主页面 ────────────────────────────────────────────────────────────────────

export default function SettingsPage() {
  const [settings, setSettings] = useState<Settings>({
    briefing_hours_back: 24,
    briefing_time: "08:00",
    maintenance_frequency: "weekly",
  });
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [settingsSaved, setSettingsSaved] = useState(false);

  const [wikiStatus, setWikiStatus] = useState<{ synced_count: number; index_exists: boolean } | null>(null);
  const [wikiRebuilding, setWikiRebuilding] = useState(false);
  const [wikiMsg, setWikiMsg] = useState("");

  useEffect(() => {
    fetch("/api/settings", { credentials: "include" })
      .then((r) => r.json())
      .then((d) => {
        if (d && typeof d === "object") setSettings((prev) => ({ ...prev, ...d }));
      })
      .catch(() => {});

    loadWikiStatus();
  }, []);

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

  return (
    <main className="min-h-screen bg-background">
      <div className="max-w-2xl mx-auto px-6 py-8 space-y-5">

        <h1 className="text-2xl font-semibold">工作室 &gt; 系统设置</h1>

        {/* ① 流程节奏 */}
        <Card id="system">
          <CardHeader className="pb-3">
            <CardTitle className="text-sm font-semibold">流程节奏</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="flex items-center gap-3">
              <label className="text-sm text-muted-foreground w-32 shrink-0">简报生成时间</label>
              <Input
                type="time"
                value={settings.briefing_time}
                onChange={(e) => setSettings({ ...settings, briefing_time: e.target.value })}
                className="w-36 text-sm"
              />
            </div>
            <div className="flex items-center gap-3">
              <label className="text-sm text-muted-foreground w-32 shrink-0">覆盖最近（小时）</label>
              <Input
                type="number"
                min={1}
                max={168}
                value={settings.briefing_hours_back}
                onChange={(e) =>
                  setSettings({ ...settings, briefing_hours_back: parseInt(e.target.value) || 24 })
                }
                className="w-20 text-sm"
              />
            </div>
            <Button
              size="sm"
              onClick={saveSchedule}
              disabled={settingsSaving}
            >
              {settingsSaving ? "保存中…" : settingsSaved ? "已保存 ✓" : "保存"}
            </Button>
          </CardContent>
        </Card>

        {/* ② Obsidian 同步 */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-sm font-semibold">Obsidian 同步</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-xs text-muted-foreground mb-3">
              只读导出：将知识库节点写入{" "}
              <code className="bg-muted px-1 rounded text-xs">user_data/wiki/</code>
              ，可将该目录作为 Obsidian vault 打开（支持双链与图谱视图）。
              新节点入库时自动导出，此处可触发全量重建。
            </p>

            {wikiStatus && (
              <div className="flex items-center gap-4 mb-3 text-sm text-muted-foreground">
                <span>已同步节点：<strong className="text-foreground">{wikiStatus.synced_count}</strong></span>
                <span className={wikiStatus.index_exists ? "text-green-600" : "text-muted-foreground"}>
                  {wikiStatus.index_exists ? "✓ index.md 存在" : "× index.md 未生成"}
                </span>
              </div>
            )}

            <div className="flex items-center gap-3">
              <Button
                size="sm"
                variant="secondary"
                onClick={rebuildWiki}
                disabled={wikiRebuilding}
              >
                {wikiRebuilding ? "重建中…" : "全量重建"}
              </Button>
              {wikiMsg && <span className="text-xs text-blue-600">{wikiMsg}</span>}
            </div>
          </CardContent>
        </Card>

        {/* ③ 数据导出 */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-sm font-semibold">数据导出</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-xs text-muted-foreground mb-3">
              打包下载 user_data/ 目录，包含 wiki 文件、配置（选题方向、写作模板、Schema）。
              解压后 wiki/ 目录可直接作为 Obsidian vault 打开。
              原始文件（raw/）最多保留 512 MB，超出时自动从最旧文件开始清理。
            </p>
            <div className="flex gap-3">
              <Button size="sm" asChild>
                <a href="/api/settings/export/no-raw">下载数据包（不含原始文件）</a>
              </Button>
              <Button size="sm" variant="outline" asChild>
                <a href="/api/settings/export">下载数据包（含原始文件）</a>
              </Button>
            </div>
          </CardContent>
        </Card>

      </div>
    </main>
  );
}
