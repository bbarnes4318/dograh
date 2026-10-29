"use client";

import { Plus, RefreshCw, Trash2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import {
  attachNumbersToInboundRoutingGroupApiV1InboundRoutingGroupsGroupIdNumbersPost,
  createInboundRoutingGroupApiV1InboundRoutingGroupsPost,
  detachNumbersFromInboundRoutingGroupApiV1InboundRoutingGroupsGroupIdNumbersDetachPost,
  getInboundRoutingGroupStatsApiV1InboundRoutingGroupsGroupIdStatsGet,
  listInboundRoutingAuditApiV1InboundRoutingGroupsAuditGet,
  summarizeInboundRoutingGroupNumbersApiV1InboundRoutingGroupsGroupIdNumbersGet,
  updateInboundRoutingGroupApiV1InboundRoutingGroupsGroupIdPatch,
} from "@/client/sdk.gen";
import type {
  InboundRoutingAuditEntry,
  InboundRoutingGroupResponse,
  InboundRoutingStatsResponse,
  RoutingNumbersSummaryResponse,
  RoutingTarget,
} from "@/client/types.gen";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { detailFromError } from "@/lib/apiError";
import { useAuth } from "@/lib/auth";

import {
  evenWeights,
  totalWeight,
  workflowLabel,
  type WorkflowOption,
} from "./routingUtils";

export type ConfirmRequest = {
  title: string;
  message: string;
  action: string;
  onConfirm: () => Promise<void>;
};

interface RoutingGroupEditorProps {
  /** null = creating a new group. */
  group: InboundRoutingGroupResponse | null;
  workflows: WorkflowOption[];
  onSaved: (group: InboundRoutingGroupResponse) => void;
  /** Numbers were attached/detached (the list's counts are stale). */
  onNumbersChanged: () => void;
  onClose: () => void;
}

function useAuthHeaders() {
  const { getAccessToken } = useAuth();
  return useCallback(async () => {
    const token = await getAccessToken();
    return { Authorization: `Bearer ${token}` };
  }, [getAccessToken]);
}

export function ConfirmDialog({
  request,
  onClose,
}: {
  request: ConfirmRequest | null;
  onClose: () => void;
}) {
  const [busy, setBusy] = useState(false);
  return (
    <AlertDialog open={!!request} onOpenChange={(open) => !open && !busy && onClose()}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{request?.title}</AlertDialogTitle>
          <AlertDialogDescription>{request?.message}</AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={busy}>Cancel</AlertDialogCancel>
          <AlertDialogAction
            disabled={busy}
            onClick={async (e) => {
              e.preventDefault();
              if (!request) return;
              setBusy(true);
              try {
                await request.onConfirm();
              } finally {
                setBusy(false);
                onClose();
              }
            }}
          >
            {busy ? "Saving…" : request?.action}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

const ACTION_LABELS: Record<string, string> = {
  group_created: "Created group",
  group_updated: "Changed group",
  group_deleted: "Deleted group",
  numbers_attached: "Attached numbers",
  numbers_detached: "Detached numbers",
};

function formatWeights(weights: unknown): string {
  if (!weights || typeof weights !== "object") return "";
  return Object.entries(weights as Record<string, number>)
    .map(([id, w]) => `${id}: ${w}%`)
    .join(", ");
}

function describeAudit(entry: InboundRoutingAuditEntry): string {
  const d = entry.details as Record<string, unknown>;
  switch (entry.action) {
    case "group_created":
      return `Split ${formatWeights(d.weights)}`;
    case "numbers_attached":
    case "numbers_detached":
      return `${d.count} number(s)`;
    case "group_deleted":
      return `${d.numbers_released ?? 0} number(s) released`;
    case "group_updated":
      return Object.entries(d)
        .filter(([key]) => key !== "name")
        .map(([key, change]) => {
          const c = change as { from: unknown; to: unknown };
          if (key === "weights") {
            return `split ${formatWeights(c.from)} → ${formatWeights(c.to)}`;
          }
          return `${key.replace(/_/g, " ")} ${String(c.from)} → ${String(c.to)}`;
        })
        .join("; ");
    default:
      return "";
  }
}

export function RoutingGroupEditor({
  group,
  workflows,
  onSaved,
  onNumbersChanged,
  onClose,
}: RoutingGroupEditorProps) {
  const authHeaders = useAuthHeaders();
  const isNew = group === null;

  const [name, setName] = useState(group?.name ?? "");
  const [isActive, setIsActive] = useState(group?.is_active ?? true);
  const [sticky, setSticky] = useState(group?.sticky_by_caller ?? true);
  const [targets, setTargets] = useState<RoutingTarget[]>(
    group?.targets.map((t) => ({ workflow_id: t.workflow_id, weight: t.weight })) ?? [],
  );
  const [addWorkflowId, setAddWorkflowId] = useState<string>("");
  const [confirm, setConfirm] = useState<ConfirmRequest | null>(null);

  const [numbers, setNumbers] = useState<RoutingNumbersSummaryResponse | null>(null);
  const [selectedConfigs, setSelectedConfigs] = useState<number[]>([]);
  const [stats, setStats] = useState<InboundRoutingStatsResponse | null>(null);
  const [audit, setAudit] = useState<InboundRoutingAuditEntry[]>([]);

  // Saved targets may be archived agents that the picker no longer lists.
  const workflowNames = useMemo(
    () =>
      new Map<number, string | null | undefined>([
        ...(group?.targets ?? []).map(
          (t) => [t.workflow_id, t.workflow_name] as [number, string | null | undefined],
        ),
        ...workflows.map((w) => [w.id, w.name] as [number, string]),
      ]),
    [group, workflows],
  );
  const total = totalWeight(targets);
  const canSave = name.trim().length > 0 && targets.length > 0 && total === 100;
  const numberCount = numbers?.number_count ?? group?.number_count ?? 0;
  const available = workflows.filter(
    (w) => !targets.some((t) => t.workflow_id === w.id),
  );

  const loadDetails = useCallback(async () => {
    if (!group) return;
    const headers = await authHeaders();
    const path = { group_id: group.id };
    const [n, s, a] = await Promise.all([
      summarizeInboundRoutingGroupNumbersApiV1InboundRoutingGroupsGroupIdNumbersGet({
        headers,
        path,
      }),
      getInboundRoutingGroupStatsApiV1InboundRoutingGroupsGroupIdStatsGet({
        headers,
        path,
        query: { hours: 24 },
      }),
      listInboundRoutingAuditApiV1InboundRoutingGroupsAuditGet({
        headers,
        query: { group_id: group.id, limit: 50 },
      }),
    ]);
    if (n.error) toast.error(detailFromError(n.error, "Failed to load numbers"));
    else setNumbers(n.data ?? null);
    if (s.error) toast.error(detailFromError(s.error, "Failed to load stats"));
    else setStats(s.data ?? null);
    if (a.error) toast.error(detailFromError(a.error, "Failed to load audit log"));
    else setAudit(a.data?.entries ?? []);
  }, [group, authHeaders]);

  useEffect(() => {
    loadDetails();
  }, [loadDetails]);

  const setWeight = (workflowId: number, raw: string) => {
    const parsed = Math.round(Number(raw));
    const weight = Number.isFinite(parsed) ? Math.min(100, Math.max(0, parsed)) : 0;
    setTargets((prev) =>
      prev.map((t) => (t.workflow_id === workflowId ? { ...t, weight } : t)),
    );
  };

  const addAgent = (value: string) => {
    const id = Number(value);
    if (!id) return;
    setTargets((prev) => {
      // The first agent starts at 100%: "route everything to agent X".
      return [...prev, { workflow_id: id, weight: prev.length === 0 ? 100 : 0 }];
    });
    setAddWorkflowId("");
  };

  const evenSplit = () => {
    const weights = evenWeights(targets.length);
    setTargets((prev) => prev.map((t, i) => ({ ...t, weight: weights[i] })));
  };

  const save = async () => {
    const headers = await authHeaders();
    const body = {
      name: name.trim(),
      is_active: isActive,
      sticky_by_caller: sticky,
      targets: targets.map((t) => ({ workflow_id: t.workflow_id, weight: t.weight })),
    };
    const res = isNew
      ? await createInboundRoutingGroupApiV1InboundRoutingGroupsPost({ headers, body })
      : await updateInboundRoutingGroupApiV1InboundRoutingGroupsGroupIdPatch({
          headers,
          path: { group_id: group.id },
          body,
        });
    if (res.error || !res.data) {
      toast.error(detailFromError(res.error, "Failed to save routing group"));
      return;
    }
    toast.success(isNew ? "Routing group created" : "Routing saved — applies to the next call");
    onSaved(res.data);
  };

  const requestSave = () => {
    setConfirm({
      title: isNew ? "Create routing group?" : "Save routing changes?",
      message: isNew
        ? "No numbers are attached yet. After saving, attach numbers below; routing changes on their next call."
        : `This changes routing for ${numberCount} number${numberCount === 1 ? "" : "s"} starting with the next call.`,
      action: "Save",
      onConfirm: save,
    });
  };

  const changeNumbers = async (mode: "attach" | "detach" | "detach_all") => {
    if (!group) return;
    const headers = await authHeaders();
    const path = { group_id: group.id };
    const selection = { telephony_configuration_ids: selectedConfigs };
    const call = (dry_run: boolean) =>
      mode === "attach"
        ? attachNumbersToInboundRoutingGroupApiV1InboundRoutingGroupsGroupIdNumbersPost({
            headers,
            path,
            body: { ...selection, dry_run },
          })
        : detachNumbersFromInboundRoutingGroupApiV1InboundRoutingGroupsGroupIdNumbersDetachPost(
            {
              headers,
              path,
              body:
                mode === "detach_all"
                  ? { detach_all: true, dry_run }
                  : { ...selection, dry_run },
            },
          );

    const preview = await call(true);
    if (preview.error || !preview.data) {
      toast.error(detailFromError(preview.error, "Failed to count numbers"));
      return;
    }
    const n = preview.data.affected;
    if (n === 0) {
      toast.info(
        mode === "attach"
          ? "Those numbers are already in this group"
          : "No numbers from the selection are in this group",
      );
      return;
    }
    setConfirm({
      title: mode === "attach" ? "Attach numbers?" : "Detach numbers?",
      message:
        mode === "attach"
          ? `This changes routing for ${n} number${n === 1 ? "" : "s"} starting with the next call. Numbers in another routing group move to this one.`
          : `This changes routing for ${n} number${n === 1 ? "" : "s"} starting with the next call. They go back to their own inbound workflow.`,
      action: mode === "attach" ? "Attach" : "Detach",
      onConfirm: async () => {
        const res = await call(false);
        if (res.error || !res.data) {
          toast.error(detailFromError(res.error, "Failed to update numbers"));
          return;
        }
        toast.success(
          `${res.data.affected} number(s) ${mode === "attach" ? "attached" : "detached"}`,
        );
        setSelectedConfigs([]);
        await loadDetails();
        onNumbersChanged();
      },
    });
  };

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle>{isNew ? "New routing group" : `Edit "${group.name}"`}</CardTitle>
          <CardDescription>
            Pick which agents answer inbound calls on this group&apos;s numbers, and
            how calls are split between them.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          <div className="grid gap-2">
            <Label htmlFor="rg-name">Name</Label>
            <Input
              id="rg-name"
              value={name}
              placeholder="Campaign callbacks"
              onChange={(e) => setName(e.target.value)}
            />
          </div>

          <div className="flex items-start justify-between gap-4">
            <div>
              <Label htmlFor="rg-active">Active</Label>
              <p className="text-sm text-muted-foreground">
                When off, the numbers use their own inbound workflow.
              </p>
            </div>
            <Switch id="rg-active" checked={isActive} onCheckedChange={setIsActive} />
          </div>

          <div className="flex items-start justify-between gap-4">
            <div>
              <Label htmlFor="rg-sticky">Sticky by caller</Label>
              <p className="text-sm text-muted-foreground">
                The same person calling back reaches the same agent (as long as the
                split doesn&apos;t change). Off: each call is picked at random by weight.
              </p>
            </div>
            <Switch id="rg-sticky" checked={sticky} onCheckedChange={setSticky} />
          </div>

          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <Label>Agents</Label>
              <Button
                variant="outline"
                size="sm"
                onClick={evenSplit}
                disabled={targets.length < 2}
              >
                Even split
              </Button>
            </div>

            {targets.length === 0 && (
              <p className="text-sm text-muted-foreground">
                Add an agent. A single agent at 100% routes every call to it.
              </p>
            )}

            {targets.map((t) => (
              <div key={t.workflow_id} className="flex items-center gap-3">
                <div className="min-w-0 flex-1 truncate text-sm font-medium">
                  {workflowLabel(t.workflow_id, workflowNames.get(t.workflow_id))}
                </div>
                <input
                  type="range"
                  min={0}
                  max={100}
                  value={t.weight}
                  aria-label={`Weight for agent ${t.workflow_id}`}
                  className="w-32 accent-primary"
                  onChange={(e) => setWeight(t.workflow_id, e.target.value)}
                />
                <div className="flex items-center gap-1">
                  <Input
                    type="number"
                    min={0}
                    max={100}
                    step={1}
                    value={t.weight}
                    className="w-20"
                    onChange={(e) => setWeight(t.workflow_id, e.target.value)}
                  />
                  <span className="text-sm text-muted-foreground">%</span>
                </div>
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label="Remove agent"
                  onClick={() =>
                    setTargets((prev) =>
                      prev.filter((x) => x.workflow_id !== t.workflow_id),
                    )
                  }
                >
                  <Trash2 className="h-4 w-4" />
                </Button>
              </div>
            ))}

            <div className="flex items-center gap-3">
              <Select value={addWorkflowId} onValueChange={addAgent}>
                <SelectTrigger className="flex-1">
                  <SelectValue placeholder="Add agent…" />
                </SelectTrigger>
                <SelectContent>
                  {available.map((w) => (
                    <SelectItem key={w.id} value={String(w.id)}>
                      {workflowLabel(w.id, w.name)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Plus className="h-4 w-4 text-muted-foreground" />
            </div>

            <div
              className={`text-sm font-medium ${total === 100 ? "text-green-600" : "text-destructive"}`}
            >
              Total: {total}%{total !== 100 && " — must equal 100% to save"}
            </div>
          </div>

          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={onClose}>
              Close
            </Button>
            <Button onClick={requestSave} disabled={!canSave}>
              Save
            </Button>
          </div>
        </CardContent>
      </Card>

      {!isNew && (
        <Card>
          <CardHeader>
            <CardTitle>Numbers</CardTitle>
            <CardDescription>
              {numberCount} number{numberCount === 1 ? "" : "s"} attached. Attach or
              detach every number on a telephony configuration.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-8" />
                  <TableHead>Telephony configuration</TableHead>
                  <TableHead className="text-right">Numbers</TableHead>
                  <TableHead className="text-right">In this group</TableHead>
                  <TableHead className="text-right">In another group</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {(numbers?.configurations ?? []).map((c) => (
                  <TableRow key={c.telephony_configuration_id}>
                    <TableCell>
                      <Checkbox
                        checked={selectedConfigs.includes(c.telephony_configuration_id)}
                        onCheckedChange={(checked) =>
                          setSelectedConfigs((prev) =>
                            checked
                              ? [...prev, c.telephony_configuration_id]
                              : prev.filter((id) => id !== c.telephony_configuration_id),
                          )
                        }
                      />
                    </TableCell>
                    <TableCell>
                      {c.telephony_configuration_id} · {c.name}{" "}
                      <span className="text-muted-foreground">({c.provider})</span>
                    </TableCell>
                    <TableCell className="text-right">{c.total_numbers}</TableCell>
                    <TableCell className="text-right">{c.in_this_group}</TableCell>
                    <TableCell className="text-right">{c.in_other_group}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            <div className="flex flex-wrap justify-end gap-2">
              <Button
                variant="outline"
                disabled={numberCount === 0}
                onClick={() => changeNumbers("detach_all")}
              >
                Detach all
              </Button>
              <Button
                variant="outline"
                disabled={selectedConfigs.length === 0}
                onClick={() => changeNumbers("detach")}
              >
                Detach selected
              </Button>
              <Button
                disabled={selectedConfigs.length === 0}
                onClick={() => changeNumbers("attach")}
              >
                Attach selected
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {!isNew && (
        <Card>
          <CardHeader className="flex flex-row items-start justify-between">
            <div className="space-y-1.5">
              <CardTitle>Last 24 hours</CardTitle>
              <CardDescription>
                {stats?.total_calls ?? 0} inbound call(s) routed through this group:
                actual split vs target.
              </CardDescription>
            </div>
            <Button variant="ghost" size="icon" aria-label="Refresh" onClick={loadDetails}>
              <RefreshCw className="h-4 w-4" />
            </Button>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Agent</TableHead>
                  <TableHead className="text-right">Calls</TableHead>
                  <TableHead className="text-right">Actual</TableHead>
                  <TableHead className="text-right">Target</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {(stats?.targets ?? []).map((row) => (
                  <TableRow key={row.workflow_id}>
                    <TableCell>
                      {workflowLabel(row.workflow_id, row.workflow_name)}
                      {!row.is_target && (
                        <span className="text-muted-foreground"> (fallback / removed)</span>
                      )}
                    </TableCell>
                    <TableCell className="text-right">{row.calls}</TableCell>
                    <TableCell className="text-right">{row.actual_percent}%</TableCell>
                    <TableCell className="text-right">{row.target_percent}%</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}

      {!isNew && (
        <Card>
          <CardHeader>
            <CardTitle>Change history</CardTitle>
            <CardDescription>Who changed this group, and when.</CardDescription>
          </CardHeader>
          <CardContent>
            {audit.length === 0 ? (
              <p className="text-sm text-muted-foreground">No changes yet.</p>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>When</TableHead>
                    <TableHead>Who</TableHead>
                    <TableHead>What</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {audit.map((entry) => (
                    <TableRow key={entry.id}>
                      <TableCell className="whitespace-nowrap">
                        {new Date(entry.created_at).toLocaleString()}
                      </TableCell>
                      <TableCell>
                        {entry.user_email ?? (entry.user_id ? `User ${entry.user_id}` : "—")}
                      </TableCell>
                      <TableCell>
                        <span className="font-medium">
                          {ACTION_LABELS[entry.action] ?? entry.action}
                        </span>{" "}
                        <span className="text-muted-foreground">{describeAudit(entry)}</span>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>
      )}

      <ConfirmDialog request={confirm} onClose={() => setConfirm(null)} />
    </div>
  );
}
