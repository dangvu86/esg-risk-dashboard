# ESG Results Page — Design Spec

**Date:** 2026-06-02
**Status:** Approved (pending spec review)
**Source mockup:** `esg-pipeline/.superpowers/brainstorm/10487-1779853904/mockup-v5.html`

## Goal

Replace the existing single-page ESG dashboard (`esg-pipeline/web/app/page.tsx`) with a
redesigned **single-page results view** styled identically to mockup-v5: a glass-morphism,
bilingual (EN/VI) table of ESG controversy events with full filtering, sorting, and pagination.

The dashboard hero, stat cards, and ticker-detail views from the mockup are **out of scope** —
only the events list is built, but with the complete filter/column set the current app already has.

## Scope decisions (locked)

| Decision | Choice |
|---|---|
| Missing data fields (fetcher, matched_alias, sector, location) | **Omit gracefully** — render only fields that exist in live data |
| Page structure | **Single page**, no tabs, no routing |
| Existing page + "Scan now" button | **Replace entirely**; remove the manual scan trigger and all cloud-function calls |
| Filters & columns | **Full set** (Ticker, E/S/G pillar, Severity, Controversy, + title search) |
| Visual style | **1:1 with mockup-v5** (Manrope, violet glass-morphism) |

## Data

Source is unchanged. Two existing API routes proxy GCS JSON:

- `GET /api/events` → array of events (`esg-risk-dashboard/esg_events.json`)
- `GET /api/tickers` → array of `{ ticker, company }` (`esg-risk-dashboard/top100.json`)

**Event shape (live, confirmed):**

```ts
interface EsgEvent {
  ticker: string;
  company: string;
  type: "E" | "S" | "G";
  date: string;                 // "YYYY-MM-DD"
  summary: string;              // Vietnamese headline
  summary_en?: string;          // English translation (may be empty)
  severity: "Cao" | "Trung bình";
  source: string;
  url: string;
  controversy_level?: "Major" | "Minor" | "No" | "";
  controversy_justification?: string;
  controversy_classified_at?: string;
  created_at?: string;
}
```

Fields shown in the mockup but **absent** from live data (`fetcher`, `matched alias`,
`sector`, `location`) are not rendered. The component should keep clean optional-field handling
so they can be added later without restructuring.

Data is fetched **once on load**; all filtering, sorting, counting, and pagination happen
**client-side** over the in-memory array (dataset is a few thousand rows — well within budget).

## UI layout (top → bottom)

All wrapped in `max-w-7xl mx-auto px-6`, on the mockup's gradient background.

1. **Nav pill** (`pill-nav`) — gradient-dot logo + "ESG Controversy" title (left), EN/VI
   `lang-toggle` (right). The mockup's Dashboard/News Feed/Ticker nav links are removed.
2. **Header** — page title + subtitle line showing the filtered event count
   ("N events · sorted by most recent" / "N sự kiện · sắp xếp theo ngày mới nhất").
3. **Filter bar** —
   - Title search input (matches against `summary` and `summary_en`)
   - Ticker dropdown (populated from `/api/tickers`, plus any tickers present in events)
   - Pillar select: All / E / S / G
   - Severity select: All / High (`Cao`) / Medium (`Trung bình`)
   - Controversy select: All / Major / Minor / No / Not classified (empty)
   - Sort control: Date (newest→oldest, default), Date (oldest→newest), Ticker (A→Z)
   - "Clear filters" button (shown only when a filter is active)
4. **Results table** (`glass-card`, paginated 50/page) — columns:
   `Date · Ticker (chip) · Headline · Pillar (badge) · Severity · Controversy (badge) · Justification · Source (link ↗)`.
   Pagination footer: "N events · page X of Y", 50/page.

## Bilingual behavior

- Toggle adds/removes the `lang-vi` class on `<body>` (same mechanism as the mockup);
  CSS rules `.lang-vi`/`.lang-en` swap visibility. Default language: **EN**.
- **Headline:** EN uses `summary_en`; if empty, fall back to `summary` (VI) with a small
  `(VI · awaiting translation)` `fallback-tag`. VI always uses `summary`.
- **Severity / Controversy / static labels:** bilingual label maps (e.g. `Cao`→High/Cao).
- **Company name:** only one form exists (from `top100.json`), shown as-is in both languages.

## Styling — port from mockup-v5

Reuse the mockup's exact values; do not re-design.

- **Font:** Manrope (weights 400–800). Load via `next/font/google` in `layout.tsx`, replacing
  the current Geist default for this page's body.
- **globals.css:** add the mockup's custom classes verbatim — `.glass-card`, `.pill-nav`,
  `.pill-btn-dark/light`, `.pill-tag`, `.nav-link`, `.lang-toggle`/`.lang-btn`, `.ticker-chip`,
  `.badge` + `.group-E/S/G`, `.row`, `.gradient-accent`, `.stat-label`, `.display-*`,
  `.subtle-text`, `.fallback-tag`, and the `lang-vi`/`lang-en` visibility rules — plus the
  `body` gradient background and CSS vars (`--ink`, `--ink-soft`, `--muted`).
- **Remove conflict:** the existing `globals.css` `prefers-color-scheme: dark` block and the
  Arial `body` font must not override the light glass design — scope or remove them.
- Tailwind utility classes from the mockup (`max-w-7xl`, `grid`, `px-6`, etc.) carry over
  unchanged under the app's Tailwind v4 build.

## Removed from current app

- "Scan now" trigger button + `handleTrigger` + `FUNCTION_URL` cloud-function call.
- Inline-styled summary cards and the old plain table styling (replaced by glass design).

## Files touched

- `esg-pipeline/web/app/page.tsx` — rewritten (single client component).
- `esg-pipeline/web/app/globals.css` — add mockup classes; remove dark-mode/Arial conflict.
- `esg-pipeline/web/app/layout.tsx` — load Manrope; update body font wiring.
- API routes (`app/api/events`, `app/api/tickers`) — unchanged.

## Out of scope

- Dashboard hero + stat cards (total/flagged/latest/last-refresh).
- Ticker Detail view and per-article cards.
- Backend changes to emit `fetcher` / `matched_alias` / `sector` / `location`.
- Server-side pagination or new API endpoints.

## Success criteria

- New page renders at `/` with mockup-v5 visual fidelity (font, colors, glass, badges).
- EN/VI toggle flips all labels and headlines; missing EN headlines fall back to VI with tag.
- All five filters + search narrow the table correctly; counts and pagination reflect the
  filtered set; sort reorders correctly.
- Source links open the original article; no "Scan now" button or cloud-function calls remain.
- No dark-mode/Arial regression from the old `globals.css`.
