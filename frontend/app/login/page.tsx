"use client";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ApiError, api } from "@/lib/api";
import { PERSONAS } from "@/lib/personas";
import { saveToken } from "@/lib/session";

function Login() {
  const router = useRouter();
  const expired = useSearchParams().get("expired") === "1";
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

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
    } finally {
      setBusy(null);
    }
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-4xl flex-col justify-center gap-6 p-8">
      <div>
        <h1 className="text-3xl font-semibold text-[var(--prism-navy)]">Prism</h1>
        <p className="text-muted-foreground">Choose a persona to sign in. Each one sees only its own data.</p>
      </div>
      {expired && <p role="status" className="text-sm text-[var(--prism-crimson)]">Your session expired. Sign in again.</p>}
      {error && <p role="alert" className="text-sm text-[var(--prism-crimson)]">{error}</p>}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {PERSONAS.map((p) => (
          <Card key={p.id}>
            <CardHeader>
              <CardTitle className="text-base">{p.name}</CardTitle>
              <CardDescription>{p.access}</CardDescription>
            </CardHeader>
            <CardContent>
              <Button className="w-full" disabled={busy !== null} onClick={() => signIn(p.id)}>
                {busy === p.id ? "Signing in…" : `Sign in as ${p.name}`}
              </Button>
            </CardContent>
          </Card>
        ))}
      </div>
    </main>
  );
}

export default function LoginPage() {
  return <Suspense><Login /></Suspense>;
}
