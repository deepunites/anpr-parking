import { describe, expect, it } from "vitest";
import { detectFormat, formatPlate, invalidChars, normalizePlate, uzProgress } from "./plate";

describe("plate input helpers", () => {
  it("normalizes like the server: case, separators, cyrillic homoglyphs", () => {
    expect(normalizePlate(" 01 а 777 аа ")).toBe("01A777AA");
    expect(normalizePlate("10-В-123 ВС")).toBe("10B123BC");
    expect(normalizePlate("01 777 ККК")).toBe("01777KKK");
  });
  it("detects formats", () => {
    expect(detectFormat("01A777AA")).toBe("UZ_STANDARD");
    expect(detectFormat("01777KKK")).toBe("UZ_LEGAL");
    expect(detectFormat("B123CD77")).toBe("FOREIGN");
    expect(detectFormat("01")).toBeNull();
  });
  it("formats groups while typing", () => {
    expect(formatPlate("01A777AA")).toBe("01 A 777 AA");
    expect(formatPlate("01A7")).toBe("01 A 7");
    expect(formatPlate("01777KKK")).toBe("01 777 KKK");
    expect(formatPlate("B123CD77")).toBe("B123CD77");
  });
  it("flags impossible characters and progress", () => {
    expect(invalidChars(normalizePlate("01Ж777АА"))).toEqual(["Ж"]);
    expect(uzProgress("01A77")).toEqual({ expected: "UZ_STANDARD", missing: 3 });
    expect(uzProgress("01A777AA")).toBeNull();
  });
});
