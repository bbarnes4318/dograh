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
