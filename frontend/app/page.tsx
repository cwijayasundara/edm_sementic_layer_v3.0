"use client";
import { SessionProvider } from "@/components/SessionProvider";
import { Workspace } from "@/components/Workspace";

export default function Home() {
  return <SessionProvider><Workspace /></SessionProvider>;
}
