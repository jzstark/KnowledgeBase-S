"use client";

import MemoryRulesPanel from "@/app/components/MemoryRulesPanel";

export default function RulesPage() {
  return (
    <main className="min-h-screen bg-background">
      <div className="mx-auto max-w-2xl space-y-5 px-6 py-8">
        <h1 className="text-2xl font-semibold">工作室 &gt; 偏好规则</h1>
        <MemoryRulesPanel />
      </div>
    </main>
  );
}
