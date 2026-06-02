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
