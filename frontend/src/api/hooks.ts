/** Серверное состояние — только TanStack Query (§1 п.3). Ключи запросов централизованы для инвалидации после WS. */
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "./client";
import type { components } from "./schema";

export type SessionOut = components["schemas"]["SessionOut"];
export type SessionStatus = components["schemas"]["SessionStatus"];
export type OccupancyOut = components["schemas"]["OccupancyOut"];
export type LotOut = components["schemas"]["LotOut"];
export type CameraOut = components["schemas"]["CameraOut"];
export type BarrierOut = components["schemas"]["BarrierOut"];
export type PaymentOut = components["schemas"]["PaymentOut"];
export type SagaOut = components["schemas"]["SagaOut"];
export type ShiftOut = components["schemas"]["ShiftOut"];
export type SessionImageOut = components["schemas"]["SessionImageOut"];
export type UserOut = components["schemas"]["UserOut"];
export type RuleOut = components["schemas"]["RuleOut"];
export type IssuedKeyOut = components["schemas"]["IssuedKeyOut"];
export type KeyOut = components["schemas"]["KeyOut"];
export type CreateCameraRequest = components["schemas"]["CreateCameraRequest"];
export type CreateBarrierRequest = components["schemas"]["CreateBarrierRequest"];
export type CreateLotRequest = components["schemas"]["CreateLotRequest"];
export type UpdateLotRequest = components["schemas"]["UpdateLotRequest"];
export type PoliciesModel = components["schemas"]["PoliciesModel"];
export type CreateUserRequest = components["schemas"]["CreateUserRequest"];
export type UpdateUserRequest = components["schemas"]["UpdateUserRequest"];
export type Role = components["schemas"]["Role"];
export type VehicleOut = components["schemas"]["VehicleOut"];
export type AddRuleRequest = components["schemas"]["AddRuleRequest"];
export type CreateSubscriptionRequest = components["schemas"]["CreateSubscriptionRequest"];

export const keys = {
  me: ["me"] as const,
  lots: ["lots"] as const,
  occupancy: (lot: string) => ["occupancy", lot] as const,
  sessions: (lot: string, statuses: string) => ["sessions", lot, statuses] as const,
  session: (id: string) => ["session", id] as const,
  saga: (id: string) => ["saga", id] as const,
  images: (id: string) => ["images", id] as const,
  videos: (id: string) => ["videos", id] as const,
  payments: (id: string) => ["payments", id] as const,
  quote: (id: string) => ["quote", id] as const,
  review: ["review-queue"] as const,
  search: (plate: string) => ["search", plate] as const,
  cameras: ["cameras"] as const,
  barriers: ["barriers"] as const,
  shift: ["shift-current"] as const,
  shifts: ["shifts"] as const,
  users: ["users"] as const,
  rules: ["rules"] as const,
  cameraKeys: (cameraId: string) => ["camera-keys", cameraId] as const,
};

export function useMe(enabled: boolean) {
  return useQuery({ queryKey: keys.me, queryFn: async () => unwrap(await api.GET("/api/v1/auth/me")), enabled, staleTime: 60_000 });
}

export function useLots(enabled = true) {
  return useQuery({ queryKey: keys.lots, queryFn: async () => unwrap(await api.GET("/api/v1/lots")), staleTime: 60_000, enabled });
}

export function useOccupancy(lotId: string | null) {
  return useQuery({
    queryKey: keys.occupancy(lotId ?? ""),
    queryFn: async () => unwrap(await api.GET("/api/v1/lots/{lot_id}/occupancy", { params: { path: { lot_id: lotId ?? "" } } })),
    enabled: lotId !== null,
    refetchInterval: 30_000,
  });
}

export function useLotSessions(lotId: string | null, statuses: SessionStatus[], limit = 50) {
  return useQuery({
    queryKey: keys.sessions(lotId ?? "", statuses.join(",")),
    queryFn: async () =>
      unwrap(await api.GET("/api/v1/lots/{lot_id}/sessions", { params: { path: { lot_id: lotId ?? "" }, query: { status: statuses, limit } } })),
    enabled: lotId !== null,
  });
}

/** Подсчёт незакрытых сессий старше N часов (dry_run — ничего не меняет) и само закрытие (→ «Осиротевшая»). */
export function useStaleSessions(lotId: string | null, hours: number) {
  return useQuery({
    queryKey: ["stale-sessions", lotId ?? "", hours] as const,
    queryFn: async () =>
      unwrap(await api.POST("/api/v1/lots/{lot_id}/sessions/close-stale", { params: { path: { lot_id: lotId ?? "" } }, body: { older_than_hours: hours, dry_run: true } })),
    enabled: lotId !== null && Number.isInteger(hours) && hours >= 1,
    retry: false,
  });
}

export function useCloseStaleSessions(lotId: string | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ hours, reason }: { hours: number; reason: string }) =>
      unwrap(await api.POST("/api/v1/lots/{lot_id}/sessions/close-stale", { params: { path: { lot_id: lotId ?? "" } }, body: { older_than_hours: hours, reason } })),
    onSuccess: () => {
      for (const key of [["sessions"], ["occupancy"], ["stale-sessions"], keys.review]) void qc.invalidateQueries({ queryKey: key });
    },
  });
}

export function useReviewQueue(enabled = true) {
  return useQuery({ queryKey: keys.review, queryFn: async () => unwrap(await api.GET("/api/v1/sessions/review-queue")), enabled, refetchInterval: 30_000 });
}

export function useSession(id: string | null) {
  return useQuery({
    queryKey: keys.session(id ?? ""),
    queryFn: async () => unwrap(await api.GET("/api/v1/sessions/{session_id}", { params: { path: { session_id: id ?? "" } } })),
    enabled: id !== null,
  });
}

export function useSessionImages(id: string | null) {
  return useQuery({
    queryKey: keys.images(id ?? ""),
    queryFn: async () => unwrap(await api.GET("/api/v1/sessions/{session_id}/images", { params: { path: { session_id: id ?? "" } } })),
    enabled: id !== null,
    retry: false,
    staleTime: 240_000, // presigned GET живёт 300 с
  });
}

export function useSessionVideos(id: string | null) {
  return useQuery({
    queryKey: keys.videos(id ?? ""),
    queryFn: async () => unwrap(await api.GET("/api/v1/sessions/{session_id}/videos", { params: { path: { session_id: id ?? "" } } })),
    enabled: id !== null,
    retry: false,
    staleTime: 240_000, // подписанная ссылка на ролик живёт 300 с
  });
}

export function useSessionPayments(id: string | null) {
  return useQuery({
    queryKey: keys.payments(id ?? ""),
    queryFn: async () => unwrap(await api.GET("/api/v1/sessions/{session_id}/payments", { params: { path: { session_id: id ?? "" } } })),
    enabled: id !== null,
  });
}

export function useQuote(id: string | null, enabled: boolean) {
  return useQuery({
    queryKey: keys.quote(id ?? ""),
    queryFn: async () => unwrap(await api.GET("/api/v1/sessions/{session_id}/quote", { params: { path: { session_id: id ?? "" } } })),
    enabled: id !== null && enabled,
    retry: false,
    staleTime: 0,
  });
}

export function useSearch(plate: string) {
  return useQuery({
    queryKey: keys.search(plate),
    queryFn: async () => unwrap(await api.GET("/api/v1/sessions/search", { params: { query: { plate } } })),
    enabled: plate.trim().length >= 2,
    retry: false,
  });
}

export function useCameras(enabled = true) {
  return useQuery({ queryKey: keys.cameras, queryFn: async () => unwrap(await api.GET("/api/v1/cameras")), refetchInterval: 30_000, enabled });
}

export function useBarriers(enabled = true) {
  return useQuery({ queryKey: keys.barriers, queryFn: async () => unwrap(await api.GET("/api/v1/barriers")), staleTime: 60_000, enabled });
}

export function useCurrentShift(enabled: boolean) {
  return useQuery({
    queryKey: keys.shift,
    queryFn: async () => {
      const result = await api.GET("/api/v1/shifts/current");
      if (result.response.status === 404) return null;
      return unwrap(result);
    },
    enabled,
    retry: false,
  });
}

export function useShifts(enabled: boolean) {
  return useQuery({ queryKey: keys.shifts, queryFn: async () => unwrap(await api.GET("/api/v1/shifts")), enabled });
}

export function useUsers(enabled: boolean) {
  return useQuery({ queryKey: keys.users, queryFn: async () => unwrap(await api.GET("/api/v1/users")), enabled });
}

export function useRules(enabled: boolean) {
  return useQuery({ queryKey: keys.rules, queryFn: async () => unwrap(await api.GET("/api/v1/access-rules", { params: { query: { limit: 200 } } })), enabled });
}

/** Мутации — без оптимистичных обновлений: платежи и команды шлагбауму откатить нельзя (§6). */
export function useCorrectPlate(sessionId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: { plate: string; reason: string }) =>
      unwrap(await api.POST("/api/v1/sessions/{session_id}/correct-plate", { params: { path: { session_id: sessionId } }, body })),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: keys.session(sessionId) });
      void qc.invalidateQueries({ queryKey: ["sessions"] });
      void qc.invalidateQueries({ queryKey: keys.review });
    },
  });
}

export function useOpenExit(sessionId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: { reason: string }) =>
      unwrap(await api.POST("/api/v1/sessions/{session_id}/open-exit", { params: { path: { session_id: sessionId } }, body })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: keys.saga(sessionId) }),
  });
}

export function useOpenBarrier() {
  return useMutation({
    mutationFn: async ({ barrierId, reason }: { barrierId: string; reason: string }) =>
      unwrap(await api.POST("/api/v1/barriers/{barrier_id}/open", { params: { path: { barrier_id: barrierId } }, body: { reason } })),
  });
}

export function useConfirmCash(sessionId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ paymentId, amountMinor }: { paymentId: string; amountMinor: number }) =>
      unwrap(await api.POST("/api/v1/payments/{payment_id}/confirm-cash", { params: { path: { payment_id: paymentId } }, body: { amount_minor: amountMinor } })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: keys.payments(sessionId) }),
  });
}

export function usePrepayCash(sessionId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: { amount_minor: number | null; reason?: string | null }) =>
      unwrap(await api.POST("/api/v1/sessions/{session_id}/payments", { params: { path: { session_id: sessionId } }, body })),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: keys.payments(sessionId) });
      void qc.invalidateQueries({ queryKey: keys.quote(sessionId) });
    },
  });
}

export function useOpenShift() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: { lot_id: string; opening_float: number }) => unwrap(await api.POST("/api/v1/shifts", { body })),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: keys.shift });
      void qc.invalidateQueries({ queryKey: keys.shifts });
    },
  });
}

export function useCloseShift() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ shiftId, countedCash, notes }: { shiftId: string; countedCash: number; notes?: string }) =>
      unwrap(await api.POST("/api/v1/shifts/{shift_id}/close", { params: { path: { shift_id: shiftId } }, body: { counted_cash: countedCash, notes: notes ?? null } })),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: keys.shift });
      void qc.invalidateQueries({ queryKey: keys.shifts });
    },
  });
}

/* ---- v2: устройства (камеры, ключи, стрелы) ---- */
export function useCreateCamera() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: CreateCameraRequest) => unwrap(await api.POST("/api/v1/cameras", { body })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: keys.cameras }),
  });
}

export function useUpdateCamera() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ cameraId, body }: { cameraId: string; body: components["schemas"]["UpdateCameraRequest"] }) =>
      unwrap(await api.PATCH("/api/v1/cameras/{camera_id}", { params: { path: { camera_id: cameraId } }, body })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: keys.cameras }),
  });
}

export function useCameraKeys(cameraId: string | null) {
  return useQuery({
    queryKey: keys.cameraKeys(cameraId ?? ""),
    queryFn: async () => unwrap(await api.GET("/api/v1/cameras/{camera_id}/keys", { params: { path: { camera_id: cameraId! } } })),
    enabled: cameraId !== null,
  });
}

export function useIssueCameraKey() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ cameraId, label, reason }: { cameraId: string; label: string | null; reason: string }) =>
      unwrap(await api.POST("/api/v1/cameras/{camera_id}/keys", { params: { path: { camera_id: cameraId } }, body: { label, reason, expires_in_days: null } })),
    onSuccess: (_d, v) => void qc.invalidateQueries({ queryKey: keys.cameraKeys(v.cameraId) }),
  });
}

export function useRotateCameraKey() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ keyId, reason, graceHours }: { keyId: string; reason: string; graceHours: number }) =>
      unwrap(await api.POST("/api/v1/camera-keys/{key_id}/rotate", { params: { path: { key_id: keyId } }, body: { reason, grace_hours: graceHours } })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["camera-keys"] }),
  });
}

export function useRevokeCameraKey() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ keyId, reason }: { keyId: string; reason: string }) =>
      unwrap(await api.POST("/api/v1/camera-keys/{key_id}/revoke", { params: { path: { key_id: keyId } }, body: { reason } })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["camera-keys"] }),
  });
}

export function useCreateBarrier() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: CreateBarrierRequest) => unwrap(await api.POST("/api/v1/barriers", { body })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: keys.barriers }),
  });
}

export function useUpdateBarrier() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ barrierId, body }: { barrierId: string; body: components["schemas"]["UpdateBarrierRequest"] }) =>
      unwrap(await api.PATCH("/api/v1/barriers/{barrier_id}", { params: { path: { barrier_id: barrierId } }, body })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: keys.barriers }),
  });
}

/* ---- v2: площадки и сотрудники ---- */
export function useCreateLot() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: CreateLotRequest) => unwrap(await api.POST("/api/v1/lots", { body })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: keys.lots }),
  });
}

export function useUpdateLot() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ lotId, body }: { lotId: string; body: UpdateLotRequest }) =>
      unwrap(await api.PATCH("/api/v1/lots/{lot_id}", { params: { path: { lot_id: lotId } }, body })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: keys.lots }),
  });
}

export function useCreateUser() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: CreateUserRequest) => unwrap(await api.POST("/api/v1/users", { body })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: keys.users }),
  });
}

export function useUpdateUser() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ userId, body }: { userId: string; body: UpdateUserRequest }) =>
      unwrap(await api.PATCH("/api/v1/users/{user_id}", { params: { path: { user_id: userId } }, body })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: keys.users }),
  });
}

export function useChangeRole() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ userId, role, reason }: { userId: string; role: Role; reason: string }) =>
      unwrap(await api.PATCH("/api/v1/users/{user_id}/role", { params: { path: { user_id: userId } }, body: { role, reason } })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: keys.users }),
  });
}

export function useDeactivateUser() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ userId, reason }: { userId: string; reason: string }) =>
      unwrap(await api.POST("/api/v1/users/{user_id}/deactivate", { params: { path: { user_id: userId } }, body: { reason } })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: keys.users }),
  });
}

export function useResetPassword() {
  return useMutation({
    mutationFn: async ({ userId, password, reason }: { userId: string; password: string; reason: string }) =>
      unwrap(await api.POST("/api/v1/users/{user_id}/password", { params: { path: { user_id: userId } }, body: { password, reason } })),
  });
}

/* ---- v2: машины (марка/модель/цвет), ручной въезд, правила доступа ---- */
export function useVehicleLookup(plate: string, enabled: boolean) {
  return useQuery({
    queryKey: ["vehicle", plate],
    queryFn: async () => unwrap(await api.POST("/api/v1/vehicles/lookup", { body: { plate } })),
    enabled,
    retry: false,
    staleTime: 10_000,
  });
}

export function useManualEntry() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ lotId, body }: { lotId: string; body: components["schemas"]["ManualEntryRequest"] }) =>
      unwrap(await api.POST("/api/v1/lots/{lot_id}/manual-entry", { params: { path: { lot_id: lotId } }, body })),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["sessions"] });
      void qc.invalidateQueries({ queryKey: ["occupancy"] });
      void qc.invalidateQueries({ queryKey: ["vehicle"] });
    },
  });
}

export function useSetSessionVehicle(sessionId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: components["schemas"]["SessionVehicleRequest"]) =>
      unwrap(await api.POST("/api/v1/sessions/{session_id}/vehicle", { params: { path: { session_id: sessionId } }, body })),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: keys.session(sessionId) });
      void qc.invalidateQueries({ queryKey: ["sessions"] });
    },
  });
}

export function useAddRule() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: AddRuleRequest) => unwrap(await api.POST("/api/v1/access-rules", { body })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: keys.rules }),
  });
}

export function useCreateSubscription() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: CreateSubscriptionRequest) => unwrap(await api.POST("/api/v1/subscriptions", { body })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: keys.rules }),
  });
}

export function useRevokeRule() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ ruleId, reason }: { ruleId: string; reason: string }) =>
      unwrap(await api.POST("/api/v1/access-rules/{rule_id}/revoke", { params: { path: { rule_id: ruleId } }, body: { reason } })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: keys.rules }),
  });
}

/* ---- v2 этап 3 «Продажа»: регистрация, подписка, тариф стоянки из мастера, клиенты платформы ---- */
export type PlatformPlanOut = components["schemas"]["PlatformPlanOut"];
export type SubscriptionOut = components["schemas"]["SubscriptionOut"];
export type PlatformTenantOut = components["schemas"]["PlatformTenantOut"];
export type SignupRequest = components["schemas"]["SignupRequest"];
export type PlatformTenantUpdateRequest = components["schemas"]["PlatformTenantUpdateRequest"];

export function usePlans() {
  return useQuery({ queryKey: ["platform-plans"], queryFn: async () => unwrap(await api.GET("/api/v1/plans")), staleTime: 5 * 60_000 });
}

export function useSignupStart() {
  return useMutation({ mutationFn: async (body: SignupRequest) => unwrap(await api.POST("/api/v1/signup", { body })) });
}

export function useSignupVerify() {
  return useMutation({
    mutationFn: async ({ signupId, code }: { signupId: string; code: string }) =>
      unwrap(await api.POST("/api/v1/signup/{signup_id}/verify", { params: { path: { signup_id: signupId } }, body: { code } })),
  });
}

export function useSubscription(enabled: boolean) {
  return useQuery({ queryKey: ["subscription"], queryFn: async () => unwrap(await api.GET("/api/v1/subscription")), enabled });
}

export function useChangePlan() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: { plan_code: string; reason: string }) => unwrap(await api.POST("/api/v1/subscription/plan", { body })),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["subscription"] });
      void qc.invalidateQueries({ queryKey: keys.me });
    },
  });
}

export function usePlatformTenants(enabled: boolean) {
  return useQuery({ queryKey: ["platform-tenants"], queryFn: async () => unwrap(await api.GET("/api/v1/platform/tenants")), enabled });
}

export function useUpdatePlatformTenant() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ tenantId, body }: { tenantId: string; body: PlatformTenantUpdateRequest }) =>
      unwrap(await api.PATCH("/api/v1/platform/tenants/{tenant_id}", { params: { path: { tenant_id: tenantId } }, body })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["platform-tenants"] }),
  });
}

export function useTariffPlans(lotId: string | null) {
  return useQuery({
    queryKey: ["tariff-plans", lotId],
    queryFn: async () => unwrap(await api.GET("/api/v1/lots/{lot_id}/tariff-plans", { params: { path: { lot_id: lotId! } } })),
    enabled: lotId !== null,
  });
}

/** Простой тариф из мастера: план по умолчанию + первая версия (суммы в тийинах, 1 сум = 100 тийинов). */
export function useQuickTariff() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ lotId, firstHour, perHour, graceMinutes, dailyMax }: { lotId: string; firstHour: number; perHour: number; graceMinutes: number; dailyMax: number | null }) => {
      const plan = unwrap(
        await api.POST("/api/v1/lots/{lot_id}/tariff-plans", { params: { path: { lot_id: lotId } }, body: { code: "BASE", name: "Основной", is_default: true, reason: "мастер настройки" } }),
      );
      return unwrap(
        await api.POST("/api/v1/tariff-plans/{plan_id}/versions", {
          params: { path: { plan_id: plan.id } },
          body: {
            rules: {
              grace_minutes: graceMinutes,
              first_hour_price: firstHour * 100,
              price_per_hour: perHour * 100,
              rounding_mode: "UP",
              rounding_step_minutes: 15,
              ...(dailyMax === null ? {} : { daily_max: dailyMax * 100 }),
            },
            valid_from: new Date().toISOString(),
            reason: "мастер настройки",
          },
        }),
      );
    },
    onSuccess: (_d, v) => void qc.invalidateQueries({ queryKey: ["tariff-plans", v.lotId] }),
  });
}

/* ---- раздел «Тариф»: текущие цены, пример расчёта по черновику, новая версия с выбранного момента ---- */
export type TariffVersionOut = components["schemas"]["VersionOut"];
export type TariffPreviewOut = components["schemas"]["PreviewOut"];
export const PREVIEW_MINUTES = [30, 60, 180, 600, 1440];

export function useTariffVersions(planId: string | null) {
  return useQuery({
    queryKey: ["tariff-versions", planId],
    queryFn: async () => unwrap(await api.GET("/api/v1/tariff-plans/{plan_id}/versions", { params: { path: { plan_id: planId! } } })),
    enabled: planId !== null,
  });
}

/** Сколько заплатят по черновику — тем же движком, что и оплата; без записи. */
export function useTariffPreview(lotId: string | null, rules: Record<string, unknown> | null) {
  return useQuery({
    queryKey: ["tariff-preview", lotId, rules],
    queryFn: async () =>
      unwrap(await api.POST("/api/v1/tariff/preview", { body: { lot_id: lotId!, rules: rules!, durations_minutes: PREVIEW_MINUTES } })),
    enabled: lotId !== null && rules !== null,
    placeholderData: keepPreviousData,
    retry: false,
  });
}

/** Новая цена: у площадки без тарифа сначала основной план, затем версия с valid_from (прошлые не меняются). */
export function usePublishTariff() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ lotId, planId, rules, validFrom, reason, holidayCalendarId }: { lotId: string; planId: string | null; rules: Record<string, unknown>; validFrom: string; reason: string; holidayCalendarId: string | null }) => {
      const id =
        planId ??
        unwrap(await api.POST("/api/v1/lots/{lot_id}/tariff-plans", { params: { path: { lot_id: lotId } }, body: { code: "BASE", name: "Основной", is_default: true, reason } })).id;
      return unwrap(
        await api.POST("/api/v1/tariff-plans/{plan_id}/versions", {
          params: { path: { plan_id: id } },
          body: { rules, valid_from: validFrom, reason, holiday_calendar_id: holidayCalendarId },
        }),
      );
    },
    // и после ошибки: план мог создаться, а версия — нет; иначе повтор упрётся в «plan code already exists»
    onSettled: (_d, _e, v) => {
      void qc.invalidateQueries({ queryKey: ["tariff-plans", v.lotId] });
      void qc.invalidateQueries({ queryKey: ["tariff-versions"] });
    },
  });
}

/* ---- v2 этап 4: сотрудники — приглашения по ссылке, смена своего пароля ---- */
export type InviteOut = components["schemas"]["InviteOut"];
export type CreateInviteRequest = components["schemas"]["CreateInviteRequest"];

export function useInvites(enabled: boolean) {
  return useQuery({ queryKey: ["invites"], queryFn: async () => unwrap(await api.GET("/api/v1/invites")), enabled });
}

export function useCreateInvite() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: CreateInviteRequest) => unwrap(await api.POST("/api/v1/invites", { body })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["invites"] }),
  });
}

export function useRevokeInvite() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ inviteId, reason }: { inviteId: string; reason: string }) =>
      unwrap(await api.POST("/api/v1/invites/{invite_id}/revoke", { params: { path: { invite_id: inviteId } }, body: { reason } })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["invites"] }),
  });
}

export function useJoinPreview(token: string) {
  return useQuery({
    queryKey: ["join", token],
    queryFn: async () => unwrap(await api.GET("/api/v1/join/{token}", { params: { path: { token } } })),
    retry: false,
  });
}

export function useAcceptInvite(token: string) {
  return useMutation({
    mutationFn: async (body: components["schemas"]["AcceptInviteRequest"]) =>
      unwrap(await api.POST("/api/v1/join/{token}", { params: { path: { token } }, body })),
  });
}

export function useChangeOwnPassword() {
  return useMutation({
    mutationFn: async (body: { current_password: string; new_password: string }) => unwrap(await api.POST("/api/v1/auth/password", { body })),
  });
}

/* ---- v2: проверка шлагбаума из «Устройств» ---- */
export type ProbeOut = components["schemas"]["ProbeOut"];

export function useProbeBarrier() {
  return useMutation({
    mutationFn: async ({ barrierId, reason }: { barrierId: string; reason: string }) =>
      unwrap(await api.POST("/api/v1/barriers/{barrier_id}/probe", { params: { path: { barrier_id: barrierId } }, body: { reason } })),
  });
}

/** Ручное открытие с ожиданием итога: команду исполняет воркер — опрашиваем до OK/FAILED/SKIPPED (до ~12 с). */
export function useTestOpenBarrier() {
  return useMutation({
    mutationFn: async ({ barrierId, reason }: { barrierId: string; reason: string }) => {
      const started = performance.now();
      const cmd = unwrap(await api.POST("/api/v1/barriers/{barrier_id}/open", { params: { path: { barrier_id: barrierId } }, body: { reason } }));
      if (!cmd.command_id) return { state: cmd.state, error: null as string | null, ms: Math.round(performance.now() - started) };
      for (let i = 0; i < 40; i++) {
        const s = unwrap(await api.GET("/api/v1/barrier-commands/{command_id}", { params: { path: { command_id: cmd.command_id } } }));
        if (s.state === "OK" || s.state === "FAILED" || s.state === "SKIPPED") return { state: s.state, error: s.error ?? null, ms: s.latency_ms ?? Math.round(performance.now() - started) };
        await new Promise((r) => setTimeout(r, 300));
      }
      return { state: "PENDING", error: null, ms: Math.round(performance.now() - started) };
    },
  });
}

/* ---- v2 этап 5: схема площадки и живая карта ---- */
export type LayoutOut = components["schemas"]["LayoutOut"];
export type MapOut = components["schemas"]["MapOut"];

export function useLayout(lotId: string | null, enabled = true) {
  return useQuery({
    queryKey: ["layout", lotId],
    queryFn: async () => unwrap(await api.GET("/api/v1/lots/{lot_id}/layout", { params: { path: { lot_id: lotId! } } })),
    enabled: enabled && lotId !== null,
  });
}

export function useSaveLayout(lotId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: components["schemas"]["SaveLayoutRequest"]) =>
      unwrap(await api.PUT("/api/v1/lots/{lot_id}/layout", { params: { path: { lot_id: lotId } }, body })),
    onSuccess: (saved) => {
      qc.setQueryData(["layout", lotId], saved);
      void qc.invalidateQueries({ queryKey: ["layout-versions", lotId] });
      void qc.invalidateQueries({ queryKey: ["map", lotId] });
    },
  });
}

export function useLayoutVersions(lotId: string | null, enabled: boolean) {
  return useQuery({
    queryKey: ["layout-versions", lotId],
    queryFn: async () => unwrap(await api.GET("/api/v1/lots/{lot_id}/layout/versions", { params: { path: { lot_id: lotId! } } })),
    enabled: enabled && lotId !== null,
  });
}

export async function fetchLayoutVersion(lotId: string, version: number): Promise<LayoutOut> {
  return unwrap(await api.GET("/api/v1/lots/{lot_id}/layout/versions/{version}", { params: { path: { lot_id: lotId, version } } }));
}

/** Живая карта: WebSocket площадки инвалидирует ["map", lot] на событиях сессий; опрос — страховка (10 с). */
export function useLotMap(lotId: string | null) {
  return useQuery({
    queryKey: ["map", lotId],
    queryFn: async () => unwrap(await api.GET("/api/v1/lots/{lot_id}/map", { params: { path: { lot_id: lotId! } } })),
    enabled: lotId !== null,
    refetchInterval: 10_000,
  });
}

// ---------------------------------------------------------------- v2 этап 6: интеграции
export type ApiKeyOut = components["schemas"]["ApiKeyOut"];
export type WebhookOut = components["schemas"]["WebhookOut"];
export type DeliveryOut = components["schemas"]["DeliveryOut"];
export type JournalOut = components["schemas"]["JournalOut"];
export type SandboxOut = components["schemas"]["SandboxOut"];
export type SandboxRequest = components["schemas"]["SandboxRequest"];
export type InboxContentOut = components["schemas"]["InboxContentOut"];
type DeliveryStatus = "PENDING" | "RETRY" | "DELIVERED" | "DEAD";
type JournalOutcome = "ACCEPTED" | "DUPLICATE" | "QUEUED" | "REJECTED" | "ERROR";

export function useIntegrationsCatalog() {
  return useQuery({ queryKey: ["integrations-catalog"], queryFn: async () => unwrap(await api.GET("/api/v1/integrations/catalog")), staleTime: 300_000 });
}

export function useApiKeys() {
  return useQuery({ queryKey: ["api-keys"], queryFn: async () => unwrap(await api.GET("/api/v1/api-keys")) });
}

export function useCreateApiKey() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: components["schemas"]["CreateApiKeyRequest"]) => unwrap(await api.POST("/api/v1/api-keys", { body })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["api-keys"] }),
  });
}

export function useRevokeApiKey() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ keyId, reason }: { keyId: string; reason: string }) =>
      unwrap(await api.POST("/api/v1/api-keys/{api_key_id}/revoke", { params: { path: { api_key_id: keyId } }, body: { reason } })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["api-keys"] }),
  });
}

export function useWebhooks() {
  return useQuery({ queryKey: ["webhooks"], queryFn: async () => unwrap(await api.GET("/api/v1/webhooks")), refetchInterval: 15_000 });
}

export function useCreateWebhook() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: components["schemas"]["CreateWebhookRequest"]) => unwrap(await api.POST("/api/v1/webhooks", { body })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["webhooks"] }),
  });
}

export function useUpdateWebhook() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, body }: { id: string; body: components["schemas"]["UpdateWebhookRequest"] }) =>
      unwrap(await api.PATCH("/api/v1/webhooks/{endpoint_id}", { params: { path: { endpoint_id: id } }, body })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["webhooks"] }),
  });
}

export function useDeleteWebhook() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, reason }: { id: string; reason: string }) =>
      unwrap(await api.POST("/api/v1/webhooks/{endpoint_id}/delete", { params: { path: { endpoint_id: id } }, body: { reason } })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["webhooks"] }),
  });
}

export function useRotateWebhookSecret() {
  return useMutation({
    mutationFn: async ({ id, reason }: { id: string; reason: string }) =>
      unwrap(await api.POST("/api/v1/webhooks/{endpoint_id}/rotate-secret", { params: { path: { endpoint_id: id } }, body: { reason } })),
  });
}

export function useTestWebhook() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (id: string) => unwrap(await api.POST("/api/v1/webhooks/{endpoint_id}/test", { params: { path: { endpoint_id: id } } })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["webhooks"] }),
  });
}

export function useDeliveries(endpointId: string | null, status: DeliveryStatus | null) {
  return useQuery({
    queryKey: ["webhook-deliveries", endpointId, status],
    queryFn: async () =>
      unwrap(await api.GET("/api/v1/webhook-deliveries", { params: { query: { endpoint_id: endpointId ?? undefined, status: status ?? undefined, limit: 50 } } })),
    refetchInterval: 5_000,
  });
}

export function useRedeliver() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, reason }: { id: string; reason: string }) =>
      unwrap(await api.POST("/api/v1/webhook-deliveries/{delivery_id}/redeliver", { params: { path: { delivery_id: id } }, body: { reason } })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["webhook-deliveries"] }),
  });
}

export function useJournal(filter: { outcome: JournalOutcome | null; correlationId: string | null; lotId: string | null }) {
  return useQuery({
    queryKey: ["event-journal", filter],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/integrations/journal", {
          params: { query: { outcome: filter.outcome ?? undefined, correlation_id: filter.correlationId ?? undefined, lot_id: filter.lotId ?? undefined, limit: 200 } },
        }),
      ),
    refetchInterval: 5_000,
  });
}

export function useSandbox() {
  return useMutation({ mutationFn: async (body: SandboxRequest) => unwrap(await api.POST("/api/v1/integrations/sandbox", { body })) });
}

export function useCreateInbox() {
  return useMutation({ mutationFn: async () => unwrap(await api.POST("/api/v1/integrations/test-inbox")) });
}

export function useInbox(token: string | null) {
  return useQuery({
    queryKey: ["test-inbox", token],
    queryFn: async () => unwrap(await api.GET("/api/v1/integrations/test-inbox/{token}", { params: { path: { token: token! } } })),
    enabled: token !== null,
    refetchInterval: 3_000,
    retry: false,
  });
}

export function useApiReference(enabled: boolean) {
  return useQuery({
    queryKey: ["api-reference"],
    queryFn: async () => unwrap(await api.GET("/api/v1/integrations/openapi")) as { paths: Record<string, Record<string, ApiOperation>> },
    enabled,
    staleTime: 600_000,
  });
}

export interface ApiOperation {
  summary?: string;
  description?: string;
  "x-permission"?: string[];
  "x-camera-auth"?: boolean;
}

// ---------------------------------------------------------------- v2 этап 7: дашборды
export type OwnerDashboardOut = components["schemas"]["OwnerDashboardOut"];
export type PlatformDashboardOut = components["schemas"]["PlatformDashboardOut"];

export function useOwnerDashboard(days: number, lotId: string | null, enabled = true) {
  return useQuery({
    queryKey: ["dashboard-owner", days, lotId],
    queryFn: async () => unwrap(await api.GET("/api/v1/dashboard/owner", { params: { query: { days, lot_id: lotId ?? undefined } } })),
    enabled,
    placeholderData: keepPreviousData, // смена периода не сбрасывает экран (вид «таблица», прокрутку)
    refetchInterval: 60_000,
  });
}

export function usePlatformDashboard(enabled: boolean) {
  return useQuery({
    queryKey: ["dashboard-platform"],
    queryFn: async () => unwrap(await api.GET("/api/v1/dashboard/platform")),
    enabled,
    refetchInterval: 60_000,
  });
}
