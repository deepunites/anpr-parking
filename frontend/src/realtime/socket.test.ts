import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LotSocket, type SocketStatus } from "./socket";

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static OPEN = 1;
  readyState = 0;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public url: string) {
    FakeWebSocket.instances.push(this);
  }
  send(data: string): void {
    this.sent.push(data);
  }
  close(): void {
    this.readyState = 3;
    this.onclose?.();
  }
  // helpers
  open(): void {
    this.readyState = 1;
    this.onopen?.();
  }
  receive(message: object): void {
    this.onmessage?.({ data: JSON.stringify(message) });
  }
}

describe("LotSocket", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    FakeWebSocket.instances = [];
    vi.stubGlobal("WebSocket", FakeWebSocket);
    vi.stubGlobal("window", { location: { protocol: "http:", host: "test" } });
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  function build() {
    const statuses: Array<[SocketStatus, boolean]> = [];
    const events: object[] = [];
    let resyncs = 0;
    let tokenCalls = 0;
    const socket = new LotSocket({
      lotId: "lot-1",
      getToken: async () => {
        tokenCalls += 1;
        return `token-${tokenCalls}`;
      },
      onEvent: (e) => events.push(e),
      onStatus: (s, stale) => statuses.push([s, stale]),
      onResync: () => {
        resyncs += 1;
      },
      wsFactory: (url) => new FakeWebSocket(url) as unknown as WebSocket,
      baseBackoffMs: 100,
      maxBackoffMs: 1000,
    });
    return { socket, statuses, events, resync: () => resyncs, tokens: () => tokenCalls };
  }

  it("authenticates with the first message (not the query string), reports open after ready", async () => {
    const { socket, statuses } = build();
    socket.start();
    const ws = FakeWebSocket.instances[0]!;
    expect(ws.url).toBe("ws://test/ws/v1/lots/lot-1");
    ws.open();
    await vi.runAllTicks();
    await Promise.resolve();
    expect(JSON.parse(ws.sent[0]!)).toEqual({ type: "auth", token: "token-1" });
    ws.receive({ type: "auth_ok" });
    ws.receive({ type: "ready" });
    expect(statuses.at(-1)).toEqual(["open", false]);
    ws.receive({ type: "ping" });
    expect(JSON.parse(ws.sent.at(-1)!)).toEqual({ type: "pong" });
  });

  it("reconnects with backoff after a drop, marks data stale and resyncs state once reconnected", async () => {
    const { socket, statuses, resync } = build();
    socket.start();
    const first = FakeWebSocket.instances[0]!;
    first.open();
    await Promise.resolve();
    first.receive({ type: "ready" });
    expect(resync()).toBe(0); // первое подключение — не «восстановление»
    first.close();
    expect(statuses.at(-1)).toEqual(["reconnecting", true]);
    expect(FakeWebSocket.instances).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(200); // бэкофф 100 мс × джиттер [0.5, 1.5)
    expect(FakeWebSocket.instances).toHaveLength(2);
    const second = FakeWebSocket.instances[1]!;
    second.open();
    await Promise.resolve();
    second.receive({ type: "ready" });
    expect(statuses.at(-1)).toEqual(["open", false]);
    expect(resync()).toBe(1); // состояние перезапрошено, а не только возобновлён поток
  });

  it("re-authenticates on auth_expiring without reconnecting and resyncs on GAP", async () => {
    const { socket, resync, tokens } = build();
    socket.start();
    const ws = FakeWebSocket.instances[0]!;
    ws.open();
    await Promise.resolve();
    ws.receive({ type: "ready" });
    ws.receive({ type: "auth_expiring" });
    await Promise.resolve();
    await Promise.resolve();
    expect(tokens()).toBe(2);
    expect(JSON.parse(ws.sent.at(-1)!)).toEqual({ type: "auth", token: "token-2" });
    expect(FakeWebSocket.instances).toHaveLength(1);
    ws.receive({ type: "GAP", dropped: 3 });
    expect(resync()).toBe(1);
  });

  it("stop() closes and never reconnects", async () => {
    const { socket, statuses } = build();
    socket.start();
    socket.stop();
    await vi.advanceTimersByTimeAsync(5000);
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(statuses.at(-1)).toEqual(["closed", true]);
  });
});
