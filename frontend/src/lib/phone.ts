/** Телефон сотрудника — как на бэкенде (identity/domain/contact.py): E.164, местный номер из 9 цифр → +998. */
const E164 = /^\+[0-9]{9,15}$/;

export function normalizePhone(raw: string): string | null {
  let v = raw.trim().replace(/[\s\-().]/g, "");
  if (!v) return null;
  if (v.startsWith("00")) v = "+" + v.slice(2);
  if (!v.startsWith("+")) {
    if (/^\d{9}$/.test(v)) v = "998" + v;
    v = "+" + v;
  }
  return E164.test(v) ? v : null;
}

/** +998901234567 → «+998 90 123-45-67»; прочие страны — как есть. */
export function formatPhone(phone: string | null | undefined): string {
  if (!phone) return "";
  const m = /^\+998(\d{2})(\d{3})(\d{2})(\d{2})$/.exec(phone);
  return m ? `+998 ${m[1]} ${m[2]}-${m[3]}-${m[4]}` : phone;
}

export const isEmail = (v: string) => /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(v.trim());
