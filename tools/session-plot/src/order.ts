// Card order for /live. A session keeps the position it had in the previous snapshot, so an update
// does not reshuffle the cards. A session not seen before goes on top, most recent first (ties by id).
// Sessions that are gone are dropped.
export function stableOrder(prev: readonly string[], sessions: readonly { id: string; lastActivity: number }[]): string[] {
  const present = new Set(sessions.map((s) => s.id));
  const kept = [...new Set(prev)].filter((id) => present.has(id));
  const keptSet = new Set(kept);
  const fresh = sessions
    .filter((s) => !keptSet.has(s.id))
    .sort((a, b) => b.lastActivity - a.lastActivity || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0))
    .map((s) => s.id);
  return [...new Set(fresh), ...kept];
}
