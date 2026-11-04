"use client";

import { useCallback, useEffect, useState } from "react";

import { api } from "@/lib/api";
import { titleCase, when } from "@/lib/format";
import { Badge, Banner, Button, Card, Table } from "@/components/ui";

type Task = {
  id: string;
  title: string;
  task_type: string;
  status: string;
  assigned_to?: string | null;
  created_at?: string;
  brief?: string;
};

export default function TasksPage() {
  const [items, setItems] = useState<Task[]>([]);
  const [demos, setDemos] = useState<Array<Record<string, unknown>>>([]);
  const [error, setError] = useState("");
  const [open, setOpen] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [tasks, demoRows] = await Promise.all([
        api<{ items: Task[] }>("/api/v1/tasks"),
        api<{ items: Array<Record<string, unknown>> }>("/api/v1/demos"),
      ]);
      setItems(tasks.items);
      setDemos(demoRows.items);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "failed to load tasks");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function update(id: string, status: string) {
    try {
      await api(`/api/v1/tasks/${id}`, { method: "PATCH", body: { status } });
      load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "update failed");
    }
  }

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold text-navy">Human hand-offs</h1>
      {error ? <Banner tone="error">{error}</Banner> : null}
      <Card title={`Open tasks (${items.length})`}>
        <Table head={["Title", "Type", "Status", "Created", ""]}>
          {items.map((task) => (
            <tr key={task.id} className="border-b border-black/5 align-top">
              <td className="px-2 py-2">
                <button className="text-left font-medium underline decoration-dotted" onClick={() => setOpen(open === task.id ? null : task.id)}>
                  {task.title}
                </button>
                {open === task.id ? <pre className="mt-2 whitespace-pre-wrap text-xs text-muted">{task.brief}</pre> : null}
              </td>
              <td className="px-2 py-2 text-muted">{titleCase(task.task_type)}</td>
              <td className="px-2 py-2">
                <Badge tone={task.status === "open" ? "warn" : task.status === "done" ? "ok" : "neutral"}>{task.status}</Badge>
              </td>
              <td className="px-2 py-2 text-muted">{when(task.created_at)}</td>
              <td className="px-2 py-2">
                <div className="flex gap-1">
                  <Button tone="ghost" onClick={() => update(task.id, "in_progress")}>
                    Start
                  </Button>
                  <Button onClick={() => update(task.id, "done")}>Done</Button>
                </div>
              </td>
            </tr>
          ))}
        </Table>
        {items.length === 0 ? <p className="text-sm text-muted">No tasks. The agent handles the simple replies itself.</p> : null}
      </Card>

      <Card title="Demos">
        <pre className="overflow-x-auto rounded bg-black/5 p-3 text-xs">{JSON.stringify(demos, null, 2)}</pre>
      </Card>
    </div>
  );
}
