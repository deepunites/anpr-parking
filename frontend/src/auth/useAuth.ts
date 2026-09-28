import { useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useAuthStore, type Me } from "./store";
import { api, unwrap } from "@/api/client";

export function useAuth() {
  const qc = useQueryClient();
  const { accessToken, me, setSession, setMe, clear } = useAuthStore();

  const login = useCallback(
    async (login: string, password: string) => {
      const token = unwrap(await api.POST("/api/v1/auth/login", { body: { login, password } }));
      setSession(token.access_token, token.expires_in);
      const profile = unwrap(await api.GET("/api/v1/auth/me"));
      setMe(profile as Me);
      qc.setQueryData(["me"], profile); // иначе RequireAuth видел бы профиль прошлого входа (флаг смены пароля)
    },
    [setSession, setMe, qc],
  );

  const logout = useCallback(async () => {
    try {
      await api.POST("/api/v1/auth/logout"); // отзывает refresh-семейство на бэкенде (§5), не только локальное состояние
    } finally {
      clear();
      qc.clear();
    }
  }, [clear, qc]);

  return { authenticated: accessToken !== null, me, login, logout };
}
