"use client";

type Props = {
  tabs: string[];
  active: string;
  onChange: (tab: string) => void;
  /** Tabs not yet ported from Streamlit — shown, but visibly not ready, so the
   *  team can see migration progress instead of hitting a blank panel. */
  ready?: Set<string>;
};

export default function TabNav({ tabs, active, onChange, ready }: Props) {
  return (
    <div className="mb-4 flex flex-wrap gap-1 rounded-xl border border-[var(--color-hairline)] bg-[var(--color-card)] p-1.5">
      {tabs.map((tab) => {
        const isActive = tab === active;
        const isReady = !ready || ready.has(tab);
        return (
          <button
            key={tab}
            type="button"
            onClick={() => onChange(tab)}
            aria-current={isActive ? "page" : undefined}
            className={[
              "rounded-lg px-4 py-1.5 text-[13px] font-semibold transition",
              isActive
                ? "bg-[var(--color-active)] text-white"
                : "text-[var(--color-ink-soft)] hover:bg-slate-100",
              isReady ? "" : "opacity-45",
            ].join(" ")}
            title={isReady ? undefined : "Not yet ported from Streamlit"}
          >
            {tab}
          </button>
        );
      })}
    </div>
  );
}
