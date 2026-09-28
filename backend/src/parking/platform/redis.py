"""Единый асинхронный клиент Redis: короткие таймауты, без ретраев на горячем пути; деградацию решает вызывающий код.

Два вида адреса (`PARKING_REDIS_URL`):
- `redis://[:password@]host:6379/0` — один сервер (dev, sandbox);
- `redis+sentinel://[:password@]s1:26379,s2:26379,s3:26379/<master_name>?db=0[&sentinel_password=…]` — production:
  клиент спрашивает у Sentinel адрес текущего master и после failover переподключается сам (`SentinelConnectionPool`
  сбрасывает соединения при смене master; операция в момент переключения получает ошибку соединения — её обрабатывает
  вызывающий код так же, как любой обрыв Redis: fail-open кешей, 202 в оффлайн-буфер, supervise() воркеров).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from redis.asyncio import BlockingConnectionPool, Redis
from redis.asyncio.sentinel import Sentinel

from parking.platform.config import Settings

SENTINEL_SCHEME = "redis+sentinel"


@dataclass(frozen=True)
class SentinelTarget:
    sentinels: tuple[tuple[str, int], ...]
    master_name: str
    db: int
    password: str | None
    sentinel_password: str | None


def parse_sentinel_url(url: str) -> SentinelTarget | None:
    """None — обычный `redis://`; иначе разобранный адрес Sentinel (≥ 1 адрес, имя master обязательно)."""
    parts = urlsplit(url)
    if parts.scheme != SENTINEL_SCHEME:
        return None
    netloc = parts.netloc
    password: str | None = None
    if "@" in netloc:
        auth, netloc = netloc.rsplit("@", 1)
        password = unquote(auth.split(":", 1)[1] if ":" in auth else auth) or None
    sentinels: list[tuple[str, int]] = []
    for item in netloc.split(","):
        host, _, port = item.strip().rpartition(":")
        if not host or not port.isdigit():
            raise ValueError(f"redis+sentinel: bad sentinel address {item!r}")
        sentinels.append((host, int(port)))
    master_name = parts.path.strip("/")
    if not master_name:
        raise ValueError("redis+sentinel: master name is required in the path")
    query = parse_qs(parts.query)
    db = int(query.get("db", ["0"])[0])
    sentinel_password = query.get("sentinel_password", [None])[0]
    return SentinelTarget(tuple(sentinels), master_name, db, password, sentinel_password)


def celery_broker(url: str) -> tuple[str, dict[str, Any]]:
    """Broker Celery (kombu): `sentinel://h1:p;sentinel://h2:p/db` + `master_name` в transport options."""
    target = parse_sentinel_url(url)
    if target is None:
        return url, {}
    auth = f":{target.password}@" if target.password else ""
    hosts = ";".join(f"sentinel://{auth}{host}:{port}/{target.db}" for host, port in target.sentinels)
    options: dict[str, Any] = {"master_name": target.master_name}
    if target.sentinel_password:
        options["sentinel_kwargs"] = {"password": target.sentinel_password}
    return hosts, options


def build_redis(settings: Settings, *, socket_timeout: float = 0.2, max_connections: int = 64) -> Redis:
    """Блокирующий пул с потолком соединений: всплеск в 100 параллельных запросов ждёт свободное соединение
    (до 1 с), а не открывает сотню новых сокетов и не ловит таймауты на connect."""
    url = settings.redis_url.get_secret_value()
    target = parse_sentinel_url(url)
    if target is not None:
        sentinel = Sentinel(  # type: ignore[no-untyped-call]
            list(target.sentinels),
            sentinel_kwargs={"socket_timeout": max(socket_timeout, 1.0), "password": target.sentinel_password},
            password=target.password,
            db=target.db,
            decode_responses=False,
            socket_timeout=socket_timeout,
            socket_connect_timeout=max(socket_timeout, 1.0),
            health_check_interval=30,
            max_connections=max_connections,
        )
        master: Redis = sentinel.master_for(target.master_name)
        return master
    pool: BlockingConnectionPool = BlockingConnectionPool.from_url(
        url,
        decode_responses=False,
        socket_timeout=socket_timeout,
        socket_connect_timeout=max(socket_timeout, 1.0),
        health_check_interval=30,
        max_connections=max_connections,
        timeout=1.0,
    )
    return Redis(connection_pool=pool)
