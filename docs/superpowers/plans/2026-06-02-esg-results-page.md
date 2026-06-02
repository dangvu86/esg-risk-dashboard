# ESG Results Page Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `esg-pipeline/web/app/page.tsx` with a single-page, bilingual (EN/VI), glass-morphism ESG events table styled 1:1 with the approved mockup, featuring full filtering, sorting, and pagination.

**Architecture:** Pure data logic (search normalization, filtering, sorting with tie-breaks, pagination, headline language fallback, ticker merge) lives in a unit-tested module `web/lib/esg.ts`. The React client component `app/page.tsx` fetches the two existing API routes once, holds UI state, and renders using those helpers. Styling is ported from the mockup into `app/globals.css`; the Manrope font is loaded via `next/font` in `app/layout.tsx`.

**Tech Stack:** Next.js 16.2.2 (App Router), React 19.2.4, Tailwind v4, TypeScript 5, Vitest (added for unit tests). Data is fetched client-side from existing `/api/events` and `/api/tickers` routes (GCS-backed); no backend changes.

**Reference docs (read before coding the relevant task):**
- Next 16 fonts: `esg-pipeline/web/node_modules/next/dist/docs/01-app/01-getting-started/13-fonts.md`
- Next 16 CSS: `esg-pipeline/web/node_modules/next/dist/docs/01-app/01-getting-started/11-css.md`
- `esg-pipeline/web/AGENTS.md` warns this Next.js differs from training data — heed it.

**Spec:** `esg-pipeline/docs/superpowers/specs/2026-06-02-esg-results-page-design.md`
**Approved mockup:** `esg-pipeline/.superpowers/brainstorm/10487-1779853904/mockup-new.html` (the single-page design; NOT the older 3-tab `mockup-v5.html`)

**Implementation note (deviation from spec, equivalent behavior):** The spec describes the
mockup's bilingual mechanism as toggling a `lang-vi` class on `<body>` with dual `.lang-en`/`.lang-vi`
spans. In React we instead drive all text off a `lang` state value (cleaner, no duplicated DOM).
The visible result is identical. `<html lang>` is updated to match for accessibility.

**Working directory for all commands:** `esg-pipeline/web`

---

## Chunk 1: Tested data layer + styled page

### Task 1: Add Vitest test infrastructure

**Files:**
- Modify: `esg-pipeline/web/package.json` (add devDependency + `test` script)
- Create: `esg-pipeline/web/vitest.config.ts`

- [ ] **Step 1: Install Vitest**

Run (in `esg-pipeline/web`): `npm install -D vitest`
Expected: `vitest` added to `devDependencies`, no errors.

- [ ] **Step 2: Add the test script**

In `package.json`, add to `"scripts"`:

```json
"test": "vitest run"
```

- [ ] **Step 3: Create the Vitest config**

Create `esg-pipeline/web/vitest.config.ts`:

```ts
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["lib/**/*.test.ts"],
    environment: "node",
  },
});
```

- [ ] **Step 4: Verify the runner works (no tests yet)**

Run: `npm run test`
Expected: Vitest runs and reports "No test files found" (exit is fine) — confirms tooling is wired.

- [ ] **Step 5: Commit**

```bash
git add esg-pipeline/web/package.json esg-pipeline/web/package-lock.json esg-pipeline/web/vitest.config.ts
git commit -m "chore(web): add Vitest for unit tests"
```

---

### Task 2: Types + `normalizeText` (accent/case-insensitive)

Use @superpowers:test-driven-development for this and every `lib/esg.ts` task.

**Files:**
- Create: `esg-pipeline/web/lib/esg.ts`
- Create: `esg-pipeline/web/lib/esg.test.ts`

- [ ] **Step 1: Write the failing test**

Create `esg-pipeline/web/lib/esg.test.ts`:

```ts
import { describe, it, expect } from "vitest";
import { normalizeText } from "./esg";

describe("normalizeText", () => {
  it("lowercases", () => {
    expect(normalizeText("Dabaco")).toBe("dabaco");
  });
  it("strips Vietnamese diacritics", () => {
    expect(normalizeText("Thanh Hoá")).toBe("thanh hoa");
  });
  it("strips the đ/Đ letter (not handled by NFD)", () => {
    expect(normalizeText("Đà Nẵng")).toBe("da nang");
  });
  it("handles empty/undefined", () => {
    expect(normalizeText("")).toBe("");
    expect(normalizeText(undefined)).toBe("");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test`
Expected: FAIL — `normalizeText` is not exported / module missing.

- [ ] **Step 3: Write minimal implementation**

Create `esg-pipeline/web/lib/esg.ts`:

```ts
export type Pillar = "E" | "S" | "G";
export type Lang = "en" | "vi";
export type Severity = "Cao" | "Trung bình";
export type ControversyLevel = "Major" | "Minor" | "No" | "";

export interface EsgEvent {
  ticker: string;
  company: string;
  type: Pillar;
  date: string; // "YYYY-MM-DD"
  summary: string; // Vietnamese headline
  summary_en?: string; // English translation (may be empty)
  severity: Severity;
  source: string;
  url: string;
  controversy_level?: ControversyLevel;
  controversy_justification?: string;
  controversy_classified_at?: string;
  created_at?: string;
}

export interface Company {
  ticker: string;
  company: string;
}

/** Lowercase + strip diacritics so search is accent- and case-insensitive. */
export function normalizeText(s: string | undefined): string {
  if (!s) return "";
  return s
    .normalize("NFD")
    .replace(/\p{Diacritic}/gu, "")
    .replace(/đ/g, "d")
    .replace(/Đ/g, "d")
    .toLowerCase();
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm run test`
Expected: PASS (4 assertions).

- [ ] **Step 5: Commit**

```bash
git add esg-pipeline/web/lib/esg.ts esg-pipeline/web/lib/esg.test.ts
git commit -m "feat(web): add EsgEvent types and accent-insensitive normalizeText"
```

---

### Task 3: `pickHeadline` (EN→VI fallback) + label maps

**Files:**
- Modify: `esg-pipeline/web/lib/esg.ts`
- Modify: `esg-pipeline/web/lib/esg.test.ts`

- [ ] **Step 1: Write the failing test** (append to `esg.test.ts`)

```ts
import { pickHeadline, severityLabel, controversyLabel } from "./esg";

const base = {
  ticker: "DBC", company: "Dabaco", type: "E" as const, date: "2026-05-27",
  summary: "Dabaco bị phạt", severity: "Trung bình" as const, source: "Lao Dong", url: "x",
};

describe("pickHeadline", () => {
  it("uses summary_en when present (en)", () => {
    expect(pickHeadline({ ...base, summary_en: "Dabaco fined" }, "en"))
      .toEqual({ text: "Dabaco fined", isFallback: false });
  });
  it("falls back to VI summary with flag when summary_en empty (en)", () => {
    expect(pickHeadline({ ...base, summary_en: "" }, "en"))
      .toEqual({ text: "Dabaco bị phạt", isFallback: true });
    expect(pickHeadline(base, "en"))
      .toEqual({ text: "Dabaco bị phạt", isFallback: true });
  });
  it("always uses VI summary in vi mode, never flagged", () => {
    expect(pickHeadline({ ...base, summary_en: "Dabaco fined" }, "vi"))
      .toEqual({ text: "Dabaco bị phạt", isFallback: false });
  });
});

describe("label maps", () => {
  it("severityLabel", () => {
    expect(severityLabel("Cao", "en")).toBe("High");
    expect(severityLabel("Cao", "vi")).toBe("Cao");
    expect(severityLabel("Trung bình", "en")).toBe("Medium");
  });
  it("controversyLabel returns dash for empty", () => {
    expect(controversyLabel("", "en")).toBe("—");
    expect(controversyLabel("Major", "vi")).toBe("Major");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test`
Expected: FAIL — `pickHeadline`/`severityLabel`/`controversyLabel` not exported.

- [ ] **Step 3: Write minimal implementation** (append to `esg.ts`)

```ts
export interface Headline {
  text: string;
  isFallback: boolean; // true when EN requested but only VI available
}

export function pickHeadline(event: EsgEvent, lang: Lang): Headline {
  if (lang === "en") {
    const en = event.summary_en?.trim();
    if (en) return { text: en, isFallback: false };
    return { text: event.summary, isFallback: true };
  }
  return { text: event.summary, isFallback: false };
}

export function severityLabel(sev: Severity, lang: Lang): string {
  if (sev === "Cao") return lang === "en" ? "High" : "Cao";
  if (sev === "Trung bình") return lang === "en" ? "Medium" : "Trung bình";
  return sev;
}

export function controversyLabel(level: ControversyLevel | undefined, lang: Lang): string {
  if (!level) return "—";
  // Levels are language-neutral tokens (Major/Minor/No); shown as-is in both languages.
  return level;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm run test`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add esg-pipeline/web/lib/esg.ts esg-pipeline/web/lib/esg.test.ts
git commit -m "feat(web): add pickHeadline fallback and bilingual label maps"
```

---

### Task 4: `filterEvents`

**Files:**
- Modify: `esg-pipeline/web/lib/esg.ts`
- Modify: `esg-pipeline/web/lib/esg.test.ts`

- [ ] **Step 1: Write the failing test** (append)

```ts
import { filterEvents, type Filters, type EsgEvent } from "./esg";

const ev = (over: Partial<EsgEvent>): EsgEvent => ({
  ticker: "DBC", company: "Dabaco", type: "E", date: "2026-05-27",
  summary: "Thanh Hoá xả thải", summary_en: "Thanh Hoa wastewater",
  severity: "Trung bình", source: "Lao Dong", url: "x", controversy_level: "Minor", ...over,
});

const NONE: Filters = { ticker: "", pillar: "", severity: "", controversy: "", query: "" };

describe("filterEvents", () => {
  const data = [
    ev({ ticker: "DBC", type: "E", severity: "Trung bình", controversy_level: "Minor" }),
    ev({ ticker: "HPG", type: "S", severity: "Cao", controversy_level: "Major" }),
    ev({ ticker: "NVL", type: "G", severity: "Trung bình", controversy_level: "" }),
  ];
  it("returns all with empty filters", () => {
    expect(filterEvents(data, NONE)).toHaveLength(3);
  });
  it("filters by ticker, pillar, severity, controversy", () => {
    expect(filterEvents(data, { ...NONE, ticker: "HPG" })).toHaveLength(1);
    expect(filterEvents(data, { ...NONE, pillar: "G" })).toHaveLength(1);
    expect(filterEvents(data, { ...NONE, severity: "Cao" })).toHaveLength(1);
    expect(filterEvents(data, { ...NONE, controversy: "Major" })).toHaveLength(1);
  });
  it("controversy 'none' matches empty/missing level", () => {
    expect(filterEvents(data, { ...NONE, controversy: "none" })).toHaveLength(1);
  });
  it("search is accent-insensitive across summary and summary_en", () => {
    expect(filterEvents(data, { ...NONE, query: "thanh hoa" })).toHaveLength(3);
    expect(filterEvents(data, { ...NONE, query: "wastewater" })).toHaveLength(3);
    expect(filterEvents(data, { ...NONE, query: "zzz" })).toHaveLength(0);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test` — Expected: FAIL (`filterEvents`/`Filters` missing).

- [ ] **Step 3: Write minimal implementation** (append to `esg.ts`)

```ts
export interface Filters {
  ticker: string;       // "" = all
  pillar: "" | Pillar;  // "" = all
  severity: "" | Severity;
  controversy: "" | ControversyLevel | "none"; // "none" = not classified
  query: string;
}

export function matchesSearch(event: EsgEvent, query: string): boolean {
  const q = normalizeText(query);
  if (!q) return true;
  return (
    normalizeText(event.summary).includes(q) ||
    normalizeText(event.summary_en).includes(q)
  );
}

export function filterEvents(events: EsgEvent[], f: Filters): EsgEvent[] {
  return events.filter((e) => {
    if (f.ticker && e.ticker !== f.ticker) return false;
    if (f.pillar && e.type !== f.pillar) return false;
    if (f.severity && e.severity !== f.severity) return false;
    if (f.controversy) {
      if (f.controversy === "none") {
        if (e.controversy_level) return false;
      } else if (e.controversy_level !== f.controversy) {
        return false;
      }
    }
    if (!matchesSearch(e, f.query)) return false;
    return true;
  });
}
```

- [ ] **Step 4: Run test to verify it passes** — Run: `npm run test` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add esg-pipeline/web/lib/esg.ts esg-pipeline/web/lib/esg.test.ts
git commit -m "feat(web): add filterEvents with accent-insensitive search"
```

---

### Task 5: `sortEvents` with deterministic tie-breaks

**Files:**
- Modify: `esg-pipeline/web/lib/esg.ts`
- Modify: `esg-pipeline/web/lib/esg.test.ts`

- [ ] **Step 1: Write the failing test** (append)

```ts
import { sortEvents, type SortKey } from "./esg";

describe("sortEvents", () => {
  const data = [
    ev({ ticker: "HPG", date: "2026-05-26", created_at: "2026-05-26T01:00:00Z" }),
    ev({ ticker: "DBC", date: "2026-05-27", created_at: "2026-05-27T02:00:00Z" }),
    ev({ ticker: "AAA", date: "2026-05-27", created_at: "2026-05-27T05:00:00Z" }),
    ev({ ticker: "AAA", date: "2026-05-27", created_at: undefined }),
  ];
  const tickers = (k: SortKey) => sortEvents(data, k).map((e) => e.ticker + "/" + e.date);

  it("date_desc: newest date first; tie -> created_at desc, missing last, then ticker asc", () => {
    // same date 2026-05-27: AAA(05:00) , DBC(02:00) , AAA(missing) ; then HPG 05-26
    expect(tickers("date_desc")).toEqual([
      "AAA/2026-05-27", "DBC/2026-05-27", "AAA/2026-05-27", "HPG/2026-05-26",
    ]);
  });
  it("date_asc: oldest date first; same-date tie still created_at desc then ticker asc", () => {
    expect(tickers("date_asc")[0]).toBe("HPG/2026-05-26");
  });
  it("ticker_asc: A→Z; tie -> date desc", () => {
    expect(sortEvents(data, "ticker_asc").map((e) => e.ticker)).toEqual([
      "AAA", "AAA", "DBC", "HPG",
    ]);
  });
  it("does not mutate the input array", () => {
    const copy = [...data];
    sortEvents(data, "date_desc");
    expect(data).toEqual(copy);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test` — Expected: FAIL (`sortEvents`/`SortKey` missing).

- [ ] **Step 3: Write minimal implementation** (append to `esg.ts`)

```ts
export type SortKey = "date_desc" | "date_asc" | "ticker_asc";

// Descending compare of created_at; missing sorts LAST.
function createdAtDesc(a: EsgEvent, b: EsgEvent): number {
  const av = a.created_at ?? "";
  const bv = b.created_at ?? "";
  if (av === bv) return 0;
  if (!av) return 1; // a missing -> a after b
  if (!bv) return -1;
  return bv.localeCompare(av); // later timestamp first
}

export function sortEvents(events: EsgEvent[], key: SortKey): EsgEvent[] {
  const copy = [...events];
  copy.sort((a, b) => {
    if (key === "ticker_asc") {
      const t = a.ticker.localeCompare(b.ticker);
      if (t !== 0) return t;
      return b.date.localeCompare(a.date); // tie -> date desc
    }
    // date sorts
    const d = key === "date_desc" ? b.date.localeCompare(a.date) : a.date.localeCompare(b.date);
    if (d !== 0) return d;
    const c = createdAtDesc(a, b); // tie -> created_at desc
    if (c !== 0) return c;
    return a.ticker.localeCompare(b.ticker); // then ticker asc
  });
  return copy;
}
```

- [ ] **Step 4: Run test to verify it passes** — Run: `npm run test` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add esg-pipeline/web/lib/esg.ts esg-pipeline/web/lib/esg.test.ts
git commit -m "feat(web): add sortEvents with deterministic tie-breaks"
```

---

### Task 6: `paginate` + `mergeTickers`

**Files:**
- Modify: `esg-pipeline/web/lib/esg.ts`
- Modify: `esg-pipeline/web/lib/esg.test.ts`

- [ ] **Step 1: Write the failing test** (append)

```ts
import { paginate, mergeTickers, PAGE_SIZE } from "./esg";

describe("paginate", () => {
  const rows = Array.from({ length: 120 }, (_, i) => i);
  it("uses PAGE_SIZE of 50", () => {
    expect(PAGE_SIZE).toBe(50);
  });
  it("returns the right slice and total pages", () => {
    const r = paginate(rows, 1, PAGE_SIZE);
    expect(r.rows).toHaveLength(50);
    expect(r.totalPages).toBe(3);
    expect(r.page).toBe(1);
  });
  it("clamps page above range to last page", () => {
    expect(paginate(rows, 99, PAGE_SIZE).page).toBe(3);
  });
  it("clamps page below 1 and handles empty -> 1 page", () => {
    expect(paginate(rows, 0, PAGE_SIZE).page).toBe(1);
    expect(paginate([], 1, PAGE_SIZE).totalPages).toBe(1);
  });
});

describe("mergeTickers", () => {
  it("merges api + event tickers, dedupes, sorts A→Z", () => {
    const api = [{ ticker: "VIC", company: "VinGroup" }, { ticker: "DBC", company: "Dabaco" }];
    const events = [ev({ ticker: "HPG" }), ev({ ticker: "DBC" })];
    expect(mergeTickers(api, events)).toEqual(["DBC", "HPG", "VIC"]);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test` — Expected: FAIL (`paginate`/`mergeTickers`/`PAGE_SIZE` missing).

- [ ] **Step 3: Write minimal implementation** (append to `esg.ts`)

```ts
export const PAGE_SIZE = 50;

export interface Page<T> {
  rows: T[];
  page: number;       // clamped, 1-based
  totalPages: number; // >= 1
}

export function paginate<T>(rows: T[], page: number, size = PAGE_SIZE): Page<T> {
  const totalPages = Math.max(1, Math.ceil(rows.length / size));
  const clamped = Math.min(Math.max(1, page), totalPages);
  const start = (clamped - 1) * size;
  return { rows: rows.slice(start, start + size), page: clamped, totalPages };
}

/** Union of API tickers and tickers seen in events, deduped, sorted A→Z. */
export function mergeTickers(apiTickers: Company[], events: EsgEvent[]): string[] {
  const set = new Set<string>();
  for (const c of apiTickers) set.add(c.ticker);
  for (const e of events) set.add(e.ticker);
  return [...set].sort((a, b) => a.localeCompare(b));
}
```

- [ ] **Step 4: Run test to verify it passes** — Run: `npm run test` — Expected: PASS (all suites).

- [ ] **Step 5: Commit**

```bash
git add esg-pipeline/web/lib/esg.ts esg-pipeline/web/lib/esg.test.ts
git commit -m "feat(web): add paginate and mergeTickers helpers"
```

---

### Task 7: Port mockup styles into `globals.css`

**Files:**
- Modify: `esg-pipeline/web/app/globals.css` (replace contents)

Read first: `node_modules/next/dist/docs/01-app/01-getting-started/11-css.md` (Tailwind v4 import).

- [ ] **Step 1: Replace `globals.css` with ported styles**

Replace the entire file with:

```css
@import "tailwindcss";

:root {
  --ink: #1a1625;
  --ink-soft: #4a4458;
  --muted: #7c7592;
}

body {
  font-family: var(--font-manrope), -apple-system, sans-serif;
  color: var(--ink);
  min-height: 100vh;
  background:
    radial-gradient(ellipse 80% 50% at 50% -20%, rgba(167, 139, 250, 0.12) 0%, transparent 65%),
    radial-gradient(ellipse 60% 40% at 100% 50%, rgba(196, 181, 253, 0.08) 0%, transparent 65%),
    linear-gradient(180deg, #fefdff 0%, #faf9ff 100%);
}

.pill-nav { background: rgba(255,255,255,0.7); backdrop-filter: blur(20px); border: 1px solid rgba(255,255,255,0.8); box-shadow: 0 4px 24px rgba(124,58,237,0.08); border-radius: 9999px; }
.pill-btn-light { background: white; color: var(--ink); border: 1px solid rgba(167,139,250,0.3); border-radius: 9999px; padding: 8px 18px; font-weight: 500; font-size: 13px; cursor: pointer; }
.pill-btn-light:disabled { opacity: 0.4; cursor: not-allowed; }
.pill-tag { display: inline-block; background: white; color: var(--ink-soft); border: 1px solid rgba(167,139,250,0.2); border-radius: 9999px; padding: 6px 16px; font-size: 13px; font-weight: 500; box-shadow: 0 2px 8px rgba(124,58,237,0.06); }
.lang-toggle { display: inline-flex; background: rgba(255,255,255,0.5); border-radius: 9999px; padding: 2px; }
.lang-btn { padding: 5px 12px; border-radius: 9999px; font-size: 12px; font-weight: 600; color: var(--muted); cursor: pointer; border: none; background: transparent; transition: all 0.15s; }
.lang-btn.active { background: linear-gradient(135deg, #a78bfa 0%, #c4b5fd 100%); color: white; box-shadow: 0 2px 8px rgba(167,139,250,0.4); }
.glass-card { background: rgba(255,255,255,0.7); backdrop-filter: blur(20px); border: 1px solid rgba(255,255,255,0.9); border-radius: 24px; box-shadow: 0 8px 32px rgba(124,58,237,0.08), 0 2px 8px rgba(124,58,237,0.04); }
.ticker-chip { display: inline-block; background: rgba(255,255,255,0.8); border: 1px solid rgba(167,139,250,0.2); color: var(--ink); padding: 4px 12px; border-radius: 9999px; font-family: "Inter", monospace; font-weight: 600; font-size: 12px; letter-spacing: 0.5px; }
.badge { display: inline-block; padding: 3px 10px; font-size: 11px; font-weight: 600; border-radius: 9999px; letter-spacing: 0.3px; }
.group-E { background: rgba(16,185,129,0.12); color: #047857; }
.group-S { background: rgba(245,158,11,0.12); color: #b45309; }
.group-G { background: rgba(244,63,94,0.12); color: #be123c; }
.ctrl-Major { background: rgba(244,63,94,0.12); color: #be123c; border: 1px solid rgba(244,63,94,0.25); }
.ctrl-Minor { background: rgba(245,158,11,0.12); color: #b45309; border: 1px solid rgba(245,158,11,0.25); }
.ctrl-No { background: rgba(16,185,129,0.12); color: #047857; border: 1px solid rgba(16,185,129,0.25); }
.row { transition: background 0.1s; }
.row:hover { background: rgba(167,139,250,0.06); }
.gradient-accent { background: linear-gradient(135deg, #a78bfa 0%, #c4b5fd 100%); }
.display-h1 { font-size: 36px; line-height: 1.1; letter-spacing: -1.2px; font-weight: 700; color: var(--ink); }
.subtle-text { color: var(--muted); }
.sev-high { color: #be123c; font-weight: 600; }
.sev-med { color: #b45309; font-weight: 600; }
.filter-input { background: rgba(255,255,255,0.7); border: 1px solid rgba(167,139,250,0.3); border-radius: 9999px; padding: 9px 16px; font-size: 13px; color: var(--ink); }
.fallback-tag { font-size: 9px; color: var(--muted); font-style: italic; margin-left: 4px; }

table { border-collapse: separate; border-spacing: 0; }
```

Note: the old `prefers-color-scheme: dark` block and the `Arial` body font are intentionally
gone (they conflicted with the light glass design — see spec). The `body` font now references
the `--font-manrope` variable wired up in Task 8.

- [ ] **Step 2: Commit**

```bash
git add esg-pipeline/web/app/globals.css
git commit -m "style(web): port mockup-v5 glass styles, drop dark-mode/Arial"
```

---

### Task 8: Load Manrope font in `layout.tsx`

**Files:**
- Modify: `esg-pipeline/web/app/layout.tsx` (replace contents)

Read first: `node_modules/next/dist/docs/01-app/01-getting-started/13-fonts.md` (Next 16 `next/font` usage).

- [ ] **Step 1: Replace `layout.tsx`**

```tsx
import type { Metadata } from "next";
import { Manrope } from "next/font/google";
import "./globals.css";

const manrope = Manrope({
  variable: "--font-manrope",
  subsets: ["latin"],
  weight: ["400", "500", "600", "700", "800"],
});

export const metadata: Metadata = {
  title: "ESG Controversy",
  description: "ESG risk monitoring for Vietnamese listed companies",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className={`${manrope.variable} h-full antialiased`}>
      <body className="min-h-full">{children}</body>
    </html>
  );
}
```

- [ ] **Step 2: Verify the build compiles**

Run: `npm run build`
Expected: build succeeds (the new `page.tsx` is built in Task 9; current page still compiles).
If the font import API differs in Next 16, consult the fonts doc and adjust.

- [ ] **Step 3: Commit**

```bash
git add esg-pipeline/web/app/layout.tsx
git commit -m "feat(web): load Manrope font via next/font"
```

---

### Task 9: Rewrite `page.tsx` as the results page

**Files:**
- Modify: `esg-pipeline/web/app/page.tsx` (replace contents)

Read first: `node_modules/next/dist/docs/01-app/01-getting-started/05-server-and-client-components.md`
(confirm `"use client"` + hooks usage for Next 16).

- [ ] **Step 1: Replace `page.tsx`**

```tsx
"use client";

import { useState, useEffect, useMemo } from "react";
import {
  type EsgEvent,
  type Company,
  type Lang,
  type Filters,
  type SortKey,
  type Pillar,
  type Severity,
  type ControversyLevel,
  filterEvents,
  sortEvents,
  paginate,
  mergeTickers,
  pickHeadline,
  severityLabel,
  controversyLabel,
  PAGE_SIZE,
} from "@/lib/esg";

const PILLAR_CLASS: Record<Pillar, string> = { E: "group-E", S: "group-S", G: "group-G" };

// Minimal bilingual UI string table.
const T = {
  tag: { en: "📰 ESG events", vi: "📰 Sự kiện ESG" },
  title: { en: "ESG controversy events", vi: "Sự kiện rủi ro ESG" },
  loading: { en: "Loading…", vi: "Đang tải…" },
  error: { en: "Couldn't load events.", vi: "Không tải được dữ liệu." },
  retry: { en: "Retry", vi: "Thử lại" },
  empty: { en: "No matching events", vi: "Không có sự kiện phù hợp" },
  search: { en: "🔍 Search headline…", vi: "🔍 Tìm tiêu đề…" },
  allTickers: { en: "All tickers", vi: "Tất cả ticker" },
  allPillars: { en: "All pillars", vi: "Tất cả nhóm" },
  allSeverity: { en: "All severity", vi: "Tất cả mức độ" },
  high: { en: "High", vi: "Cao" },
  medium: { en: "Medium", vi: "Trung bình" },
  allControversy: { en: "All controversy", vi: "Tất cả tranh cãi" },
  notClassified: { en: "Not classified", vi: "Chưa phân loại" },
  clear: { en: "Clear", vi: "Xoá lọc" },
  colDate: { en: "Date", vi: "Ngày" },
  colCompany: { en: "Company", vi: "Công ty" },
  colHeadline: { en: "Headline", vi: "Tiêu đề" },
  colPillar: { en: "Pillar", vi: "Nhóm" },
  colSeverity: { en: "Severity", vi: "Mức độ" },
  colControversy: { en: "Controversy", vi: "Tranh cãi" },
  colJustification: { en: "Justification", vi: "Lý do" },
  colSource: { en: "Source", vi: "Nguồn" },
  prev: { en: "← Prev", vi: "← Trước" },
  next: { en: "Next →", vi: "Sau →" },
  footer: {
    en: "English translations auto-generated · Original VN preserved · Falls back to VN if translation pending",
    vi: "Bản tiếng Anh dịch tự động · Bản gốc VN luôn giữ nguyên · Hiện bản VN nếu chưa dịch",
  },
} as const;

const SORT_LABELS: Record<SortKey, { en: string; vi: string }> = {
  date_desc: { en: "Sort: Newest", vi: "Sắp xếp: Mới nhất" },
  date_asc: { en: "Sort: Oldest", vi: "Sắp xếp: Cũ nhất" },
  ticker_asc: { en: "Sort: Ticker A→Z", vi: "Sắp xếp: Ticker A→Z" },
};

const EMPTY_FILTERS: Filters = { ticker: "", pillar: "", severity: "", controversy: "", query: "" };

export default function Home() {
  const [events, setEvents] = useState<EsgEvent[]>([]);
  const [companies, setCompanies] = useState<Company[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  const [lang, setLang] = useState<Lang>("en");
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [sortKey, setSortKey] = useState<SortKey>("date_desc");
  const [page, setPage] = useState(1);

  // Keep <html lang> in sync for accessibility.
  useEffect(() => {
    document.documentElement.lang = lang;
  }, [lang]);

  function load() {
    setLoading(true);
    setError(false);
    fetch("/api/events")
      .then((r) => {
        if (!r.ok) throw new Error("events");
        return r.json();
      })
      .then((data: EsgEvent[]) => setEvents(data))
      .catch(() => setError(true))
      .finally(() => setLoading(false));
    // Tickers are best-effort; failure degrades to event-derived tickers.
    fetch("/api/tickers")
      .then((r) => (r.ok ? r.json() : []))
      .then((data: Company[]) => setCompanies(data))
      .catch(() => setCompanies([]));
  }

  useEffect(load, []);

  // Any change to a filter, the query, or the sort returns to page 1.
  function updateFilters(patch: Partial<Filters>) {
    setFilters((f) => ({ ...f, ...patch }));
    setPage(1);
  }
  function updateSort(key: SortKey) {
    setSortKey(key);
    setPage(1);
  }
  function clearAll() {
    setFilters(EMPTY_FILTERS);
    setSortKey("date_desc");
    setPage(1);
  }

  const tickerOptions = useMemo(() => mergeTickers(companies, events), [companies, events]);
  const processed = useMemo(
    () => sortEvents(filterEvents(events, filters), sortKey),
    [events, filters, sortKey],
  );
  const pageData = useMemo(() => paginate(processed, page, PAGE_SIZE), [processed, page]);

  const isFiltered =
    filters.ticker || filters.pillar || filters.severity || filters.controversy || filters.query;

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen subtle-text">
        {T.loading[lang]}
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center min-h-screen gap-4">
        <p className="subtle-text">{T.error[lang]}</p>
        <button className="pill-btn-light" onClick={load}>{T.retry[lang]}</button>
      </div>
    );
  }

  return (
    <>
      {/* Nav pill */}
      <div className="max-w-7xl mx-auto px-6 pt-6">
        <nav className="pill-nav flex items-center justify-between h-14 px-4">
          <div className="flex items-center gap-3 pl-2">
            <div className="w-7 h-7 rounded-full gradient-accent flex items-center justify-center">
              <div className="w-3 h-3 bg-white rounded-full" />
            </div>
            <span className="font-bold text-base">ESG Controversy</span>
          </div>
          <div className="lang-toggle">
            <button className={`lang-btn ${lang === "en" ? "active" : ""}`} onClick={() => setLang("en")}>EN</button>
            <button className={`lang-btn ${lang === "vi" ? "active" : ""}`} onClick={() => setLang("vi")}>VI</button>
          </div>
        </nav>
      </div>

      <div className="max-w-7xl mx-auto px-6 py-10">
        {/* Header */}
        <div className="mb-7">
          <span className="pill-tag mb-4 inline-block">{T.tag[lang]}</span>
          <h1 className="display-h1">{T.title[lang]}</h1>
          <p className="subtle-text mt-2 text-sm">
            {processed.length.toLocaleString()}{" "}
            {lang === "en" ? "events · sorted by most recent" : "sự kiện · sắp xếp theo ngày mới nhất"}
          </p>
        </div>

        {/* Filter bar */}
        <div className="flex flex-wrap items-center gap-3 mb-5">
          <input
            className="filter-input flex-1 min-w-[220px]"
            placeholder={T.search[lang]}
            value={filters.query}
            onChange={(e) => updateFilters({ query: e.target.value })}
          />
          <select className="filter-input" value={filters.ticker}
            onChange={(e) => updateFilters({ ticker: e.target.value })}>
            <option value="">{T.allTickers[lang]}</option>
            {tickerOptions.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
          <select className="filter-input" value={filters.pillar}
            onChange={(e) => updateFilters({ pillar: e.target.value as "" | Pillar })}>
            <option value="">{T.allPillars[lang]}</option>
            <option value="E">E</option>
            <option value="S">S</option>
            <option value="G">G</option>
          </select>
          <select className="filter-input" value={filters.severity}
            onChange={(e) => updateFilters({ severity: e.target.value as "" | Severity })}>
            <option value="">{T.allSeverity[lang]}</option>
            <option value="Cao">{T.high[lang]}</option>
            <option value="Trung bình">{T.medium[lang]}</option>
          </select>
          <select className="filter-input" value={filters.controversy}
            onChange={(e) => updateFilters({ controversy: e.target.value as Filters["controversy"] })}>
            <option value="">{T.allControversy[lang]}</option>
            <option value="Major">Major</option>
            <option value="Minor">Minor</option>
            <option value="No">No</option>
            <option value="none">{T.notClassified[lang]}</option>
          </select>
          <select className="filter-input" value={sortKey}
            onChange={(e) => updateSort(e.target.value as SortKey)}>
            {(Object.keys(SORT_LABELS) as SortKey[]).map((k) => (
              <option key={k} value={k}>{SORT_LABELS[k][lang]}</option>
            ))}
          </select>
          {isFiltered ? (
            <button className="text-xs text-violet-600 hover:underline ml-1" onClick={clearAll}>
              {T.clear[lang]}
            </button>
          ) : null}
        </div>

        {/* Results table */}
        <div className="glass-card overflow-hidden">
          <div className="px-7 py-4 flex justify-between items-center text-sm border-b border-violet-100/50">
            <div>
              <strong>{processed.length.toLocaleString()}</strong>{" "}
              {lang === "en"
                ? `events · page ${pageData.page} of ${pageData.totalPages}`
                : `sự kiện · trang ${pageData.page}/${pageData.totalPages}`}
            </div>
            <div className="subtle-text">{PAGE_SIZE} / page</div>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-xs subtle-text uppercase tracking-wider">
                <tr className="border-b border-violet-100">
                  <th className="text-left px-7 py-3 font-semibold">{T.colDate[lang]}</th>
                  <th className="text-left py-3 font-semibold">Ticker</th>
                  <th className="text-left py-3 font-semibold">{T.colCompany[lang]}</th>
                  <th className="text-left py-3 font-semibold">{T.colHeadline[lang]}</th>
                  <th className="text-left py-3 font-semibold">{T.colPillar[lang]}</th>
                  <th className="text-left py-3 font-semibold">{T.colSeverity[lang]}</th>
                  <th className="text-left py-3 font-semibold">{T.colControversy[lang]}</th>
                  <th className="text-left py-3 font-semibold">{T.colJustification[lang]}</th>
                  <th className="text-left pr-7 py-3 font-semibold">{T.colSource[lang]}</th>
                </tr>
              </thead>
              <tbody>
                {pageData.rows.length === 0 ? (
                  <tr><td colSpan={9} className="px-7 py-10 text-center subtle-text">{T.empty[lang]}</td></tr>
                ) : (
                  pageData.rows.map((e, i) => {
                    const h = pickHeadline(e, lang);
                    return (
                      <tr key={`${e.ticker}-${e.date}-${i}`} className="row border-b border-violet-100/30 align-top">
                        <td className="px-7 py-4 font-mono subtle-text whitespace-nowrap">{e.date}</td>
                        <td className="py-4"><span className="ticker-chip">{e.ticker}</span></td>
                        <td className="py-4 subtle-text">{e.company}</td>
                        <td className="py-4 font-medium max-w-md">
                          {h.text}
                          {h.isFallback ? <span className="fallback-tag">(VI · awaiting translation)</span> : null}
                        </td>
                        <td className="py-4"><span className={`badge ${PILLAR_CLASS[e.type]}`}>{e.type}</span></td>
                        <td className="py-4">
                          <span className={e.severity === "Cao" ? "sev-high" : "sev-med"}>
                            {severityLabel(e.severity, lang)}
                          </span>
                        </td>
                        <td className="py-4">
                          {e.controversy_level
                            ? <span className={`badge ctrl-${e.controversy_level}`}>{controversyLabel(e.controversy_level, lang)}</span>
                            : <span className="subtle-text text-xs">—</span>}
                        </td>
                        <td className="py-4 text-xs subtle-text max-w-[200px]">
                          {e.controversy_justification || "—"}
                        </td>
                        <td className="pr-7 py-4 subtle-text">
                          {e.url
                            ? <a href={e.url} target="_blank" rel="noopener noreferrer" className="text-violet-600 hover:underline">{e.source} ↗</a>
                            : e.source}
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>

          {/* Pagination footer */}
          <div className="px-7 py-4 flex justify-between items-center text-sm border-t border-violet-100/50">
            <span className="subtle-text">
              {lang === "en"
                ? `Showing page ${pageData.page} of ${pageData.totalPages}`
                : `Trang ${pageData.page}/${pageData.totalPages}`}
            </span>
            <div className="flex items-center gap-2">
              <button className="pill-btn-light" disabled={pageData.page <= 1}
                onClick={() => setPage((p) => p - 1)}>{T.prev[lang]}</button>
              <span className="subtle-text text-xs px-2">
                {lang === "en" ? `Page ${pageData.page} of ${pageData.totalPages}` : `Trang ${pageData.page}/${pageData.totalPages}`}
              </span>
              <button className="pill-btn-light" disabled={pageData.page >= pageData.totalPages}
                onClick={() => setPage((p) => p + 1)}>{T.next[lang]}</button>
            </div>
          </div>
        </div>

        <div className="mt-6 text-xs subtle-text text-center">{T.footer[lang]}</div>
      </div>
    </>
  );
}
```

- [ ] **Step 2: Lint and type-check via build**

Run: `npm run lint`
Expected: no errors.

Run: `npm run build`
Expected: build succeeds with the new page.

- [ ] **Step 3: Commit**

```bash
git add esg-pipeline/web/app/page.tsx
git commit -m "feat(web): rewrite home as bilingual ESG results page (mockup-v5 style)"
```

---

### Task 10: Manual verification

Use @superpowers:verification-before-completion — do not claim done until each box is checked
against real observed output.

**Files:** none (verification only).

- [ ] **Step 1: Run unit tests**

Run: `npm run test`
Expected: all suites in `lib/esg.test.ts` PASS.

- [ ] **Step 2: Start the dev server and verify in a browser**

Run: `npm run dev`, open http://localhost:3000 .
Confirm by observation:
- [ ] Page matches `mockup-new.html` visually: Manrope font, violet gradient background, glass cards, pill nav, colored E/S/G badges.
- [ ] EN/VI toggle flips header, filter labels, column headers, and footer; headlines switch between `summary_en` and `summary`. A row with no `summary_en` shows VI text + the "(VI · awaiting translation)" tag in EN mode.
- [ ] Ticker dropdown lists tickers A→Z; selecting one narrows the table.
- [ ] Pillar / Severity / Controversy selects each narrow the table; "Not classified" shows only rows with empty controversy.
- [ ] Search box: typing "thanh hoa" matches rows whose headline contains "Thanh Hoá".
- [ ] Sort control reorders rows (newest/oldest/ticker).
- [ ] Changing any filter/search/sort resets to page 1; Prev disabled on page 1, Next disabled on last page; counts update.
- [ ] "Clear" appears only when a filter is active and resets everything.
- [ ] No "Scan now" button anywhere; no console errors; no dark-mode/Arial regression.

- [ ] **Step 3: Verify the error state**

Temporarily break the events URL (e.g. in DevTools block `/api/events`, or stop network) and
reload: confirm the error message + Retry button render instead of a blank table. Restore.

- [ ] **Step 4: Final commit (only if any fixes were needed)**

```bash
git add -A
git commit -m "fix(web): address manual verification findings"
```

---

## Out of scope (do not build)

- Dashboard hero + stat cards, Ticker Detail view, per-article cards.
- Backend changes to emit `fetcher` / `matched_alias` / `sector` / `location`.
- Server-side pagination or new API endpoints.
- React component (render) unit tests — the render layer is verified manually; only the pure
  `lib/esg.ts` logic is unit-tested.
