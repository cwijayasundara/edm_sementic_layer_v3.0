"use client";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { useSession } from "@/components/SessionProvider";
import { ApiError, Unauthorized, api } from "@/lib/api";
import type { DashboardRun, DashboardSummary, Recipe, Widget } from "@/lib/schemas";

const MAX_ITEMS = 8;

export function DashboardsMenu({ pinned, onOpen }:
  { pinned: { widget: Widget; recipe: Recipe }[]; onOpen: (run: DashboardRun) => void }) {
  const { call } = useSession();
  const [list, setList] = useState<DashboardSummary[] | null>(null);
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [title, setTitle] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const tooMany = pinned.length > MAX_ITEMS;

  async function refresh() {
    try { setList(await call((t) => api.dashboards.list(t))); }
    catch (e) { if (e instanceof Unauthorized) return; setMessage("Dashboards are unavailable right now."); }
  }

  async function save() {
    setSaveError(null);
    try {
      await call((t) => api.dashboards.save(t, title.trim(), pinned));
      setSaving(false);
      setTitle("");
      setMessage("Dashboard saved.");
      if (open) await refresh();
    } catch (e) {
      if (e instanceof Unauthorized) return;
      setSaveError(e instanceof ApiError && e.status === 409 ? "You already have 20 dashboards. Delete one first."
        : "The dashboard could not be saved. Please try again.");
    }
  }

  async function openOne(d: DashboardSummary) {
    setOpen(false);
    try { onOpen(await call((t) => api.dashboards.run(t, d.id))); }
    catch (e) { if (e instanceof Unauthorized) return; setMessage("The dashboard could not be opened. Please try again."); }
  }

  async function remove(d: DashboardSummary) {
    try { await call((t) => api.dashboards.remove(t, d.id)); await refresh(); }
    catch (e) { if (e instanceof Unauthorized) return; setMessage("The dashboard could not be deleted. Please try again."); }
  }

  return (
    <div className="relative flex items-center gap-2">
      <Button size="sm" variant="secondary" disabled={pinned.length === 0 || tooMany}
        title={tooMany ? "A dashboard holds at most 8 widgets. Unpin some to save." : undefined}
        onClick={() => { setMessage(null); setSaveError(null); setSaving(true); }}>Save pinned ({pinned.length})</Button>
      <Button size="sm" variant="secondary" aria-expanded={open}
        onClick={() => { const next = !open; setOpen(next); if (next) void refresh(); }}>Dashboards</Button>
      {message && <span role="status" className="text-xs">{message}</span>}
      {open && (
        <div className="absolute right-0 top-10 z-20 w-80 rounded-md border bg-white p-2 text-sm text-foreground shadow-lg">
          {list === null ? <p className="p-2 text-muted-foreground">Loading…</p>
            : list.length === 0 ? <p className="p-2 text-muted-foreground">No saved dashboards yet. Pin widgets, then save.</p>
            : <ul>{list.map((d) => (
              <li key={d.id} className="flex items-center justify-between gap-2 rounded px-2 py-1 hover:bg-slate-50">
                <button className="flex-1 text-left" aria-label={`Open ${d.title}`} onClick={() => void openOne(d)}>
                  {d.title} <span className="text-xs text-muted-foreground">({d.widget_count})</span></button>
                <button aria-label={`Delete ${d.title}`} className="text-xs text-[var(--prism-crimson)]"
                  onClick={() => void remove(d)}>Delete</button>
              </li>))}</ul>}
        </div>
      )}
      <Dialog open={saving} onOpenChange={setSaving}>
        <DialogContent>
          <DialogHeader><DialogTitle>Save pinned widgets</DialogTitle></DialogHeader>
          <Input aria-label="Dashboard title" maxLength={80} value={title} onChange={(e) => setTitle(e.target.value)}
            placeholder="e.g. Morning cash check" />
          {saveError && <p className="text-sm text-[var(--prism-crimson)]">{saveError}</p>}
          <DialogFooter>
            <Button disabled={!title.trim()} onClick={() => void save()}>Save</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
