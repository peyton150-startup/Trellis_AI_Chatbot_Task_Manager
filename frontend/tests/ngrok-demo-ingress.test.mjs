import assert from "node:assert/strict";
import test from "node:test";

let ingress;
try {
  ingress = await import("../lib/ngrokDemoIngress.ts");
} catch {
  ingress = null;
}

test("NGROK_BYPASS_HEADERS bypasses the free-plan browser warning", () => {
  assert.ok(ingress, "the ngrok demo ingress module must exist");

  const request = new Request("https://example.test/api/tasks", {
    headers: ingress.NGROK_BYPASS_HEADERS,
  });

  assert.equal(request.headers.get("ngrok-skip-browser-warning"), "1");
});

test("withNgrokBypassHeaders preserves caller headers and enforces the bypass", () => {
  assert.equal(
    typeof ingress.withNgrokBypassHeaders,
    "function",
    "the ngrok header merger must exist",
  );

  const headers = ingress.withNgrokBypassHeaders({
    accept: "application/json",
    "ngrok-skip-browser-warning": "",
  });

  assert.equal(headers.get("accept"), "application/json");
  assert.equal(headers.get("ngrok-skip-browser-warning"), "1");
});
