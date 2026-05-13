"use client";

import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";

interface MemoryRule {
  id: number;
  template_name: string;
  rule: string;
  rule_type: string;
  confidence: number;
  count: number;
}

const RULE_TYPE_LABELS: Record<string, string> = {
  style: "风格",
  structure: "结构",
  content: "内容",
  tone: "语气",
};

function ConfidenceBar({ value }: { value: number }) {
  const pct = Math.round(value * 100);
  const color =
    value >= 0.8 ? "bg-green-500" : value >= 0.5 ? "bg-blue-400" : "bg-muted-foreground/30";
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-20 overflow-hidden rounded-full bg-muted">
        <div className={`h-full rounded-full ${color}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs text-muted-foreground">{pct}%</span>
    </div>
  );
}

export default function MemoryRulesPanel() {
  const [rules, setRules] = useState<MemoryRule[]>([]);
  const [rulesLoading, setRulesLoading] = useState(true);

  useEffect(() => {
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

  async function deleteRule(id: number) {
    await fetch(`/api/kb/memory/${id}`, { method: "DELETE", credentials: "include" });
    setRules((prev) => prev.filter((r) => r.id !== id));
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-sm font-semibold">写作偏好规则</CardTitle>
      </CardHeader>
      <CardContent>
        <p className="mb-3 text-xs text-muted-foreground">
          由系统从你的定稿修改中自动学习。置信度 &gt;= 80% 的规则会在生成草稿时自动应用。
        </p>

        {rulesLoading ? (
          <p className="text-sm text-muted-foreground">加载中…</p>
        ) : rules.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            暂无学习到的偏好规则。在草稿历史页提交定稿后，系统会自动学习。
          </p>
        ) : (
          <div className="space-y-0">
            {rules.map((r, i) => (
              <div key={r.id}>
                <div className="flex items-start gap-3 py-2.5">
                  <div className="min-w-0 flex-1">
                    <p className="text-sm leading-relaxed">{r.rule}</p>
                    <div className="mt-1.5 flex items-center gap-3">
                      <Badge variant="secondary" className="px-1.5 py-0 text-xs">
                        {RULE_TYPE_LABELS[r.rule_type] || r.rule_type}
                      </Badge>
                      <ConfidenceBar value={r.confidence} />
                      <span className="text-xs text-muted-foreground">出现 {r.count} 次</span>
                      {r.template_name && (
                        <span className="text-xs text-blue-500">{r.template_name}</span>
                      )}
                    </div>
                  </div>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-6 w-6 shrink-0 text-muted-foreground hover:text-destructive"
                    onClick={() => deleteRule(r.id)}
                    title="删除此规则"
                  >
                    ×
                  </Button>
                </div>
                {i < rules.length - 1 && <Separator />}
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
