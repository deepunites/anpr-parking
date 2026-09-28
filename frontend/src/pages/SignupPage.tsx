import { useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { ArrowLeft, MailCheck } from "lucide-react";
import { fmt, useTranslation } from "@/i18n";
import { useAuth } from "@/auth/useAuth";
import { usePlans, useSignupStart, useSignupVerify } from "@/api/hooks";
import { ApiError } from "@/api/errors";
import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/input";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorBox } from "@/components/ErrorBox";
import { PlanCards } from "@/components/billing/PlanCards";

const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;

/** Самостоятельная регистрация клиента (v2 этап 3): тариф → данные → код подтверждения → вход → мастер настройки. */
export function SignupPage() {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const { login } = useAuth();
  const plans = usePlans();
  const start = useSignupStart();
  const verify = useSignupVerify();

  const [plan, setPlan] = useState(params.get("plan") ?? "START");
  const [company, setCompany] = useState("");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  const [password, setPassword] = useState("");
  const [terms, setTerms] = useState(false);
  const [code, setCode] = useState("");
  const [loginError, setLoginError] = useState<unknown>(null);
  const [entering, setEntering] = useState(false);
  const pending = start.data ?? null;

  const formValid = company.trim().length >= 2 && name.trim().length >= 2 && EMAIL.test(email.trim()) && password.length >= 12 && terms;

  const submitForm = (e: FormEvent) => {
    e.preventDefault();
    if (!formValid || start.isPending) return;
    start.mutate({
      company: company.trim(),
      name: name.trim(),
      email: email.trim(),
      phone: phone.trim() || null,
      password,
      plan_code: plan,
      locale: i18n.language,
      accept_terms: terms,
    });
  };

  const submitCode = (e: FormEvent) => {
    e.preventDefault();
    if (!pending || code.trim().length < 4 || verify.isPending) return;
    verify.mutate(
      { signupId: pending.signup_id, code: code.trim() },
      {
        onSuccess: async () => {
          setEntering(true);
          try {
            await login(email.trim(), password);
            navigate("/welcome", { replace: true });
          } catch (err) {
            setLoginError(err);
            setEntering(false);
          }
        },
      },
    );
  };

  const attemptsLeft = verify.error instanceof ApiError && verify.error.code === "WRONG_CODE" ? verify.error.details?.attempts_left : undefined;

  return (
    <div className="flex min-h-full items-start justify-center bg-[radial-gradient(900px_500px_at_10%_0%,color-mix(in_srgb,var(--accent)_18%,transparent),transparent_60%)] p-4 sm:items-center">
      <Card className="w-full max-w-3xl p-5 sm:p-7">
        <div className="mb-5 flex items-center gap-3">
          <span className="grid h-10 w-10 place-items-center rounded-xl bg-[linear-gradient(145deg,#2b6cf6,#7fa7f6)] text-lg font-extrabold text-white">P</span>
          <div>
            <h1 className="text-lg font-extrabold">{pending ? t("signup.codeTitle") : t("signup.title")}</h1>
            <div className="text-[12.5px] font-semibold text-muted">{t("signup.subtitle")}</div>
          </div>
        </div>

        {!pending && (
          <form onSubmit={submitForm} className="space-y-5" data-testid="signup-form">
            {plans.isPending ? <Skeleton className="h-40 w-full" /> : plans.data && <PlanCards plans={plans.data} value={plan} onChange={setPlan} />}
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="sm:col-span-2">
                <Label htmlFor="su-company">{t("signup.company")}</Label>
                <Input id="su-company" required autoComplete="organization" placeholder={t("signup.companyPh")} value={company} onChange={(e) => setCompany(e.target.value)} />
              </div>
              <div>
                <Label htmlFor="su-name">{t("signup.name")}</Label>
                <Input id="su-name" required autoComplete="name" placeholder={t("signup.namePh")} value={name} onChange={(e) => setName(e.target.value)} />
              </div>
              <div>
                <Label htmlFor="su-phone">{t("signup.phone")}</Label>
                <Input id="su-phone" type="tel" autoComplete="tel" inputMode="tel" placeholder={t("signup.phonePh")} value={phone} onChange={(e) => setPhone(e.target.value)} />
              </div>
              <div>
                <Label htmlFor="su-email">{t("signup.email")}</Label>
                <Input id="su-email" type="email" required autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} />
              </div>
              <div>
                <Label htmlFor="su-password">{t("signup.password")}</Label>
                <Input id="su-password" type="password" required minLength={12} autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} aria-describedby="su-password-hint" />
                <div id="su-password-hint" className={password.length > 0 && password.length < 12 ? "mt-1 text-[12px] font-semibold text-warn-ink" : "mt-1 text-[12px] font-semibold text-muted"}>
                  {t("signup.passwordHint")}
                </div>
              </div>
            </div>
            <label className="flex items-start gap-2.5 text-[13.5px] font-semibold">
              <input type="checkbox" className="mt-0.5 h-5 w-5 accent-[var(--accent)]" checked={terms} onChange={(e) => setTerms(e.target.checked)} data-testid="su-terms" />
              {t("signup.terms")}
            </label>
            {start.isError && <ErrorBox error={start.error} />}
            <div className="flex flex-wrap items-center gap-3">
              <span className="text-[13px] font-semibold text-muted">
                {t("login.haveAccount")}{" "}
                <Link to="/login" className="font-bold text-accent-ink underline">
                  {t("login.submit")}
                </Link>
              </span>
              <Button type="submit" size="lg" className="ml-auto" disabled={!formValid || start.isPending} data-testid="su-submit">
                {t("signup.next")}
              </Button>
            </div>
          </form>
        )}

        {pending && (
          <form onSubmit={submitCode} className="mx-auto max-w-sm space-y-4" data-testid="signup-code">
            <p className="flex items-start gap-2 text-[13.5px] font-semibold text-ink-2">
              <MailCheck className="mt-0.5 h-5 w-5 shrink-0 text-accent" aria-hidden="true" />
              {t("signup.codeSent")}
            </p>
            {pending.dev_code && (
              <div className="flex items-center gap-2 rounded-xl border border-dashed border-warn bg-warn-soft px-3 py-2 text-[13px] font-bold text-warn-ink" data-testid="su-dev-code">
                {t("signup.devCode")} <span className="font-mono text-[16px] tracking-[0.2em]">{pending.dev_code}</span>
                <button type="button" className="ml-auto underline" onClick={() => setCode(pending.dev_code ?? "")}>
                  {t("signup.useCode")}
                </button>
              </div>
            )}
            <div>
              <Label htmlFor="su-code">{t("signup.code")}</Label>
              <Input
                id="su-code"
                autoFocus
                inputMode="numeric"
                autoComplete="one-time-code"
                maxLength={6}
                className="h-14 text-center font-mono text-[26px] tracking-[0.4em]"
                value={code}
                onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
                data-testid="su-code"
              />
              {attemptsLeft !== undefined && <div className="mt-1 text-[12.5px] font-bold text-warn-ink">{fmt(t("signup.attemptsLeft"), { n: String(attemptsLeft) })}</div>}
            </div>
            {verify.isError && <ErrorBox error={verify.error} />}
            {loginError !== null && <ErrorBox error={loginError} />}
            <div className="flex items-center gap-3">
              <Button
                type="button"
                variant="ghost"
                onClick={() => {
                  start.reset();
                  verify.reset();
                  setCode("");
                }}
              >
                <ArrowLeft className="h-4 w-4" aria-hidden="true" /> {t("signup.back")}
              </Button>
              <Button type="submit" size="lg" className="ml-auto" disabled={code.length < 4 || verify.isPending || entering} data-testid="su-confirm">
                {entering ? t("signup.creating") : t("signup.confirm")}
              </Button>
            </div>
          </form>
        )}
      </Card>
    </div>
  );
}
