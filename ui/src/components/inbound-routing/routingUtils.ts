import type { InboundRoutingGroupResponse, RoutingTarget } from "@/client/types.gen";

export type WorkflowOption = { id: number; name: string };

export function workflowLabel(id: number, name?: string | null): string {
  return name ? `${id} ${name}` : `${id}`;
}

/** "15 Alex 50% / 25 Robert 50%" */
export function describeSplit(group: InboundRoutingGroupResponse): string {
  const parts = group.targets
    .filter((t) => t.weight > 0)
    .map((t) => `${workflowLabel(t.workflow_id, t.workflow_name)} ${t.weight}%`);
  return parts.length ? parts.join(" / ") : "No agents";
}

/** Spread 100 across ``count`` agents; the remainder goes to the first ones. */
export function evenWeights(count: number): number[] {
  if (count <= 0) return [];
  const base = Math.floor(100 / count);
  const remainder = 100 - base * count;
  return Array.from({ length: count }, (_, i) => base + (i < remainder ? 1 : 0));
}

export function totalWeight(targets: RoutingTarget[]): number {
  return targets.reduce((sum, t) => sum + (t.weight > 0 ? t.weight : 0), 0);
}
