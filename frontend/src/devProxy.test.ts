import { once } from "node:events";
import { mkdtemp, rm } from "node:fs/promises";
import http from "node:http";
import type { AddressInfo } from "node:net";
import os from "node:os";
import path from "node:path";
import { createServer } from "vite";

const configFile = path.resolve(__dirname, "../vite.config.ts");

// Sends one request through the real dashboard dev server to a backend that
// reports the headers it received.
async function proxiedHeaders(
  apiKey: string | undefined,
  host: (devPort: number) => string,
): Promise<{ headers: http.IncomingHttpHeaders; backendPort: number }> {
  const backend = http.createServer((req, res) => {
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify(req.headers));
  });
  backend.listen(0, "localhost");
  await once(backend, "listening");
  const backendPort = (backend.address() as AddressInfo).port;
  const cacheDir = await mkdtemp(path.join(os.tmpdir(), "dev-proxy-"));
  vi.stubEnv("MINDROOM_PORT", String(backendPort));
  vi.stubEnv("MINDROOM_API_KEY", apiKey ?? "");
  const vite = await createServer({
    configFile,
    cacheDir,
    logLevel: "silent",
    optimizeDeps: { noDiscovery: true, include: [] },
    server: { host: "127.0.0.1", port: 0, watch: null },
  });
  vi.unstubAllEnvs();
  try {
    await vite.listen();
    const devPort = (vite.httpServer?.address() as AddressInfo).port;
    const body = await new Promise<string>((resolve, reject) => {
      http
        .get(
          {
            host: "127.0.0.1",
            port: devPort,
            path: "/api/health",
            headers: { Host: host(devPort) },
          },
          (res) => {
            let data = "";
            res.on("data", (chunk) => (data += chunk));
            res.on("end", () => resolve(data));
          },
        )
        .on("error", reject);
    });
    return { headers: JSON.parse(body), backendPort };
  } finally {
    await vite.close();
    backend.close();
    await rm(cacheDir, { recursive: true, force: true });
  }
}

describe("dashboard dev proxy", () => {
  it.each([undefined, "dev-proxy-key"])(
    "forwards a rebinding page's Host to the backend unchanged (key: %s)",
    async (apiKey) => {
      const { headers } = await proxiedHeaders(
        apiKey,
        (devPort) => `evil.local:${devPort}`,
      );
      expect(headers.host).toMatch(/^evil\.local:\d+$/);
      expect(headers.authorization).toBeUndefined();
    },
  );

  it("addresses the backend as its own client when it attaches the key", async () => {
    const { headers, backendPort } = await proxiedHeaders(
      "dev-proxy-key",
      (devPort) => `localhost:${devPort}`,
    );
    expect(headers.host).toBe(`localhost:${backendPort}`);
    expect(headers.authorization).toBe("Bearer dev-proxy-key");
  });
});
