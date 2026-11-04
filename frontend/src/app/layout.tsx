import type { Metadata } from "next";

import Layout from "@/components/Layout";

import "./globals.css";

export const metadata: Metadata = {
  title: "Mantiq Outreach Agent",
  description: "ProcureAI outbound operations: campaigns, review queue, replies, suppression and system health.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="bg-canvas">
        <Layout>{children}</Layout>
      </body>
    </html>
  );
}
