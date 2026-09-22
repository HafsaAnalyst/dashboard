import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "The Migration Dashboard",
  description: "Live data from GHL · Meta Ads · GA4 · GSC",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en-AU">
      <body className="min-h-screen">
        <div className="mx-auto max-w-[1500px] px-4 py-5">{children}</div>
      </body>
    </html>
  );
}
