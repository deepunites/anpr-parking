#!/usr/bin/env python
"""Аналог HAProxy лаборатории: TCP-прокси на текущий primary по REST Patroni (`GET /primary` → 200 только у лидера).

    leader_proxy.py <listen_port> <rest1,rest2,...> <pg1,pg2,...>

В production — HAProxy (deploy/postgres/haproxy.cfg): frontend 5000 → primary (option httpchk GET /primary),
frontend 5001 → реплики (GET /replica). Здесь: опрос раз в 0.5 с, при смене лидера открытые соединения рвутся —
клиенты (пул SQLAlchemy, PgBouncer) переподключаются к новому primary.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import urllib.request


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()


class LeaderProxy:
    def __init__(self, rest: list[str], pg: list[str]) -> None:
        self.rest, self.pg = rest, pg
        self.leader: str | None = None
        self.connections: set[asyncio.StreamWriter] = set()

    def probe(self) -> str | None:
        for rest, pg in zip(self.rest, self.pg, strict=True):
            try:
                with urllib.request.urlopen(f"http://{rest}/primary", timeout=0.5) as resp:  # noqa: S310
                    if resp.status == 200:
                        return pg
            except Exception:
                continue
        return None

    async def watch(self) -> None:
        while True:
            leader = await asyncio.to_thread(self.probe)
            if leader != self.leader:
                print(f"leader: {self.leader} -> {leader}", flush=True)
                self.leader = leader
                for w in list(self.connections):  # рвём соединения к старому primary
                    with contextlib.suppress(Exception):
                        w.close()
                self.connections.clear()
            await asyncio.sleep(0.5)

    async def handle(self, cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
        if self.leader is None:
            cw.close()
            return
        host, port = self.leader.rsplit(":", 1)
        try:
            ur, uw = await asyncio.open_connection(host, int(port))
        except OSError:
            cw.close()
            return
        self.connections.update({cw, uw})
        await asyncio.gather(pipe(cr, uw), pipe(ur, cw))
        self.connections.difference_update({cw, uw})


async def main() -> None:
    port, rest, pg = int(sys.argv[1]), sys.argv[2].split(","), sys.argv[3].split(",")
    proxy = LeaderProxy(rest, pg)
    server = await asyncio.start_server(proxy.handle, "127.0.0.1", port)
    async with server:
        await asyncio.gather(server.serve_forever(), proxy.watch())


if __name__ == "__main__":
    asyncio.run(main())
