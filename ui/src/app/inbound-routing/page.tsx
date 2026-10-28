"use client";

import { Pencil, Plus, Trash2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";

import {
  deleteInboundRoutingGroupApiV1InboundRoutingGroupsGroupIdDelete,
  getWorkflowsSummaryApiV1WorkflowSummaryGet,
  listInboundRoutingGroupsApiV1InboundRoutingGroupsGet,
  updateInboundRoutingGroupApiV1InboundRoutingGroupsGroupIdPatch,
} from "@/client/sdk.gen";
import type { InboundRoutingGroupResponse } from "@/client/types.gen";
import {
  ConfirmDialog,
  type ConfirmRequest,
  RoutingGroupEditor,
} from "@/components/inbound-routing/RoutingGroupEditor";
import {
  describeSplit,
  type WorkflowOption,
} from "@/components/inbound-routing/routingUtils";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
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

// "new" = creating a group; a number = editing that group; null = list only.
type Selection = "new" | number | null;

export default function InboundRoutingPage() {
  const { user, getAccessToken, loading: authLoading } = useAuth();
  const [groups, setGroups] = useState<InboundRoutingGroupResponse[]>([]);
  const [workflows, setWorkflows] = useState<WorkflowOption[]>([]);
  const [loading, setLoading] = useState(true);
  const [selection, setSelection] = useState<Selection>(null);
  const [confirm, setConfirm] = useState<ConfirmRequest | null>(null);

  const headers = useCallback(async () => {
    const token = await getAccessToken();
    return { Authorization: `Bearer ${token}` };
  }, [getAccessToken]);

  const fetchGroups = useCallback(async () => {
    if (authLoading || !user) return;
    const res = await listInboundRoutingGroupsApiV1InboundRoutingGroupsGet({
      headers: await headers(),
    });
    if (res.error) {
      toast.error(detailFromError(res.error, "Failed to load routing groups"));
      return;
    }
    setGroups(res.data?.groups ?? []);
  }, [authLoading, user, headers]);

  useEffect(() => {
    if (authLoading || !user) return;
    (async () => {
      setLoading(true);
      const [, wf] = await Promise.all([
        fetchGroups(),
        getWorkflowsSummaryApiV1WorkflowSummaryGet({
          headers: await headers(),
          query: { status: "active" },
        }),
      ]);
      if (wf.error) toast.error(detailFromError(wf.error, "Failed to load agents"));
      setWorkflows(
        (wf.data ?? [])
          .map((w) => ({ id: w.id, name: w.name }))
          .sort((a, b) => a.id - b.id),
      );
      setLoading(false);
    })();
  }, [authLoading, user, fetchGroups, headers]);

  const toggleActive = (group: InboundRoutingGroupResponse, next: boolean) => {
    setConfirm({
      title: next ? "Activate routing group?" : "Deactivate routing group?",
      message: `This changes routing for ${group.number_count} number${group.number_count === 1 ? "" : "s"} starting with the next call.${next ? "" : " They go back to their own inbound workflow."}`,
      action: next ? "Activate" : "Deactivate",
      onConfirm: async () => {
        const res = await updateInboundRoutingGroupApiV1InboundRoutingGroupsGroupIdPatch({
          headers: await headers(),
          path: { group_id: group.id },
          body: { is_active: next },
        });
        if (res.error) {
          toast.error(detailFromError(res.error, "Failed to update routing group"));
          return;
        }
        await fetchGroups();
      },
    });
  };

  const deleteGroup = (group: InboundRoutingGroupResponse) => {
    setConfirm({
      title: `Delete "${group.name}"?`,
      message: `This changes routing for ${group.number_count} number${group.number_count === 1 ? "" : "s"} starting with the next call: they go back to their own inbound workflow.`,
      action: "Delete",
      onConfirm: async () => {
        const res = await deleteInboundRoutingGroupApiV1InboundRoutingGroupsGroupIdDelete({
          headers: await headers(),
          path: { group_id: group.id },
        });
        if (res.error) {
          toast.error(detailFromError(res.error, "Failed to delete routing group"));
          return;
        }
        if (selection === group.id) setSelection(null);
        await fetchGroups();
      },
    });
  };

  const selectedGroup =
    typeof selection === "number"
      ? (groups.find((g) => g.id === selection) ?? null)
      : null;

  return (
    <div className="flex justify-center py-12 px-4">
      <div className="w-full max-w-4xl space-y-6">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h1 className="text-2xl font-bold">Inbound Routing</h1>
            <p className="text-muted-foreground">
              Choose which agents answer inbound calls on a group of numbers, and
              split calls between them. Changes apply to the next call. Numbers not
              in a group keep using their own inbound workflow.
            </p>
          </div>
          <Button onClick={() => setSelection("new")}>
            <Plus className="h-4 w-4" />
            New group
          </Button>
        </div>

        <Card>
          <CardHeader>
            <CardTitle>Routing groups</CardTitle>
            <CardDescription>
              Turning a group off, or detaching its numbers, sends those numbers back
              to their own inbound workflow.
            </CardDescription>
          </CardHeader>
          <CardContent>
            {loading ? (
              <Skeleton className="h-24 w-full" />
            ) : groups.length === 0 ? (
              <p className="text-sm text-muted-foreground">No routing groups yet.</p>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Name</TableHead>
                    <TableHead>Active</TableHead>
                    <TableHead className="text-right">Numbers</TableHead>
                    <TableHead>Split</TableHead>
                    <TableHead className="w-24" />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {groups.map((g) => (
                    <TableRow key={g.id} data-state={selection === g.id ? "selected" : undefined}>
                      <TableCell className="font-medium">{g.name}</TableCell>
                      <TableCell>
                        <Switch
                          checked={g.is_active}
                          onCheckedChange={(next) => toggleActive(g, next)}
                        />
                      </TableCell>
                      <TableCell className="text-right">{g.number_count}</TableCell>
                      <TableCell>
                        {describeSplit(g)}
                        {g.sticky_by_caller && (
                          <span className="text-muted-foreground"> · sticky</span>
                        )}
                      </TableCell>
                      <TableCell>
                        <div className="flex justify-end gap-1">
                          <Button
                            variant="ghost"
                            size="icon"
                            aria-label="Edit"
                            onClick={() => setSelection(g.id)}
                          >
                            <Pencil className="h-4 w-4" />
                          </Button>
                          <Button
                            variant="ghost"
                            size="icon"
                            aria-label="Delete"
                            onClick={() => deleteGroup(g)}
                          >
                            <Trash2 className="h-4 w-4" />
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>

        {selection !== null && (selection === "new" || selectedGroup) && (
          <RoutingGroupEditor
            // Remount per group (and after each save) so the form resets.
            key={selection === "new" ? "new" : `${selectedGroup?.id}-${selectedGroup?.updated_at}`}
            group={selection === "new" ? null : selectedGroup}
            workflows={workflows}
            onSaved={async (saved) => {
              await fetchGroups();
              setSelection(saved.id);
            }}
            onNumbersChanged={fetchGroups}
            onClose={() => setSelection(null)}
          />
        )}

        <ConfirmDialog request={confirm} onClose={() => setConfirm(null)} />
      </div>
    </div>
  );
}
