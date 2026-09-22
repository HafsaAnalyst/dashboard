"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { ApiError, login } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(password);
      // Full reload rather than router.push: the session cookie is set by the
      // API response, and middleware must see it on the next request.
      window.location.href = "/";
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Sign-in failed. Please try again.");
      setBusy(false);
    }
  }

  return (
    <main className="flex min-h-[70vh] items-center justify-center">
      <form
        onSubmit={submit}
        className="w-full max-w-sm rounded-xl border border-[var(--color-hairline)] bg-[var(--color-card)] p-6"
      >
        <h1 className="text-[22px] font-bold text-[var(--color-ink)]">The Migration Dashboard</h1>
        <p className="mt-1 text-[12px] text-[var(--color-ink-faint)]">
          Enter the team password to continue.
        </p>

        <input
          type="password"
          autoFocus
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          className="mt-5 w-full rounded-lg border border-[var(--color-hairline)] px-3 py-2 text-[14px] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-active)]"
          placeholder="Password"
        />

        {error && <p className="mt-3 text-[13px] text-[var(--color-down)]">{error}</p>}

        <button
          type="submit"
          disabled={busy || !password}
          className="mt-4 w-full rounded-lg bg-[var(--color-active)] px-4 py-2 text-[14px] font-semibold text-white disabled:opacity-50"
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
