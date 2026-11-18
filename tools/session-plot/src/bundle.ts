// Node-only: bundles browser entry points (src/main.ts, src/livepage.ts) with esbuild.
import { build } from "esbuild";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = dirname(dirname(fileURLToPath(import.meta.url)));

export async function bundleFile(entry: string, minify: boolean): Promise<string> {
  const result = await build({
    entryPoints: [join(projectRoot, entry)],
    bundle: true,
    write: false,
    format: "esm",
    platform: "browser",
    target: "es2022",
    minify,
  });
  const out = result.outputFiles?.[0];
  if (!out) throw new Error("esbuild produced no output");
  return out.text;
}

export function bundleClient(minify: boolean): Promise<string> {
  return bundleFile(join("src", "main.ts"), minify);
}

export function bundleLive(minify: boolean): Promise<string> {
  return bundleFile(join("src", "livepage.ts"), minify);
}
