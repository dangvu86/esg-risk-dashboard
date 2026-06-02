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
