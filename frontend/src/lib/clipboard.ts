/** Копирование в буфер обмена. На http://<IP компьютера> (так панель открывают с телефонов в сети площадки) страница
 *  не считается защищённой и navigator.clipboard там нет — запасной путь: невидимое поле и execCommand("copy"). */
export async function copyToClipboard(text: string): Promise<void> {
  if (window.isSecureContext && navigator.clipboard) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.top = "0";
  area.style.opacity = "0";
  document.body.appendChild(area);
  const previous = document.activeElement as HTMLElement | null;
  area.focus({ preventScroll: true });
  area.select();
  try {
    // устаревший, но единственный способ скопировать на незащищённой странице
    if (!document.execCommand("copy")) throw new Error("copy command was rejected");
  } finally {
    area.remove();
    previous?.focus({ preventScroll: true }); // фокус — обратно на кнопку (клавиатура, диалоги)
  }
}
