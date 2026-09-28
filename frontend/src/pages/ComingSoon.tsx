import { useTranslation } from "@/i18n";
import { EmptyState } from "@/components/ui/empty";

export function ComingSoon() {
  const { t } = useTranslation();
  return (
    <div className="mx-auto max-w-lg py-10">
      <EmptyState title={t("soon.title")} hint={t("soon.hint")} testId="coming-soon" />
    </div>
  );
}
