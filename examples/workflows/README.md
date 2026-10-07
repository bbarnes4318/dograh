# Example workflows

Import any `.json` file here with **Upload Workflow** in the Dograh UI. Upload
only imports the graph (`name` + `workflow_definition`). Tools, settings, and
URLs have to be set by hand after import, as described below.

## `fe_outbound_alex_4_node.json`: Final Expense outbound (Alex)

An outbound burial-coverage agent. It confirms the prospect, asks a few basic
questions, and live-transfers to a licensed agent.

```
                     ┌──────────── Global (persona, voice rules, objections, callbacks, never-say)
                     ▼ prepended to every agent node
Opening (startCall) ──user_has_interest_or_acknowledges──▶ Discovery (agentNode)
   │  callback_requested / user_declined_or_unavailable        │  qualified_ready_for_agent
   ▼                                                           ▼
 Exit (endCall) ◀──callback_requested / user_failed_qualification_or_declined── Live Transfer (agentNode)
   ▲                                                                              │
   └────────────────────── callback_requested / user_declined_transfer ───────────┘
```

Dograh has no separate "Set Condition" node. Each edge's `label` becomes the
LLM's transition function, and its `condition` becomes that function's
description.

### After import

1. **Transfer tools.** Create two Transfer Call tools named `W` (age eighty-five
   or under) and `O` (over eighty-five), then attach both to **Live Transfer**.
   The prompt refers to them by name. Use the custom pre-transfer message
   "Perfect, stay right with me while I get a licensed agent on the line." and a
   30 s ring timeout.
2. **Agent screen-pop (optional).** Set W and O to *Dynamic (HTTP resolver)*
   with preset parameters `route`, `first_name` / `state` / `phone` from
   `{{initial_context.*}}`, and the Discovery fields from
   `{{gathered_context.*}}`, all marked not required. The resolver must return
   `{"transfer_context": {"destination": "+1..."}}`. A non-2xx response or a
   missing destination counts as a failed transfer, and Alex moves to the
   callback line.
3. **Post-Call Webhook.** Paste your URL and turn the node on. It sends
   `dnc_request`. Route `dnc_request = true` to your suppression list, so that
   "I'll take your number off our list" is actually true.
4. **Workflow Settings** (these aren't part of the import file):

| Setting | Value |
| --- | --- |
| Turn start strategy | `min_words`, 3 |
| Buffer muted speech / Speak during transition | On (default) |
| Max call duration | 420 s |
| Call hygiene → machine keyword hangup | On. Ends voicemail calls without leaving a message |
| Call hygiene → screener response | "This is Alex with Family Life, following up on a burial coverage request." |
| Idle behavior nudges | "You still with me?" → after 5 s, end call with "Sounds like I lost you. I'll try you another time." |

### Compliance notes

- Alex identifies as a virtual assistant when asked and never claims to be
  human. The FCC treats AI voices as artificial voices under the TCPA.
- Nothing suggests the coverage is a state, government, or Medicare program.
- Alex never repeats or records health conditions. Only a `health_flag`
  boolean is captured.
- **Open item:** TCPA artificial-voice rules require the call to state a
  callback number for the business. Add it to the Exit node's closing lines
  before going live.

## `fe_outbound_4_node_v2.json`: Final Expense outbound, 4-node v2

Same 4-node shape, built around one rule per node: Opening doesn't qualify,
Discovery doesn't sell, Transfer doesn't discover, and Exit doesn't recover.

```
Opening ──engaged_coverage_answered──▶ Discovery ──qualified_or_requests_agent──▶ Live Transfer
   │ requests_agent ─────────────────────────────────────────────────────────────▲      │
   │ wrong_number_dnc_decline        │ ineligible_decline_dnc     transfer_not_completed │
   └────────────────────────────────▶ Exit ◀──────────────────────────────────────────────┘
```

Discovery asks three things in order: who handles arrangements, age, then
living situation. Ages fifty through eighty-five go to transfer, and anything
outside that range exits. Nursing home or hospice residence is captured but
does not disqualify.

### After import

1. Create the transfer tool from `fe_outbound_4_node_v2.transfer_tool.json`.
   Change the resolver `url` to your routing endpoint first. Keep the name
   **Transfer Final Expense**, because the prompt calls it as
   `transfer_final_expense`.
2. Attach that tool to the **LIVE TRANSFER** node.
3. Your resolver returns `{"transfer_context": {"destination": "+1..."}}`,
   or a `PJSIP/...` endpoint for Asterisk.
4. Turn on the Post-Call Webhook and route `dnc_request = true` to
   suppression. The DNC closing line promises this.

## `final_expense_alex_live_transfer_corrected.json`: Final Expense - Alex (Live Transfer)

Replacement graph for the production **Final Expense - Alex (Live Transfer)**
agent. Nodes: Global, Greeting, Opening & Qualify, Wrap Up, and Transfer Failed
- Callback. The file's `name` is "... - Corrected Conversion Test"; rename it to
`Final Expense - Alex (Live Transfer)` after import.

Nodes reference two tool UUIDs that must exist in the target instance:
`71f1d90f-c8af-4326-9a70-5eca2be629c8` (all nodes except Global and Wrap Up;
presumably end_call) and `2c883edd-8ce6-42ae-99b6-b8914c6a071f` (Opening &
Qualify; presumably transfer_call). Re-attach the tools by hand if they differ.
Upload imports the graph only; voice, call hygiene and webhook settings stay as
configured on the agent.
