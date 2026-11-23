import type { InboundRoutingGroupResponse, RoutingTarget } from "@/client/types.gen";

export type WorkflowOption = {
  id: number;
  name: string;
  /** The workspace the agent runs in (its calls are stored there). */
  organizationLabel?: string | null;
  /** True for an agent in a workspace this one has linked. */
  linked?: boolean;
};

export function workflowLabel(id: number, name?: string | null): string {
  return name ? `${id} ${name}` : `${id}`;
}

/** "Alex — Final Expense", or just the name when the workspace is unnamed. */
export function agentLabel(name?: string | null, organizationLabel?: string | null): string {
  const agent = name || "Unknown agent";
  return organizationLabel ? `${agent} — ${organizationLabel}` : agent;
}

/** "Alex — Final Expense 50% / Riley — Real Estate 25% / ..." */
export function describeSplit(group: InboundRoutingGroupResponse): string {
  const parts = group.targets
    .filter((t) => t.weight > 0)
    .map((t) =>
      t.agent_name
        ? `${agentLabel(t.agent_name, t.organization_label)} ${t.weight}%`
        : `${workflowLabel(t.workflow_id, t.workflow_name)} ${t.weight}%`,
    );
  const paused = group.targets.filter((t) => t.weight === 0).length;
  const text = parts.length ? parts.join(" / ") : "No agents";
  return paused ? `${text} (${paused} paused at 0%)` : text;
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
