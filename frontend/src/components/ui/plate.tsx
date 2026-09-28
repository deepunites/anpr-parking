import { cn } from "@/lib/cn";

/* Номер — главный объект панели: физическая табличка (белое поле, синяя полоса UZ, моноширинные группы),
   а не строка текста. Разбивка 01 A 777 AA — визуальная, для поиска и API номер остаётся 01A777AA. */
// eslint-disable-next-line react-refresh/only-export-components -- форматирование номера живёт рядом с компонентом
export function splitPlate(plate: string): string {
  const p = plate.replace(/\s+/g, "").toUpperCase();
  // маскированный номер из API (01A***AA) разбивается так же, как полный
  const m = /^(\d{2})([A-Z*])([\d*]{3})([A-Z*]{2})$/.exec(p) ?? /^(\d{2})([\d*]{3})([A-Z*]{3})$/.exec(p);
  return m ? m.slice(1).join(" ") : p;
}

const HIDDEN_LABEL = "номер скрыт";

export function Plate({ plate, size = "md", className, highlight }: { plate: string | null | undefined; size?: "sm" | "md" | "lg" | "xl"; className?: string; highlight?: string }) {
  // номер не расшифрован (другой ключ, анонимизация) — не показываем служебную строку вроде <UNDECRYPTABLE>
  if (plate && /[^A-Za-z0-9*\s]/.test(plate)) {
    return (
      <span className={cn("inline-flex items-center rounded-md border border-dashed border-line px-2 py-1 text-[12px] font-bold text-muted", className)} data-testid="plate-hidden" title={plate}>
        {HIDDEN_LABEL}
      </span>
    );
  }
  const text = plate ? splitPlate(plate) : "— — —";
  const sizes = {
    sm: "text-[14px] px-1.5 py-0.5 gap-1.5 [&>i]:px-1 [&>i]:py-1 [&>i]:text-[7px]",
    md: "text-[18px] px-2 py-1 gap-2 [&>i]:px-1 [&>i]:py-1.5 [&>i]:text-[8px]",
    lg: "text-[24px] px-2.5 py-1.5 gap-2.5 [&>i]:px-1.5 [&>i]:py-2 [&>i]:text-[10px]",
    xl: "text-[30px] px-3 py-2 gap-3 [&>i]:px-1.5 [&>i]:py-3 [&>i]:text-[12px]",
  }[size];
  let body: React.ReactNode = text;
  if (highlight && plate) {
    const h = splitPlate(highlight).replace(/\s/g, "");
    const raw = text.replace(/\s/g, "");
    const idx = raw.indexOf(h);
    if (idx >= 0 && h.length > 0) {
      // подсветка совпавшей части с учётом пробелов-разделителей
      let pos = 0;
      const chars = text.split("").map((ch, i) => {
        const inMatch = ch !== " " && pos >= idx && pos < idx + h.length;
        if (ch !== " ") pos += 1;
        return inMatch ? (
          <mark key={i} className="rounded-sm bg-warn/60 text-[color:var(--plate-ink)]">
            {ch}
          </mark>
        ) : (
          <span key={i}>{ch}</span>
        );
      });
      body = chars;
    }
  }
  return (
    <span
      className={cn(
        "inline-flex items-center whitespace-nowrap rounded-md border-[1.5px] border-[color:var(--plate-ink)] bg-[color:var(--plate-bg)] font-mono font-bold tracking-[0.05em] text-[color:var(--plate-ink)] shadow-[inset_0_0_0_2px_var(--plate-bg),0_1px_0_rgba(0,0,0,0.25)]",
        sizes,
        className,
      )}
      data-testid="plate"
    >
      <i aria-hidden="true" className="not-italic rounded-[3px] bg-[#2b6cf6] font-sans font-extrabold leading-none text-white">
        UZ
      </i>{" "}
      <span>{body}</span>
    </span>
  );
}
