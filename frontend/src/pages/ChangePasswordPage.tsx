import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { KeyRound } from "lucide-react";
import { useTranslation } from "@/i18n";
import { useAuth } from "@/auth/useAuth";
import { useChangeOwnPassword } from "@/api/hooks";
import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/input";
import { Card } from "@/components/ui/card";
import { ErrorBox } from "@/components/ErrorBox";
import { toast } from "@/components/ui/toast";

/** Смена своего пароля: обязательная после временного пароля администратора или по желанию из шапки.
    Сервер завершает все сеансы — входим заново новым паролем тем же логином. */
export function ChangePasswordPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { me, login, logout } = useAuth();
  const change = useChangeOwnPassword();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const [loginError, setLoginError] = useState<unknown>(null);
  const forced = me?.must_change_password === true;
  const ident = me?.email || me?.phone || "";
  const valid = current.length > 0 && next.length >= 12 && next === repeat;

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!valid || change.isPending) return;
    change.mutate(
      { current_password: current, new_password: next },
      {
        onSuccess: async () => {
          try {
            await login(ident, next);
            toast.ok(t("pwd.done"));
            navigate("/", { replace: true });
          } catch (err) {
            setLoginError(err);
          }
        },
      },
    );
  };

  return (
    <div className="flex min-h-full items-center justify-center p-4">
      <Card className="w-full max-w-sm p-6" data-testid="change-password">
        <div className="mb-4 flex items-center gap-3">
          <span className="grid h-10 w-10 place-items-center rounded-xl bg-accent-soft text-accent-ink">
            <KeyRound className="h-5 w-5" aria-hidden="true" />
          </span>
          <div>
            <h1 className="text-lg font-extrabold">{t("pwd.title")}</h1>
            <div className="text-[12.5px] font-semibold text-muted">{me?.display_name ?? ident}</div>
          </div>
        </div>
        <p className={forced ? "mb-3 rounded-xl bg-warn-soft p-3 text-[13px] font-bold text-warn-ink" : "mb-3 text-[13px] font-semibold text-muted"} data-testid="pwd-reason">
          {forced ? t("pwd.forced") : t("pwd.voluntary")}
        </p>
        <form onSubmit={submit} className="space-y-3">
          <div>
            <Label htmlFor="p-cur">{t("pwd.current")}</Label>
            <Input id="p-cur" type="password" autoComplete="current-password" autoFocus value={current} onChange={(e) => setCurrent(e.target.value)} data-testid="pwd-current" />
          </div>
          <div>
            <Label htmlFor="p-new">{t("pwd.new")}</Label>
            <Input id="p-new" type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} data-testid="pwd-new" />
            <p className="mt-1 text-[12px] font-semibold text-muted">{t("join.hint")}</p>
          </div>
          <div>
            <Label htmlFor="p-rep">{t("pwd.repeat")}</Label>
            <Input id="p-rep" type="password" autoComplete="new-password" value={repeat} onChange={(e) => setRepeat(e.target.value)} data-testid="pwd-repeat" />
            {repeat.length > 0 && next !== repeat && <p className="mt-1 text-[12px] font-semibold text-warn-ink">{t("join.mismatch")}</p>}
          </div>
          {change.isError && <ErrorBox error={change.error} />}
          {loginError !== null && <ErrorBox error={loginError} />}
          <Button type="submit" size="lg" className="w-full" disabled={!valid || change.isPending} data-testid="pwd-submit">
            {t("pwd.submit")}
          </Button>
          <div className="flex justify-between text-[13px] font-semibold">
            {!forced ? (
              <button type="button" className="text-muted underline" onClick={() => navigate(-1)}>
                {t("signup.back")}
              </button>
            ) : (
              <span />
            )}
            <button
              type="button"
              className="text-muted underline"
              onClick={async () => {
                await logout();
                navigate("/login");
              }}
            >
              {t("app.logout")}
            </button>
          </div>
        </form>
      </Card>
    </div>
  );
}
