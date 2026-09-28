import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { LotSocket, type SocketStatus, type WsEvent } from "./socket";
import { refreshAccessToken, tokenNeedsRefresh } from "@/api/client";
import { useAuthStore } from "@/auth/store";
import { keys } from "@/api/hooks";

export interface FeedItem extends WsEvent {
  receivedAt: number;
}

const FEED_LIMIT = 200;

/** Лента событий площадки + статус соединения; при (пере)подключении и GAP инвалидируются серверные запросы. */
export function useLotSocket(lotId: string | null) {
  const qc = useQueryClient();
  const [status, setStatus] = useState<SocketStatus>("closed");
  const [stale, setStale] = useState(true);
  const [feed, setFeed] = useState<FeedItem[]>([]);
  const [alerts, setAlerts] = useState<FeedItem[]>([]);
  const socketRef = useRef<LotSocket | null>(null);

  useEffect(() => {
    if (lotId === null) return;
    const resync = () => {
      void qc.invalidateQueries({ queryKey: keys.occupancy(lotId) });
      void qc.invalidateQueries({ queryKey: ["sessions", lotId] });
      void qc.invalidateQueries({ queryKey: keys.review });
      void qc.invalidateQueries({ queryKey: keys.cameras });
      void qc.invalidateQueries({ queryKey: ["map", lotId] });
    };
    const socket = new LotSocket({
      lotId,
      getToken: async () => {
        if (tokenNeedsRefresh()) await refreshAccessToken();
        return useAuthStore.getState().accessToken;
      },
      onStatus: (next, isStale) => {
        setStatus(next);
        setStale(isStale);
      },
      onResync: resync,
      onEvent: (event) => {
        const item: FeedItem = { ...event, receivedAt: Date.now() };
        if (event.type === "ALERT") setAlerts((prev) => [item, ...prev].slice(0, 50));
        if (["ENTRY", "EXIT", "PAYMENT", "BARRIER", "ALERT", "SESSION_CLOSED", "SESSION_UPDATED"].includes(event.type)) {
          setFeed((prev) => [item, ...prev].slice(0, FEED_LIMIT));
        }
        switch (event.type) {
          case "OCCUPANCY":
            qc.setQueryData(keys.occupancy(lotId), (old: { occupied: number; capacity: number; free: number; ratio: number; lot_id: string } | undefined) => {
              if (!old) return old;
              const occupied = Number(event.data?.["occupied"] ?? old.occupied);
              return { ...old, occupied, free: Math.max(old.capacity - occupied, 0), ratio: old.capacity ? occupied / old.capacity : 0 };
            });
            break;
          case "SESSION_CREATED":
          case "SESSION_CLOSED":
          case "SESSION_UPDATED":
            void qc.invalidateQueries({ queryKey: ["sessions", lotId] });
            void qc.invalidateQueries({ queryKey: ["map", lotId] }); // v2 этап 5: счётчики зон на карте
            void qc.invalidateQueries({ queryKey: keys.review });
            if (typeof event.data?.["session_id"] === "string") void qc.invalidateQueries({ queryKey: keys.session(event.data["session_id"]) });
            break;
          case "PAYMENT":
            if (typeof event.data?.["session_id"] === "string") void qc.invalidateQueries({ queryKey: keys.payments(event.data["session_id"]) });
            break;
          case "CAMERA_STATUS":
            void qc.invalidateQueries({ queryKey: keys.cameras });
            void qc.invalidateQueries({ queryKey: ["map", lotId] });
            break;
          default:
            break;
        }
      },
    });
    socketRef.current = socket;
    socket.start();
    return () => {
      socket.stop();
      socketRef.current = null;
    };
  }, [lotId, qc]);

  return { status, stale, feed, alerts };
}
