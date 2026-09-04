import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "StartupOS",
  description: "The founder operating system that opens with work already done.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
