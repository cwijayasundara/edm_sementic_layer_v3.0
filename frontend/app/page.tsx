"use client";
import { Header } from "@/components/Header";
import { KpiStrip } from "@/components/KpiStrip";
import { SessionProvider } from "@/components/SessionProvider";

export default function Home() {
  return (
    <SessionProvider>
      <Header />
      <main className="p-6"><KpiStrip /></main>
    </SessionProvider>
  );
}
