"use client";
import { useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { Unauthorized } from "@/lib/api";
import { type Claims, clearSession, loadSession } from "@/lib/session";

type Session = {
  token: string;
  claims: Claims;
  signOut: () => void;
  call: <T>(fn: (token: string) => Promise<T>) => Promise<T>;
};

const Ctx = createContext<Session | null>(null);

export function SessionProvider({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const [session, setSession] = useState<{ token: string; claims: Claims } | null | undefined>(undefined);

  useEffect(() => {
    const s = loadSession();
    setSession((prev) => (prev && s && prev.token === s.token ? prev : s));
    if (!s) router.replace("/login");
  }, [router]);

  const signOut = useCallback(() => { clearSession(); router.replace("/login"); }, [router]);

  const call = useCallback(async <T,>(fn: (token: string) => Promise<T>): Promise<T> => {
    const current = loadSession();
    if (!current) { clearSession(); router.replace("/login?expired=1"); throw new Unauthorized(); }
    try {
      return await fn(current.token);
    } catch (e) {
      if (e instanceof Unauthorized) { clearSession(); router.replace("/login?expired=1"); }
      throw e;
    }
  }, [router]);

  const value = useMemo(() => (session ? { ...session, signOut, call } : null), [session, signOut, call]);
  if (!value) return null;
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useSession(): Session {
  const s = useContext(Ctx);
  if (!s) throw new Error("useSession outside SessionProvider");
  return s;
}
