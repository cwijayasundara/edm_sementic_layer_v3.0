import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = { title: "Prism", description: "Ask the data platform a question, get a dashboard back." };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">{children}</body>
    </html>
  );
}
