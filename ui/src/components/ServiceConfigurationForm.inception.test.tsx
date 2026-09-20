/**
 * Inception in the BYOK LLM tab.
 *
 * The BYOK form is schema-driven: it renders whatever
 * /api/v1/user/configurations/defaults returns. The fixture here is a snapshot
 * of that payload taken from the Python registry (every LLM provider verbatim;
 * the other sections trimmed to their default provider), so these assertions
 * exercise the same provider list and field metadata the real page gets.
 * Refresh it with `python -m scripts.dump_byok_schema_fixture`.
 *
 * Assertions read the hidden native <select> that Radix renders for form
 * submission — its value and options are unambiguous, unlike the visible
 * trigger text which Radix duplicates.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeAll, describe, expect, it, vi } from "vitest";

import defaultsFixture from "./__fixtures__/byok-llm-schemas.json";
import {
    type ServiceConfigurationDefaults,
    ServiceConfigurationForm,
} from "./ServiceConfigurationForm";

vi.mock("@/context/UserConfigContext", () => ({
    useUserConfig: () => ({ userConfig: null }),
}));

// Radix's Select relies on ResizeObserver, pointer capture and scrollIntoView,
// none of which jsdom implements. Stub them so the dropdown can be opened.
beforeAll(() => {
    globalThis.ResizeObserver = class {
        observe() { }
        unobserve() { }
        disconnect() { }
    } as unknown as typeof ResizeObserver;
    Element.prototype.hasPointerCapture = () => false;
    Element.prototype.setPointerCapture = () => { };
    Element.prototype.releasePointerCapture = () => { };
    Element.prototype.scrollIntoView = () => { };
});

const defaults = defaultsFixture as unknown as ServiceConfigurationDefaults;

const SAVED_INCEPTION_CONFIG = {
    provider: "inception",
    model: "mercury-2.5",
    api_key: "sk_saved",
    base_url: "https://api.inceptionlabs.ai/v1",
    reasoning_effort: "low",
};

function renderByokForm(initialConfig: Record<string, unknown> | null = null) {
    const onSave = vi.fn().mockResolvedValue(undefined);
    render(
        <ServiceConfigurationForm
            mode="global"
            forceRealtime={false}
            configurationDefaults={defaults}
            initialConfig={initialConfig}
            onSave={onSave}
        />,
    );
    return { onSave };
}

/** The hidden native select that offers `optionValue`, if one is rendered. */
function selectOffering(optionValue: string): HTMLSelectElement | undefined {
    return Array.from(document.querySelectorAll("select")).find((el) =>
        Array.from(el.options).some((option) => option.value === optionValue),
    );
}

function providerSelect(): HTMLSelectElement {
    const select = selectOffering("inception");
    if (!select) throw new Error("LLM provider select not rendered");
    return select;
}

function reasoningEffortSelect(): HTMLSelectElement | undefined {
    return selectOffering("instant");
}

/** Open a Radix Select popup (pointerdown, not click). */
function openSelect(trigger: Element) {
    fireEvent.pointerDown(trigger, {
        button: 0,
        ctrlKey: false,
        pointerType: "mouse",
    });
}

async function chooseInceptionProvider() {
    const trigger = document.querySelector('button[data-slot="select-trigger"]');
    if (!trigger) throw new Error("provider select trigger not rendered");
    openSelect(trigger);
    const listbox = await screen.findByRole("listbox");
    fireEvent.click(within(listbox).getByRole("option", { name: "Inception" }));
}

describe("BYOK LLM tab — Inception", () => {
    it("offers Inception in the LLM provider list", async () => {
        renderByokForm();
        await waitFor(() => expect(providerSelect()).toBeTruthy());

        const options = Array.from(providerSelect().options).map((o) => o.value);
        expect(options).toContain("inception");
    });

    it("leaves the organization default LLM provider on OpenAI", async () => {
        renderByokForm();
        await waitFor(() => expect(providerSelect()).toBeTruthy());

        expect(defaults.default_providers.llm).toBe("openai");
        expect(providerSelect().value).toBe("openai");
    });

    it("does not render reasoning effort for a non-Inception provider", async () => {
        renderByokForm();
        await waitFor(() => expect(providerSelect().value).toBe("openai"));

        expect(reasoningEffortSelect()).toBeUndefined();
        expect(screen.queryByText(/reasoning effort/i)).toBeNull();
    });

    it("reveals reasoning effort with 'low' preselected when Inception is chosen", async () => {
        renderByokForm();
        await waitFor(() => expect(providerSelect().value).toBe("openai"));

        await chooseInceptionProvider();

        await waitFor(() => expect(providerSelect().value).toBe("inception"));
        expect(screen.getAllByText(/reasoning effort/i).length).toBeGreaterThan(0);
        expect(reasoningEffortSelect()?.value).toBe("low");
        expect(selectOffering("mercury-2.5")?.value).toBe("mercury-2.5");
        expect(
            screen.getByDisplayValue("https://api.inceptionlabs.ai/v1"),
        ).toBeTruthy();
    });

    it("rehydrates a saved Inception config, so model and effort survive a reload", async () => {
        renderByokForm({ llm: SAVED_INCEPTION_CONFIG });

        await waitFor(() => expect(providerSelect().value).toBe("inception"));
        expect(selectOffering("mercury-2.5")?.value).toBe("mercury-2.5");
        expect(reasoningEffortSelect()?.value).toBe("low");
        expect(
            screen.getByDisplayValue("https://api.inceptionlabs.ai/v1"),
        ).toBeTruthy();
    });

    it("submits reasoning_effort alongside the rest of the LLM config", async () => {
        const { onSave } = renderByokForm({
            llm: { ...SAVED_INCEPTION_CONFIG, reasoning_effort: "medium" },
        });

        await waitFor(() => expect(providerSelect().value).toBe("inception"));
        fireEvent.click(screen.getByRole("button", { name: /save configuration/i }));

        await waitFor(() => expect(onSave).toHaveBeenCalled());
        const saved = onSave.mock.calls[0][0] as Record<string, unknown>;
        expect(saved.llm).toMatchObject({
            provider: "inception",
            model: "mercury-2.5",
            reasoning_effort: "medium",
            base_url: "https://api.inceptionlabs.ai/v1",
        });
    });

    it("carries reasoning_effort through an agent-level LLM override", async () => {
        const onSave = vi.fn().mockResolvedValue(undefined);
        render(
            <ServiceConfigurationForm
                mode="override"
                forceRealtime={false}
                configurationDefaults={defaults}
                initialConfig={{
                    llm: { provider: "openai", model: "gpt-4.1", api_key: "sk_org" },
                }}
                currentOverrides={{
                    llm: {
                        provider: "inception",
                        model: "mercury-2.5",
                        reasoning_effort: "high",
                    },
                }}
                onSave={onSave}
            />,
        );

        // An existing override arrives with its toggle already on.
        const toggle = await screen.findByRole("switch", { name: /override llm/i });
        await waitFor(() =>
            expect(toggle.getAttribute("data-state")).toBe("checked"),
        );

        await waitFor(() => expect(providerSelect().value).toBe("inception"));
        expect(reasoningEffortSelect()?.value).toBe("high");

        fireEvent.click(screen.getByRole("button", { name: /save configuration/i }));
        await waitFor(() => expect(onSave).toHaveBeenCalled());
        // Override mode posts model_overrides, and only for the services whose
        // toggle is on — the org-level config is left untouched.
        const saved = onSave.mock.calls[0][0] as {
            model_overrides: Record<string, unknown>;
        };
        expect(saved.model_overrides.llm).toMatchObject({
            provider: "inception",
            model: "mercury-2.5",
            reasoning_effort: "high",
        });
        expect(Object.keys(saved.model_overrides)).toEqual(["llm"]);
    });
});
