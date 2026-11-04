export function pct(value: unknown): string {
  const number = typeof value === "number" ? value : Number(value ?? 0);
  return `${(number * 100).toFixed(number > 0 && number < 0.01 ? 2 : 1)}%`;
}

export function usd(value: unknown): string {
  const number = typeof value === "number" ? value : Number(value ?? 0);
  return `$${number.toFixed(2)}`;
}

export function when(value: unknown): string {
  if (!value) return "—";
  const date = new Date(String(value));
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function titleCase(value: string): string {
  return value.replace(/_/g, " ").replace(/\b\w/g, (character) => character.toUpperCase());
}
