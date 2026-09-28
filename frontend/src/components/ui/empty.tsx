import type { ReactNode } from "react";

/* Пустое состояние с подсказкой: не «пусто», а что это значит и что делать. */
export function EmptyState({ title, hint, action, testId }: { title: string; hint?: string; action?: ReactNode; testId?: string }) {
  return (
    <div className="flex flex-col items-center gap-2 rounded-xl border border-dashed border-line px-4 py-8 text-center" data-testid={testId}>
      <div className="text-[15px] font-bold">{title}</div>
      {hint && <div className="max-w-[40ch] text-[13px] text-muted">{hint}</div>}
      {action}
    </div>
  );
}
