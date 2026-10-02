"use client";
import { LogOut } from "lucide-react";
import { Logo } from "@/components/Logo";
import { useSession } from "@/components/SessionProvider";
import { accessChips } from "@/lib/session";
import { chipColor } from "@/lib/sources";

function initials(name: string): string {
  return name.split(/[\s-]+/).filter((w) => /^[A-Z]/.test(w)).slice(0, 2).map((w) => w[0]).join("");
}

export function Header({ actions }: { actions?: React.ReactNode }) {
  const { claims, signOut } = useSession();
  return (
    <header className="relative z-30 bg-[var(--prism-navy)] text-white">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-2.5 sm:px-6">
        <div className="flex items-center gap-2.5">
          <Logo className="size-7" />
          <span className="text-[1.05rem] font-semibold">Prism</span>
        </div>
        <div className="ml-auto flex items-center gap-2 lg:order-last">
          {actions}
          <span className="mx-1 hidden h-6 w-px bg-white/15 sm:block" aria-hidden />
          <div className="flex items-center gap-2.5">
            <span aria-hidden className="grid size-8 place-items-center rounded-full bg-white/10 text-xs font-semibold">
              {initials(claims.name)}</span>
            <span className="hidden text-sm font-medium md:inline">{claims.name}</span>
          </div>
          <button type="button" onClick={signOut} aria-label="Sign out" title="Sign out"
            className="grid size-8 place-items-center rounded-md text-white/70 transition-colors hover:bg-white/10 hover:text-white">
            <LogOut className="size-4" aria-hidden />
          </button>
        </div>
        <ul className="flex w-full flex-wrap items-center gap-1.5 lg:w-auto lg:flex-1 lg:border-l lg:border-white/15 lg:pl-4"
          aria-label="Access">
          {accessChips(claims).map((chip) => {
            const color = chipColor(chip);
            return (
              <li key={chip} className="inline-flex h-6 items-center gap-1.5 rounded-full bg-white/[0.08] px-2.5 text-xs text-white/85">
                {color && <span aria-hidden className="size-1.5 rounded-full" style={{ background: color }} />}
                {chip}
              </li>
            );
          })}
        </ul>
      </div>
    </header>
  );
}
