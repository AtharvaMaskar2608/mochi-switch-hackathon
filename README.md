# Chief: an AI chief of staff for your team chat, built on Switch

Developers lose hours to interruptions whose real cost is rebuilding context: "where is this data?", "what did we ship?", "can you look at this bug?".
Answering takes a minute. Working out who is asking, why, and how much detail they want takes the rest.
Chief watches the team's rooms and takes those questions itself. It knows the people, holds non-urgent pings during focus time, and asks a human before it does anything consequential.

Chief is one Python process on Switch's agent protocol. It answers in Slack or Mattermost, and it only acts through Switch operations. The org chart and every repeatable process are Switch templates in git. Chief picks the template and Switch runs it.

---

## How Switch is used

Without Switch, Chief has no rooms, no roles to address, no templates to run, no addressing policy, and no specialists to hand work to. Every feature below is a Switch feature that Chief drives.

| Chief feature | Switch feature it relies on | Where |
|---|---|---|
| The whole org is code: a group, four rooms, the standing agents and their roles | v2 **room templates** with `resources` (group, agent, room) | [`org/brightcart-org.yaml`](org/brightcart-org.yaml) |
| Each repeatable process is a template: data question, shipped digest, bug fix + review, release train | v1 **room templates** with `params`, `roles`, `instructions`, `kickoff` | [`workflows/`](workflows/) |
| Hand work to "whoever is the data steward", not to a named bot | **roles** + `send_targeted_message(target_roles=[...])` | `chief/main.py` (`LEAD_ROLE`), `chief/switch_client.py` (`target`) |
| One on-call, one release owner at a time | **exclusive roles** (`exclusive: true`) | `org/brightcart-org.yaml` (`on-call`, `release-owner`) |
| The company's rooms live under one heading | **room groups** (`type: group`) | `org/brightcart-org.yaml` (`brightcart`) |
| Anyone in a team room can ask Chief; only owners can drive specialists | **addressing policy**: Chief `anyone_in: [product, leadership, eng, chief_ops]`, specialists and interns `owner-agents` | `org/brightcart-org.yaml` |
| An intern pauses for its developer before risky tool calls | **approval cards**: interns run with `auto_mode: false` | `org/brightcart-org.yaml`, `agents/intern-*/CLAUDE.md` |
| Chief steps in without being @-mentioned | **agent protocol**, `connection_model: always_on`, event stream with `filter=all` | `chief/switch_client.py` (`PROFILE`, `_run`) |
| Team knowledge every agent in a room can read | **room documents** (`create_room_document` / `update_room_document`) | `chief/memory/dreamer.py` (`run_nightly`), `chief/switch_client.py` (`room_doc`) |
| A question opens its own private work room, with Chief's brief as the room instructions | **`create_room_from_yaml`** with `inputs` (`run_template` is wired in the client too) | `chief/main.py` (`open_work_room`), `chief/factory.py` |
| Bring existing tools (a SQLite data agent, a security scanner, a video agent) into rooms as teammates | **custom-agent protocol**: register, stream, reply in thread | `connectors/base.py` (`run_connector`) |
| The context after a dropped connection | `read_context` after a `gap` event | `chief/switch_client.py`, `chief/main.py` (`step`) |

## Architecture

```mermaid
flowchart TB
  phone["Phone (Wispr Flow keyboard)<br/>or laptop"] --> chat["Slack (demo) / Mattermost (judge path)"]
  chat <--> switch

  subgraph switch["SWITCH"]
    rooms["rooms + room group<br/>roles (exclusive)<br/>addressing policy<br/>room documents"]
    tpl["templates<br/>org/ + workflows/"]
  end

  chief["Chief (Python, always_on)<br/>filter=all stream"] <-->|"agent protocol:<br/>post_message, send_targeted_message,<br/>create_room_from_yaml, room documents"| switch
  chief -->|"1 call per message"| jev["TypeSafe Jev<br/>(gate)"]
  chief -->|"briefs, restyles, offers"| claude["Claude API"]
  chief <-->|"read / write"| mem[("memory/<br/>Markdown in git")]
  dreamer["dreamer (nightly)"] --> mem
  dreamer -->|"publish 'Team knowledge'"| switch

  switch <--> mochi["mochi-data<br/>(Claude Code + Mochi MCP)"]
  switch <--> ship["ship<br/>(Claude Code + GitHub)"]
  switch <--> rev["reviewer<br/>(Claude Code + code-review skill)"]
  switch <--> interns["intern-meera, intern-dev<br/>(Claude Code)"]
  switch <--> sre["sre<br/>(Claude Code, read only)"]
  switch <-->|"custom-agent protocol"| conn["connectors/<br/>standin-data, security, designer"]
  alerts["Mochi automations"] --> bridge["mochi_bridge<br/>(fake Slack gateway)"] --> switch
```

## How a message flows

1. Switch streams every message in Chief's rooms (`filter=all`).
2. Chief handles its commands itself (`!memory`, `!quiet`, `remember …`, `forget …`, `approve CODE`, `!tick …`).
3. Otherwise Chief makes one Jev call with the message, the sender's profile, the room, whether a mentioned teammate is in focus time, and the template and role lists.
4. Code turns the decision into a rung on the trust ladder (below). Jev can lower the rung but never raise it above the ceiling.
5. Chief acts. It answers from memory in the asker's style, holds the message for later, offers in one line, asks the owner for approval, or opens a work room from a template and addresses its lead role with a brief.

## The stories

**1. PM data question (Ravi, `#product`).** "Where's the refunds data and what did it do last month?" Jev picks `data-question`, which is a read. Reading Mochi is pre-approved in Ravi's profile, so it runs at `act` with no approval step. Chief opens `work-data-question-…` with `mochi-data` in it and writes a brief in the room instructions: who asked, why, what they already know, the format, and which Mochi token to use. `mochi-data` takes the `data-steward` role and answers with a table, the query and a Mochi artifact link. Next time, if memory already holds the answer, Chief answers straight from `memory/` in the asker's style and opens no room.

**2. CEO "what shipped", by voice from the phone (Asha).** On a walk, Asha dictates into Slack through the Wispr Flow keyboard: "Hey chief, what did the team ship this week? Make it a one-minute video for the all-hands." Chief opens `shipped-digest`. `ship` writes the digest from merged PRs and releases, then addresses `designer` (the `video` role, our design-agent connector) because the brief asks for a video. Asha's profile says one line plus a chart.

**3. Developer focus time + bug → fix → review (Meera).** Meera's profile has a focus block from 10:00 to 15:00 IST. At 11:00 Dev asks Meera something non-urgent. Jev rates the urgency below "within the hour", so Chief **holds** it. A releaser checks every minute, and when her block ends it sends her one batched brief, addressed to her by name. When a bug comes in, Chief opens `bug-fix-review`, which is built on Switch's red-blue workroom. `intern-meera` fixes on a branch, `reviewer` pushes back until each acceptance item is met, and nobody can merge. When someone in the work room says "merge" or "ship it", Chief posts an approval request owned by the asker. Meera replies `approve K7QX` from her phone, and only then does Chief tell `intern-meera` that the merge is approved.

**Org factory.** "Add a growth team: one dev, one marketer." Creating agents is always ask-first, and the org admin approves it. `chief/factory.py` asks Claude for a small JSON spec only. Code renders the Switch YAML from `org/org-team.yaml.tmpl`: `#growth`, `#growth-eng` with `reviewer` and an exclusive `on-call` role, and one `owner-agents` intern per developer. It validates names and refuses duplicates. If it cannot list the existing rooms and agents, it fails closed. Then it launches the YAML with `create_room_from_yaml` and writes `org/growth.yaml` and the intern's `CLAUDE.md`. The model never writes YAML.

**DevSecOps release train (simulated deploy).** Asking for a release maps to `deploy`, which is hard ask-first, so the release owner approves before the train room opens. In `work-release-train-…`, the agents run in a fixed order, each one addressing the next: `reviewer` reviews the range, `security` runs the scan, `ship` writes release notes, and the dry-run line ends it: "would deploy vX to staging: N services, M migrations". Nothing deploys. The security gate is code, not a model call. `connectors/security.py` posts `BLOCKED` if the scan crashes, the report is stale, there are any high or critical findings, or the secrets policy is breached. Findings go back to the developer's intern. Otherwise it posts `PASSED` and hands over to `release-scribe`.

## Trust ladder

| Rung | What Chief does |
|---|---|
| `silent` (watch) | Reads and remembers. Never posts. |
| `offer` | One line in the thread: "I can pull that from Mochi, want me to?" |
| `act` | Reversible only: answer, summarise, run a read query, open a work room. |
| `ask_first` | Posts an approval request to the owner of the affected thing, then waits. |

**Code sets the ceiling. Jev can only lower it.** From [`chief/ladder.py`](chief/ladder.py):

```python
HARD_ASK_FIRST = frozenset({"merge", "deploy", "external_message", "interrupt_focus", "create_agent"})
```

- These actions are always `ask_first`, whatever Jev says.
- A sender with no profile (role `unknown`) never goes above `offer`, even for pre-approved actions. Agents and unknown senders can never start an approval.
- A person's profile can lift their own `offer` to `act` for listed actions (`preapproved: [mochi_read]` for Ravi). It never removes anything from the hard list.
- At most 3 unprompted posts per room per hour. `!quiet` stops unprompted posts in a room, and `!quiet off` turns them back on.

**Who approves what** (decided in code, never by the model):

| Action | Approver |
|---|---|
| `merge` | the person who asked for the work room |
| `deploy` | the release owner (`Ctx.release_owner`, Meera in the demo) |
| `create_agent` | the org admin (`Ctx.org_admin`, Asha in the demo) |

An approval is a 4-character code (no 0/O/1/I). Only the named owner's `approve CODE` resolves it, and a code works once. Interns also get Switch's built-in approval cards for their own risky tool calls, because they run with `auto_mode: false`.

## Decisions with TypeSafe Jev

Chief makes **one** `system_one` call per message (`jev-1.14.0`, 5 s timeout, no retries). It asks 4 questions:

| Question | Type | Use |
|---|---|---|
| `for_me` | Noul | Is this for the team's assistant, or chat between people? Unaddressed messages at ≤ 0.3 are ignored. |
| `rung` | Choice | `silent` / `offer` / `act` / `ask_first` (then capped by code) |
| `template` + `fits` | Choice + Noul | Which workflow, and does it clearly fit? Chief uses the template only if `fits` ≥ 0.7. |
| `urgency` | Score | can wait / today / within the hour / interrupt now. Below "within the hour", a ping to someone in focus time is held. |

If Jev fails (network, auth, timeout, malformed answer), Chief falls back to `offer` when addressed and `silent` otherwise. The chat loop never blocks. Run `uv run python -m chief.gate "where is the refunds data?"` to try one call and see its latency.

## Memory

Everything Chief remembers is Markdown in git that people can read and edit.

```
memory/
  MEMORY.md                      # generated index: edit the item files, not this
  people/<name>/profile.md       # role, style, focus hours, pre-approvals
  people/<name>/items/m_*.md     # one memory per file
  team/items/m_*.md              # shared team knowledge
  team/proposals/m_*.md          # dreamer suggestions; never active until a person promotes them
```

Each item has front matter (`kind`, `status`, `source`, `use_count`, `version`, `supersedes`, `edited_by_person`) and one line of text:

```markdown
---
id: m_0a11e002
tier: team
owner: team
kind: definition
status: active
source: {room: '#product', date: '2026-11-07', by: person}
edited_by_person: true
...
---
Glossary: GMV is gross merchandise value before refunds; AOV is average order value; a refund-queue ticket is tagged Refunds.
```

- **In chat:** `!memory` lists what Chief holds for you and the team. `remember …` adds a fact as your own edit. `forget …` archives the closest match.
- **The dreamer** (`chief/memory/dreamer.py`, `run_nightly`) reads each room with `read_context` and extracts candidates. It dedups each one against similar items, then tidies each person's memory by merging, updating or retiring items. Items unused for 90 days are archived by code. If a lesson shows up for 3 or more people, the dreamer proposes it as team knowledge, and it refuses any proposal that quotes a person. Then it rewrites `MEMORY.md`, commits only `memory/` to git, publishes the team items as each room's **Team knowledge** document, and returns a "What I learned" note.
- **Person edits win.** The dreamer never edits, merges or archives an item with `edited_by_person: true`. It also skips a candidate that only matches the person's own words.
- The extract, dedup, dream-personal and dream-team prompts in [`chief/memory/prompts.py`](chief/memory/prompts.py) are **ported from Mochi's memory system**, the author's own product, and reworded for Chief.

## Reproduce it

This reproduces story 1 on Switch's bundled Mattermost. The only thing you supply is an API key.

**Prerequisites:** Docker (running), [`just`](https://github.com/casey/just), [`uv`](https://docs.astral.sh/uv/), Node, git, openssl, curl and perl.

**Keys:** in `.env`. If it is missing, `up.sh` copies it from `.env.example`.

| Key | Needed for |
|---|---|
| `ANTHROPIC_API_KEY` (required) | Chief's briefs and restyles, the dreamer, the org factory, the stand-in data agent |
| `TYPESAFE_API_KEY` (optional) | The Jev gate, one call per message. Without it Chief gates with Claude and never acts on its own: it offers, and you reply "yes" |

Mochi, design-agent and the cyber-security scanner are not needed. The judge path uses the bundled stand-in data agent instead of Mochi.

```bash
git clone <this repo> chief && cd chief
cp .env.example .env            # add ANTHROPIC_API_KEY (and TYPESAFE_API_KEY if you have one)
bash scripts/up.sh              # if a local Postgres holds 5432: SWITCH_DB_PORT=5434 bash scripts/up.sh
```

What `scripts/up.sh` does, in order:

1. It checks the prerequisites, loads `.env`, and stops if `ANTHROPIC_API_KEY` is empty.
2. It uses the Switch checkout in `SWITCH_DIR`. The default is `./.switch`, which it clones from `https://github.com/sandbox-quantum/switch` if it is missing.
3. In the Switch checkout, it runs `just init-env` once. That writes Switch's `.env` with fresh secrets. Then `up.sh` sets `FEATURE_FLAGS_ENABLED=agent_management,service_connections`, and adds a random `CONTROLLER_TOKEN_SECRET` if there is none. If `SWITCH_DB_PORT` is set, it writes `DB_PORT` and moves the Postgres host port in `deploy/local/docker-compose.yml` to match, because that file pins 5432.
4. It starts Switch, unless something already answers on `:8000/health`. It runs `just up` (Postgres and Mattermost), then `just migrate`, then `just run` in the background. Then it waits for the core and for Mattermost.
5. It runs `uv run python scripts/setup_demo.py`. Each step is skipped if it is already done, so a second run changes nothing. `--dry-run` lists what it would do. Logged in as the gateway admin from Switch's `.env`, it:
   - creates a personal registration key, `chief-hackathon-local`, and saves it to `.local-switch.env`. Agents registered with this key have an owner, and an agent needs an owner to open rooms.
   - creates the Mattermost users `asha`, `ravi`, `meera` and `dev` and the matching Switch users, then links each pair. To link a pair, it claims the identity as that person and posts the one-time `!link-account` code from their Mattermost account. Generated passwords go to `.local-test-users.json` (mode 600).
   - registers `chief` and `standin-data`, and saves their keys to `.chief.json` and `.connector-standin-data.json` (mode 600).
   - launches `org/brightcart-org.yaml`, filling Chief's slot with the existing `chief`. The specialist agents (`mochi-data`, `ship`, `reviewer`, `sre`, the interns) can't start without a Switch Console machine. If the full launch fails, the script says so. Then it launches the same group, rooms, people, roles and docs with Chief as the only agent. Story 1 doesn't need the specialists.
   - sets Chief's addressing policy: its owner, plus anyone in a `brightcart` room. A filled slot ignores the template's `anyone_in`, so the script sets this itself.
   - builds the stand-in SQLite database, `sample/brightcart.db`.
6. It starts `connectors.standin_data` and `chief` in the background (`chief` with `CHIEF_DATA_AGENT=standin-data`), with logs in `.run/`.
7. It prints the Mattermost URL (`http://localhost:8065`), ravi's login, and the three things to type:
   1. As `ravi`, in `product`, type `@chief where is the refunds data and what did it do last month?`
   2. Without Jev, Chief offers to open a data-question room. Reply `yes` in its thread.
   3. Open the new `work-data-question-…` channel. Chief's brief is there, followed by `standin-data`'s answer table.

`bash scripts/down.sh` stops chief, standin-data and Switch core. Then it runs `just down`, which keeps the data volumes. The scripts never print a secret. The admin logins stay in Switch's `.env`, and the test users' logins are in `.local-test-users.json`.

On Apple Silicon, Mattermost runs under amd64 emulation, and it crashed once in testing. If that happens, run `bash scripts/up.sh` again.

Tests do not touch the network: `uv sync && uv run pytest`.

## Honest limitations

- **Switch has no scheduler.** Chief posts its own `[tick] <kind>` messages into `#chief-ops` and acts only on ticks it posted itself. A `[tick]` from anyone else is ignored. `!tick friday-digest` fires one by hand for the demo. Hackathon issue #771 would add scheduled messages to Switch. Chief does not depend on it.
- **The task protocol is unfinished.** Chief registers with `task_protocol: {can_delegate: false, can_accept: false}`. Hand-offs are targeted @-mentions plus step-by-step room instructions in each template, and each agent addresses the next role when its step is done.
- **Interns are owned by the admin who deploys the template.** The template schema has no owner field. Chief enforces who approves an intern's merge (the asker), so Switch's ownership is not what protects it.
- **Chief's approvals are chat replies, not native cards.** A custom agent posts `approve CODE` / `deny CODE`, and Chief checks the replier against the owner it recorded. Native approval cards appear only for the interns' own tool calls.
- In-process state (rate caps, holds, pending approvals) is lost when Chief restarts.
- The security connector expects a local checkout of the cyber-security scanner. The designer connector needs a running design-agent. Real Mochi needs its own server and tokens.

## Built with

- **Switch**: rooms, roles, templates, addressing, approvals, and the agent protocol
- **TypeSafe Jev**: one typed decision call per message
- **Claude**: Claude Code for the specialists and interns, the Claude API for Chief's text (`claude-haiku-5-5`, `claude-sonnet-5-5`)
- **Mochi**: the governed data agent (over MCP), plus the memory prompts and the SRE first-pass design
- **design-agent**: the author's video and design agent, behind `connectors/designer.py`
- **cyber-security scanner**: the author's scanner, behind `connectors/security.py`
- **Wispr Flow**: the phone keyboard in the demo, and the whole project was built by voice
