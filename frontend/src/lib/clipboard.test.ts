import { afterEach, describe, expect, it, vi } from "vitest";
import { copyToClipboard } from "./clipboard";

describe("copyToClipboard", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("uses the Clipboard API on a secure page", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("isSecureContext", true);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    await copyToClipboard("http://localhost:8080/join/abc");
    expect(writeText).toHaveBeenCalledWith("http://localhost:8080/join/abc");
  });

  it("falls back to execCommand on http://<IP> where the Clipboard API is missing", async () => {
    vi.stubGlobal("isSecureContext", false);
    vi.stubGlobal("navigator", {});
    let copied = "";
    const exec = vi.fn(() => {
      copied = (document.activeElement as HTMLTextAreaElement | null)?.value ?? "";
      return true;
    });
    document.execCommand = exec;
    await copyToClipboard("http://192.168.1.10:8080/join/abc");
    expect(exec).toHaveBeenCalledWith("copy");
    expect(copied).toBe("http://192.168.1.10:8080/join/abc");
    expect(document.querySelector("textarea")).toBeNull(); // временное поле убрано
  });

  it("reports failure when the browser refuses to copy", async () => {
    vi.stubGlobal("isSecureContext", false);
    vi.stubGlobal("navigator", {});
    document.execCommand = vi.fn(() => false);
    await expect(copyToClipboard("x")).rejects.toThrow();
    expect(document.querySelector("textarea")).toBeNull();
  });
});
