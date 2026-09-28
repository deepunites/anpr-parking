import { useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { UserPlus } from "lucide-react";
import { fmt, useTranslation } from "@/i18n";
import { useAuth } from "@/auth/useAuth";
import { useAcceptInvite, useJoinPreview } from "@/api/hooks";
import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/input";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorBox } from "@/components/ErrorBox";
import { formatPhone } from "@/lib/phone";

/** Сотрудник по ссылке приглашения: видит клиента и роль, придумывает пароль — и сразу входит. */
export function JoinPage() {
  const { token = "" } = useParams();
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const { login } = useAuth();
  const preview = useJoinPreview(token);
  const accept = useAcceptInvite(token);
  const [name, setName] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const [password2, setPassword2] = useState("");
  const [loginError, setLoginError] = useState<unknown>(null);
  const p = preview.data;
  const mismatch = password2.length > 0 && password !== password2;
  const valid = password.length >= 12 && password === password2;

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!valid || !p || accept.isPending) return;
    accept.mutate(
      { password, display_name: (name ?? p.display_name).trim() || null, locale: i18n.language },
      {
        onSuccess: async (r) => {
          try {
            await login(r.login, password);
            navigate("/", { replace: true });
          } catch (err) {
            setLoginError(err);
          }
        },
      },
    );
  };

  return (
    <div className="flex min-h-full items-center justify-center bg-[radial-gradient(900px_500px_at_10%_0%,color-mix(in_srgb,var(--accent)_18%,transparent),transparent_60%)] p-4">
      <Card className="w-full max-w-md p-6" data-testid="join-page">
        <div className="mb-5 flex items-center gap-3">
          <span className="grid h-10 w-10 place-items-center rounded-xl bg-[linear-gradient(145deg,#2b6cf6,#7fa7f6)] text-white">
            <UserPlus className="h-5 w-5" aria-hidden="true" />
          </span>
          <div>
            <h1 className="text-lg font-extrabold">{p ? fmt(t("join.to"), { company: p.tenant_name }) : t("join.title")}</h1>
            {p && (
              <div className="text-[12.5px] font-semibold text-muted">
                {t("join.as")}: {t(`roles.${p.role}`)} · {t(`roleHint.${p.role}`, { defaultValue: "" })}
              </div>
            )}
          </div>
        </div>
        {preview.isPending && <Skeleton className="h-40 w-full" />}
        {preview.isError && (
          <div className="space-y-3" data-testid="join-invalid">
            <ErrorBox error={preview.error} />
            <p className="text-[13px] font-semibold text-ink-2">{t("join.invalidHint")}</p>
            <Link to="/login" className="font-bold text-accent-ink underline">
              {t("join.toLogin")}
            </Link>
          </div>
        )}
        {p && (
          <form onSubmit={submit} className="space-y-3">
            <div className="rounded-xl bg-surface-2 p-3 text-[13px] font-semibold">
              {t("join.login")}: <span className="font-mono" data-testid="join-login">{p.login.includes("@") ? p.login : formatPhone(p.login)}</span>
            </div>
            <div>
              <Label htmlFor="j-name">{t("join.name")}</Label>
              <Input id="j-name" value={name ?? p.display_name} onChange={(e) => setName(e.target.value)} autoComplete="name" />
            </div>
            <div>
              <Label htmlFor="j-pass">{t("join.password")}</Label>
              <Input id="j-pass" type="password" autoComplete="new-password" autoFocus value={password} onChange={(e) => setPassword(e.target.value)} data-testid="join-password" />
              <p className={password.length > 0 && password.length < 12 ? "mt-1 text-[12px] font-semibold text-warn-ink" : "mt-1 text-[12px] font-semibold text-muted"}>{t("join.hint")}</p>
            </div>
            <div>
              <Label htmlFor="j-pass2">{t("join.password2")}</Label>
              <Input id="j-pass2" type="password" autoComplete="new-password" value={password2} onChange={(e) => setPassword2(e.target.value)} data-testid="join-password2" />
              {mismatch && <p className="mt-1 text-[12px] font-semibold text-warn-ink">{t("join.mismatch")}</p>}
            </div>
            {accept.isError && <ErrorBox error={accept.error} />}
            {loginError !== null && <ErrorBox error={loginError} />}
            <Button type="submit" size="lg" className="w-full" disabled={!valid || accept.isPending} data-testid="join-submit">
              {t("join.submit")}
            </Button>
          </form>
        )}
      </Card>
    </div>
  );
}
