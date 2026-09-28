/**
 * Клиент `/ws/v1/lots/{lot_id}` (фаза 6 §3–4): auth первым сообщением, ping/pong, переавторизация по `auth_expiring`,
 * автопереподключение с экспоненциальным бэкоффом и джиттером. После восстановления вызывается `onResync` —
 * состояние перезапрашивается REST'ом, а не «возобновляется» из потока. Пока соединения нет — `stale = true`.
 */
export type SocketStatus = "connecting" | "open" | "reconnecting" | "closed";

export interface WsEvent {
  type: string;
  lot_id?: string;
  data?: Record<string, unknown>;
  ts?: string;
  event_id?: string;
  [key: string]: unknown;
}

export interface LotSocketOptions {
  lotId: string;
  getToken: () => Promise<string | null>;
  onEvent: (event: WsEvent) => void;
  onStatus: (status: SocketStatus, stale: boolean) => void;
  onResync: () => void;
  onGap?: (dropped: number) => void;
  wsFactory?: (url: string) => WebSocket;
  baseBackoffMs?: number;
  maxBackoffMs?: number;
  now?: () => number;
}

export class LotSocket {
  private ws: WebSocket | null = null;
  private attempt = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private stopped = false;
  private everOpened = false;
  readonly status: { value: SocketStatus } = { value: "closed" };

  constructor(private readonly opts: LotSocketOptions) {}

  static url(lotId: string): string {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${window.location.host}/ws/v1/lots/${lotId}`;
  }

  start(): void {
    this.stopped = false;
    void this.connect();
  }

  stop(): void {
    this.stopped = true;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.ws?.close(1000, "client stop");
    this.ws = null;
    this.setStatus("closed", true);
  }

  private setStatus(status: SocketStatus, stale: boolean): void {
    this.status.value = status;
    this.opts.onStatus(status, stale);
  }

  private async connect(): Promise<void> {
    if (this.stopped) return;
    this.setStatus(this.everOpened ? "reconnecting" : "connecting", true);
    const factory = this.opts.wsFactory ?? ((url: string) => new WebSocket(url));
    let ws: WebSocket;
    try {
      ws = factory(LotSocket.url(this.opts.lotId));
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;
    ws.onopen = async () => {
      const token = await this.opts.getToken();
      if (token === null) {
        ws.close(4401, "no token");
        return;
      }
      ws.send(JSON.stringify({ type: "auth", token }));
    };
    ws.onmessage = (raw: MessageEvent<string>) => {
      let message: WsEvent;
      try {
        message = JSON.parse(raw.data) as WsEvent;
      } catch {
        return;
      }
      this.handle(ws, message);
    };
    ws.onclose = () => {
      if (this.ws === ws) this.ws = null;
      if (!this.stopped) this.scheduleReconnect();
    };
    ws.onerror = () => {
      /* onclose придёт следом */
    };
  }

  private handle(ws: WebSocket, message: WsEvent): void {
    switch (message.type) {
      case "auth_ok":
        return;
      case "ready": {
        const reconnected = this.everOpened;
        this.everOpened = true;
        this.attempt = 0;
        this.setStatus("open", false);
        if (reconnected) this.opts.onResync(); // после восстановления — перезапрос состояния (§3)
        return;
      }
      case "ping":
        ws.send(JSON.stringify({ type: "pong" }));
        return;
      case "pong":
        return;
      case "auth_expiring":
        void this.opts.getToken().then((token) => {
          if (token !== null && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "auth", token }));
        });
        return;
      case "GAP":
        this.opts.onGap?.(Number(message["dropped"] ?? 0));
        this.opts.onResync();
        return;
      case "error":
        return;
      default:
        this.opts.onEvent(message);
    }
  }

  private scheduleReconnect(): void {
    if (this.stopped) return;
    this.setStatus("reconnecting", true);
    const base = this.opts.baseBackoffMs ?? 1000;
    const max = this.opts.maxBackoffMs ?? 30_000;
    const delay = Math.min(max, base * 2 ** this.attempt) * (0.5 + Math.random());
    this.attempt += 1;
    this.timer = setTimeout(() => void this.connect(), delay);
  }
}
