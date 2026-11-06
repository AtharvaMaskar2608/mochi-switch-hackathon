# Demo script (3:00)

**Cast:** the author plays four Switch users in Slack. Asha (CEO) is on the phone. Ravi (PM), Meera and Dev are each in their own browser profile.
**Before recording:**
- Launch `org/brightcart-org.yaml`, and start Chief and the connectors.
- Publish `brightcart-shop` with `sample/make_shop_repo.sh`. It plants bug BC-142 (refund rounding) and `requests==2.19.1`.
- Set `release_owner = meera` and `org_admin = asha`, which are the defaults.
- Open `#chief-ops` in a spare tab so you can fire ticks by hand.
- Keep the Switch room list visible in one shot, so viewers see work rooms appear.

| Time | Shot | Do exactly this | What the viewer sees |
|---|---|---|---|
| 0:00–0:20 | **Phone, by voice** | On the phone (Asha's Slack, Wispr Flow keyboard), in `#leadership`, hold the mic and say: "Hey chief, what did the team ship this week? Make it a one-minute video for the all-hands, and find out why refunds jumped." Tap send. | The dictated text appears. Chief replies in the thread, "opened work-shipped-digest-… for this". |
| 0:20–0:35 | Cut to laptop: the work room | Click the new `work-shipped-digest-…` room. Scroll the room instructions to show the brief. | `ship` posts the digest and addresses `designer`, which replies with the video file. Caption: "Switch template + roles. Nobody named a bot." |
| 0:35–0:55 | **PM data question** | As Ravi, in `#product`, type: `where's the refunds data and what did it do last month?` Press Enter. Click the `work-data-question-…` room when it appears. | The brief in the room instructions says who asked, his style (tables), and the PM token. `mochi-data` answers with a table, the SQL and a Mochi artifact link. Click the link to show the live Mochi artifact. |
| 0:55–1:15 | **Memory changes the answer** | In `#chief-ops`, type `!tick dream` (or show the dreamer run from the night before). Open `memory/MEMORY.md` on GitHub and show the new item. Then, as Ravi, in `#product`, type: `where's the refunds data again?` | Caption: "Next day." Chief answers instantly from memory in Ravi's table style. No work room opens. Show `!memory` listing the item, and say "he can edit or `forget` it". |
| 1:15–1:40 | **Focus time** | Set the clock caption to "11:00 IST". As Dev, in `#eng`, type: `@chief ask meera which branch has the refunds export, no rush` Then: `@chief also ask meera if BC-140 is still open` Then fire the release (clock caption "15:00", or wait for the 60 s releaser after her block ends). | Chief says nothing in `#eng`, and Meera's tab stays quiet. At 15:00 Meera gets **one** batched brief addressed to her. Caption: "2 pings → 1 brief, after focus time." |
| 1:40–2:10 | **Bug → fix → review, phone approval** | As Meera, in `#eng`, type: `chief, BC-142: refunds round down by a paisa, can my intern fix it?` Click the `work-bug-fix-review-…` room. When `reviewer` says ready, Meera types `merge it`. Pick up the phone (Meera's Slack) and type `approve <CODE>` with the code Chief posted. | `intern-meera` posts a plan and a branch. `reviewer` pushes back once, then says ready. Chief posts "meera: this needs your ok (merge). Reply 'approve K7QX'…". After the approve, Chief addresses `intern-meera` with "merge approved by meera". |
| 2:10–2:30 | **Org factory** | As Asha, in `#leadership`, type: `chief, add a growth team: one dev, one marketer` and send. On the phone, type `approve <CODE>`. Click the Switch room list. | Chief asks Asha (org admin) for approval. After it, `#growth` and `#growth-eng` appear with `intern-<name>`. Show the new `org/growth.yaml` in git. Caption: "Model wrote JSON. Code wrote the YAML." |
| 2:30–2:50 | **Release train** | As Ravi, in `#eng`, type: `chief, cut a release of brightcart-shop` and send. On Meera's phone, type `approve <CODE>`. Click `work-release-train-…`. First run: `security` posts **BLOCKED** (requests 2.19.1, high CVEs) back to the intern. Cut to a second run after the bump: **PASSED**. | `reviewer` → `security` → `ship` notes → the dry-run line: "would deploy v1.4.2 to staging: 3 services, 1 migration". Caption: "Nothing deployed. The gate is code." |
| 2:50–3:00 | **Close** | Back to the phone on the walk, with Asha watching the video. Fade in the room list. | Title card: **"Nobody on the team was interrupted."** Then the repo URL and "Built on Switch". |

## Notes for the take

- If Chief offers instead of acting, Jev set the rung to `offer`. Ask again and address Chief (`@chief …`).
- Address Chief in the focus-time shot. An unaddressed message that Jev reads as chat between people (`for_me` ≤ 0.3) is ignored, not held.
- Approval codes are 4 characters and only work once. Read them off the screen.
- The judge path is Mattermost with the stand-in data agent. It answers with a table only and no Mochi artifact link, so the video uses real Mochi.
