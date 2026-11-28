# session-plot

Plots a Claude Code session over time: the main session's context size, plus every
subagent (fork) and its own context size, and the API-equivalent cost of each. Reads the
JSONL transcripts under `~/.claude/projects/<projectDir>/` directly. Browser UI is
hand-built SVG with no external libraries.

## Run the dev server

```sh
npm install
npm run dev
```

Then open http://127.0.0.1:4395/?session=<sessionId>. Without `?session=` the first
session in the list is shown, and the picker switches between sessions.

## Live monitor

http://127.0.0.1:4395/live shows usage as it happens:

- Totals: $ today, $ last hour, output tokens in the last hour, and out/$ today and last hour (output tokens per dollar).
- "Cost per minute (last 60 min)": stacked by model, hover or tap a bar for its breakdown.
- One card per session active in the last 24 h, most recent first. Sessions active in the
  last 2 minutes are marked live. Each card shows the main model, a context gauge
  (filled in the main model's colour; the ctx number turns amber above 150k, red above 185k,
  of 200k), $ today, $ last hour and out/$ today, and the active subagents. Cards link to
  the session plot with live refresh. Cards keep their order across updates: new sessions
  appear on top, and a reload resets to most-recent-first.

The session plot is live at `/?session=<id>&live=1`. It refetches the session (at most every
3 s) when the server reports that one of its transcripts changed.

Endpoints: `GET /api/live` (snapshot JSON), `GET /api/live/stream` (Server-Sent Events:
`snapshot` after each debounced change, `changed` with the session ids that changed,
`: ping` every 15 s).

The live index reads each transcript from the 24 h window once at startup, then only the
bytes appended since the last read. It watches `~/.claude/projects` with `fs.watch`
(recursive) and falls back to a 2 s poll if that is unavailable. Snapshots are recomputed
at most once per second.

## Cost

Each assistant turn is priced at the API list price for its model, using the rates in
`pricing.json` at the repo root. Cache writes are priced at 1.25x input (5 min) or 2x
input (1 h), cache reads at the model's cache-read rate, and fast-mode turns use the fast
rates. In a fast-mode turn the cache read and write multipliers apply to the fast rates:
cache reads cost the fast input rate times the model's cache-read/input ratio, and cache
writes cost 1.25x (5 min) or 2x (1 h) the fast input rate. Haiku uses its long-prompt rates
above 100k prompt tokens.

**This is the API-equivalent list price, not subscription billing.** A Claude subscription
does not charge per token, so the numbers show what the same usage would cost through the
API.

Models with no entry in `pricing.json` are counted as $0 and listed as `unpricedModels`
(shown in the status line). Add an entry to `pricing.json` to price them. Edit the file and
reload; the dev server picks up the new rates on restart. Synthetic placeholder messages
(`<synthetic>`) cost nothing.

The session view shows the cost per series (table column, tooltip), the session total in
the status line, and a "Cumulative cost" panel with lines for main, all subagents, and the
total. The table, tooltip and status line also show output tokens per API-list-price dollar
(Out/$, in thousands, e.g. 78.9k); it is dominated by context re-reads (cache reads/writes), not output price, so it
mostly reflects context size and model mix.

## Export one session as a standalone HTML file

```sh
npm run export -- <sessionId> [outFile]
```

The default output is `out/<first 8 chars of id>.html`. The file has the session data
and client code inlined and makes no network requests when opened. The live monitor is
not included.

## Other scripts

- `npm run typecheck`: `tsc --noEmit` over `src/`, `server.ts`, `export.ts`, `scripts/`, `test/`.
- `npm run check -- <sessionId>`: prints summary numbers for the given session, cost
  totals per model, and a hand-computed cost for one turn.
- `npm test`: unit tests (`node:test`) for cost, live index, transcript assembly and the
  Host check. They use temp directories and make no changes under `~/.claude`.

The server listens on `127.0.0.1` at port 4395, or at `PORT` when set. It answers only
requests whose `Host` header is `127.0.0.1:<port>`, `localhost:<port>` or `[::1]:<port>`
(DNS-rebinding guard); other hosts get 403.

## Layout

- `src/parse.ts`: transcript parsing (Node only). Dedupes assistant turns by `message.id`,
  finds subagent spawn times via `tool_use` ids, merges compaction markers within 120 s,
  and prices each turn.
- `src/cost.ts`: turn pricing from `pricing.json` (Node and browser).
- `pricing.json`: USD per million tokens per model, cache multipliers, source note.
- `src/live.ts`: live index over the last 24 h of transcripts (Node only).
- `src/types.ts`: shared types.
- `src/main.ts`: browser client for the session view (context chart, forks, a Branches graph
  where main is the trunk and each subagent forks off and merges back, cumulative cost, table,
  hover, live refresh).
- `src/branches.ts`: pure lane layout for the Branches panel (`branchLanes`).
- `src/livepage.ts`: browser client for `/live`.
- `src/bundle.ts`: esbuild bundling of the browser clients.
- `server.ts`: HTTP server on port 4395 (`/api/sessions`, `/api/session/:id`,
  `/api/live`, `/api/live/stream`, `/`, `/main.js`, `/live`, `/live.js`).
- `export.ts`: single-file HTML export.
- `index.html`: session page shell and CSS (shared by the dev server and the export).
- `live.html`: live page shell and CSS.
