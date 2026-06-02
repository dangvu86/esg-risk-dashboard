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
