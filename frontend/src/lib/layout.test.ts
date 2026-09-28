import { describe, expect, it } from "vitest";
import { duplicateCodes, generateLayout, nextPrefix, normalize, spotsOf, zoneAt, zoneCapacity } from "./layout";

describe("layout", () => {
  it("generates spots like the backend (horizontal and vertical rows)", () => {
    const doc = normalize({
      rows: [
        { id: "r1", x: 40, y: 40, count: 3, direction: "h", prefix: "A", start: 1, spot_w: 25, spot_h: 50 },
        { id: "r2", x: 670, y: 40, count: 2, direction: "v", prefix: "V", start: 1, spot_w: 25, spot_h: 50 },
      ],
    });
    const spots = spotsOf(doc);
    expect(spots.map((s) => s.code)).toEqual(["A1", "A2", "A3", "V1", "V2"]);
    expect(spots[2]).toMatchObject({ x: 90, y: 40 });
    expect(spots[4]).toMatchObject({ x: 670, y: 90 });
  });

  it("prefers the innermost zone", () => {
    const doc = normalize({ zones: [
      { id: "outer", name: "O", color: "blue", x: 0, y: 0, w: 1000, h: 800 },
      { id: "inner", name: "I", color: "green", x: 100, y: 100, w: 200, h: 200 },
    ] });
    expect(zoneAt(doc, 150, 150)?.id).toBe("inner");
    expect(zoneAt(doc, 900, 700)?.id).toBe("outer");
    expect(zoneAt(doc, 1200, 900)).toBeUndefined();
  });

  it("finds duplicate spot codes", () => {
    const doc = normalize({
      rows: [
        { id: "r1", x: 0, y: 0, count: 5, direction: "h", prefix: "A", start: 1 },
        { id: "r2", x: 0, y: 60, count: 5, direction: "h", prefix: "A", start: 4 },
      ],
    });
    expect(duplicateCodes(doc)).toEqual(["A4", "A5"]);
    expect(nextPrefix(doc)).toBe("B");
  });

  it("quick layout scales to big lots within the canvas limits", () => {
    for (const capacity of [1, 7, 500, 2000, 5000]) {
      const doc = generateLayout({ capacity, zoneName: "Z", cameras: [], barriers: [] });
      expect(spotsOf(doc)).toHaveLength(capacity);
      expect(duplicateCodes(doc)).toEqual([]);
      expect(doc.width).toBeLessThanOrEqual(5000);
      expect(doc.height).toBeLessThanOrEqual(5000);
      for (const r of doc.rows) expect(r.prefix).toMatch(/^[A-Z0-9-]{0,6}$/);
    }
  });

  it("quick layout fits the lot capacity and places devices", () => {
    const doc = generateLayout({ capacity: 60, zoneName: "Главная", cameras: [{ id: "c1", direction: "IN" }, { id: "c2", direction: "OUT" }], barriers: [{ id: "b1" }] });
    expect(spotsOf(doc)).toHaveLength(60);
    expect(duplicateCodes(doc)).toEqual([]);
    expect(zoneCapacity(doc, doc.zones[0]!)).toBe(60);
    expect(doc.devices.map((d) => d.kind)).toEqual(["camera", "camera", "barrier"]);
    for (const s of spotsOf(doc)) {
      expect(s.x + s.w).toBeLessThanOrEqual(doc.width);
      expect(s.y + s.h).toBeLessThanOrEqual(doc.height);
      expect(zoneAt(doc, s.x + 1, s.y + 1)?.id).toBe("z1");
    }
  });
});
