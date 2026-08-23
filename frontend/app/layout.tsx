import { Analytics } from "@vercel/analytics/next";
import type { Metadata } from "next";
import type { ReactNode } from "react";
import { TooltipProvider } from "@/components/ui/tooltip";

import "./globals.css";

export const metadata: Metadata = {
  title: "Trellis task board",
  description: "Committed task state from the Trellis agent demo",
};

export default function RootLayout({
  children,
}: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <TooltipProvider>{children}</TooltipProvider>
        {/* D-82. Observational only, and mounted at the root so it covers the
            whole application once. It sits outside TooltipProvider because it
            renders no UI and depends on no context. The server component stays
            a server component: @vercel/analytics/next is the maintained
            integration, so no "use client" is needed here. */}
        <Analytics />
      </body>
    </html>
  );
}
