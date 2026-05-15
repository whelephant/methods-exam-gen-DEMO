import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";
import "katex/dist/katex.min.css";
import Link from "next/link";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "VCE Hub — Methods practice questions",
  description:
    "Browse and assemble VCE Mathematical Methods practice exams from real past VCAA questions, tagged against the study design.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col bg-stone-50 text-stone-900">
        <header className="border-b border-stone-200 bg-white">
          <div className="max-w-6xl mx-auto px-6 py-4 flex items-center justify-between">
            <Link href="/" className="font-semibold text-lg tracking-tight">
              VCE Hub
            </Link>
            <nav className="text-sm text-stone-600 flex gap-5">
              <Link href="/" className="hover:text-stone-900">Browse</Link>
              <Link href="/stats" className="hover:text-stone-900">Stats</Link>
            </nav>
          </div>
        </header>
        <main className="flex-1 max-w-6xl w-full mx-auto px-6 py-8">
          {children}
        </main>
        <footer className="border-t border-stone-200 bg-white text-xs text-stone-500">
          <div className="max-w-6xl mx-auto px-6 py-4">
            Past questions and examiner-report commentary © VCAA. Demo: practice tool only.
          </div>
        </footer>
      </body>
    </html>
  );
}
