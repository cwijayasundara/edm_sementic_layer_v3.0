"use client";
import { ChevronRight, Loader2 } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { Logo } from "@/components/Logo";
import { RefractionDiagram } from "@/components/RefractionDiagram";
import { ApiError, api } from "@/lib/api";
import { PERSONAS } from "@/lib/personas";
import { saveToken } from "@/lib/session";
import { SOURCES } from "@/lib/sources";

const FACTS = [
  ["Traceable", "Every chart opens to the query that produced it and the rows behind it."],
  ["Governed", "Role and row-level access are enforced at the data gateway, on every question."],
  ["Reusable", "Pin the answers you need each morning and save them as a dashboard."],
] as const;

function Login() {
  const router = useRouter();
  const expired = useSearchParams().get("expired") === "1";
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [focus, setFocus] = useState<string | null>(null);
  const active = PERSONAS.find((p) => p.id === (busy ?? focus)) ?? null;

  async function signIn(id: string) {
    setBusy(id);
    setError(null);
    try {
      saveToken(await api.devToken(id));
      router.replace("/");
    } catch (e) {
      setError(e instanceof ApiError && e.status === 404
        ? "Sign-in is disabled on this agent. Set PRISM_AGENT_DEV_TOKEN_ENABLED=true for local demos."
        : "The agent is not reachable. Start it with scripts/start_backend.sh.");
      setBusy(null);
    }
  }

  return (
    <div className="grid min-h-screen lg:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]">
      <section className="relative flex flex-col gap-10 overflow-hidden bg-[var(--prism-navy)] px-6 py-8 text-white sm:px-12 sm:py-10 lg:min-h-screen">
        <div className="flex items-center gap-2.5">
          <Logo />
          <span className="text-lg font-semibold">Prism</span>
        </div>
        <div className="max-w-xl">
          <h1 className="text-[2rem] leading-[1.15] font-semibold tracking-[-0.01em] text-balance sm:text-[2.6rem]">
            Ask your operations data a question. Get an answer you can trace.
          </h1>
          <p className="mt-4 max-w-[34rem] text-[1.05rem] leading-relaxed text-white/70">
            Prism answers in charts and tables drawn from {SOURCES.length} platforms, and only from the data your role is
            allowed to see.
          </p>
        </div>
        <figure className="hidden sm:block">
          <RefractionDiagram visible={active?.sources ?? null} question={active?.examples[0] ?? "One question"} />
          <figcaption className="mt-3 min-h-5 text-sm text-white/60" aria-live="polite">
            {active ? `${active.name} reaches ${active.sources.length === SOURCES.length ? "every source"
              : active.sources.map((id) => SOURCES.find((s) => s.id === id)!.name).join(", ")}.`
              : "Point at a role to see which sources it reaches."}
          </figcaption>
        </figure>
        <dl className="mt-auto hidden gap-6 border-t border-white/15 pt-6 sm:grid sm:grid-cols-3">
          {FACTS.map(([term, text]) => (
            <div key={term}>
              <dt className="text-sm font-semibold">{term}</dt>
              <dd className="mt-1 text-sm leading-relaxed text-white/60">{text}</dd>
            </div>
          ))}
        </dl>
      </section>

      <main className="flex items-center justify-center bg-white px-6 py-10 sm:px-12">
        <div className="w-full max-w-md">
          <h2 className="text-2xl font-semibold text-[var(--prism-navy)]">Sign in</h2>
          <p className="mt-1.5 text-[0.95rem] text-muted-foreground">Choose a demo role. Each role sees only its own data.</p>

          {expired && <p role="status" className="mt-5 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900">
            Your session expired. Sign in again.</p>}
          {error && <p role="alert" className="mt-5 rounded-md border border-[var(--prism-crimson)]/30 bg-[#fbecef] px-3 py-2 text-sm text-[var(--prism-crimson)]">
            {error}</p>}

          <ul className="mt-6 divide-y overflow-hidden rounded-lg border" onMouseLeave={() => setFocus(null)}>
            {PERSONAS.map((p) => (
              <li key={p.id}>
                <button type="button" aria-label={`Sign in as ${p.name}`} aria-describedby={`${p.id}-access`}
                  disabled={busy !== null} onClick={() => void signIn(p.id)}
                  onMouseEnter={() => setFocus(p.id)} onFocus={() => setFocus(p.id)} onBlur={() => setFocus(null)}
                  className="group flex w-full items-center gap-4 px-4 py-3.5 text-left transition-colors hover:bg-[#f4f6f9] focus-visible:bg-[#f4f6f9] focus-visible:outline-offset-[-2px] disabled:cursor-wait disabled:opacity-60 data-[busy=true]:opacity-100"
                  data-busy={busy === p.id}>
                  <span className="min-w-0 flex-1">
                    <span className="block font-medium text-[var(--prism-ink)]">{p.name}</span>
                    <span id={`${p.id}-access`} className="mt-0.5 block text-sm text-muted-foreground">{p.access}</span>
                    <span className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1" aria-hidden>
                      {p.sources.length === SOURCES.length
                        ? <span className="inline-flex items-center gap-1.5 text-xs text-[var(--prism-muted)]">
                            <span className="flex gap-0.5">{SOURCES.map((s) =>
                              <span key={s.id} className="size-2 rounded-[2px]" style={{ background: s.color }} />)}</span>
                            All {SOURCES.length} sources</span>
                        : p.sources.map((id) => {
                          const s = SOURCES.find((x) => x.id === id)!;
                          return <span key={id} className="inline-flex items-center gap-1.5 text-xs text-[var(--prism-muted)]">
                            <span className="size-2 rounded-[2px]" style={{ background: s.color }} />{s.name}</span>;
                        })}
                    </span>
                  </span>
                  {busy === p.id
                    ? <Loader2 className="size-4 shrink-0 animate-spin text-[var(--prism-navy)]" aria-hidden />
                    : <ChevronRight className="size-4 shrink-0 text-[var(--prism-muted)] transition-transform group-hover:translate-x-0.5" aria-hidden />}
                </button>
              </li>
            ))}
          </ul>
          <p className="sr-only" role="status">{busy ? "Signing in…" : ""}</p>
          <p className="mt-6 text-xs leading-relaxed text-muted-foreground">
            These are demo roles for local use. Access is enforced by the agent and the data gateway, not by this page.
          </p>
        </div>
      </main>
    </div>
  );
}

export default function LoginPage() {
  return <Suspense><Login /></Suspense>;
}
