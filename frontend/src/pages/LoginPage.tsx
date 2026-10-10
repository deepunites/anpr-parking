import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useTranslation } from "@/i18n";
import { useAuth } from "@/auth/useAuth";
import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/input";
import { Card } from "@/components/ui/card";
import { ErrorBox } from "@/components/ErrorBox";

export function LoginPage() {
  const { t } = useTranslation();
  const { login } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(email, password);
      navigate("/");
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-full items-center justify-center bg-[radial-gradient(900px_500px_at_10%_0%,color-mix(in_srgb,var(--accent)_18%,transparent),transparent_60%)] p-4">
      <Card className="w-full max-w-sm p-6">
        <div className="mb-5 flex items-center gap-3">
          <span className="grid h-10 w-10 place-items-center rounded-xl bg-[linear-gradient(145deg,#2b6cf6,#7fa7f6)] text-lg font-extrabold text-white">P</span>
          <div>
            <div className="text-lg font-extrabold">{t("app.title")}</div>
            <div className="text-[12.5px] font-semibold text-muted">{t("login.title")}</div>
          </div>
        </div>
        <form onSubmit={submit} className="space-y-3">
          <div>
            <Label htmlFor="email">{t("login.email")}</Label>
            <Input id="email" type="text" inputMode="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} placeholder="name@company.uz · +998 90 123-45-67" />
          </div>
          <div>
            <Label htmlFor="password">{t("login.password")}</Label>
            <Input id="password" type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
          </div>
          <ErrorBox error={error} messages={{ UNAUTHENTICATED: t("login.wrongCredentials") }} />
          <Button type="submit" size="lg" className="w-full" disabled={busy} data-testid="login-submit">
            {t("login.submit")}
          </Button>
        </form>
        <p className="mt-4 text-center text-[13px] font-semibold text-muted">
          {t("login.noAccount")}{" "}
          <Link to="/signup" className="font-bold text-accent-ink underline" data-testid="signup-link">
            {t("login.signup")}
          </Link>
        </p>
      </Card>
    </div>
  );
}
