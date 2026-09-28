import { COLORS } from "./catalog";

export interface VehicleValue {
  make: string;
  model: string;
  color: string;
}

export const EMPTY_VEHICLE: VehicleValue = { make: "", model: "", color: "" };

/** «Chevrolet Cobalt · белый» — для списков и карточки. */
export function vehicleLabel(t: (k: string, o?: { defaultValue?: string }) => string, make?: string | null, model?: string | null, color?: string | null): string | null {
  const name = [make, model].filter(Boolean).join(" ");
  const col = color ? t(`colors.${color}`, { defaultValue: color }) : "";
  const out = [name, col].filter(Boolean).join(" · ");
  return out || null;
}

export function colorSwatch(code?: string | null): string | null {
  return COLORS.find((c) => c.code === code)?.swatch ?? null;
}
