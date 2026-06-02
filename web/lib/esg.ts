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
