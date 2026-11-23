"use client";

import { RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { listSharedNumbersApiV1InboundRoutingGroupsSharedNumbersGet } from "@/client/sdk.gen";
import type { SharedNumbersResponse } from "@/client/types.gen";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
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

const MAX_ROWS = 200;

/**
 * The one inventory of inbound numbers as this workspace sees it.
 *
 * - Numbers it owns: the routing group that controls each, and the linked
 *   workspaces that get a share of its calls. Changed from the groups above.
 * - Numbers other workspaces share with it: read-only. Only the owner changes
 *   their routing; this workspace sees its own share and its own agents.
 */
export function SharedNumbersPanel({ refreshKey = 0 }: { refreshKey?: number }) {
  const { user, getAccessToken, loading: authLoading } = useAuth();
  const [data, setData] = useState<SharedNumbersResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState("");
  const [sharedOnly, setSharedOnly] = useState(true);

  const load = useCallback(async () => {
    if (authLoading || !user) return;
    setLoading(true);
    const token = await getAccessToken();
    const res = await listSharedNumbersApiV1InboundRoutingGroupsSharedNumbersGet({
      headers: { Authorization: `Bearer ${token}` },
    });
    if (res.error) {
      toast.error(detailFromError(res.error, "Failed to load shared numbers"));
    } else {
      setData(res.data ?? null);
    }
    setLoading(false);
  }, [authLoading, user, getAccessToken]);

  useEffect(() => {
    load();
  }, [load, refreshKey]);

  const owned = useMemo(() => {
    const q = filter.replace(/\D/g, "");
    return (data?.owned ?? []).filter(
      (n) =>
        (!sharedOnly || (n.shared_with ?? []).length > 0 || n.inbound_routing_group_id) &&
        (!q || n.address.replace(/\D/g, "").includes(q)),
    );
  }, [data, filter, sharedOnly]);
  const sharedWithMe = data?.shared_with_me ?? [];
  const ownName =
    !data?.organization_label || data.organization_label === "This workspace"
      ? "this workspace"
      : data.organization_label;

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between">
        <div className="space-y-1.5">
          <CardTitle>Shared numbers</CardTitle>
          <CardDescription>
            The same real phone numbers in every workspace that uses them. Each
            number has one owner, and only the owner&apos;s routing group decides
            where its calls go. Sharing a number for inbound calls does not let
            anyone use it as an outbound caller ID.
          </CardDescription>
        </div>
        <Button variant="ghost" size="icon" aria-label="Refresh" onClick={load}>
          <RefreshCw className="h-4 w-4" />
        </Button>
      </CardHeader>
      <CardContent className="space-y-6">
        {loading && !data ? (
          <Skeleton className="h-24 w-full" />
        ) : (
          <>
            {sharedWithMe.length > 0 && (
              <div className="space-y-2">
                <h3 className="text-sm font-semibold">
                  Shared with {ownName} (read-only)
                </h3>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Number</TableHead>
                      <TableHead>Owner</TableHead>
                      <TableHead>Routing group</TableHead>
                      <TableHead>Your agents</TableHead>
                      <TableHead className="text-right">Your share</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {sharedWithMe.map((n) => (
                      <TableRow key={`${n.owner_organization_id}-${n.address}`}>
                        <TableCell className="font-mono">{n.address}</TableCell>
                        <TableCell>{n.owner_label}</TableCell>
                        <TableCell>
                          {n.routing_group_name}
                          {!n.routing_group_active && (
                            <span className="text-muted-foreground"> (off)</span>
                          )}
                        </TableCell>
                        <TableCell>
                          {n.my_agents
                            .map((a) => `${a.name} ${a.share_percent}%`)
                            .join(", ")}
                        </TableCell>
                        <TableCell className="text-right">
                          {n.my_share_percent}%
                          <span className="text-muted-foreground">
                            {" "}
                            · others {n.other_share_percent}%
                          </span>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
                <p className="text-xs text-muted-foreground">
                  Calls your agents take are yours: they run in this workspace, with its
                  own settings, and their recordings and transcripts are stored here.
                  To change the split, ask the owner.
                </p>
              </div>
            )}

            {(data?.owned.length ?? 0) > 0 && (
              <div className="space-y-2">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <h3 className="text-sm font-semibold">
                    Owned by {ownName}
                  </h3>
                  <div className="flex items-center gap-3">
                    <label className="flex items-center gap-2 text-sm text-muted-foreground">
                      <input
                        type="checkbox"
                        checked={sharedOnly}
                        onChange={(e) => setSharedOnly(e.target.checked)}
                      />
                      Only numbers in a routing group
                    </label>
                    <Input
                      className="w-44"
                      placeholder="Search number"
                      value={filter}
                      onChange={(e) => setFilter(e.target.value)}
                    />
                  </div>
                </div>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Number</TableHead>
                      <TableHead>Controlled by</TableHead>
                      <TableHead>Shared with</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {owned.slice(0, MAX_ROWS).map((n) => (
                      <TableRow key={n.phone_number_id}>
                        <TableCell className="font-mono">
                          {n.address}
                          {!n.is_active && (
                            <span className="text-muted-foreground"> (inactive)</span>
                          )}
                        </TableCell>
                        <TableCell>
                          {n.routing_group_name ? (
                            <>
                              Group: {n.routing_group_name}
                              {n.routing_group_active === false && (
                                <span className="text-muted-foreground">
                                  {" "}
                                  (off — uses its own agent)
                                </span>
                              )}
                            </>
                          ) : n.inbound_workflow_name ? (
                            <>Agent: {n.inbound_workflow_name}</>
                          ) : (
                            <span className="text-muted-foreground">Not answered by AI</span>
                          )}
                        </TableCell>
                        <TableCell>
                          {(n.shared_with ?? []).length === 0 ? (
                            <span className="text-muted-foreground">Not shared</span>
                          ) : (
                            (n.shared_with ?? [])
                              .map((s) => `${s.label} ${s.share_percent}%`)
                              .join(", ")
                          )}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
                {owned.length > MAX_ROWS && (
                  <p className="text-xs text-muted-foreground">
                    Showing {MAX_ROWS} of {owned.length}. Search to narrow it down.
                  </p>
                )}
              </div>
            )}

            {sharedWithMe.length === 0 && (data?.owned.length ?? 0) === 0 && (
              <p className="text-sm text-muted-foreground">
                No phone numbers yet, and none shared with this workspace.
              </p>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}
