/** Справочник марок и моделей, популярных в Узбекистане, — только подсказки при вводе: можно ввести любую марку. */
export const MAKES: Record<string, string[]> = {
  Chevrolet: ["Cobalt", "Nexia", "Gentra", "Lacetti", "Spark", "Malibu", "Tracker", "Onix", "Damas", "Labo", "Captiva", "Equinox", "Tahoe", "Trailblazer"],
  Daewoo: ["Matiz", "Nexia", "Lacetti", "Gentra"],
  Ravon: ["R2", "R3", "R4", "Nexia R3"],
  Lada: ["Granta", "Vesta", "Largus", "Niva", "XRAY"],
  Hyundai: ["Accent", "Elantra", "Sonata", "Tucson", "Santa Fe", "Creta", "Palisade"],
  Kia: ["Rio", "K5", "Sportage", "Sorento", "Seltos", "Carnival"],
  Toyota: ["Camry", "Corolla", "RAV4", "Land Cruiser", "Land Cruiser Prado", "Highlander"],
  BYD: ["Song Plus", "Han", "Seal", "Chazor", "E2", "Yuan Plus", "Tang", "Qin Plus"],
  Chery: ["Tiggo 4", "Tiggo 7 Pro", "Tiggo 8 Pro", "Arrizo 6"],
  Haval: ["Jolion", "H6", "Dargo", "M6"],
  Jetour: ["X70", "X90", "Dashing"],
  Changan: ["CS35", "CS55", "CS75", "UNI-K", "UNI-V"],
  Geely: ["Coolray", "Monjaro", "Atlas", "Emgrand"],
  "Li Auto": ["L7", "L8", "L9"],
  Zeekr: ["001", "007", "X"],
  Tesla: ["Model 3", "Model Y", "Model S", "Model X"],
  "Mercedes-Benz": ["C-Class", "E-Class", "S-Class", "GLE", "GLS", "Sprinter"],
  BMW: ["3 Series", "5 Series", "7 Series", "X5", "X7"],
  Lexus: ["ES", "RX", "LX", "GX"],
  Nissan: ["Almera", "X-Trail", "Patrol", "Qashqai"],
  Volkswagen: ["Polo", "Passat", "Tiguan", "ID.4", "ID.6"],
  Honda: ["Accord", "Civic", "CR-V"],
  Mitsubishi: ["Outlander", "Pajero", "L200"],
  Isuzu: ["NQR", "D-Max"],
  GAZ: ["Газель", "Газель Next", "Соболь"],
  Hongqi: ["H5", "HS5", "E-HS9"],
  Tank: ["300", "500"],
  Leapmotor: ["C11", "C10"],
};

export const MAKE_NAMES = Object.keys(MAKES);

/** Цвета — коды хранятся на сервере, подпись — из словаря; незнакомое значение показывается как есть. */
/* swatch — готовый класс Tailwind (CSP style-src 'self': инлайн-стилей в разметке нет). */
export const COLORS: { code: string; swatch: string }[] = [
  { code: "white", swatch: "bg-[#ffffff]" },
  { code: "black", swatch: "bg-[#111111]" },
  { code: "silver", swatch: "bg-[#c9ced6]" },
  { code: "gray", swatch: "bg-[#7a828c]" },
  { code: "blue", swatch: "bg-[#2b5fcf]" },
  { code: "red", swatch: "bg-[#c62828]" },
  { code: "beige", swatch: "bg-[#d8c6a3]" },
  { code: "brown", swatch: "bg-[#6d4c35]" },
  { code: "green", swatch: "bg-[#2e7d4f]" },
  { code: "yellow", swatch: "bg-[#f2c230]" },
  { code: "orange", swatch: "bg-[#ee7a1f]" },
];

export function findMake(input: string): string | null {
  const q = input.trim().toLowerCase();
  if (!q) return null;
  return MAKE_NAMES.find((m) => m.toLowerCase() === q) ?? null;
}
