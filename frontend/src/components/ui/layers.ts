/* Открытые слои (диалог поверх боковой панели и т. п.): Escape и Tab обрабатывает только верхний, иначе Escape в окне
   основания закрывал бы и панель под ним. */
const stack: symbol[] = [];

export function pushLayer(): symbol {
  const id = Symbol("layer");
  stack.push(id);
  return id;
}

export function popLayer(id: symbol): void {
  const i = stack.lastIndexOf(id);
  if (i >= 0) stack.splice(i, 1);
}

export function isTopLayer(id: symbol): boolean {
  return stack[stack.length - 1] === id;
}

/** Открыт ли сейчас диалог или боковая панель (горячие клавиши экрана под ними не работают). */
export function hasLayers(): boolean {
  return stack.length > 0;
}
