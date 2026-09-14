import type { OutgoingHttpHeader, OutgoingHttpHeaders } from "node:http";
import path from "node:path";
import { searchForWorkspaceRoot, type Plugin, type ViteDevServer } from "vite";

function configureSvgzHeaders(server: Pick<ViteDevServer, "middlewares">) {
  server.middlewares.use((request, response, next) => {
    const url = new URL(request.url ?? "/", "http://localhost");
    if (url.pathname.endsWith(".svgz")) {
      // Prevent Vite preview from compressing an already compressed asset again.
      response.setHeader("Content-Encoding", "gzip");
      const writeHead = response.writeHead.bind(response);
      response.writeHead = (
        statusCode: number,
        statusMessage?: string | OutgoingHttpHeaders | OutgoingHttpHeader[],
        headers?: OutgoingHttpHeaders | OutgoingHttpHeader[],
      ) => {
        const outgoingHeaders =
          typeof statusMessage === "string" ? headers : statusMessage;
        const contentType =
          response.getHeader("Content-Type") ??
          (outgoingHeaders && !Array.isArray(outgoingHeaders)
            ? (outgoingHeaders["Content-Type"] ??
              outgoingHeaders["content-type"])
            : undefined);
        if (statusCode < 400 && contentType === "image/svg+xml") {
          response.setHeader("Content-Encoding", "gzip");
        } else {
          response.removeHeader("Content-Encoding");
        }
        return typeof statusMessage === "string"
          ? writeHead(statusCode, statusMessage, headers)
          : writeHead(statusCode, statusMessage);
      };
    }
    next();
  });
}

export const svgzPlugin: Plugin = {
  name: "svgz-headers",
  config: (config) => ({
    server: {
      fs: {
        allow: [
          searchForWorkspaceRoot(config.root ?? __dirname),
          path.resolve(__dirname, "../assets/logo"),
        ],
      },
    },
  }),
  configureServer: configureSvgzHeaders,
  configurePreviewServer: configureSvgzHeaders,
};
