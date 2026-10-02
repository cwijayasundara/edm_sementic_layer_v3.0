"use client";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useSession } from "@/components/SessionProvider";
import { accessChips } from "@/lib/session";

export function Header({ actions }: { actions?: React.ReactNode }) {
  const { claims, signOut } = useSession();
  return (
    <header className="flex flex-wrap items-center gap-3 bg-[var(--prism-navy)] px-6 py-3 text-white">
      <span className="text-lg font-semibold tracking-wide">Prism</span>
      <span className="text-sm opacity-90">{claims.name}</span>
      <div className="flex flex-wrap gap-1" aria-label="Access">
        {accessChips(claims).map((chip) => (
          <Badge key={chip} variant="secondary" className="bg-white/10 text-white">{chip}</Badge>
        ))}
      </div>
      <div className="ml-auto flex items-center gap-2">
        {actions}
        <Button variant="secondary" size="sm" onClick={signOut}>Sign out</Button>
      </div>
    </header>
  );
}
