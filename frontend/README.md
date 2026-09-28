# ANPR Parking — frontend (фаза 6)

React 18 + TypeScript 5 + Vite 7, TanStack Query (серверное состояние), Zustand (только клиентское: токен в памяти,
площадка, язык), React Router v6, Tailwind v4, типы из `docs/openapi.json` (`openapi-typescript`), клиент `openapi-fetch`.

```bash
export PATH=~/.local/node/bin:$PATH          # Node 24 LTS без root (см. корневой README)
npm install
npm run gen:api                              # src/api/schema.d.ts ← ../docs/openapi.json (руками не правится)
npm run dev                                  # :5173, прокси /api и /ws → VITE_API_ORIGIN (по умолчанию http://127.0.0.1:8000)
npm run build                                # gen:api + tsc --noEmit + vite build → dist/
npm run lint && npm run typecheck && npm test
npm run e2e                                  # Playwright: поднимает backend/scripts/e2e_stack.py и vite preview (CSP)
npm run lighthouse                           # Live Parking на стенде e2e (стенд и preview должны быть запущены)
npm run check:api                            # CI: типы не разошлись с docs/openapi.json
```

Безопасность (§5): access-токен только в памяти, refresh — httpOnly cookie `parking_refresh` (SameSite=Strict,
path `/api/v1/auth`); один origin через прокси (dev/preview — Vite, prod — `nginx.conf`); CSP без `unsafe-inline`/
`unsafe-eval` для скриптов и стилей (инлайн-`style` запрещён eslint-правилом), HSTS, nosniff, Referrer-Policy,
Permissions-Policy; секретов в бандле нет (`detect-secrets` по `dist/` в gate); логаут отзывает refresh-семейство.
