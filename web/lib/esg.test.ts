import { describe, it, expect } from "vitest";
import { normalizeText } from "./esg";
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
