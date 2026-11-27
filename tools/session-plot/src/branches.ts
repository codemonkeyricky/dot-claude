// Lane layout for the session view's Branches panel: the main session is the trunk (lane 0),
// and each subagent is a branch that forks off the trunk and merges back. Pure: no DOM.
import type { Series } from "./types";

export interface Lane {
  id: string;
  lane: number; // 0 is the trunk; positive lanes are drawn above it, negative below
  fork: number; // epoch ms: spawn time, or start when there is no spawn
  merge: number; // epoch ms: end time, never before fork
}

// Lays the subagents out as git-style branches. Subs are processed by fork time (ties by id), and
// each takes the free lane nearest the trunk, trying +1, -1, +2, -2, ... A lane is free when every
// branch already on it has merged at least `gap` ms before this fork, so branches never touch.
// Result is in processing order (fork time, then id).
export function branchLanes(subs: Series[], gap = 0): Lane[] {
  const items = subs.map((s) => {
    const fork = s.spawn ?? s.start;
    const end = Number.isFinite(s.end) ? s.end : fork;
    return { id: s.id, fork, merge: Math.max(end, fork) };
  });
  items.sort((a, b) => a.fork - b.fork || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));

  // Latest merge time on each lane. A lane with no entry has never been used.
  const laneMerge = new Map<number, number>();
  const out: Lane[] = [];
  for (const it of items) {
    let lane = 0;
    for (let k = 1; lane === 0; k++) {
      for (const cand of [k, -k]) {
        const last = laneMerge.get(cand);
        if (last === undefined || last + gap < it.fork) {
          lane = cand;
          break;
        }
      }
    }
    laneMerge.set(lane, Math.max(laneMerge.get(lane) ?? -Infinity, it.merge));
    out.push({ id: it.id, lane, fork: it.fork, merge: it.merge });
  }
  return out;
}
