// Node-safe, no side effects. DNS-rebinding guard: the server only answers requests whose Host
// header names the loopback address it is bound to (127.0.0.1, localhost or [::1]) on its port.
export function isAllowedHost(host: string | undefined, port: number): boolean {
  if (typeof host !== "string") return false;
  const h = host.toLowerCase();
  return h === `127.0.0.1:${port}` || h === `localhost:${port}` || h === `[::1]:${port}`;
}
