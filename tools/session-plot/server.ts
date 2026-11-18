import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { listSessions, parseSession, SessionNotFoundError } from "./src/parse";
import { bundleClient, bundleLive } from "./src/bundle";
import { LiveIndex, type LiveUpdate } from "./src/live";

const here = dirname(fileURLToPath(import.meta.url));
const HOST = "127.0.0.1";
const PORT = 4395;

// One index for the process; it watches ~/.claude/projects and keeps itself current.
const live = new LiveIndex();
console.log(
  `live index: first snapshot in ${live.firstSnapshotMs.toFixed(0)} ms ` +
    `(${live.sessionCount()} sessions, ${live.fileCount()} transcript files, last 24 h)`,
);

function send(res: ServerResponse, status: number, body: string, type: string): void {
  res.writeHead(status, { "content-type": type, "cache-control": "no-store" });
  res.end(body);
}

function sendJson(res: ServerResponse, status: number, value: unknown): void {
  send(res, status, JSON.stringify(value), "application/json; charset=utf-8");
}

let liveJs: Promise<string> | null = null;

function streamLive(req: IncomingMessage, res: ServerResponse): void {
  res.writeHead(200, {
    "content-type": "text/event-stream; charset=utf-8",
    "cache-control": "no-cache",
    connection: "keep-alive",
    "x-accel-buffering": "no",
  });
  const sendSnapshot = (json: string): void => {
    res.write(`event: snapshot\ndata: ${json}\n\n`);
  };
  sendSnapshot(JSON.stringify(live.snapshot()));

  const onUpdate = (u: LiveUpdate): void => {
    sendSnapshot(JSON.stringify(u.snapshot));
    if (u.changed.length > 0) {
      res.write(`event: changed\ndata: ${JSON.stringify({ sessionIds: u.changed })}\n\n`);
    }
  };
  live.on("update", onUpdate);
  const ping = setInterval(() => res.write(": ping\n\n"), 15_000);
  // res 'close' fires when the client disconnects (req 'close' fires early for GET requests).
  res.on("close", () => {
    clearInterval(ping);
    live.off("update", onUpdate);
  });
  res.flushHeaders();
}

const server = createServer(async (req: IncomingMessage, res: ServerResponse) => {
  try {
    if (req.method !== "GET" && req.method !== "HEAD") {
      return send(res, 405, "method not allowed", "text/plain; charset=utf-8");
    }
    const url = new URL(req.url ?? "/", `http://${HOST}:${PORT}`);
    const path = url.pathname;

    if (path === "/api/sessions") {
      return sendJson(res, 200, listSessions());
    }

    const m = /^\/api\/session\/([^/]+)$/.exec(path);
    if (m) {
      try {
        return sendJson(res, 200, parseSession(decodeURIComponent(m[1])));
      } catch (err) {
        if (err instanceof SessionNotFoundError) return sendJson(res, 404, { error: "session not found" });
        throw err;
      }
    }

    if (path === "/api/live") {
      return sendJson(res, 200, live.snapshot());
    }

    if (path === "/api/live/stream") {
      return streamLive(req, res);
    }

    if (path === "/" || path === "/index.html") {
      return send(res, 200, readFileSync(join(here, "index.html"), "utf8"), "text/html; charset=utf-8");
    }

    if (path === "/live" || path === "/live.html") {
      return send(res, 200, readFileSync(join(here, "live.html"), "utf8"), "text/html; charset=utf-8");
    }

    if (path === "/main.js") {
      const js = await bundleClient(false);
      return send(res, 200, js, "text/javascript; charset=utf-8");
    }

    if (path === "/live.js") {
      liveJs ??= bundleLive(false);
      return send(res, 200, await liveJs, "text/javascript; charset=utf-8");
    }

    return send(res, 404, "not found", "text/plain; charset=utf-8");
  } catch (err) {
    console.error(err);
    return send(res, 500, String(err), "text/plain; charset=utf-8");
  }
});

server.listen(PORT, HOST, () => {
  console.log(`session-plot listening on http://${HOST}:${PORT}`);
});
