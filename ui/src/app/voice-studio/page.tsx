"use client";

import {
  AlertTriangle,
  Check,
  Copy,
  Globe2,
  Loader2,
  Pause,
  Play,
  RefreshCw,
  Search,
  SlidersHorizontal,
  Trash2,
  Upload,
  UserRound,
  Wand2,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import {
  AGENT_PROMPT_BLOCK,
  ARPABET_PRESETS,
  ARPABET_REFERENCE_URL,
  EMOTION_GROUPS,
  PARALANGUAGE_TAGS,
  markerFamilyFor,
  renderMarker,
  renderPhoneme,
  stripMarkers,
} from "@/lib/fish-markers";

/**
 * Fish Voice Studio.
 *
 * Single-viewport dashboard: the root is locked to the height left over below
 * Dograh's app header and never scrolls the page. The left column holds the
 * script tester and marker palette; the right column pins its own controls and
 * scrolls only the voice list.
 *
 * Everything talks to /api/fish/*, which proxies fish.audio server-side — the
 * Fish key is never in the browser. The voice id you land on here is what goes
 * into the TTS config as `voice` (Fish calls it reference_id).
 */

interface FishSample {
  audio?: string;
  title?: string;
}

interface FishVoice {
  _id?: string;
  id?: string;
  title?: string;
  description?: string;
  state?: string;
  visibility?: string;
  languages?: string[];
  tags?: string[];
  author?: { nickname?: string };
  samples?: FishSample[];
  like_count?: number;
  task_count?: number;
}

interface FishStatus {
  configured: boolean;
  defaultModel: string;
  models: string[];
  credit?: { credit?: string | number } | null;
}

type VoiceSource = "mine" | "library";

const MODELS: Record<string, { label: string; note: string }> = {
  "s2.1-pro-free": { label: "S2.1 Pro Free", note: "No cost · no latency guarantee" },
  "s2.1-pro": { label: "S2.1 Pro", note: "Production · guaranteed TTFA" },
  "s2-pro": { label: "S2 Pro", note: "Previous generation" },
  s1: { label: "S1", note: "Legacy · (parenthesis) markers" },
};

const LANGUAGES: { value: string; label: string }[] = [
  { value: "any", label: "Any language" },
  { value: "en", label: "English" },
  { value: "es", label: "Spanish" },
  { value: "zh", label: "Chinese" },
  { value: "ja", label: "Japanese" },
  { value: "ko", label: "Korean" },
  { value: "fr", label: "French" },
  { value: "de", label: "German" },
  { value: "pt", label: "Portuguese" },
  { value: "it", label: "Italian" },
  { value: "ar", label: "Arabic" },
  { value: "ru", label: "Russian" },
  { value: "hi", label: "Hindi" },
  { value: "nl", label: "Dutch" },
  { value: "pl", label: "Polish" },
];

const SORTS: { value: string; label: string }[] = [
  { value: "default", label: "Best match" },
  { value: "task_count", label: "Most used" },
  { value: "created_at", label: "Newest" },
];

const TAG_PRESETS = [
  "male",
  "female",
  "young",
  "mature",
  "calm",
  "energetic",
  "narration",
  "conversational",
];

const PAGE_SIZE = 24;

/** Shared chip styling for every marker button in the palette. */
const CHIP =
  "rounded border border-neutral-200 bg-background px-2 py-0.5 font-mono text-xs leading-5 transition-colors hover:border-primary hover:bg-accent dark:border-neutral-800";

function voiceId(voice: FishVoice): string {
  return voice._id || voice.id || "";
}

async function readError(response: Response, fallback: string): Promise<string> {
  try {
    const body = await response.json();
    return body?.error?.message || fallback;
  } catch {
    return fallback;
  }
}

/** First playable sample Fish returned for a voice, if any. */
function sampleUrl(voice: FishVoice): string | null {
  return voice.samples?.find((sample) => sample.audio)?.audio || null;
}

export default function VoiceStudioPage() {
  const [status, setStatus] = useState<FishStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);

  // ------------------------------------------------------------ library
  const [source, setSource] = useState<VoiceSource>("library");
  const [voices, setVoices] = useState<FishVoice[]>([]);
  const [loadingVoices, setLoadingVoices] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [voicesError, setVoicesError] = useState<string | null>(null);

  const [searchTitle, setSearchTitle] = useState("");
  const [language, setLanguage] = useState("any");
  const [sortBy, setSortBy] = useState("default");
  const [tag, setTag] = useState("");
  const [page, setPage] = useState(1);
  const [hasMore, setHasMore] = useState(false);

  const [selectedVoice, setSelectedVoice] = useState<FishVoice | null>(null);
  const [copiedId, setCopiedId] = useState(false);
  const [renaming, setRenaming] = useState<FishVoice | null>(null);
  const [deleting, setDeleting] = useState<FishVoice | null>(null);

  // ------------------------------------------------------------ preview
  const [model, setModel] = useState("s2.1-pro-free");
  const [script, setScript] = useState(
    "[confident] Hi, this is Alex on a recorded line. [break] I'm calling about the hospital indemnity plan you asked about.",
  );
  const [latency, setLatency] = useState("balanced");
  const [speed, setSpeed] = useState(1.0);
  const [volume, setVolume] = useState(0);
  const [chunkLength, setChunkLength] = useState(200);
  const [normalize, setNormalize] = useState(true);

  const [previewing, setPreviewing] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [previewMs, setPreviewMs] = useState<number | null>(null);
  const [playing, setPlaying] = useState(false);

  // Sample playback is separate from the script preview so one never stops the
  // other mid-audition.
  const [samplePlayingId, setSamplePlayingId] = useState<string | null>(null);
  const sampleAudioRef = useRef<HTMLAudioElement | null>(null);

  const audioRef = useRef<HTMLAudioElement | null>(null);
  const audioUrlRef = useRef<string | null>(null);
  const scriptRef = useRef<HTMLTextAreaElement | null>(null);

  const markerFamily = markerFamilyFor(model);
  const spokenText = useMemo(() => stripMarkers(script), [script]);
  const activeFilters =
    (language !== "any" ? 1 : 0) + (sortBy !== "default" ? 1 : 0) + (tag.trim() ? 1 : 0);

  // ---------------------------------------------------------------- status

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const response = await fetch("/api/fish/status");
        if (cancelled) return;
        if (!response.ok) {
          setStatusError(await readError(response, "Could not reach the Voice Studio API."));
          return;
        }
        const body: FishStatus = await response.json();
        setStatus(body);
        if (body.defaultModel) setModel(body.defaultModel);
      } catch {
        if (!cancelled) setStatusError("Could not reach the Voice Studio API.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // ---------------------------------------------------------------- voices

  const fetchVoices = useCallback(
    async (pageNumber: number, append: boolean) => {
      if (append) setLoadingMore(true);
      else setLoadingVoices(true);
      setVoicesError(null);

      try {
        const params = new URLSearchParams({
          self: source === "mine" ? "true" : "false",
          page_size: String(PAGE_SIZE),
          page_number: String(pageNumber),
        });
        if (searchTitle.trim()) params.set("title", searchTitle.trim());
        if (language !== "any") params.set("language", language);
        if (sortBy !== "default") params.set("sort_by", sortBy);
        if (tag.trim()) params.set("tag", tag.trim());

        const response = await fetch(`/api/fish/voices?${params.toString()}`);
        if (!response.ok) {
          setVoicesError(await readError(response, "Could not load voices."));
          if (!append) setVoices([]);
          return;
        }

        const body = await response.json();
        const items: FishVoice[] = Array.isArray(body?.items) ? body.items : [];
        setVoices((prev) => (append ? [...prev, ...items] : items));
        setHasMore(items.length === PAGE_SIZE);
        setPage(pageNumber);
      } catch {
        setVoicesError("Could not reach Fish Audio.");
        if (!append) setVoices([]);
      } finally {
        setLoadingVoices(false);
        setLoadingMore(false);
      }
    },
    [source, searchTitle, language, sortBy, tag],
  );

  const reloadVoices = useCallback(() => void fetchVoices(1, false), [fetchVoices]);

  // Dropdown and tag filters re-query immediately; the title box waits for
  // Enter or the Search button so typing doesn't hammer Fish.
  useEffect(() => {
    if (status?.configured) void fetchVoices(1, false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status?.configured, source, language, sortBy, tag]);

  const stopSample = useCallback(() => {
    sampleAudioRef.current?.pause();
    sampleAudioRef.current = null;
    setSamplePlayingId(null);
  }, []);

  const toggleSample = useCallback(
    (voice: FishVoice) => {
      const url = sampleUrl(voice);
      const id = voiceId(voice);
      if (!url) return;

      if (samplePlayingId === id) {
        stopSample();
        return;
      }

      sampleAudioRef.current?.pause();
      const audio = new Audio(url);
      audio.onended = () => setSamplePlayingId(null);
      audio.onerror = () => setSamplePlayingId(null);
      sampleAudioRef.current = audio;
      void audio.play().then(
        () => setSamplePlayingId(id),
        () => setSamplePlayingId(null),
      );
    },
    [samplePlayingId, stopSample],
  );

  useEffect(() => () => sampleAudioRef.current?.pause(), []);

  const confirmDelete = useCallback(async () => {
    const voice = deleting;
    const id = voice ? voiceId(voice) : "";
    if (!voice || !id) return;

    try {
      const response = await fetch(`/api/fish/voices/${encodeURIComponent(id)}`, {
        method: "DELETE",
      });
      if (!response.ok) {
        setVoicesError(await readError(response, "Could not delete the voice."));
        return;
      }
      if (selectedVoice && voiceId(selectedVoice) === id) setSelectedVoice(null);
      reloadVoices();
    } catch {
      setVoicesError("Could not reach Fish Audio.");
    } finally {
      setDeleting(null);
    }
  }, [deleting, reloadVoices, selectedVoice]);

  const submitRename = useCallback(
    async (title: string) => {
      const voice = renaming;
      const id = voice ? voiceId(voice) : "";
      if (!voice || !id || !title.trim()) return;

      try {
        const response = await fetch(`/api/fish/voices/${encodeURIComponent(id)}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title: title.trim() }),
        });
        if (!response.ok) {
          setVoicesError(await readError(response, "Could not rename the voice."));
          return;
        }
        setSelectedVoice((prev) =>
          prev && voiceId(prev) === id ? { ...prev, title: title.trim() } : prev,
        );
        reloadVoices();
      } catch {
        setVoicesError("Could not reach Fish Audio.");
      } finally {
        setRenaming(null);
      }
    },
    [renaming, reloadVoices],
  );

  // --------------------------------------------------------------- preview

  const runPreview = useCallback(async () => {
    if (!script.trim()) return;
    stopSample();
    setPreviewing(true);
    setPreviewError(null);
    setPreviewMs(null);

    const startedAt = performance.now();
    try {
      const response = await fetch("/api/fish/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text: script,
          voice: selectedVoice ? voiceId(selectedVoice) : undefined,
          model,
          latency,
          speed,
          volume,
          chunk_length: chunkLength,
          normalize,
        }),
      });

      if (!response.ok) {
        setPreviewError(await readError(response, "Preview failed."));
        return;
      }

      const blob = await response.blob();
      if (audioUrlRef.current) URL.revokeObjectURL(audioUrlRef.current);
      const url = URL.createObjectURL(blob);
      audioUrlRef.current = url;

      setPreviewMs(Math.round(performance.now() - startedAt));

      const audio = audioRef.current;
      if (audio) {
        audio.src = url;
        void audio.play().catch(() => setPlaying(false));
      }
    } catch {
      setPreviewError("Could not reach Fish Audio.");
    } finally {
      setPreviewing(false);
    }
  }, [script, selectedVoice, model, latency, speed, volume, chunkLength, normalize, stopSample]);

  useEffect(
    () => () => {
      if (audioUrlRef.current) URL.revokeObjectURL(audioUrlRef.current);
    },
    [],
  );

  /** Insert marker text at the caret so it lands where you were typing. */
  const insertAtCaret = useCallback(
    (snippet: string) => {
      const textarea = scriptRef.current;
      if (!textarea) {
        setScript((prev) => `${prev}${snippet}`);
        return;
      }
      const start = textarea.selectionStart ?? script.length;
      const end = textarea.selectionEnd ?? script.length;
      const next = `${script.slice(0, start)}${snippet}${script.slice(end)}`;
      setScript(next);
      requestAnimationFrame(() => {
        textarea.focus();
        const caret = start + snippet.length;
        textarea.setSelectionRange(caret, caret);
      });
    },
    [script],
  );

  const copyVoiceId = useCallback(async () => {
    const id = selectedVoice ? voiceId(selectedVoice) : "";
    if (!id) return;
    try {
      await navigator.clipboard.writeText(id);
      setCopiedId(true);
      setTimeout(() => setCopiedId(false), 1800);
    } catch {
      /* clipboard unavailable — the id is on screen to copy by hand */
    }
  }, [selectedVoice]);

  const clearFilters = useCallback(() => {
    setLanguage("any");
    setSortBy("default");
    setTag("");
    setSearchTitle("");
  }, []);

  // ----------------------------------------------------------------- views

  if (statusError) {
    return (
      <div className="flex h-[calc(100vh-3.25rem)] flex-col items-center justify-center gap-3 px-4 text-center">
        <AlertTriangle className="h-8 w-8 text-muted-foreground" />
        <p className="text-sm text-muted-foreground">{statusError}</p>
      </div>
    );
  }

  if (!status) {
    return (
      <div className="flex h-[calc(100vh-3.25rem)] items-center justify-center">
        <Loader2 className="h-6 w-6 animate-spin text-primary" />
      </div>
    );
  }

  if (!status.configured) {
    return (
      <div className="mx-auto max-w-xl px-4 py-16">
        <Card>
          <CardHeader>
            <CardTitle>Fish Audio is not configured</CardTitle>
            <CardDescription>
              Set <code className="rounded bg-muted px-1 font-mono">FISH_API_KEY</code> on the ui
              service and restart it. The key is read server-side only.
            </CardDescription>
          </CardHeader>
        </Card>
      </div>
    );
  }

  const credit = status.credit?.credit;

  return (
    // Root is locked to the height left below Dograh's app header and never
    // scrolls: every scrollbar on this page belongs to a specific panel.
    <div className="flex h-[calc(100vh-3.25rem)] gap-3 overflow-hidden p-3">
      <audio
        ref={audioRef}
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onEnded={() => setPlaying(false)}
        className="hidden"
      />

      {/* ================================================== LEFT: script + markers */}
      <div className="flex min-h-0 w-[46%] shrink-0 flex-col gap-2 overflow-hidden">
        {/* Header — title, description and status on one row */}
        <div className="flex shrink-0 items-baseline justify-between gap-3">
          <div className="flex min-w-0 items-baseline gap-2">
            <h1 className="text-base font-semibold tracking-tight">Voice Studio</h1>
            <p className="truncate text-xs text-muted-foreground">
              Browse, clone and audition Fish voices
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            {credit !== undefined && credit !== null && (
              <Badge variant="outline" className="h-5 px-1.5 font-mono text-[10px]">
                ${typeof credit === "number" ? credit.toFixed(2) : credit}
              </Badge>
            )}
            {model === "s2.1-pro-free" && (
              <Badge
                variant="outline"
                className="h-5 border-amber-500/40 px-1.5 text-[10px] text-amber-600"
              >
                Free tier
              </Badge>
            )}
          </div>
        </div>

        {/* Selected voice — compact single row */}
        <div className="flex shrink-0 items-center gap-2 rounded-md border border-neutral-200 px-2.5 py-1.5 dark:border-neutral-800">
          {selectedVoice ? (
            <>
              <UserRound className="h-3.5 w-3.5 shrink-0 text-primary" />
              <span className="truncate text-xs font-semibold">
                {selectedVoice.title || voiceId(selectedVoice)}
              </span>
              <code className="truncate font-mono text-[10px] text-muted-foreground">
                {voiceId(selectedVoice)}
              </code>
              <div className="ml-auto flex shrink-0 items-center gap-1">
                <Button size="sm" variant="ghost" className="h-6 px-1.5" onClick={copyVoiceId}>
                  {copiedId ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  className="h-6 px-1.5 text-[11px]"
                  onClick={() => setSelectedVoice(null)}
                >
                  Clear
                </Button>
              </div>
            </>
          ) : (
            <span className="text-xs text-muted-foreground">
              No voice selected — previews use Fish&apos;s default.
            </span>
          )}
        </div>

        {/* Script tester */}
        <div className="shrink-0 space-y-2 rounded-md border border-neutral-200 p-3 dark:border-neutral-800">
          <Textarea
            ref={scriptRef}
            value={script}
            onChange={(event) => setScript(event.target.value)}
            rows={3}
            className="resize-none font-mono text-xs leading-relaxed"
            placeholder="Type what the agent should say. Click markers below to shape delivery."
          />

          <div className="flex flex-wrap items-center gap-1.5">
            <Button size="sm" className="h-7" onClick={runPreview} disabled={previewing || !script.trim()}>
              {previewing ? (
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
              ) : (
                <Play className="mr-1.5 h-3.5 w-3.5" />
              )}
              {previewing ? "Generating…" : "Hear it"}
            </Button>

            <Button
              size="sm"
              variant="outline"
              className="h-7"
              disabled={!audioUrlRef.current}
              onClick={() => {
                const audio = audioRef.current;
                if (!audio) return;
                if (playing) {
                  audio.pause();
                } else {
                  audio.currentTime = 0;
                  void audio.play();
                }
              }}
            >
              {playing ? <Pause className="mr-1.5 h-3.5 w-3.5" /> : <Play className="mr-1.5 h-3.5 w-3.5" />}
              {playing ? "Pause" : "Replay"}
            </Button>

            <Select value={model} onValueChange={setModel}>
              <SelectTrigger className="h-7 w-[150px] text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(status.models || []).map((value) => (
                  <SelectItem key={value} value={value} className="text-xs">
                    {MODELS[value]?.label || value}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <span className="ml-auto font-mono text-[10px] text-muted-foreground">
              {spokenText.length}c
              {script.length !== spokenText.length && ` +${script.length - spokenText.length}`}
              {previewMs !== null && ` · ${previewMs}ms`}
            </span>
          </div>

          {previewError && (
            <p className="flex items-start gap-1.5 text-xs text-destructive">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              {previewError}
            </p>
          )}
        </div>

        {/* Marker palette — the only flexible block, so it absorbs leftover height */}
        <div className="flex min-h-0 flex-1 flex-col rounded-md border border-neutral-200 p-3 dark:border-neutral-800">
          <Tabs defaultValue="emotion" className="flex min-h-0 flex-1 flex-col">
            <TabsList className="grid h-7 w-full shrink-0 grid-cols-4 p-1">
              <TabsTrigger value="emotion" className="px-2 py-0.5 text-xs">
                Emotion &amp; tone
              </TabsTrigger>
              <TabsTrigger value="fine" className="px-2 py-0.5 text-xs">
                Pronunciation
              </TabsTrigger>
              <TabsTrigger value="prosody" className="px-2 py-0.5 text-xs">
                Prosody
              </TabsTrigger>
              <TabsTrigger value="agent" className="px-2 py-0.5 text-xs">
                Agent
              </TabsTrigger>
            </TabsList>

            <TabsContent
              value="emotion"
              className="mt-2 min-h-0 flex-1 space-y-1.5 overflow-y-auto pr-1"
            >
              {EMOTION_GROUPS.map((group) => (
                <div key={group.label} className="space-y-1">
                  <p className="text-[11px] font-semibold text-muted-foreground">{group.label}</p>
                  <div className="flex flex-wrap gap-1">
                    {group.tags.map(({ tag: markerTag, note }) => (
                      <button
                        key={markerTag}
                        type="button"
                        title={note}
                        onClick={() => insertAtCaret(`${renderMarker(markerTag, markerFamily)} `)}
                        className={CHIP}
                      >
                        {renderMarker(markerTag, markerFamily)}
                      </button>
                    ))}
                  </div>
                </div>
              ))}

              <div className="space-y-1">
                <p className="text-[11px] font-semibold text-muted-foreground">Sounds &amp; pauses</p>
                <div className="flex flex-wrap gap-1">
                  {PARALANGUAGE_TAGS.map(({ tag: paraTag, note }) => (
                    <button
                      key={paraTag}
                      type="button"
                      title={note}
                      onClick={() => insertAtCaret(`(${paraTag}) `)}
                      className={CHIP}
                    >
                      ({paraTag})
                    </button>
                  ))}
                </div>
              </div>

              <p className="pt-0.5 text-[10px] leading-snug text-muted-foreground">
                Markers are inline text, not settings — the identical string works in a live call.
                {markerFamily === "s1"
                  ? " S1 uses (parentheses) and only the fixed tags above."
                  : " S2 uses [brackets] and accepts free-form cues like [slightly sad]."}
              </p>
            </TabsContent>

            <TabsContent
              value="fine"
              className="mt-2 min-h-0 flex-1 space-y-2 overflow-y-auto pr-1"
            >
              <PhonemeBuilder onInsert={insertAtCaret} />
              <p className="rounded-md border border-neutral-200 bg-muted/40 p-2 text-[10px] leading-snug text-muted-foreground dark:border-neutral-800">
                Keep <span className="font-medium">Normalize</span> on. Phoneme tags survive
                normalization; switching it off makes Fish read prices, dates and phone numbers
                unreliably — which is most of what the agent says.
              </p>
            </TabsContent>

            <TabsContent
              value="prosody"
              className="mt-2 min-h-0 flex-1 space-y-3 overflow-y-auto pr-1"
            >
              <div className="grid grid-cols-2 gap-x-4 gap-y-2">
                <SliderField
                  label="Speed"
                  value={speed}
                  min={0.5}
                  max={2}
                  step={0.05}
                  format={(v) => `${v.toFixed(2)}×`}
                  onChange={setSpeed}
                />
                <SliderField
                  label="Volume"
                  value={volume}
                  min={-20}
                  max={20}
                  step={1}
                  format={(v) => `${v > 0 ? "+" : ""}${v} dB`}
                  onChange={setVolume}
                />
                <SliderField
                  label="Chunk length"
                  value={chunkLength}
                  min={100}
                  max={300}
                  step={10}
                  format={(v) => `${v}t`}
                  onChange={setChunkLength}
                />
                <div className="space-y-1">
                  <Label className="text-[11px]">Latency</Label>
                  <Select value={latency} onValueChange={setLatency}>
                    <SelectTrigger className="h-7 text-xs">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="balanced" className="text-xs">
                        balanced — ~300ms, for calls
                      </SelectItem>
                      <SelectItem value="normal" className="text-xs">
                        normal — slower, most stable
                      </SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              </div>

              <div className="flex items-center justify-between rounded-md border border-neutral-200 p-2 dark:border-neutral-800">
                <div>
                  <Label className="text-[11px]">Normalize text</Label>
                  <p className="text-[10px] text-muted-foreground">
                    Expands numbers, dates and currency.
                  </p>
                </div>
                <Switch checked={normalize} onCheckedChange={setNormalize} />
              </div>
              <p className="text-[10px] text-muted-foreground">
                &quot;balanced&quot; is the faster mode, despite the name.
              </p>
            </TabsContent>

            <TabsContent
              value="agent"
              className="mt-2 flex min-h-0 flex-1 flex-col gap-2 overflow-hidden"
            >
              <p className="shrink-0 text-[10px] leading-snug text-muted-foreground">
                Markers only reach Fish if the LLM writes them. Paste this into the agent&apos;s
                globalNode prompt and set TTS to Fish Audio with the voice id above.
              </p>
              <Textarea
                readOnly
                value={AGENT_PROMPT_BLOCK}
                className="min-h-0 flex-1 resize-none font-mono text-[10px] leading-snug"
              />
              <Button
                variant="outline"
                size="sm"
                className="h-7 shrink-0"
                onClick={() => void navigator.clipboard.writeText(AGENT_PROMPT_BLOCK)}
              >
                <Copy className="mr-1.5 h-3.5 w-3.5" />
                Copy prompt block
              </Button>
            </TabsContent>
          </Tabs>
        </div>
      </div>

      {/* ================================================= RIGHT: voice browser */}
      <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-md border border-neutral-200 dark:border-neutral-800">
        <Tabs defaultValue="voices" className="flex min-h-0 flex-1 flex-col">
          {/* Consolidated sticky control header */}
          <div className="shrink-0 space-y-2 border-b border-neutral-200 p-3 dark:border-neutral-800">
            <div className="flex items-center gap-2">
              <TabsList className="h-7 p-1">
                <TabsTrigger value="voices" className="px-2.5 py-0.5 text-xs">
                  Voices
                </TabsTrigger>
                <TabsTrigger value="clone" className="px-2.5 py-0.5 text-xs">
                  <Wand2 className="mr-1 h-3 w-3" />
                  Clone
                </TabsTrigger>
              </TabsList>

              <div className="ml-auto flex items-center gap-1 rounded-md border border-neutral-200 bg-muted/40 p-0.5 dark:border-neutral-800">
                <button
                  type="button"
                  onClick={() => setSource("library")}
                  className={`inline-flex items-center gap-1 rounded px-2 py-1 text-[11px] font-semibold transition-colors ${
                    source === "library"
                      ? "bg-background text-foreground shadow-sm"
                      : "text-muted-foreground hover:text-foreground"
                  }`}
                >
                  <Globe2 className="h-3 w-3" />
                  Fish library
                </button>
                <button
                  type="button"
                  onClick={() => setSource("mine")}
                  className={`inline-flex items-center gap-1 rounded px-2 py-1 text-[11px] font-semibold transition-colors ${
                    source === "mine"
                      ? "bg-background text-foreground shadow-sm"
                      : "text-muted-foreground hover:text-foreground"
                  }`}
                >
                  <UserRound className="h-3 w-3" />
                  My voices
                </button>
              </div>
            </div>

            <div className="flex items-center gap-1.5">
              <div className="relative min-w-0 flex-1">
                <Search className="absolute left-2 top-2 h-3.5 w-3.5 text-muted-foreground" />
                <Input
                  value={searchTitle}
                  onChange={(event) => setSearchTitle(event.target.value)}
                  onKeyDown={(event) => event.key === "Enter" && reloadVoices()}
                  placeholder={source === "mine" ? "Search your voices" : "Search Fish library"}
                  className="h-7 pl-7 text-xs"
                />
              </div>

              <Select value={sortBy} onValueChange={setSortBy}>
                <SelectTrigger className="h-7 w-[120px] shrink-0 text-xs">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {SORTS.map((option) => (
                    <SelectItem key={option.value} value={option.value} className="text-xs">
                      {option.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>

              {/* Language + tags collapse into a popover so the header stays two rows */}
              <Popover>
                <PopoverTrigger asChild>
                  <Button size="sm" variant="outline" className="h-7 shrink-0 px-2 text-xs">
                    <SlidersHorizontal className="mr-1 h-3.5 w-3.5" />
                    Filters
                    {activeFilters > 0 && (
                      <Badge variant="secondary" className="ml-1 h-4 px-1 text-[10px]">
                        {activeFilters}
                      </Badge>
                    )}
                  </Button>
                </PopoverTrigger>
                <PopoverContent align="end" className="w-72 space-y-2 p-3">
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-semibold">Filters</span>
                    {(activeFilters > 0 || searchTitle) && (
                      <Button
                        size="sm"
                        variant="ghost"
                        className="h-6 px-2 text-[11px]"
                        onClick={clearFilters}
                      >
                        Clear all
                      </Button>
                    )}
                  </div>

                  <div className="space-y-1">
                    <Label className="text-[11px]">Language</Label>
                    <Select value={language} onValueChange={setLanguage}>
                      <SelectTrigger className="h-7 text-xs">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {LANGUAGES.map((option) => (
                          <SelectItem key={option.value} value={option.value} className="text-xs">
                            {option.label}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>

                  <div className="space-y-1">
                    <Label className="text-[11px]">Tag</Label>
                    <div className="flex flex-wrap gap-1">
                      {TAG_PRESETS.map((preset) => (
                        <button
                          key={preset}
                          type="button"
                          onClick={() => setTag((prev) => (prev === preset ? "" : preset))}
                          className={`rounded-full border px-2 py-0.5 text-[11px] capitalize transition-colors ${
                            tag === preset
                              ? "border-primary bg-primary/10 text-primary"
                              : "border-neutral-200 bg-background text-muted-foreground hover:border-primary/40 hover:text-foreground dark:border-neutral-800"
                          }`}
                        >
                          {preset}
                        </button>
                      ))}
                    </div>
                  </div>
                </PopoverContent>
              </Popover>

              <Button
                size="sm"
                variant="ghost"
                className="h-7 shrink-0 px-2"
                onClick={reloadVoices}
                disabled={loadingVoices}
                title="Refresh"
              >
                <RefreshCw className={`h-3.5 w-3.5 ${loadingVoices ? "animate-spin" : ""}`} />
              </Button>
            </div>
          </div>

          {/* --------------------------------------------------- voice list */}
          <TabsContent value="voices" className="mt-0 flex min-h-0 flex-1 flex-col overflow-hidden">
            {voicesError && (
              <p className="flex shrink-0 items-start gap-1.5 px-3 pt-2 text-xs text-destructive">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                {voicesError}
              </p>
            )}

            {loadingVoices ? (
              <div className="min-h-0 flex-1 space-y-1.5 overflow-y-auto p-3">
                {[0, 1, 2, 3, 4, 5, 6, 7].map((i) => (
                  <div key={i} className="h-[56px] animate-pulse rounded-md bg-muted/60" />
                ))}
              </div>
            ) : voices.length === 0 ? (
              <div className="flex min-h-0 flex-1 items-center justify-center p-6 text-center">
                <p className="text-xs text-muted-foreground">
                  {source === "mine"
                    ? "No voices cloned yet."
                    : "No matches. Try clearing a filter."}
                </p>
              </div>
            ) : (
              <>
                <ul className="min-h-0 flex-1 space-y-1 overflow-y-auto p-2 pr-1">
                  {voices.map((voice) => {
                    const id = voiceId(voice);
                    const active = selectedVoice ? voiceId(selectedVoice) === id : false;
                    const preview = sampleUrl(voice);
                    const isSamplePlaying = samplePlayingId === id;
                    const meta = [voice.description, voice.author?.nickname && `by ${voice.author.nickname}`]
                      .filter(Boolean)
                      .join(" · ");

                    return (
                      <li key={id}>
                        <div
                          className={`group flex items-center gap-2 rounded-md border px-2.5 py-1.5 transition-colors ${
                            active
                              ? "border-primary bg-accent"
                              : "border-neutral-200 hover:border-primary/40 hover:bg-accent/40 dark:border-neutral-800"
                          }`}
                        >
                          <button
                            type="button"
                            onClick={() => setSelectedVoice(voice)}
                            className="min-w-0 flex-1 text-left"
                          >
                            {/* Row 1 — name, badges */}
                            <div className="flex items-center gap-1.5">
                              <span className="truncate text-xs font-semibold">
                                {voice.title || id}
                              </span>
                              {active && <Check className="h-3 w-3 shrink-0 text-primary" />}
                              {(voice.languages || []).slice(0, 2).map((code) => (
                                <Badge
                                  key={code}
                                  variant="secondary"
                                  className="h-4 shrink-0 px-1 text-[9px] uppercase"
                                >
                                  {code}
                                </Badge>
                              ))}
                              {(voice.tags || []).slice(0, 1).map((voiceTag) => (
                                <Badge
                                  key={voiceTag}
                                  variant="outline"
                                  className="h-4 shrink-0 px-1 text-[9px]"
                                >
                                  {voiceTag}
                                </Badge>
                              ))}
                              {voice.state && voice.state !== "trained" && (
                                <Badge variant="outline" className="h-4 shrink-0 px-1 text-[9px]">
                                  {voice.state}
                                </Badge>
                              )}
                            </div>

                            {/* Row 2 — metadata + id */}
                            <div className="flex items-baseline gap-1.5">
                              {meta && (
                                <span className="truncate text-[10px] text-muted-foreground">
                                  {meta}
                                </span>
                              )}
                              <code className="ml-auto shrink-0 font-mono text-[9px] text-muted-foreground/70">
                                {id.slice(0, 8)}…
                              </code>
                            </div>
                          </button>

                          {preview && (
                            <button
                              type="button"
                              title={isSamplePlaying ? "Stop sample" : "Play sample"}
                              onClick={() => toggleSample(voice)}
                              className="flex h-6 w-6 shrink-0 items-center justify-center rounded border border-neutral-200 transition-colors hover:border-primary hover:text-primary dark:border-neutral-800"
                            >
                              {isSamplePlaying ? (
                                <Pause className="h-3 w-3" />
                              ) : (
                                <Play className="ml-px h-3 w-3" />
                              )}
                            </button>
                          )}

                          {source === "mine" && (
                            <div className="flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
                              <button
                                type="button"
                                onClick={() => setRenaming(voice)}
                                className="rounded px-1.5 py-0.5 text-[10px] text-muted-foreground hover:bg-accent hover:text-foreground"
                              >
                                Rename
                              </button>
                              <button
                                type="button"
                                onClick={() => setDeleting(voice)}
                                className="rounded p-1 text-destructive hover:bg-destructive/10"
                              >
                                <Trash2 className="h-3 w-3" />
                              </button>
                            </div>
                          )}
                        </div>
                      </li>
                    );
                  })}

                  {hasMore && (
                    <li className="pt-1">
                      <Button
                        variant="outline"
                        size="sm"
                        className="h-7 w-full text-xs"
                        disabled={loadingMore}
                        onClick={() => void fetchVoices(page + 1, true)}
                      >
                        {loadingMore ? <Loader2 className="mr-1.5 h-3 w-3 animate-spin" /> : null}
                        {loadingMore ? "Loading…" : "Load more"}
                      </Button>
                    </li>
                  )}
                </ul>

                <div className="shrink-0 border-t border-neutral-200 px-3 py-1 text-center text-[10px] text-muted-foreground dark:border-neutral-800">
                  {voices.length} voice{voices.length === 1 ? "" : "s"}
                  {hasMore ? " · more available" : ""}
                </div>
              </>
            )}
          </TabsContent>

          <TabsContent value="clone" className="mt-0 min-h-0 flex-1 overflow-y-auto p-3">
            <CloneVoiceCard
              onCloned={() => {
                setSource("mine");
                reloadVoices();
              }}
            />
          </TabsContent>
        </Tabs>
      </div>

      <RenameDialog
        voice={renaming}
        onClose={() => setRenaming(null)}
        onSubmit={(title) => void submitRename(title)}
      />
      <DeleteDialog
        voice={deleting}
        onClose={() => setDeleting(null)}
        onConfirm={() => void confirmDelete()}
      />
    </div>
  );
}

/* ------------------------------------------------------------------ parts */

function RenameDialog({
  voice,
  onClose,
  onSubmit,
}: {
  voice: FishVoice | null;
  onClose: () => void;
  onSubmit: (title: string) => void;
}) {
  const [title, setTitle] = useState("");

  useEffect(() => {
    if (voice) setTitle(voice.title || "");
  }, [voice]);

  return (
    <Dialog open={!!voice} onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Rename voice</DialogTitle>
          <DialogDescription>
            The id stays the same, so agents already using this voice keep working.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-2">
          <Label className="text-sm">Name</Label>
          <Input
            value={title}
            autoFocus
            onChange={(event) => setTitle(event.target.value)}
            onKeyDown={(event) => event.key === "Enter" && title.trim() && onSubmit(title)}
          />
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button disabled={!title.trim()} onClick={() => onSubmit(title)}>
            Save
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/**
 * Deleting a Fish model is irreversible and silently breaks every agent still
 * pointing at that reference_id, so the id is shown in full and the consequence
 * spelled out rather than hidden behind a generic confirm.
 */
function DeleteDialog({
  voice,
  onClose,
  onConfirm,
}: {
  voice: FishVoice | null;
  onClose: () => void;
  onConfirm: () => void;
}) {
  return (
    <Dialog open={!!voice} onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            Delete &ldquo;{voice?.title || (voice ? voiceId(voice) : "")}&rdquo;?
          </DialogTitle>
          <DialogDescription>
            This cannot be undone. Any agent still configured with this voice id will start failing
            on its next call.
          </DialogDescription>
        </DialogHeader>
        {voice && (
          <code className="block truncate rounded bg-muted px-2 py-1.5 font-mono text-xs">
            {voiceId(voice)}
          </code>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="destructive" onClick={onConfirm}>
            <Trash2 className="mr-2 h-4 w-4" />
            Delete permanently
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** Native range input — Dograh has no Slider component and this needs no dependency. */
function SliderField({
  label,
  value,
  min,
  max,
  step,
  format,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  format: (value: number) => string;
  onChange: (value: number) => void;
}) {
  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between">
        <Label className="text-[11px]">{label}</Label>
        <span className="font-mono text-[10px] text-muted-foreground">{format(value)}</span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
        className="h-1 w-full accent-primary"
      />
    </div>
  );
}

/**
 * Phoneme control. English replaces exactly one word per tag with CMU Arpabet,
 * so the builder is word-in / Arpabet-out rather than a free text box.
 */
function PhonemeBuilder({ onInsert }: { onInsert: (snippet: string) => void }) {
  const [arpabet, setArpabet] = useState("");

  return (
    <div className="space-y-1.5">
      <p className="text-[11px] font-semibold text-muted-foreground">
        Pronunciation ·{" "}
        <a
          href={ARPABET_REFERENCE_URL}
          target="_blank"
          rel="noreferrer"
          className="font-normal underline underline-offset-2"
        >
          Arpabet reference
        </a>
      </p>
      <div className="flex gap-1.5">
        <Input
          value={arpabet}
          onChange={(event) => setArpabet(event.target.value)}
          placeholder="EH1 N JH AH0 N IH1 R"
          className="h-7 font-mono text-xs"
        />
        <Button
          variant="outline"
          size="sm"
          className="h-7 shrink-0 text-xs"
          disabled={!arpabet.trim()}
          onClick={() => {
            onInsert(`${renderPhoneme(arpabet)} `);
            setArpabet("");
          }}
        >
          Insert
        </Button>
      </div>
      <div className="flex flex-wrap gap-1">
        {ARPABET_PRESETS.map((preset) => (
          <button
            key={preset.word}
            type="button"
            title={`${preset.arpabet}${preset.note ? ` — ${preset.note}` : ""}`}
            onClick={() => onInsert(`${renderPhoneme(preset.arpabet)} `)}
            className="rounded border border-neutral-200 bg-background px-2 py-0.5 text-xs leading-5 transition-colors hover:border-primary hover:bg-accent dark:border-neutral-800"
          >
            {preset.word}
          </button>
        ))}
      </div>
      <p className="text-[10px] leading-snug text-muted-foreground">
        Replaces one English word with CMU Arpabet. Stress digits: 1 primary, 2 secondary, 0 none.
      </p>
    </div>
  );
}

function CloneVoiceCard({ onCloned }: { onCloned: () => void }) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [transcript, setTranscript] = useState("");
  const [enhance, setEnhance] = useState(true);
  const [files, setFiles] = useState<File[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<string | null>(null);

  const submit = useCallback(async () => {
    if (!title.trim() || files.length === 0) return;
    setSubmitting(true);
    setError(null);
    setCreated(null);

    const form = new FormData();
    form.set("title", title.trim());
    if (description.trim()) form.set("description", description.trim());
    // Visibility is deliberately not exposed: everything clones private. A
    // sales voice published to Fish's public library is not recoverable.
    form.set("visibility", "private");
    form.set("enhance_audio_quality", enhance ? "true" : "false");
    files.forEach((file) => form.append("voices", file, file.name));
    if (transcript.trim()) form.append("texts", transcript.trim());

    try {
      const response = await fetch("/api/fish/voices", { method: "POST", body: form });
      if (!response.ok) {
        setError(await readError(response, "Cloning failed."));
        return;
      }
      const body = await response.json();
      setCreated(body?._id || body?.id || "created");
      setTitle("");
      setDescription("");
      setTranscript("");
      setFiles([]);
      onCloned();
    } catch {
      setError("Could not reach Fish Audio.");
    } finally {
      setSubmitting(false);
    }
  }, [title, description, transcript, enhance, files, onCloned]);

  return (
    <div className="space-y-2.5">
      <p className="text-[11px] leading-snug text-muted-foreground">
        Clean, mono, single-speaker audio. 10s works; a minute or two is better. No music, no
        reverb, no overlapping voices. Everything clones private.
      </p>

      <div className="space-y-1">
        <Label className="text-[11px]">Name</Label>
        <Input
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          placeholder="Alex — outbound"
          className="h-7 text-xs"
        />
      </div>

      <div className="space-y-1">
        <Label className="text-[11px]">Description</Label>
        <Input
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          placeholder="Optional"
          className="h-7 text-xs"
        />
      </div>

      <div className="space-y-1">
        <Label className="text-[11px]">Samples</Label>
        <label className="flex cursor-pointer flex-col items-center justify-center gap-1 rounded-md border border-dashed border-neutral-200 py-4 text-xs text-muted-foreground transition-colors hover:border-primary hover:text-foreground dark:border-neutral-800">
          <Upload className="h-3.5 w-3.5" />
          <span>
            {files.length > 0
              ? `${files.length} file${files.length === 1 ? "" : "s"} selected`
              : "Choose .wav .mp3 .m4a .opus"}
          </span>
          <input
            type="file"
            multiple
            accept=".wav,.mp3,.m4a,.opus,audio/*"
            className="hidden"
            onChange={(event) => setFiles(Array.from(event.target.files || []))}
          />
        </label>
        {files.length > 0 && (
          <ul className="space-y-0.5">
            {files.map((file) => (
              <li key={file.name} className="truncate font-mono text-[10px] text-muted-foreground">
                {file.name}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="space-y-1">
        <Label className="text-[11px]">Transcript (optional)</Label>
        <Textarea
          value={transcript}
          onChange={(event) => setTranscript(event.target.value)}
          rows={2}
          placeholder="The exact words spoken in the sample — sharpens pronunciation."
          className="resize-none text-xs"
        />
      </div>

      <div className="flex items-center justify-between rounded-md border border-neutral-200 p-2 dark:border-neutral-800">
        <div>
          <Label className="text-[11px]">Enhance audio</Label>
          <p className="text-[10px] text-muted-foreground">
            Denoise and level. Off only for studio-grade audio.
          </p>
        </div>
        <Switch checked={enhance} onCheckedChange={setEnhance} />
      </div>

      {error && <p className="text-[11px] text-destructive">{error}</p>}
      {created && (
        <p className="text-[11px] text-emerald-600">
          Cloned. New voice id: <code className="font-mono">{created}</code>
        </p>
      )}

      <Button
        className="h-8 w-full text-xs"
        onClick={submit}
        disabled={submitting || !title.trim() || files.length === 0}
      >
        {submitting ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : null}
        {submitting ? "Cloning…" : "Clone voice"}
      </Button>
    </div>
  );
}
