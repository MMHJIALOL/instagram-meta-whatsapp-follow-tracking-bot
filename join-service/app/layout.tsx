import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Kalakriti Studio | Workshop",
  description: "Join your Kalakriti Studio workshop on WhatsApp.",
  icons: {
    icon: "/favicon.svg",
    shortcut: "/favicon.svg",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className="antialiased">{children}</body>
    </html>
  );
}
