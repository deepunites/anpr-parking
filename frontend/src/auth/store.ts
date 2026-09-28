/**
 * Клиентское состояние аутентификации (Zustand): access-токен живёт только в памяти (§5), refresh — в httpOnly
 * cookie бэкенда (`parking_refresh`, SameSite=Strict, path /api/v1/auth). Серверные данные здесь не дублируются.
 */
import { create } from "zustand";

export interface Me {
  user_id: string;
  tenant_id: string;
  role: string;
  permissions: string[];
  lot_scope: string[];
  display_name?: string | null;
  email?: string | null;
  tenant_name?: string | null;
  phone?: string | null;
  must_change_password?: boolean;
  plan_code?: string | null;
  tenant_status?: string | null;
  trial_ends_at?: string | null;
  trial_days_left?: number | null;
  paid_until?: string | null;
  blocked_reason?: string | null;
}

interface AuthState {
  accessToken: string | null;
  expiresAt: number | null; // epoch ms
  me: Me | null;
  setSession: (token: string, expiresIn: number) => void;
  setMe: (me: Me | null) => void;
  clear: () => void;
}

export const useAuthStore = create<AuthState>((set) => ({
  accessToken: null,
  expiresAt: null,
  me: null,
  setSession: (token, expiresIn) => set({ accessToken: token, expiresAt: Date.now() + expiresIn * 1000 }),
  setMe: (me) => set({ me }),
  clear: () => set({ accessToken: null, expiresAt: null, me: null }),
}));

export function hasPermission(me: Me | null, permission: string): boolean {
  return me !== null && me.permissions.includes(permission);
}
