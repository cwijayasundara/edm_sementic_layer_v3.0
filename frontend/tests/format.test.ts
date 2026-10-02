import { describe, expect, it } from "vitest";
import { formatCell, formatValue } from "@/lib/format";

describe("formatValue", () => {
  it("formats percent, USD, counts and missing values", () => {
    expect(formatValue(87.456, "%")).toBe("87.5%");
    expect(formatValue(1234567, "USD")).toBe("$1.2M");
    expect(formatValue(12, "")).toBe("12");
    expect(formatValue(1234.5678, null)).toBe("1,234.57");
    expect(formatValue(3, "breaks")).toBe("3 breaks");
    expect(formatValue(null, "%")).toBe("—");
    expect(formatValue(Number.NaN, "%")).toBe("—");
  });
});

describe("formatCell", () => {
  it("renders cells as text", () => {
    expect(formatCell(null)).toBe("");
    expect(formatCell(1234.5)).toBe("1,234.5");
    expect(formatCell({ a: 1 })).toBe('{"a":1}');
    expect(formatCell("EMEA")).toBe("EMEA");
  });
});
