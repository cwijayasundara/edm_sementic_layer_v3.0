"use client";
import { BookmarkPlus, LayoutGrid, Trash2 } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { useSession } from "@/components/SessionProvider";
import { ApiError, Unauthorized, api } from "@/lib/api";
import type { DashboardRun, DashboardSummary, Recipe, Widget } from "@/lib/schemas";

const MAX_ITEMS = 8;
const HEADER_BTN = "inline-flex h-8 items-center gap-1.5 rounded-md px-2.5 text-[0.8rem] font-medium text-white/85 transition-colors hover:bg-white/10 hover:text-white disabled:pointer-events-none disabled:opacity-40 aria-expanded:bg-white/10";

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
    <div className="relative flex items-center gap-1.5">
      {message && <span role="status" className="text-xs text-white/75">{message}</span>}
      <button type="button" className={HEADER_BTN} disabled={pinned.length === 0 || tooMany}
        title={tooMany ? "A dashboard holds at most 8 widgets. Unpin some to save." : undefined}
        onClick={() => { setMessage(null); setSaveError(null); setSaving(true); }}>
        <BookmarkPlus className="size-4" aria-hidden />Save pinned ({pinned.length})</button>
      <button type="button" className={HEADER_BTN} aria-expanded={open}
        onClick={() => { const next = !open; setOpen(next); if (next) void refresh(); }}>
        <LayoutGrid className="size-4" aria-hidden />Dashboards</button>
      {open && (
        <div className="fixed inset-x-4 top-20 z-40 sm:absolute sm:inset-x-auto sm:right-0 sm:top-11 sm:w-[22rem] overflow-hidden rounded-lg border bg-white text-sm text-foreground shadow-[0_12px_32px_-8px_rgba(20,33,61,0.28)]">
          <p className="border-b px-3 py-2.5 text-xs font-medium text-muted-foreground">Saved dashboards</p>
          {list === null ? <p className="px-3 py-4 text-muted-foreground">Loading…</p>
            : list.length === 0 ? <p className="px-3 py-4 text-muted-foreground">No saved dashboards yet. Pin widgets, then save.</p>
            : <ul className="max-h-80 overflow-y-auto py-1">{list.map((d) => (
              <li key={d.id} className="group flex items-center gap-2 px-1.5">
                <button className="flex min-w-0 flex-1 items-center justify-between gap-3 rounded-md px-2 py-2 text-left hover:bg-muted"
                  aria-label={`Open ${d.title}`} onClick={() => void openOne(d)}>
                  <span className="truncate font-medium">{d.title}</span>
                  <span className="shrink-0 text-xs text-muted-foreground">{d.widget_count} widgets</span></button>
                <button aria-label={`Delete ${d.title}`} title="Delete"
                  className="grid size-7 shrink-0 place-items-center rounded-md text-muted-foreground hover:bg-[#fbecef] hover:text-[var(--prism-crimson)]"
                  onClick={() => void remove(d)}><Trash2 className="size-3.5" aria-hidden /></button>
              </li>))}</ul>}
        </div>
      )}
      <Dialog open={saving} onOpenChange={setSaving}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Save pinned widgets</DialogTitle>
            <DialogDescription>Saves the {pinned.length} pinned {pinned.length === 1 ? "widget" : "widgets"} as a dashboard you can reopen later.
              It reruns each query when opened.</DialogDescription>
          </DialogHeader>
          <Input aria-label="Dashboard title" className="h-9" maxLength={80} value={title} onChange={(e) => setTitle(e.target.value)}
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
