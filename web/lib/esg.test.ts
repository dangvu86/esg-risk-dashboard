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
