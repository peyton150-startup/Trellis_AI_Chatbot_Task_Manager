export const NGROK_BYPASS_HEADERS = {
  "ngrok-skip-browser-warning": "1",
} satisfies Record<string, string>;

export function withNgrokBypassHeaders(headers?: HeadersInit): Headers {
  const merged = new Headers(headers);

  for (const [name, value] of Object.entries(NGROK_BYPASS_HEADERS)) {
    merged.set(name, value);
  }

  return merged;
}
