"use client";

import {
    Select,
    SelectContent,
    SelectGroup,
    SelectItem,
    SelectLabel,
    SelectTrigger,
    SelectValue,
} from "@/components/ui/select";

export interface VoiceCatalogEntry {
    value: string;
    label: string;
    description: string;
    group: string;
}

interface OpenAIVoiceSelectProps {
    catalog: VoiceCatalogEntry[];
    value: string;
    onChange: (voice: string) => void;
}

/**
 * Grouped voice selector driven by the backend's single voice catalog
 * (`voice_catalog` in the config JSON schema). Group and entry order come from
 * the backend, so recommended voices stay first. No audio preview is offered:
 * there is no preview endpoint, and we do not fake one.
 */
export function OpenAIVoiceSelect({ catalog, value, onChange }: OpenAIVoiceSelectProps) {
    const groups: { name: string; voices: VoiceCatalogEntry[] }[] = [];
    for (const voice of catalog) {
        let group = groups.find((g) => g.name === voice.group);
        if (!group) {
            group = { name: voice.group, voices: [] };
            groups.push(group);
        }
        group.voices.push(voice);
    }
    const selected = catalog.find((v) => v.value === value);

    return (
        <Select
            value={value}
            onValueChange={(next) => {
                if (next) onChange(next);
            }}
        >
            <SelectTrigger className="w-full">
                <SelectValue placeholder="Select voice">
                    {selected ? `${selected.label} — ${selected.description}` : value}
                </SelectValue>
            </SelectTrigger>
            <SelectContent>
                {groups.map((group) => (
                    <SelectGroup key={group.name}>
                        <SelectLabel>{group.name}</SelectLabel>
                        {group.voices.map((voice) => (
                            <SelectItem key={voice.value} value={voice.value}>
                                <span className="font-medium">{voice.label}</span>
                                <span className="ml-2 text-xs text-muted-foreground">
                                    {voice.description}
                                </span>
                            </SelectItem>
                        ))}
                    </SelectGroup>
                ))}
            </SelectContent>
        </Select>
    );
}
