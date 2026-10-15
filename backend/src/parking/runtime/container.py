"""Composition root: единственное место, где application-сервисы собираются из инфраструктуры."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

from redis.asyncio import Redis

from parking.modules.access.application.admin_service import AccessAdminService, AccessRepositories
from parking.modules.access.application.resolver import CachedAccessResolver, LocalDecisionCache
from parking.modules.access.infrastructure.redis_cache import RedisDecisionCache
from parking.modules.access.infrastructure.repositories import SqlAccessRuleRepository, SqlSubscriptionRepository
from parking.modules.anpr.application.dlq import DlqAdminService
from parking.modules.anpr.application.ingest import IngestPolicy, IngestRepositories, IngestService
from parking.modules.anpr.application.replay import OfflineReplayer
from parking.modules.anpr.infrastructure.redis_stores import (
    RedisBarrierInfoCache,
    RedisDlqStore,
    RedisOfflineBuffer,
    RedisRateLimiter,
    RedisResponseCache,
)
from parking.modules.anpr.infrastructure.repositories import (
    SqlAnprEventRepository,
    SqlIdempotencyRepository,
    is_connectivity_error,
    is_timeout_error,
)
from parking.modules.audit.infrastructure.writer import SqlAuditWriter
from parking.modules.billing.application.cash import CashierService
from parking.modules.billing.application.common import BillingRepositories
from parking.modules.billing.application.online import OnlinePaymentService
from parking.modules.billing.application.receipt import ReceiptService
from parking.modules.billing.application.reconciliation import ReconciliationService
from parking.modules.billing.application.refunds import RefundService
from parking.modules.billing.application.webhooks import WebhookService
from parking.modules.billing.infrastructure.escpos import EscPosNetworkPrinter, NoPrinter
from parking.modules.billing.infrastructure.gateways import HttpGatewayRegistry
from parking.modules.billing.infrastructure.obligations import SqlPaymentObligations
from parking.modules.billing.infrastructure.repositories import (
    SqlAttemptRepository,
    SqlPaymentRepository,
    SqlProviderAccountLookup,
    SqlReconciliationRepository,
    SqlRefundRepository,
    SqlShiftRepository,
    SqlWebhookInboxRepository,
)
from parking.modules.billing.infrastructure.scanner import SqlBillingScanner
from parking.modules.billing.infrastructure.webhooks import PROTOCOLS
from parking.modules.devices.application.admin import AdminRepositories, DeviceAdminService
from parking.modules.devices.application.camera_auth import CameraAuthService
from parking.modules.devices.application.commands import BarrierCommandExecutor, DeviceRepositories, SqlBarrierCommands
from parking.modules.devices.application.health import CameraHealthJob
from parking.modules.devices.infrastructure.drivers import (
    DriverRegistry,
    HikvisionIsapiDriver,
    HttpRestDriver,
    ModbusTcpDriver,
)
from parking.modules.devices.infrastructure.endpoint_resolver import SystemEndpointResolver
from parking.modules.devices.infrastructure.redis_store import RedisCameraAuthCache, RedisHeartbeatStore
from parking.modules.devices.infrastructure.repositories import (
    BarrierCache,
    SqlBarrierCommandRepository,
    SqlBarrierRepository,
    SqlCameraHealthStore,
    SqlCameraHealthWriter,
    SqlCameraKeyLookup,
    SqlCameraKeyRepository,
    SqlCameraRepository,
)
from parking.modules.identity.application.auth_service import AuthService, IdentityRepositories
from parking.modules.identity.application.invite_service import InviteService
from parking.modules.identity.application.user_service import UserService
from parking.modules.identity.infrastructure.argon2_hasher import Argon2Hasher
from parking.modules.identity.infrastructure.jwt_tokens import JwtAccessTokens
from parking.modules.identity.infrastructure.repositories import (
    SqlInviteRepository,
    SqlLoginAttemptRepository,
    SqlRefreshTokenRepository,
    SqlUserRepository,
)
from parking.modules.integrations.application.api_keys import ApiKeyRepositories, ApiKeyService
from parking.modules.integrations.application.journal import JournalService
from parking.modules.integrations.application.webhooks import OutgoingWebhookService, WebhookRepositories
from parking.modules.integrations.infrastructure.repositories import (
    SqlApiKeyRepository,
    SqlDeliveryQueue,
    SqlJournalRepository,
    SqlWebhookRepository,
)
from parking.modules.integrations.infrastructure.sender import HttpWebhookSender
from parking.modules.media.application.service import MediaRepositories, MediaService
from parking.modules.media.application.video import VideoService
from parking.modules.media.infrastructure.clip_tokens import JwtClipTokens
from parking.modules.media.infrastructure.image_links import JwtImageLinks
from parking.modules.media.infrastructure.pending import RedisPendingFrames
from parking.modules.media.infrastructure.repositories import SqlMediaRepository
from parking.modules.media.infrastructure.s3 import S3ObjectStore
from parking.modules.media.infrastructure.video_archive import FileVideoArchive
from parking.modules.parking.application.services import ParkingRepositories, ParkingService
from parking.modules.parking.infrastructure.occupancy import RedisOccupancy
from parking.modules.parking.infrastructure.repositories import (
    SqlCorrectionRepository,
    SqlSagaRepository,
    SqlSagaScanner,
    SqlSessionRepository,
    SqlVehicleRepository,
)
from parking.modules.privacy.application.service import PrivacyRepositories, PrivacyService
from parking.modules.privacy.infrastructure.repositories import SqlDeletionRequestRepository, SqlSubjectDataRepository
from parking.modules.reports.application.dashboard import OwnerDashboardService, PlatformDashboardService
from parking.modules.reports.infrastructure.prometheus import PrometheusHealth
from parking.modules.reports.infrastructure.queries import SqlOwnerQueries, SqlPlatformQueries
from parking.modules.tariff.application.service import SqlTariffCalculator, TariffAdminService, TariffRepositories
from parking.modules.tariff.infrastructure.repositories import SqlTariffRepository, TariffCache
from parking.modules.tenancy.application.layout_service import LayoutRepositories, LayoutService
from parking.modules.tenancy.application.lot_service import LotService, TenancyRepositories
from parking.modules.tenancy.application.subscription_service import (
    PlatformService,
    SignupService,
    SubscriptionRepositories,
    SubscriptionService,
)
from parking.modules.tenancy.infrastructure.layout import SqlLayoutRepository
from parking.modules.tenancy.infrastructure.repositories import LotCache, SqlLotDirectory, SqlLotRepository
from parking.modules.tenancy.infrastructure.subscription import (
    ConsoleCodeSender,
    RedisSignupStore,
    SqlPlatformDirectory,
    SqlTenantSubscriptionRepository,
    refresh_wrapped_keys,
)
from parking.modules.tenancy.infrastructure.tenant_reader import SqlTenantReader
from parking.platform.breaker import BreakerRegistry, BreakerState, CircuitBreaker
from parking.platform.config import Environment, Settings
from parking.platform.crypto import EnvKeyProvider, KeyProvider, KeyWrapper, LayeredKeyProvider, PlateCrypto
from parking.platform.db.engine import Database, Pool
from parking.platform.db.uow import UnitOfWork
from parking.platform.http.vault_keys import VaultKeyProvider
from parking.platform.logging import get_logger
from parking.platform.redis import build_redis
from parking.platform.secret_box import TenantSecretBox
from parking.platform.secrets import JsonSecretProvider
from parking.runtime.metrics_refresh import MetricsRefresher
from parking.shared.clock import Clock, SystemClock
from parking.shared.ttl_cache import TtlCache

log = get_logger("runtime")


@dataclass(slots=True)
class ReferenceCaches:
    """Кеши процесса для справочников горячего пути camera-события (долг §4 п.1); TTL — reference_cache_ttl_seconds."""

    lots: LotCache
    barriers: BarrierCache
    tariffs: TariffCache

    def clear(self) -> None:
        self.lots.clear()
        self.barriers.clear()
        self.tariffs.clear()


@dataclass(slots=True)
class Container:
    settings: Settings
    database: Database
    clock: Clock
    hasher: Argon2Hasher
    tokens: JwtAccessTokens
    key_provider: KeyProvider
    plate_crypto: PlateCrypto
    auth: AuthService
    users: UserService
    invites: InviteService
    api_keys: ApiKeyService
    outgoing_webhooks: OutgoingWebhookService
    webhook_sender: HttpWebhookSender
    journal: JournalService
    owner_dashboard: OwnerDashboardService
    platform_dashboard: PlatformDashboardService
    prometheus: PrometheusHealth
    lots: LotService
    layouts: LayoutService
    tariffs: TariffAdminService
    redis: Redis
    access_resolver: CachedAccessResolver
    access: AccessAdminService
    parking: ParkingService
    barrier_executor: BarrierCommandExecutor
    saga_scanner: SqlSagaScanner
    occupancy: RedisOccupancy
    camera_auth: CameraAuthService
    devices: DeviceAdminService
    ingest: IngestService
    media: MediaService
    video: VideoService
    privacy: PrivacyService
    dlq: DlqAdminService
    camera_health: CameraHealthJob
    heartbeats: RedisHeartbeatStore
    offline_buffer: RedisOfflineBuffer
    db_breaker: CircuitBreaker
    object_store: S3ObjectStore
    drivers: DriverRegistry
    cashier: CashierService
    receipts: ReceiptService
    refunds: RefundService
    online_payments: OnlinePaymentService
    webhooks: WebhookService
    reconciliation: ReconciliationService
    gateways: HttpGatewayRegistry
    billing_scanner: SqlBillingScanner
    metrics_refresher: MetricsRefresher
    reference_caches: ReferenceCaches
    subscriptions: SubscriptionService
    signup: SignupService
    platform: PlatformService
    layered_keys: LayeredKeyProvider
    key_wrapper: KeyWrapper | None

    async def refresh_tenant_keys(self) -> int:
        """Ключи клиентов, зарегистрированных в других процессах (v2 этап 3). Без KEK — нечего подхватывать."""
        if self.key_wrapper is None:
            return 0
        return await refresh_wrapped_keys(
            self.database.session_factory(Pool.MAINTENANCE), self.key_wrapper, self.layered_keys
        )

    def uow(self, tenant_id: UUID | None, pool: Pool = Pool.CRITICAL) -> UnitOfWork:
        return UnitOfWork(self.database.session_factory(pool), tenant_id)

    def offline_replayer(self, *, consumer_name: str = "replayer-1", **kwargs: object) -> OfflineReplayer:
        return OfflineReplayer(
            buffer=self.offline_buffer,
            ingest=self.ingest,
            uow_factory=self.uow,
            audit=lambda uow: SqlAuditWriter(uow.session),
            is_connectivity_error=is_connectivity_error,
            consumer_name=consumer_name,
            **kwargs,  # type: ignore[arg-type]
        )

    async def aclose(self) -> None:
        await self.journal.close()
        await self.webhook_sender.aclose()
        await self.prometheus.aclose()
        await self.object_store.aclose()
        await self.redis.aclose()

    def tenant_reader(self, uow: UnitOfWork) -> SqlTenantReader:
        return SqlTenantReader(uow.session)


def build_billing_repos(uow: UnitOfWork) -> BillingRepositories:
    return BillingRepositories(
        payments=SqlPaymentRepository(uow.session),
        attempts=SqlAttemptRepository(uow.session),
        refunds=SqlRefundRepository(uow.session),
        shifts=SqlShiftRepository(uow.session),
        inbox=SqlWebhookInboxRepository(uow.session),
        accounts=SqlProviderAccountLookup(uow.session),
        reconciliation=SqlReconciliationRepository(uow.session),
        lots=SqlLotDirectory(uow.session),
        audit=SqlAuditWriter(uow.session),
    )


def build_device_repos(uow: UnitOfWork) -> DeviceRepositories:
    return DeviceRepositories(
        barriers=SqlBarrierRepository(uow.session),
        commands=SqlBarrierCommandRepository(uow.session),
        audit=SqlAuditWriter(uow.session),
    )


def subscription_repos(uow: UnitOfWork) -> SubscriptionRepositories:
    return SubscriptionRepositories(
        subscriptions=SqlTenantSubscriptionRepository(uow.session), audit=SqlAuditWriter(uow.session)
    )


def _key_provider(settings: Settings) -> KeyProvider:
    if settings.keys_source == "env":
        return EnvKeyProvider(settings.tenant_keys_json.get_secret_value())
    assert settings.vault_addr and settings.vault_role_id is not None and settings.vault_secret_id is not None  # I-14
    return VaultKeyProvider(  # читает ключи при старте: без Vault процесс не стартует (fail-fast)
        addr=settings.vault_addr,
        role_id=settings.vault_role_id.get_secret_value(),
        secret_id=settings.vault_secret_id.get_secret_value(),
        mount=settings.vault_kv_mount,
        path=settings.vault_keys_path,
        namespace=settings.vault_namespace,
        ca_file=settings.vault_ca_file,
    )


def build_container(settings: Settings, database: Database, *, clock: Clock | None = None) -> Container:  # noqa: PLR0915 — корень композиции
    clock = clock or SystemClock()
    hasher = Argon2Hasher()
    tokens = JwtAccessTokens(
        private_key_pem=settings.jwt_private_key.get_secret_value(),
        public_key_pem=settings.jwt_public_key,
        issuer=settings.jwt_issuer,
        ttl=timedelta(seconds=settings.access_token_ttl_seconds),
    )

    def uow_factory(tenant_id: UUID | None) -> UnitOfWork:
        return UnitOfWork(database.session_factory(Pool.CRITICAL), tenant_id)

    ttl = settings.reference_cache_ttl_seconds
    caches = ReferenceCaches(lots=TtlCache(ttl), barriers=TtlCache(ttl), tariffs=TtlCache(ttl))

    def identity_repos(uow: UnitOfWork) -> IdentityRepositories:
        return IdentityRepositories(
            users=SqlUserRepository(uow.session),
            refresh_tokens=SqlRefreshTokenRepository(uow.session),
            audit=SqlAuditWriter(uow.session),
            invites=SqlInviteRepository(uow.session),
        )

    def tenancy_repos(uow: UnitOfWork) -> TenancyRepositories:
        return TenancyRepositories(
            lots=SqlLotRepository(uow.session, cache=caches.lots), audit=SqlAuditWriter(uow.session)
        )

    def layout_repos(uow: UnitOfWork) -> LayoutRepositories:
        return LayoutRepositories(
            lots=SqlLotRepository(uow.session, cache=caches.lots),
            layouts=SqlLayoutRepository(uow.session),
            audit=SqlAuditWriter(uow.session),
        )

    def access_repos(uow: UnitOfWork) -> AccessRepositories:
        return AccessRepositories(
            rules=SqlAccessRuleRepository(uow.session),
            audit=SqlAuditWriter(uow.session),
            subscriptions=SqlSubscriptionRepository(uow.session),
        )

    def device_repos(uow: UnitOfWork) -> DeviceRepositories:
        return DeviceRepositories(
            barriers=SqlBarrierRepository(uow.session, cache=caches.barriers, tenant_id=uow.tenant_id),
            commands=SqlBarrierCommandRepository(uow.session),
            audit=SqlAuditWriter(uow.session),
        )

    def admin_repos(uow: UnitOfWork) -> AdminRepositories:
        return AdminRepositories(
            cameras=SqlCameraRepository(uow.session),
            keys=SqlCameraKeyRepository(uow.session),
            barriers=SqlBarrierRepository(uow.session, cache=caches.barriers, tenant_id=uow.tenant_id),
            lots=SqlLotDirectory(uow.session),
            audit=SqlAuditWriter(uow.session),
            devices=device_repos(uow),
        )

    def ingest_repos(uow: UnitOfWork) -> IngestRepositories:
        return IngestRepositories(
            events=SqlAnprEventRepository(uow.session), idempotency=SqlIdempotencyRepository(uow.session)
        )

    billing_repos = build_billing_repos

    def media_repos(uow: UnitOfWork) -> MediaRepositories:
        return MediaRepositories(media=SqlMediaRepository(uow.session), events=SqlAnprEventRepository(uow.session))

    def parking_repos(uow: UnitOfWork) -> ParkingRepositories:
        return ParkingRepositories(
            vehicles=SqlVehicleRepository(uow.session),
            sessions=SqlSessionRepository(uow.session),
            sagas=SqlSagaRepository(uow.session),
            corrections=SqlCorrectionRepository(uow.session),
            lots=SqlLotDirectory(uow.session, cache=caches.lots, tenant_id=uow.tenant_id),
            barriers=SqlBarrierCommands(uow, device_repos(uow), clock),
            payments=SqlPaymentObligations(uow, clock),
            tariff=SqlTariffCalculator(tariff_repos(uow)),
            audit=SqlAuditWriter(uow.session),
        )

    def tariff_repos(uow: UnitOfWork) -> TariffRepositories:
        return TariffRepositories(
            tariffs=SqlTariffRepository(uow.session, cache=caches.tariffs, tenant_id=uow.tenant_id),
            lots=SqlLotDirectory(uow.session, cache=caches.lots, tenant_id=uow.tenant_id),
            audit=SqlAuditWriter(uow.session),
        )

    auth = AuthService(
        uow_factory=uow_factory,
        repos=identity_repos,
        attempts=SqlLoginAttemptRepository(database.session_factory(Pool.CRITICAL)),
        hasher=hasher,
        tokens=tokens,
        clock=clock,
        refresh_ttl=timedelta(seconds=settings.refresh_token_ttl_seconds),
    )
    layered_keys = LayeredKeyProvider(_key_provider(settings))
    key_provider: KeyProvider = layered_keys
    raw_kek = settings.key_wrapping_key.get_secret_value().strip()
    key_wrapper = KeyWrapper(base64.b64decode(raw_kek)) if raw_kek else None
    plate_crypto = PlateCrypto(key_provider)
    redis = build_redis(settings, socket_timeout=settings.redis_socket_timeout_seconds)
    decision_cache = RedisDecisionCache(redis)
    local_cache = LocalDecisionCache()
    occupancy = RedisOccupancy(redis)
    resolver = SystemEndpointResolver()
    secrets = JsonSecretProvider(settings.secrets_json.get_secret_value())
    secret_box = TenantSecretBox(layered_keys)  # секреты клиента: подписи вебхуков, логины контроллеров шлагбаумов
    drivers = DriverRegistry(
        enabled=settings.barrier_drivers_enabled,
        drivers={
            "HTTP_REST": HttpRestDriver(resolver=resolver, secrets=secrets, timeout=settings.barrier_timeout_seconds),
            "MODBUS_TCP": ModbusTcpDriver(resolver=resolver, timeout=settings.barrier_timeout_seconds),
            "HIKVISION_ISAPI": HikvisionIsapiDriver(
                resolver=resolver, box=secret_box, timeout=settings.barrier_timeout_seconds
            ),
        },
    )
    barrier_cache = RedisBarrierInfoCache(redis)
    auth_cache = RedisCameraAuthCache(redis)
    heartbeats = RedisHeartbeatStore(redis)
    pepper = bytes.fromhex(settings.camera_key_pepper.get_secret_value())
    db_breaker = CircuitBreaker(
        "database",
        threshold=settings.db_breaker_threshold,
        ttl_seconds=settings.db_breaker_ttl_seconds,
        on_state_change=_log_db_breaker,
    )
    offline_buffer = RedisOfflineBuffer(redis, maxlen_cap=int(settings.offline_stream_maxlen * 1.2))
    object_store = S3ObjectStore(
        endpoint=settings.s3_endpoint,
        access_key=settings.s3_access_key.get_secret_value(),
        secret_key=settings.s3_secret_key.get_secret_value(),
        bucket=settings.s3_bucket,
        region=settings.s3_region,
    )
    access_resolver = CachedAccessResolver(
        uow_factory=uow_factory,
        repository=lambda uow: SqlAccessRuleRepository(uow.session),
        cache=decision_cache,
        local=local_cache,
        clock=clock,
    )
    parking = ParkingService(
        uow_factory=uow_factory,
        repos=parking_repos,
        crypto=plate_crypto,
        occupancy=occupancy,
        clock=clock,
        access=access_resolver,
    )
    gateways = HttpGatewayRegistry(
        secrets,
        breakers=BreakerRegistry(threshold=5, ttl_seconds=60.0, on_state_change=_log_gateway_breaker),
        timeout=settings.provider_timeout_seconds,
        base_urls=settings.provider_base_urls,
    )
    camera_auth = CameraAuthService(
        uow_factory=uow_factory,
        lookup=lambda uow: SqlCameraKeyLookup(uow.session),
        cache=auth_cache,
        pepper=pepper,
        clock=clock,
        abuse_threshold=settings.camera_auth_abuse_threshold,
        is_connectivity_error=is_connectivity_error,
    )
    media = MediaService(
        store=object_store,
        uow_factory=uow_factory,
        repos=media_repos,
        session_lookup=parking.get_session,
        clock=clock,
        max_bytes=settings.media_max_bytes,
        put_ttl_seconds=settings.s3_presign_put_ttl_seconds,
        get_ttl_seconds=settings.s3_presign_get_ttl_seconds,
        pending=RedisPendingFrames(redis),
        image_links=(
            JwtImageLinks(
                private_key_pem=settings.jwt_private_key.get_secret_value(),
                public_key_pem=settings.jwt_public_key,
                issuer=settings.jwt_issuer,
                ttl=timedelta(seconds=settings.s3_presign_get_ttl_seconds),
            )
            if settings.media_proxy
            else None
        ),
    )
    video = VideoService(
        archive=(
            FileVideoArchive(
                Path(settings.video_dir), ZoneInfo(settings.video_timezone), Path(settings.video_clip_cache_dir)
            )
            if settings.video_dir
            else None
        ),
        tokens=JwtClipTokens(
            private_key_pem=settings.jwt_private_key.get_secret_value(),
            public_key_pem=settings.jwt_public_key,
            issuer=settings.jwt_issuer,
            ttl=timedelta(seconds=settings.video_link_ttl_seconds),
        ),
        session_lookup=parking.get_session,
        clock=clock,
        before_seconds=settings.video_clip_before_seconds,
        after_seconds=settings.video_clip_after_seconds,
    )

    def privacy_repos(uow: UnitOfWork) -> PrivacyRepositories:
        return PrivacyRepositories(
            requests=SqlDeletionRequestRepository(uow.session),
            subject=SqlSubjectDataRepository(uow.session),
            audit=SqlAuditWriter(uow.session),
        )

    async def invalidate_plate_caches(tenant_id: UUID, plate_hash: bytes) -> None:
        local_cache.invalidate(tenant_id, plate_hash)
        await decision_cache.invalidate(tenant_id, plate_hash)

    privacy = PrivacyService(
        uow_factory=uow_factory,
        repos=privacy_repos,
        crypto=plate_crypto,
        objects=object_store,
        clock=clock,
        invalidate_cache=invalidate_plate_caches,
    )
    users = UserService(uow_factory=uow_factory, repos=identity_repos, hasher=hasher, clock=clock)
    invites = InviteService(uow_factory=uow_factory, repos=identity_repos, hasher=hasher, clock=clock)
    api_keys = ApiKeyService(
        uow_factory=uow_factory,
        repos=lambda uow: ApiKeyRepositories(keys=SqlApiKeyRepository(uow.session), audit=SqlAuditWriter(uow.session)),
        pepper=pepper,
        clock=clock,
    )
    webhook_sender = HttpWebhookSender(allow_private=settings.webhook_allow_private)
    outgoing_webhooks = OutgoingWebhookService(
        uow_factory=uow_factory,
        repos=lambda uow: WebhookRepositories(
            webhooks=SqlWebhookRepository(uow.session), audit=SqlAuditWriter(uow.session)
        ),
        queue=SqlDeliveryQueue(database.session_factory(Pool.MAINTENANCE)),
        box=secret_box,
        sender=webhook_sender,
        clock=clock,
        allow_http=settings.webhook_allow_http,
    )
    prometheus = PrometheusHealth(settings.prometheus_url)
    owner_dashboard = OwnerDashboardService(
        uow_factory=lambda tenant_id: UnitOfWork(database.session_factory(Pool.BULK), tenant_id),
        queries=lambda uow: SqlOwnerQueries(uow.session),
        clock=clock,
    )

    async def dlq_length() -> int:
        return int(await redis.xlen("events:dlq")) + int(await redis.xlen("anpr:dlq"))

    platform_dashboard = PlatformDashboardService(
        queries=SqlPlatformQueries(database.session_factory(Pool.MAINTENANCE)),
        health=prometheus,
        dlq_length=dlq_length,
        clock=clock,
    )
    journal = JournalService(
        uow_factory=lambda tenant_id: UnitOfWork(database.session_factory(Pool.BULK), tenant_id),
        repo=lambda uow: SqlJournalRepository(uow.session),
        clock=clock,
    )
    subscriptions = SubscriptionService(uow_factory=uow_factory, repos=subscription_repos, clock=clock)
    signup = SignupService(
        uow_factory=uow_factory,
        repos=subscription_repos,
        store=RedisSignupStore(redis),
        sender=ConsoleCodeSender(log_codes=settings.signup_dev_codes),
        accounts=users,
        wrapper=key_wrapper,
        keys=layered_keys,
        clock=clock,
        code_ttl_seconds=settings.signup_code_ttl_seconds,
        rate_per_hour=settings.signup_rate_per_hour,
        dev_codes=settings.signup_dev_codes,
        # A-76: реального канала (SMS/Telegram) ещё нет — вне dev код некуда доставить, регистрация выключена
        enabled=settings.signup_enabled and settings.env is Environment.DEV,
    )
    platform = PlatformService(
        uow_factory=uow_factory,
        repos=subscription_repos,
        directory=SqlPlatformDirectory(database.session_factory(Pool.MAINTENANCE)),
    )
    lot_service = LotService(uow_factory=uow_factory, repos=tenancy_repos, gate=subscriptions)
    cashier = CashierService(uow_factory=uow_factory, repos=billing_repos, parking=parking, clock=clock)
    return Container(
        settings=settings,
        database=database,
        clock=clock,
        hasher=hasher,
        tokens=tokens,
        key_provider=key_provider,
        plate_crypto=plate_crypto,
        auth=auth,
        users=users,
        invites=invites,
        api_keys=api_keys,
        outgoing_webhooks=outgoing_webhooks,
        webhook_sender=webhook_sender,
        journal=journal,
        owner_dashboard=owner_dashboard,
        platform_dashboard=platform_dashboard,
        prometheus=prometheus,
        lots=lot_service,
        layouts=LayoutService(uow_factory=uow_factory, repos=layout_repos),
        tariffs=TariffAdminService(uow_factory=uow_factory, repos=tariff_repos),
        redis=redis,
        access_resolver=access_resolver,
        access=AccessAdminService(
            uow_factory=uow_factory,
            repos=access_repos,
            crypto=plate_crypto,
            cache=decision_cache,
            local=local_cache,
            clock=clock,
        ),
        parking=parking,
        barrier_executor=BarrierCommandExecutor(
            uow_factory=uow_factory,
            repos=device_repos,
            drivers=drivers,
            clock=clock,
            breakers=BreakerRegistry(
                threshold=settings.barrier_breaker_threshold,
                ttl_seconds=settings.barrier_breaker_ttl_seconds,
                on_state_change=_log_barrier_breaker,
            ),
            retries=settings.barrier_retries,
            barrier_cache=barrier_cache,
        ),
        saga_scanner=SqlSagaScanner(database.session_factory(Pool.MAINTENANCE)),
        occupancy=occupancy,
        camera_auth=camera_auth,
        devices=DeviceAdminService(
            uow_factory=uow_factory,
            repos=admin_repos,
            resolver=resolver,
            auth_cache=auth_cache,
            pepper=pepper,
            clock=clock,
            gate=subscriptions,
            drivers=drivers,
            box=secret_box,
        ),
        ingest=IngestService(
            uow_factory=uow_factory,
            repos=ingest_repos,
            parking=parking,
            access=access_resolver,
            crypto=plate_crypto,
            rate_limiter=RedisRateLimiter(redis),
            response_cache=RedisResponseCache(redis),
            offline=offline_buffer,
            barrier_cache=barrier_cache,
            drivers=drivers,
            heartbeats=heartbeats,
            db_breaker=db_breaker,
            is_connectivity_error=is_connectivity_error,
            is_timeout_error=is_timeout_error,
            image_key_ok=media.key_belongs_to,
            clock=clock,
            policy=IngestPolicy(
                rate_limit_per_minute=settings.camera_rate_limit_per_minute,
                late_after=timedelta(seconds=settings.camera_late_after_seconds),
                offline_maxlen=settings.offline_stream_maxlen,
                offline_alarm_ratio=settings.offline_stream_alarm_ratio,
            ),
        ),
        media=media,
        video=video,
        privacy=privacy,
        dlq=DlqAdminService(
            store=RedisDlqStore(redis), uow_factory=uow_factory, audit=lambda uow: SqlAuditWriter(uow.session)
        ),
        camera_health=CameraHealthJob(
            store=SqlCameraHealthStore(database.session_factory(Pool.MAINTENANCE)),
            heartbeats=heartbeats,
            uow_factory=lambda tenant_id, pool: UnitOfWork(database.session_factory(pool), tenant_id),
            writer=SqlCameraHealthWriter(),
            clock=clock,
        ),
        heartbeats=heartbeats,
        offline_buffer=offline_buffer,
        db_breaker=db_breaker,
        object_store=object_store,
        drivers=drivers,
        cashier=cashier,
        receipts=ReceiptService(
            printer=EscPosNetworkPrinter(settings.receipt_printer) if settings.receipt_printer.strip() else NoPrinter(),
            payment_lookup=cashier.get_payment,
            session_lookup=parking.get_session,
            lot_lookup=lot_service.get,
        ),
        refunds=RefundService(uow_factory=uow_factory, repos=billing_repos, gateways=gateways, clock=clock),
        online_payments=OnlinePaymentService(
            uow_factory=uow_factory, repos=billing_repos, gateways=gateways, clock=clock
        ),
        webhooks=WebhookService(
            uow_factory=uow_factory, repos=billing_repos, protocols=PROTOCOLS, secrets=secrets, clock=clock
        ),
        reconciliation=ReconciliationService(
            uow_factory=uow_factory, repos=billing_repos, gateways=gateways, clock=clock
        ),
        gateways=gateways,
        billing_scanner=SqlBillingScanner(database.session_factory(Pool.MAINTENANCE)),
        metrics_refresher=MetricsRefresher(
            database.session_factory(Pool.MAINTENANCE), redis, occupancy, db_breaker=db_breaker
        ),
        reference_caches=caches,
        subscriptions=subscriptions,
        signup=signup,
        platform=platform,
        layered_keys=layered_keys,
        key_wrapper=key_wrapper,
    )


def _log_gateway_breaker(name: str, old: BreakerState, new: BreakerState) -> None:
    log.error("gateway_breaker_state", provider=name, old=old.value, new=new.value)


def _log_db_breaker(name: str, old: BreakerState, new: BreakerState) -> None:
    log.error("db_breaker_state", breaker=name, old=old.value, new=new.value)


def _log_barrier_breaker(name: str, old: BreakerState, new: BreakerState) -> None:
    log.warning("barrier_breaker_state", barrier_id=name, old=old.value, new=new.value)
