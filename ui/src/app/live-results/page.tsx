'use client';

import { format } from 'date-fns';
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Clock,
  Download,
  Lightbulb,
  PhoneCall,
  PhoneForwarded,
  RefreshCw,
  Search,
  Target,
  Users,
  Voicemail,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { MediaPreviewButton, MediaPreviewDialog } from '@/components/MediaPreviewDialog';

import { client } from '@/client/client.gen';
import { getWorkflowOptionsApiV1OrganizationsReportsWorkflowsGet } from '@/client/sdk.gen';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { cn } from '@/lib/utils';

/* ------------------------------------------------------------------ types */

interface FunnelStage {
  stage: string;
  count: number;
}

interface InsightItem {
  severity: 'critical' | 'warn' | 'info' | 'good';
  title: string;
  detail: string;
  count: number;
}

interface CallRow {
  run_id: number;
  time: string;
  phone: string | null;
  duration: number;
  category: string;
  result: string;
  reason: string | null;
  recording_url: string | null;
  transcript_url: string | null;
  transcript?: string | null;
  transfer?: boolean;
}

interface InsightsPayload {
  date: string;
  timezone: string;
  generated_at: string;
  status: {
    active_calls: number;
    total_dials: number;
    last_call_at: string | null;
  };
  funnel: FunnelStage[];
  kpis: {
    dials: number;
    answered: number;
    voicemails: number;
    humans: number;
    offers: number;
    transfers: number;
    transfers_answered: number;
    talk_minutes: number;
    human_rate: number;
    offer_rate: number;
    transfer_rate: number;
  };
  outcomes: Array<{ result: string; count: number }>;
  reasons: Array<{ reason: string; count: number }>;
  insights: InsightItem[];
  calls: CallRow[];
}

interface WorkflowOption {
  id: number;
  name: string;
}

/* ------------------------------------------------------------- helpers */

const REFRESH_SECONDS = 15;

function fmtDuration(sec: number): string {
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return m > 0 ? `${m}m ${String(s).padStart(2, '0')}s` : `${s}s`;
}

function pct(part: number, whole: number): string {
  if (!whole) return '0%';
  return `${Math.round((part / whole) * 100)}%`;
}

const RESULT_BADGE: Record<string, string> = {
  'Transferred - buyer answered':
    'bg-emerald-500/15 text-emerald-700 dark:text-emerald-400 border-emerald-500/30',
  'Transferred - buyer no-answer':
    'bg-amber-500/15 text-amber-700 dark:text-amber-400 border-amber-500/30',
  'Offer made - declined':
    'bg-blue-500/15 text-blue-700 dark:text-blue-400 border-blue-500/30',
  'Qualified (pre-offer)':
    'bg-teal-500/15 text-teal-700 dark:text-teal-400 border-teal-500/30',
  'DNC / remove request':
    'bg-red-500/15 text-red-700 dark:text-red-400 border-red-500/30',
  'System error':
    'bg-red-500/15 text-red-700 dark:text-red-400 border-red-500/30',
};

const CATEGORY_LABEL: Record<string, string> = {
  human: 'Human',
  voicemail: 'Machine',
  echo: 'Echo/IVR',
  no_speech: 'No speech',
  no_answer: 'No answer',
};

const CATEGORY_BADGE: Record<string, string> = {
  human: 'bg-primary/10 text-primary border-primary/30',
  voicemail: 'bg-muted text-muted-foreground border-transparent',
  echo: 'bg-muted text-muted-foreground border-transparent',
  no_speech: 'bg-muted text-muted-foreground border-transparent',
  no_answer: 'bg-muted text-muted-foreground border-transparent',
};

const SEVERITY_DOT: Record<InsightItem['severity'], string> = {
  critical: 'bg-red-500',
  warn: 'bg-amber-500',
  info: 'bg-blue-500',
  good: 'bg-emerald-500',
};

const SEVERITY_ICON_STYLE: Record<InsightItem['severity'], string> = {
  critical: 'text-red-500',
  warn: 'text-amber-500',
  info: 'text-blue-500',
  good: 'text-emerald-500',
};

function severityIcon(sev: InsightItem['severity']) {
  if (sev === 'good') return CheckCircle2;
  if (sev === 'critical' || sev === 'warn') return AlertTriangle;
  return Lightbulb;
}

type TableFilter = 'humans' | 'transfers' | 'all' | 'machines';
type BreakdownTab = 'outcomes' | 'reasons';

/* ---------------------------------------------------------- primitives */

/** Small uppercase section eyebrow used across every panel. */
function Eyebrow({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <span
      className={cn(
        'text-[11px] font-semibold uppercase tracking-wider text-muted-foreground',
        className
      )}
    >
      {children}
    </span>
  );
}

/** Compact panel — tighter than the default Card padding. */
function Panel({ className, children }: { className?: string; children: React.ReactNode }) {
  return <Card className={cn('p-4', className)}>{children}</Card>;
}

/* ---------------------------------------------------------------- page */

export default function LiveResultsPage() {
  const browserTz =
    typeof Intl !== 'undefined'
      ? Intl.DateTimeFormat().resolvedOptions().timeZone
      : 'America/New_York';

  const today = format(new Date(), 'yyyy-MM-dd');
  const [startDate, setStartDate] = useState<string>(today);
  const [endDate, setEndDate] = useState<string>(today);
  const [workflows, setWorkflows] = useState<WorkflowOption[]>([]);
  const [selectedWorkflow, setSelectedWorkflow] = useState<string>('all');
  const [data, setData] = useState<InsightsPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [secondsAgo, setSecondsAgo] = useState(0);
  const [tableFilter, setTableFilter] = useState<TableFilter>('humans');
  const [breakdownTab, setBreakdownTab] = useState<BreakdownTab>('outcomes');
  const [search, setSearch] = useState('');
  const [visibleRows, setVisibleRows] = useState(100);
  const mediaPreview = MediaPreviewDialog();
  const inFlight = useRef(false);

  const isToday = endDate === today;

  const fetchData = useCallback(
    async (isBackground: boolean) => {
      if (inFlight.current) return;
      inFlight.current = true;
      if (!isBackground) setLoading(true);
      else setRefreshing(true);
      try {
        const query: Record<string, string | number> = {
          start_date: startDate,
          end_date: endDate,
          timezone: browserTz,
        };
        if (selectedWorkflow !== 'all') query.workflow_id = Number(selectedWorkflow);
        const res = await client.get({
          url: '/api/v1/organizations/reports/campaign-insights',
          query,
        });
        if (res.data) {
          setData(res.data as unknown as InsightsPayload);
          setError(null);
          setLastUpdated(new Date());
        } else {
          setError('Failed to load results');
        }
      } catch {
        setError('Failed to load results');
      } finally {
        inFlight.current = false;
        setLoading(false);
        setRefreshing(false);
      }
    },
    [startDate, endDate, browserTz, selectedWorkflow]
  );

  useEffect(() => {
    setVisibleRows(100);
    fetchData(false);
  }, [fetchData]);

  useEffect(() => {
    if (!isToday) return;
    const id = setInterval(() => {
      if (typeof document !== 'undefined' && document.hidden) return;
      fetchData(true);
    }, REFRESH_SECONDS * 1000);
    return () => clearInterval(id);
  }, [fetchData, isToday]);

  useEffect(() => {
    const id = setInterval(() => {
      if (lastUpdated) {
        setSecondsAgo(Math.round((Date.now() - lastUpdated.getTime()) / 1000));
      }
    }, 1000);
    return () => clearInterval(id);
  }, [lastUpdated]);

  useEffect(() => {
    (async () => {
      try {
        const res = await getWorkflowOptionsApiV1OrganizationsReportsWorkflowsGet();
        if (res.data) setWorkflows(res.data as WorkflowOption[]);
      } catch {
        /* non-fatal */
      }
    })();
  }, []);

  const filteredCalls = useMemo(() => {
    if (!data) return [];
    let rows = data.calls;
    if (tableFilter === 'humans') rows = rows.filter((c) => c.category === 'human');
    else if (tableFilter === 'transfers')
      rows = rows.filter((c) => c.result.startsWith('Transferred'));
    else if (tableFilter === 'machines') rows = rows.filter((c) => c.category !== 'human');
    if (search.trim()) {
      const q = search.replace(/[^0-9+]/g, '');
      if (q) rows = rows.filter((c) => (c.phone || '').includes(q));
    }
    return [...rows].sort((a, b) => (a.time < b.time ? 1 : -1));
  }, [data, tableFilter, search]);

  const callCounts = useMemo<Record<TableFilter, number>>(() => {
    const rows = data?.calls ?? [];
    return {
      humans: rows.filter((c) => c.category === 'human').length,
      transfers: rows.filter((c) => c.result.startsWith('Transferred')).length,
      machines: rows.filter((c) => c.category !== 'human').length,
      all: rows.length,
    };
  }, [data]);

  const funnelMax = data ? Math.max(...data.funnel.map((f) => f.count), 1) : 1;
  const reasonsMax = data ? Math.max(...data.reasons.map((r) => r.count), 1) : 1;
  const outcomesMax = data ? Math.max(...data.outcomes.map((o) => o.count), 1) : 1;

  const k = data?.kpis;

  const exportCsv = () => {
    const rows = filteredCalls;
    const header = ['time', 'phone', 'duration_sec', 'who_answered', 'result', 'reason', 'transcript'];
    const esc = (v: unknown) => `"${String(v ?? '').replace(/"/g, '""')}"`;
    const lines = [header.join(',')];
    for (const c of rows) {
      const t = format(new Date(c.time), 'yyyy-MM-dd HH:mm:ss');
      const cleanTranscript = (c.transcript || '').replace(/[\r\n]+/g, ' | ');
      lines.push([t, c.phone || '', c.duration, c.category, c.result, c.reason || '', cleanTranscript].map(esc).join(','));
    }
    const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `calls_${startDate}_to_${endDate}_${tableFilter}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  /* ------------------------------------------------------------ render */

  return (
    <div className="mx-auto max-w-[1400px] space-y-4 p-4 md:p-5">
      {/* Header */}
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="flex items-center gap-2.5">
          <h1 className="text-lg font-semibold tracking-tight">Live Results</h1>
          {isToday && (
            <span className="flex items-center gap-1.5 rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-emerald-600 dark:text-emerald-400">
              <span className="relative flex h-1.5 w-1.5">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-500 opacity-60" />
                <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-emerald-500" />
              </span>
              Live
            </span>
          )}
          {data?.status.last_call_at && (
            <span className="hidden text-xs text-muted-foreground sm:inline">
              · last call {format(new Date(data.status.last_call_at), 'h:mm a')}
            </span>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <div className="flex h-8 items-center gap-1 rounded-md border bg-card px-2">
            <span className="text-[11px] font-medium text-muted-foreground">From</span>
            <Input
              type="date"
              value={startDate}
              max={endDate}
              onChange={(e) => e.target.value && setStartDate(e.target.value)}
              className="h-7 w-[132px] border-0 px-1 text-xs shadow-none focus-visible:ring-0"
            />
            <span className="text-[11px] font-medium text-muted-foreground">To</span>
            <Input
              type="date"
              value={endDate}
              min={startDate}
              max={today}
              onChange={(e) => e.target.value && setEndDate(e.target.value)}
              className="h-7 w-[132px] border-0 px-1 text-xs shadow-none focus-visible:ring-0"
            />
          </div>

          <Select value={selectedWorkflow} onValueChange={setSelectedWorkflow}>
            <SelectTrigger className="h-8 w-[170px] text-xs">
              <SelectValue placeholder="All agents" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All agents</SelectItem>
              {workflows.map((w) => (
                <SelectItem key={w.id} value={String(w.id)}>
                  {w.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Button
            variant="outline"
            size="sm"
            className="h-8 gap-1.5 text-xs"
            onClick={() => fetchData(true)}
            disabled={refreshing}
          >
            <RefreshCw className={cn('h-3.5 w-3.5', refreshing && 'animate-spin')} />
            {lastUpdated ? `${secondsAgo}s` : 'Refresh'}
          </Button>
        </div>
      </div>

      {error && (
        <Card className="border-red-500/40 bg-red-500/5 p-3 text-sm text-red-600 dark:text-red-400">
          {error}
        </Card>
      )}

      {loading || !data ? (
        <div className="space-y-4">
          <Skeleton className="h-20 rounded-xl" />
          <div className="grid gap-4 lg:grid-cols-3">
            <Skeleton className="h-56 rounded-xl lg:col-span-2" />
            <Skeleton className="h-56 rounded-xl" />
          </div>
          <Skeleton className="h-96 rounded-xl" />
        </div>
      ) : (
        <>
          {/* KPI rail — one divided strip instead of six tall cards */}
          <Card className="overflow-hidden p-0">
            <div className="grid grid-cols-2 divide-x divide-y divide-border sm:grid-cols-3 lg:grid-cols-6 lg:divide-y-0">
              <KpiCell icon={PhoneCall} label="Dials" value={k!.dials} sub="attempts" />
              <KpiCell
                icon={PhoneCall}
                label="Answered"
                value={k!.answered}
                sub={`${pct(k!.answered, k!.dials)} of dials`}
              />
              <KpiCell
                icon={Users}
                label="Humans"
                value={k!.humans}
                sub={`${pct(k!.humans, k!.answered)} of answered`}
              />
              <KpiCell
                icon={Target}
                label="Reached offer"
                value={k!.offers}
                sub={`${pct(k!.offers, k!.humans)} of humans`}
              />
              <KpiCell
                icon={PhoneForwarded}
                label="Transferred"
                value={k!.transfers}
                sub={`${pct(k!.transfers, k!.humans)} of humans`}
              />
              <KpiCell
                icon={CheckCircle2}
                label="Buyer answered"
                value={k!.transfers_answered}
                sub={`of ${k!.transfers} transfers`}
                danger={k!.transfers > 0 && k!.transfers_answered === 0}
              />
            </div>
            {/* thin live meta footer */}
            <div className="flex flex-wrap items-center gap-x-5 gap-y-1 border-t border-border bg-muted/30 px-4 py-1.5 text-[11px] text-muted-foreground">
              <span className="flex items-center gap-1.5">
                <Activity className="h-3 w-3 text-primary" />
                <span className="font-semibold text-foreground tabular-nums">
                  {data.status.active_calls}
                </span>
                in progress
              </span>
              <span className="flex items-center gap-1.5">
                <Voicemail className="h-3 w-3" />
                <span className="font-semibold text-foreground tabular-nums">
                  {k!.voicemails}
                </span>
                machines
              </span>
              <span className="flex items-center gap-1.5">
                <Clock className="h-3 w-3" />
                <span className="font-semibold text-foreground tabular-nums">
                  {k!.talk_minutes}m
                </span>
                talk time
              </span>
            </div>
          </Card>

          {/* Funnel + Insights side by side */}
          <div className="grid gap-4 lg:grid-cols-3">
            <Panel className="lg:col-span-2">
              <Eyebrow>Conversion funnel</Eyebrow>
              <div className="mt-3 space-y-2">
                {data.funnel.map((f, i) => {
                  const prev = i > 0 ? data.funnel[i - 1].count : null;
                  return (
                    <div key={f.stage} className="flex items-center gap-3">
                      <div className="w-36 shrink-0 truncate text-xs text-muted-foreground">
                        {f.stage}
                      </div>
                      <div className="relative h-5 flex-1 overflow-hidden rounded bg-muted">
                        <div
                          className={cn(
                            'h-full rounded transition-all duration-700',
                            i === data.funnel.length - 1 && f.count === 0
                              ? 'bg-red-500/70'
                              : 'bg-primary'
                          )}
                          style={{
                            width: `${Math.max((f.count / funnelMax) * 100, f.count > 0 ? 2 : 0)}%`,
                            opacity: 1 - i * 0.08,
                          }}
                        />
                        <span className="absolute inset-y-0 left-2 flex items-center text-[11px] font-semibold tabular-nums mix-blend-difference text-white">
                          {f.count.toLocaleString()}
                        </span>
                      </div>
                      <div className="w-12 shrink-0 text-right text-[11px] tabular-nums text-muted-foreground">
                        {prev !== null ? pct(f.count, prev) : ''}
                      </div>
                    </div>
                  );
                })}
              </div>
            </Panel>

            <Panel>
              <Eyebrow>What&apos;s costing us</Eyebrow>
              {data.insights.length === 0 ? (
                <p className="mt-3 text-xs text-muted-foreground">Nothing flagged yet.</p>
              ) : (
                <div className="mt-2 divide-y divide-border">
                  {data.insights.map((ins) => {
                    const Icon = severityIcon(ins.severity);
                    return (
                      <details
                        key={ins.title}
                        className="group py-2 first:pt-1 last:pb-0"
                      >
                        <summary className="flex cursor-pointer list-none items-center gap-2">
                          <span
                            className={cn(
                              'h-1.5 w-1.5 shrink-0 rounded-full',
                              SEVERITY_DOT[ins.severity]
                            )}
                          />
                          <Icon
                            className={cn('h-3.5 w-3.5 shrink-0', SEVERITY_ICON_STYLE[ins.severity])}
                          />
                          <span className="min-w-0 flex-1 truncate text-xs font-medium">
                            {ins.title}
                          </span>
                          {ins.count > 0 && (
                            <span className="shrink-0 text-[11px] font-semibold tabular-nums text-muted-foreground">
                              {ins.count}
                            </span>
                          )}
                        </summary>
                        <p className="mt-1 pl-6 text-[11px] leading-snug text-muted-foreground">
                          {ins.detail}
                        </p>
                      </details>
                    );
                  })}
                </div>
              )}
            </Panel>
          </div>

          {/* Breakdown (tabbed) + Call log */}
          <Panel>
            <div className="flex items-center justify-between">
              <Eyebrow>Breakdown</Eyebrow>
              <div className="inline-flex rounded-md border bg-muted/40 p-0.5">
                {(
                  [
                    ['outcomes', 'Outcomes'],
                    ['reasons', 'Non-conversion'],
                  ] as Array<[BreakdownTab, string]>
                ).map(([key, label]) => (
                  <button
                    key={key}
                    onClick={() => setBreakdownTab(key)}
                    className={cn(
                      'rounded px-2.5 py-1 text-[11px] font-medium transition-colors',
                      breakdownTab === key
                        ? 'bg-card text-foreground shadow-sm'
                        : 'text-muted-foreground hover:text-foreground'
                    )}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>
            <div className="mt-3 grid gap-x-8 gap-y-1.5 sm:grid-cols-2">
              {breakdownTab === 'outcomes'
                ? data.outcomes.length === 0
                  ? <p className="text-xs text-muted-foreground">No human calls yet.</p>
                  : data.outcomes.map((o) => (
                      <BarRow key={o.result} label={o.result} count={o.count} max={outcomesMax} total={k!.humans} />
                    ))
                : data.reasons.length === 0
                ? <p className="text-xs text-muted-foreground">Nothing to show yet.</p>
                : data.reasons.map((r) => (
                    <BarRow
                      key={r.reason}
                      label={r.reason}
                      count={r.count}
                      max={reasonsMax}
                      total={k!.humans}
                      accent={r.reason.includes('lost at handoff') ? 'bg-red-500/80' : undefined}
                    />
                  ))}
            </div>
          </Panel>

          {/* Call log */}
          <Panel className="p-0">
            <div className="flex flex-col gap-2 border-b border-border p-3 md:flex-row md:items-center md:justify-between">
              <Eyebrow>Call-by-call log</Eyebrow>
              <div className="flex flex-wrap items-center gap-1.5">
                <div className="inline-flex rounded-md border bg-muted/40 p-0.5">
                  {(
                    [
                      ['humans', 'Humans'],
                      ['transfers', 'Transfers'],
                      ['machines', 'Machines'],
                      ['all', 'All'],
                    ] as Array<[TableFilter, string]>
                  ).map(([key, label]) => (
                    <button
                      key={key}
                      onClick={() => {
                        setTableFilter(key);
                        setVisibleRows(100);
                      }}
                      className={cn(
                        'rounded px-2 py-1 text-[11px] font-medium transition-colors',
                        tableFilter === key
                          ? 'bg-card text-foreground shadow-sm'
                          : 'text-muted-foreground hover:text-foreground'
                      )}
                    >
                      {label}
                      <span className="ml-1 tabular-nums opacity-60">{callCounts[key]}</span>
                    </button>
                  ))}
                </div>
                <div className="relative">
                  <Search className="absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
                  <Input
                    placeholder="Search number…"
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                    className="h-8 w-40 pl-7 text-xs"
                  />
                </div>
                <Button variant="outline" size="sm" className="h-8 gap-1.5 text-xs" onClick={exportCsv}>
                  <Download className="h-3.5 w-3.5" /> CSV
                </Button>
              </div>
            </div>

            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow className="hover:bg-transparent">
                    <TableHead className="h-8 px-3 text-[11px] uppercase tracking-wide">Time</TableHead>
                    <TableHead className="h-8 px-3 text-[11px] uppercase tracking-wide">Phone</TableHead>
                    <TableHead className="h-8 px-3 text-[11px] uppercase tracking-wide">Dur</TableHead>
                    <TableHead className="h-8 px-3 text-[11px] uppercase tracking-wide">Answered</TableHead>
                    <TableHead className="h-8 px-3 text-[11px] uppercase tracking-wide">Result</TableHead>
                    <TableHead className="h-8 px-3 text-center text-[11px] uppercase tracking-wide">Rec</TableHead>
                    <TableHead className="hidden h-8 px-3 text-[11px] uppercase tracking-wide lg:table-cell">
                      Reason
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {filteredCalls.slice(0, visibleRows).map((c) => (
                    <TableRow key={c.run_id} className="border-border/60">
                      <TableCell className="whitespace-nowrap px-3 py-1.5 text-xs tabular-nums text-muted-foreground">
                        {format(new Date(c.time), 'h:mm:ss a')}
                      </TableCell>
                      <TableCell className="px-3 py-1.5 font-mono text-xs">{c.phone || '—'}</TableCell>
                      <TableCell className="px-3 py-1.5 text-xs tabular-nums text-muted-foreground">
                        {fmtDuration(c.duration)}
                      </TableCell>
                      <TableCell className="px-3 py-1.5">
                        <Badge
                          variant="outline"
                          className={cn('px-1.5 py-0 text-[10px] font-normal', CATEGORY_BADGE[c.category])}
                        >
                          {CATEGORY_LABEL[c.category] || c.category}
                        </Badge>
                      </TableCell>
                      <TableCell className="px-3 py-1.5">
                        <Badge
                          variant="outline"
                          className={cn(
                            'px-1.5 py-0 text-[10px] font-normal',
                            RESULT_BADGE[c.result] || 'bg-muted text-muted-foreground border-transparent'
                          )}
                        >
                          {c.result}
                        </Badge>
                      </TableCell>
                      <TableCell className="px-3 py-1.5 text-center">
                        <div className="flex justify-center">
                          <MediaPreviewButton
                            recordingUrl={c.recording_url}
                            transcriptUrl={c.transcript_url}
                            runId={c.run_id}
                            onOpenPreview={mediaPreview.openPreview}
                          />
                        </div>
                      </TableCell>
                      <TableCell className="hidden max-w-72 truncate px-3 py-1.5 text-xs text-muted-foreground lg:table-cell">
                        {c.reason || ''}
                      </TableCell>
                    </TableRow>
                  ))}
                  {filteredCalls.length === 0 && (
                    <TableRow className="hover:bg-transparent">
                      <TableCell colSpan={7} className="py-8 text-center text-xs text-muted-foreground">
                        No calls match this filter yet.
                      </TableCell>
                    </TableRow>
                  )}
                </TableBody>
              </Table>
            </div>

            {filteredCalls.length > visibleRows && (
              <div className="flex justify-center border-t border-border p-2">
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-7 text-xs"
                  onClick={() => setVisibleRows((v) => v + 100)}
                >
                  Show more ({filteredCalls.length - visibleRows} remaining)
                </Button>
              </div>
            )}
          </Panel>
        </>
      )}
      {mediaPreview.dialog}
    </div>
  );
}

/* ------------------------------------------------------- subcomponents */

function KpiCell({
  icon: Icon,
  label,
  value,
  sub,
  danger,
}: {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  value: number;
  sub: string;
  danger?: boolean;
}) {
  return (
    <div className={cn('px-4 py-3', danger && 'bg-red-500/5')}>
      <div className="flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        <Icon className={cn('h-3.5 w-3.5', danger ? 'text-red-500' : 'text-primary')} />
        <span className="truncate">{label}</span>
      </div>
      <div className="mt-1.5 flex items-baseline gap-1.5">
        <span
          className={cn(
            'text-2xl font-semibold leading-none tabular-nums',
            danger && 'text-red-600 dark:text-red-400'
          )}
        >
          {value.toLocaleString()}
        </span>
      </div>
      <div className="mt-1 truncate text-[11px] text-muted-foreground">{sub}</div>
    </div>
  );
}

function BarRow({
  label,
  count,
  max,
  total,
  accent,
}: {
  label: string;
  count: number;
  max: number;
  total: number;
  accent?: string;
}) {
  return (
    <div className="flex items-center gap-2.5">
      <div className="w-44 shrink-0 truncate text-xs" title={label}>
        {label}
      </div>
      <div className="relative h-3.5 flex-1 overflow-hidden rounded bg-muted">
        <div
          className={cn('h-full rounded transition-all duration-500', accent || 'bg-primary/70')}
          style={{ width: `${Math.max((count / max) * 100, 2)}%` }}
        />
      </div>
      <div className="w-14 shrink-0 text-right text-[11px] tabular-nums text-muted-foreground">
        {count} · {pct(count, total)}
      </div>
    </div>
  );
}
