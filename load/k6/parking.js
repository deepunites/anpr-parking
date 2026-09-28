// k6 (фаза 8 §6): профиль §3 CORE, а не ровный поток. Сценарий выбирается K6_SCENARIO, длительности делятся на SCALE
// (локально SCALE=30: 1 ч → 2 мин; staging/CI — SCALE=1). Соотношение ENTRY:EXIT ≈ 1:1, дубликаты ≈ 5 %,
// низкая уверенность ≈ 3 %; heartbeat 100 RPS и 50 операторов (WS + API) — параллельно с событиями.
//
//   K6_SCENARIO=baseline|peak|absolute|soak|operators SCALE=30 k6 run --summary-export results/x.json load/k6/parking.js
//
// Переменные: API, ENTRY_KEY, EXIT_KEY, LOT_ID, TOKEN (JWT оператора), RUN_ID (метка событий для сверки).
import http from "k6/http";
import ws from "k6/ws";
import { check, sleep } from "k6";
import { Counter, Trend } from "k6/metrics";

const API = __ENV.API || "http://127.0.0.1:8010";
const WS_URL = API.replace(/^http/, "ws");
const SCALE = Number(__ENV.SCALE || 1);
const RUN_ID = __ENV.RUN_ID || `k6-${Date.now()}`;
const SCENARIO = __ENV.K6_SCENARIO || "baseline";
const WITH_OPERATORS = (__ENV.WITH_OPERATORS || "1") === "1";
// ключи камер: по одному на VU по кругу — как тысячи реальных камер, а не один ключ (лимит 120/мин на ключ)
const ENTRY_CAMERAS = JSON.parse(__ENV.ENTRY_CAMERAS || "[]").concat(__ENV.ENTRY_KEY ? [{ key: __ENV.ENTRY_KEY, code: "ENTRY_01" }] : []);
const EXIT_CAMERAS = JSON.parse(__ENV.EXIT_CAMERAS || "[]").concat(__ENV.EXIT_KEY ? [{ key: __ENV.EXIT_KEY, code: "EXIT_01" }] : []);
function cameraFor(kind) {
  const pool = kind === "ENTRY" ? ENTRY_CAMERAS : EXIT_CAMERAS;
  return pool[(__VU - 1) % pool.length];
}

export const eventsAccepted = new Counter("events_accepted"); // 200/202: сервер принял (сверка: == в БД + DLQ)
export const eventsQueued = new Counter("events_queued"); // 202 — оффлайн-буфер
export const eventsDuplicate = new Counter("events_duplicate");
export const eventsRejected = new Counter("events_rejected"); // 4xx по бизнес-правилам (низкая уверенность и т. п.)
export const eventsFailed = new Counter("events_failed"); // 5xx / сеть
export const exitsRequested = new Counter("exits_requested");
export const entryLatency = new Trend("entry_latency", true);
export const exitLatency = new Trend("exit_latency", true);
export const wsMessages = new Counter("ws_messages");

function scaled(spec) {
  const m = /^(\d+)([smh])$/.exec(spec);
  const seconds = Number(m[1]) * { s: 1, m: 60, h: 3600 }[m[2]];
  return `${Math.max(10, Math.round(seconds / SCALE))}s`;
}

const PROFILES = {
  baseline: { rate: 30, duration: "1h", vus: 80 },
  peak: { rate: 200, duration: "30m", vus: 400 },
  absolute: { rate: 400, duration: "5m", vus: 800 },
  soak: { rate: 30, duration: "12h", vus: 80 },
  operators: { rate: 10, duration: "10m", vus: 40 },
};
const RATE_SCALE = Number(__ENV.RATE_SCALE || 1); // делитель интенсивностей для слабых стендов (в отчёте — явно)
const profile = { ...PROFILES[SCENARIO] };
profile.rate = Math.max(1, Math.round((__ENV.RATE ? Number(__ENV.RATE) : profile.rate) / RATE_SCALE));
profile.vus = Math.max(10, Math.round(profile.vus / RATE_SCALE));
const HEARTBEAT_RATE = Math.max(1, Math.round(100 / RATE_SCALE));
const OPERATORS = Math.max(2, Math.round(50 / RATE_SCALE));
const duration = __ENV.DURATION || scaled(profile.duration);

const scenarios = {
  camera_events: {
    executor: "constant-arrival-rate",
    rate: profile.rate,
    timeUnit: "1s",
    duration,
    preAllocatedVUs: profile.vus,
    maxVUs: profile.vus * 2,
    exec: "cameraEvent",
    tags: { scenario: SCENARIO },
  },
  heartbeat: {
    executor: "constant-arrival-rate",
    rate: HEARTBEAT_RATE,
    timeUnit: "1s",
    duration,
    preAllocatedVUs: Math.max(5, Math.round(40 / RATE_SCALE)),
    maxVUs: Math.max(10, Math.round(120 / RATE_SCALE)),
    exec: "heartbeat",
  },
};
if (WITH_OPERATORS || SCENARIO === "operators") {
  scenarios.operators = { executor: "constant-vus", vus: OPERATORS, duration, exec: "operator" };
}

export const options = {
  scenarios,
  thresholds: {
    "http_req_duration{name:camera_events}": ["p(95)<150", "p(99)<400"],
    "http_req_failed{name:camera_events}": ["rate<0.001"],
    "http_req_duration{name:heartbeat}": ["p(95)<150"],
    "http_req_failed{name:heartbeat}": ["rate<0.001"],
    "http_req_failed{name:operator_api}": ["rate<0.001"],
    events_failed: ["count==0"],
  },
  summaryTrendStats: ["avg", "med", "p(95)", "p(99)", "max", "count"],
};

// ---------------------------------------------------------------- генерация событий
const ALPHABET = "ABCDEFGHJKLMNPRSTUVXYZ";
function plate() {
  const n = Math.floor(Math.random() * 900) + 100;
  const a = ALPHABET[Math.floor(Math.random() * ALPHABET.length)];
  const b = ALPHABET[Math.floor(Math.random() * ALPHABET.length)];
  const c = ALPHABET[Math.floor(Math.random() * ALPHABET.length)];
  const region = String(10 + Math.floor(Math.random() * 80));
  return `${region}${a}${n}${b}${c}`;
}

const VIP_PLATES = JSON.parse(__ENV.VIP_PLATES || "[]"); // ≈30 % въездов — льготники: выезд без оплаты (SLI EXIT e2e)
const entered = []; // очередь въехавших машин этого VU (EXIT 1:1)
let lastBody = null;

function body(kind, pl, confidence) {
  return {
    camera_code: cameraFor(kind).code,
    event_type: kind,
    plate_number: pl,
    confidence,
    event_time: new Date().toISOString(),
    direction: kind === "ENTRY" ? "IN" : "OUT",
    recognition: { provider: "k6", version: RUN_ID },
  };
}

export function cameraEvent() {
  const dice = Math.random();
  let kind;
  let payload;
  if (dice < 0.05 && lastBody !== null) {
    payload = lastBody; // дубликат: тот же кадр повторно (≈5 %)
    kind = payload.event_type;
  } else if (entered.length > 0 && (dice < 0.55 || entered.length > 20)) {
    kind = "EXIT";
    payload = body("EXIT", entered.shift(), 95);
  } else {
    kind = "ENTRY";
    const low = Math.random() < 0.03; // низкая уверенность ≈3 % → MANUAL_REVIEW
    const pl = VIP_PLATES.length > 0 && Math.random() < 0.3 ? VIP_PLATES[Math.floor(Math.random() * VIP_PLATES.length)] : plate();
    payload = body("ENTRY", pl, low ? 70 : 95);
    if (!low) entered.push(pl);
  }
  lastBody = payload;
  const res = http.post(`${API}/api/v1/camera/events`, JSON.stringify(payload), {
    headers: { "Content-Type": "application/json", "X-Camera-Key": cameraFor(kind).key },
    tags: { name: "camera_events", kind },
  });
  (kind === "ENTRY" ? entryLatency : exitLatency).add(res.timings.duration);
  if (res.status === 200 || res.status === 202) {
    eventsAccepted.add(1);
    if (res.status === 202) eventsQueued.add(1);
    if (kind === "EXIT") exitsRequested.add(1);
    let parsed = null;
    try {
      parsed = res.json();
    } catch (e) {
      parsed = null;
    }
    if (parsed && parsed.duplicate) eventsDuplicate.add(1);
  } else if (res.status >= 500 || res.status === 0) {
    eventsFailed.add(1);
  } else {
    eventsRejected.add(1);
  }
  check(res, { "camera event 2xx": (r) => r.status === 200 || r.status === 202 || r.status === 422 });
}

export function heartbeat() {
  const res = http.post(`${API}/api/v1/camera/heartbeat`, JSON.stringify({ firmware_version: "k6", uptime_seconds: 100 }), {
    headers: { "Content-Type": "application/json", "X-Camera-Key": cameraFor("ENTRY").key },
    tags: { name: "heartbeat" },
  });
  check(res, { "heartbeat 204": (r) => r.status === 204 });
}

export function operator() {
  const headers = { Authorization: `Bearer ${__ENV.TOKEN}` };
  const lot = __ENV.LOT_ID;
  const res = ws.connect(`${WS_URL}/ws/v1/lots/${lot}`, {}, (socket) => {
    socket.on("open", () => socket.send(JSON.stringify({ type: "auth", token: __ENV.TOKEN })));
    socket.on("message", () => wsMessages.add(1));
    socket.setInterval(() => {
      const r1 = http.get(`${API}/api/v1/lots/${lot}/occupancy`, { headers, tags: { name: "operator_api" } });
      const r2 = http.get(`${API}/api/v1/lots/${lot}/sessions?limit=50`, { headers, tags: { name: "operator_api" } });
      check(r1, { "occupancy 200": (r) => r.status === 200 });
      check(r2, { "sessions 200": (r) => r.status === 200 });
    }, 5000);
    socket.setTimeout(() => socket.close(), 20000 + Math.random() * 10000); // переподключение, как реальный клиент
  });
  check(res, { "ws 101": (r) => r && r.status === 101 });
  sleep(1);
}
