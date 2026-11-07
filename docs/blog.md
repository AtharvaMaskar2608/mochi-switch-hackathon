# How I built an AI chief of staff on Switch

I wanted an assistant that sits in the team chat and takes the questions that pull developers out of deep work, like "where is the refunds data?", "what did we ship this week?" and "can someone fix this bug?". It had to know who is asking, keep quiet while people are focused, and never merge or deploy anything without a human saying yes.

I built it on Switch in a day and called it **Chief**. This post is for someone who has never used Switch. It covers the parts of Switch I used, with real code from the repo, in the order you would need them.

## What Switch gives you

Switch connects AI agents to team chat: Slack, or the Mattermost it ships with. The ideas you need:

- **Rooms**: chat channels that people and agents share. Rooms can sit in a **room group** (I used one group per company).
- **Agents**: Claude Code sessions that Switch runs for you, or your own processes that speak Switch's **agent protocol**.
- **Roles**: named jobs in a room (`data-steward`, `reviewer`, `on-call`). You address a role, not a bot. An **exclusive** role has one holder at a time.
- **Addressing policy**: who may make an agent act. `owner-agents` means only its owner's side can drive it. `anyone_in: [rooms]` means anyone in those rooms can.
- **Templates**: YAML that creates rooms, roles, agents and a kickoff message in one go.
- **Room documents**: text attached to a room that every agent in it can read.
- **Approval cards**: Switch pauses an agent's risky tool call until its owner approves.

Chief uses every one of these. The trick to the design was to put as little as possible in my own code and push the rest into Switch.

## Step 1: run Switch locally

You need Docker, `just` and `uv`. In a Switch checkout:

```bash
just init-env
# add to .env:
FEATURE_FLAGS_ENABLED=agent_management,service_connections
uv sync && just gateway-install && just up && just migrate && just run
just gateway-dev      # in a second shell
```

`curl localhost:8000/docs` should answer, and Mattermost runs on `:8065`. The feature flags matter: without `agent_management`, templates cannot create agents.

## Step 2: a custom agent that sees every message

The managed Claude Code agents only wake up when someone @-mentions them. A chief of staff has to notice things nobody asked it about, so Chief is a custom agent. It registers once with an **always-on** profile (this is `chief/switch_client.py`):

```python
PROFILE = {
    "connection_model": "always_on",
    "message_exchange": True,
    "pre_invocation_mediation": [],
    "post_invocation_mediation": [],
    "event_reporting": [],
    "task_protocol": {"can_delegate": False, "can_accept": False},
}

r = httpx.post(
    f"{base_url.rstrip('/')}/agents",
    headers={"Authorization": f"Bearer {registration_token}"},
    json={"name": name, "description": description, "connector_type": "e2e",
          "integration_profile": PROFILE},
    timeout=120,  # registration provisions chat bot accounts; slow on a cold server
)
```

Then it opens a server-sent event stream. The one parameter that makes Chief proactive is `filter=all`: you get every message in your rooms, not only the ones addressed to you.

```python
params = {"connection_id": self.connection_id, "scope": "all", "filter": "all"}
with self.http.stream("GET", f"{self.base}/events", params=params, headers=headers) as resp:
    ...
```

Things I learned the hard way:

- **Heartbeat.** Post to `/connection/beat` with your cursor at the interval the `connection_state` frame gives you. A 404 or 409 means the connection is gone, so reconnect with `Last-Event-ID`.
- **Enter a room before acting in it.** Operations run against the room your connection has claimed, so call `connect_to_room` first. Its result also lists the room's participants and documents.
- **Gaps.** If the server sends a `gap` event, you missed messages. Re-read the room with `read_context` before you act on the next one.
- **A literal `@name` in your output addresses that agent.** Every piece of text Chief writes passes through a sanitizer that swaps `@` for a full-width `＠`. Otherwise a quoted name can start an agent by accident.

Acting is just operations: `POST /agents/{id}/ops/{name}`. Chief only uses a few:

```python
self.op("post_message", body=body, thread_id=thread_id)
self.op("send_targeted_message", body=body, target_names=names,
        target_roles=roles, thread_id=thread_id)
self.op("read_context", limit=limit)
self.op("create_room_from_yaml", yaml=yaml, inputs=inputs)
self.op("create_room_document", name=name, description=name,
        instructions="Maintained by Chief.", content=content)
```

`target_roles` is the important one. In most work rooms Chief never names a bot. It addresses `release-scribe` or `reviewer`, and whoever holds that role in the room picks it up. The one exception is the data question: the `data-steward` role has no holder until the data agent takes it at kickoff, so Chief targets that agent by name (`mochi-data`, or `standin-data` on the judge path).

Switch decides whether a message is addressed to you from its @-mentions. A message dictated by voice ("Hey chief, …") has none, so Chief also counts a person's message that starts with "chief" or "hey chief" as addressed to it.

## Step 3: the org as a template

The whole demo company, Brightcart, is one v2 template, `org/brightcart-org.yaml`. A v2 template declares `resources`: a group, agents and rooms. A cut-down excerpt:

```yaml
version: 2
resources:
  brightcart:
    type: group
    name: brightcart
  chief:
    type: agent
    name: chief
    allow_existing: true  # Chief is a custom always_on agent that already exists
    addressing:
      anyone_in: [product, leadership, eng, chief_ops]
  intern_meera:
    type: agent
    name: intern-meera
    directory: "{repo_dir}/agents/intern-meera"
    connections: [github]
    auto_mode: false  # approval cards appear
    addressing: owner-agents
  eng:
    type: room
    group: brightcart
    name: eng
    agents: [chief, reviewer, sre, intern_meera, intern_dev]
    users: [ravi, meera, dev]
    roles:
      - name: on-call
        exclusive: true
        instructions: Takes the first look at incidents and alerts. One holder at a time.
      - name: release-owner
        exclusive: true
        instructions: Approves a release before the deployer dry run. One holder at a time.
```

Each managed agent is a Claude Code session in its own directory. The directory holds a `CLAUDE.md` and, for the data agent, a `.mcp.json` that points at Mochi. Every agent's `CLAUDE.md` starts with the same rule: "When you first join a room that defines the role `X`, call Switch's `assume_role` tool with `X`." Without it, the agent sits in the room and never answers its role.

## Step 4: every process is a template

This was the best decision in the project. Each repeatable process is a small v1 template in `workflows/`. The template is the process: who is in the room, which roles they hold, what order they go in, and when it ends. Here is the data question, unabridged:

```yaml
# Data question: the data-steward answers with a table plus a Mochi artifact link.
# Chief's open_work_room loads this and passes inputs {brief, asker}.
version: 1
params:
  brief:
    type: string
    multiline: true
    label: Question
    description: The data question, with the time range and the token (PM or DEV) to use.
  asker:
    type: user
    label: Asker
  data_agent:
    type: agent
    label: Data agent
    description: The agent that answers data questions (mochi-data, or standin-data on the judge path).
    default: mochi-data
room:
  name: work-data-question-{$timestamp}
  description: A data question for the data-steward.
  channel_type: channel_private
  agents: ["{data_agent}", chief]
  users: ["{asker}"]
  roles:
    - name: data-steward
      instructions: Answer with a table and a Mochi artifact link, then post DONE.
  instructions: |
    Data question from {asker}.

    Brief:
    {brief}

    Steps:
    1. The data-steward ({data_agent}) answers in this thread: one line, a table, the Mochi artifact link, and the query.
    2. The data-steward posts DONE on its own line. That ends the work. It does not @ anyone.
    3. Chief then restyles the answer for {asker} in the thread the question came from.
kickoff: |
  @{data_agent} take the data-steward role. Read the brief in the room instructions and answer it as your CLAUDE.md says. Post DONE when finished.
```

When Chief decides a message is a data question, it does three things. It writes a brief with Claude (who asked, why, what they already know, the format they like). It calls `create_room_from_yaml` with `{brief, asker, data_agent}`. Then it addresses the data agent in the new room. The specialist never sees the chat history of other rooms. It gets one self-contained brief in the room instructions.

When the room's lead posts its answer (a `DONE`, a table, or a long reply), Chief restyles it for the asker and posts it once in the thread the question came from. The asker never has to open the work room.

The other templates use the same pattern:

- `shipped-digest`: `ship` writes from GitHub. If the brief mentions a video, Chief adds an explicit hand-off, and `ship` passes the digest to `designer` for a one-minute video.
- `bug-fix-review`: built on Switch's red-blue workroom example. The intern fixes, the reviewer pushes back, and only the asker ends the loop by asking to merge. The intern merges only when Chief posts `merge approved by <developer>`, after that developer's approval.
- `release-train`: reviewer, then the security gate, then release notes with `services: N` and `migrations: M` lines, then `ship` addresses `@chief`. Chief asks the release owner to approve (the second approval: the first came before the room opened), and then posts the dry-run line in the room and in the requesting thread: "would deploy v1.4.2 to staging: 3 services, 1 migration".

Switch's task protocol for delegation isn't finished yet, so each template's instructions say which role to address next. That turned out to be a fine stand-in.

## Step 5: deciding, cheaply

Chief sees every message, so the per-message decision has to be cheap and fast. I used TypeSafe's Jev. It is one `system_one` call per message, which asks four questions together and returns typed answers:

- `for_me` (yes/no probability)
- `rung` (choice)
- `template` plus `fits` (choice and yes/no)
- `urgency` (score)

Jev is optional, though preferred. Without `TYPESAFE_API_KEY`, Chief asks `claude-haiku-5-5` the same four questions and parses one JSON answer, so only `ANTHROPIC_API_KEY` is required. Either gate only ever *suggests*. Code decides what is allowed, and the same ceiling applies to both.

There is one exception to "one call per message". When someone addresses Chief with several requests at once ("what shipped, make it a video, and why did refunds jump?"), Claude splits the message into at most 3 parts, and each part gets its own gate call. Chief replies "On it, 2 jobs: …" and runs each one. A message that isn't addressed to Chief always gets exactly one call.

## Step 6: guardrails in code, not in the prompt

The trust ladder has four rungs: `silent`, `offer`, `act` and `ask_first`. The ceiling is a hard-coded list, and a model answer can lower a rung but never lift it. This is all of `final_rung` in `chief/ladder.py`:

```python
HARD_ASK_FIRST = frozenset({"merge", "deploy", "external_message", "interrupt_focus", "create_agent"})
_ORDER: list[Rung] = ["silent", "offer", "act", "ask_first"]


def final_rung(d: Decision, action: str, sender: Profile) -> Rung:
    if action in HARD_ASK_FIRST:
        return "ask_first"
    if sender.role == "unknown":  # unlinked sender: never above offer, even if preapproved
        return min(d.rung, "offer", key=_ORDER.index)
    if action in sender.preapproved and d.rung == "offer":
        return "act"
    return d.rung
```

The approver is chosen in code too. A merge goes to the person who asked for the work room, a deploy to the release owner, and creating agents to the org admin. Chief posts a one-time 4-character code and only accepts `approve CODE` from that person.

Below the hard list there is a softer step. When Chief isn't sure, it offers in one line and names the workflow it would run. If the same person replies "yes" in that thread within 30 minutes, Chief runs it. A yes can never stand in for an approval: an offer never names a hard-listed action.

Lean on what Switch already enforces. Interns run with `auto_mode: false`, so Switch shows its own approval cards for their risky tool calls. The addressing policy stops anyone outside the owner's side from driving a specialist.

Other small rules make Chief pleasant to have around:

- At most 3 unprompted posts per room per hour.
- `@chief quiet` silences it in a room.
- A ping to someone in focus time that can wait (below "within the hour") is held, even if it was chat between people and not for Chief. Chief tells the asker in their thread, "Meera is heads-down until 15:00; I'll pass this on", and adds anything memory already knows. When her block ends (or on the `evening` tick (`@chief tick evening`)), she gets one batched brief per room.

## Step 7: memory people can read

Chief's memory is Markdown in git, one item per file, so a person can open it, edit it or delete it. Here is a real one from `memory/team/items/`:

```markdown
---
id: m_0a11e001
tier: team
owner: team
kind: fact
status: active
source: {room: '#eng', date: '2026-11-07', by: person}
added_at: '2026-11-07T09:00:00'
last_used_at: '2026-11-07T09:00:00'
use_count: 0
version: 1
supersedes: null
edited_by_person: true
---
Owners: refunds code is meera; payments is dev; data questions go to ravi via Mochi.
```

Profiles live next to the items. Ravi's says `style: tables and definitions` and `preapproved: [mochi_read]`, and Meera's says `focus: ["10:00-15:00"]`. Those lines are why the same answer comes back as a table for Ravi and as one line for Asha.

A nightly **dreamer** reads each room with `read_context` and pulls out durable facts and preferences. It dedups them against existing items, tidies each person's memory, and proposes team knowledge when three or more people have learned the same lesson. It commits `memory/` and publishes the team items into each room as a **room document** called "Chief: team knowledge", so every agent in that room can read it. One rule: **person edits win**. The dreamer never touches an item marked `edited_by_person`. I ported its prompts from the memory system in Mochi, my data product. They were already tuned for "what is worth remembering", and that is the hard part.

## Step 8: bring your own agents

Not every agent has to be Claude Code. `connectors/base.py` is about 90 lines. It registers a custom agent, listens for messages addressed to it, runs a handler in a thread (posting "working on it" if the job is slow), and replies in the thread, by role if needed. Three connectors use it:

- `standin_data`: a SQLite stand-in for Mochi, so anyone can reproduce the demo. It enforces read-only SELECTs, and an authorizer masks the PII columns.
- `security`: runs a scanner and gates on severity in code. It posts `PASSED` on to the release scribe, or `BLOCKED`, which stops the train in the room. (It sends findings to an intern only when the message names an `owner:`.)
- `designer`: bridges to a video and design agent over HTTP. It waits for the update that names an output file, and posts the file as a local path.

## What I would tell a first-time Switch user

1. **Start with templates.** If a process has steps and roles, write it as a template before you write any code.
2. **Address roles, not names.** It is what lets you swap the agent behind a job.
3. **Use `filter=all` only for one agent**, the one whose job is noticing. Everything else should wait to be addressed.
4. **Put every brief in the room instructions.** Agents arrive with no context, and the instructions are the one place they all read.
5. **Keep authority in code and in Switch's own policies.** Let the model choose a path, never the permission.

The code is in this repo. `scripts/up.sh` brings up Switch (pinned to the commit I tested) with Mattermost and the Brightcart org, then starts Chief and the stand-in data agent. The other specialists need a Switch Console machine, so on a laptop they are skipped. Only `ANTHROPIC_API_KEY` is required; add `TYPESAFE_API_KEY` to gate with Jev.
