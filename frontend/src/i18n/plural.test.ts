import { describe, expect, it } from "vitest";
import { fmt, plural } from "./index";

describe("plural", () => {
  const forms = "площадка|площадки|площадок";
  it.each([
    [1, "площадка"],
    [2, "площадки"],
    [4, "площадки"],
    [5, "площадок"],
    [11, "площадок"],
    [12, "площадок"],
    [21, "площадка"],
    [22, "площадки"],
    [100, "площадок"],
  ])("%i → %s", (n, word) => expect(plural(n, forms)).toBe(word));
  it("single form is not declined (uz)", () => expect(plural(5, "maydon")).toBe("maydon"));
});

describe("fmt", () => {
  it("substitutes known names and keeps unknown", () => expect(fmt("{a} из {b} {c}", { a: 1, b: "2" })).toBe("1 из 2 {c}"));
});
