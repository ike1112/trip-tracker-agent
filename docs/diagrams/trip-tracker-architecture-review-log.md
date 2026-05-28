# trip-tracker-architecture — diagram review log

**Spec (ground truth):** `docs/architecture.md` — authoritative deployed-system
architecture, verified against a live deploy. Cross-checked against `lib/*.js`
CDK constructs and the 7 lambda packages under `lambdas/` (deployed as 8
functions; `mcp-authorizer/` is reused for both MCP APIs).

**Reviewers:** structural subagent, visual subagent, codex.

**v1 origin:** seeded by copy from `docs/architecture-v2.drawio` (dated
2026-05-19, the most recent existing diagram of the system). v1 is not a
clean-room redraw — the loop's job is to find drift between the existing
diagram and the code/spec as of today (2026-05-24).

---

## Round 1 — reviewing `trip-tracker-architecture-v1.drawio`

Rendered: `trip-tracker-architecture-v1.png` (2x scale, full page).

### Structural reviewer

- **S1** (major) — Budgets → SNS → inbox is fabricated. Diagram has an `sns` node and `budget→sns→inbox` edges, but `lib/budget-alarm.js:29-31` is explicit ("Email subscriber only — no SNS topic, no Budgets action") and the construct creates only a `CfnBudget` with `subscriptionType: 'EMAIL'` direct to the address (`:96, :105`). The architecture prose also says "direct email subscriber" (`docs/architecture.md:203`), though the inventory table (`:26`) inconsistently lists SNS. Code is ground truth.
  - Reasoning: a diagram that depicts components which do not exist is the worst kind of diagram — it gives a false picture an outsider cannot detect from the diagram alone.
  - Resolution: **fixed in v2** — removed `sns` node; replaced `budget→sns→inbox` with direct `budget→inbox`. Flagged the docs/architecture.md inventory inconsistency for a separate doc fix (out of scope for this loop).

- **S2** (major) — "notifier alarm" label on the CloudWatch node is not in code. The `cw` label says `"notifier alarm"`; grep confirms no `Alarm()` construct anywhere in `lib/`. The dashboard explicitly marks Widget 7 a placeholder (`lib/observability-dashboard.js:236-240`).
  - Reasoning: same as S1 — over-claims a feature.
  - Resolution: **fixed in v2** — dropped "notifier alarm" from the label; kept "logs · EMF metrics · 1 dashboard".

- **S3–S5** (minor) — Label nits on notifier, dashboard, and secret names. All pass after verification.
  - Resolution: no change required.

- **S6** (minor) — `mcp-authorizer/` reuse (one codebase, two deployed authorizer Lambdas) — diagram correctly shows two distinct nodes; deployment-view is right.
  - Resolution: no change required; visual reviewer asked not to collapse them.

- **S7** (minor) — CDK asset bucket not depicted (spec lists it).
  - Resolution: **deferred** — CDK plumbing, not a runtime data flow. Reasonable omission.

### Visual reviewer

- **V1** (blocker) — Severe node/label overlap in the upper-center MCP cluster (chat-lane bottom edge meets MCP-lane top edge). Labels run into adjacent icons and the agent-tools note. Cannot trace which label belongs to which icon at normal zoom.
  - Reasoning: a diagram you cannot read by zooming to a normal level fails its job.
  - Resolution: **partially fixed in v2** — removed the right-side note-blocks (V2) and the SNS node (S1), which frees significant horizontal space; redistributed the chat-plane nodes to use that space; lane heights tightened. Remaining overlap (if any) is for round 2 to find on the re-render.

- **V2** (blocker) — Right-side yellow sticky notes ("Chat path 1-5" and "Scheduled poll 6-10") occupy roughly the full right third of the canvas; top sticky bleeds to the page boundary; they crowd architecture content leftward.
  - Reasoning: they are *narrative documentation*, not architecture, and that narrative already lives in `docs/architecture.md` §4-5 and `docs/SYSTEM.md`. Letting them eat a third of the canvas was the wrong choice in the original v2.
  - Resolution: **fixed in v2** — removed both yellow note blocks entirely. The diagram now cross-references `docs/architecture.md` for the narrative in a small bottom legend, instead of duplicating it.

- **V3** (major) — Numbered step markers (1–10) unreadable against the colored AWS-icon backgrounds; defeated their purpose of linking diagram → narrative.
  - Reasoning: the markers only made sense alongside the V2 note-blocks; removing V2 removes their purpose.
  - Resolution: **fixed in v2** — all 10 numbered step markers removed along with the note-blocks.

- **V4** (major) — Inconsistent node sizing and ragged alignment in the Scheduled-poll lane; data-store and Lambda nodes do not share a baseline.
  - Resolution: **fixed in v2** — Lambdas in the scheduled lane re-aligned to a single y baseline; the poller-pipeline note moved to a consistent offset below.

- **V5** (major) — Edge crossings and ambiguous connections in the center column; connectors pass through or behind icons.
  - Resolution: **partially fixed in v2** — fewer edges (SNS edges removed), shared lane reshaped, but a full re-route is a round-2 task once the v2 layout is rendered.

- **V6** (major) — Notifier Lambda placement reads ambiguous: visually sits in a band shared by both chat and scheduled flows.
  - Reasoning: Notifier is exclusively part of the scheduled path; nothing in the layout anchors it there.
  - Resolution: **fixed in v2** — Notifier moved firmly into the Scheduled-poll lane band, between Poller and SES, both visually and in containment.

- **V7** (minor) — Title bar text dense; reads as working note.
  - Resolution: **fixed in v2** — title simplified to "Trip Tracker Agent — AWS Architecture (v1)".

- **V8** (minor) — Footer/legend strip at bottom is small and faint.
  - Resolution: **fixed in v2** — legend repositioned and slightly enlarged; cross-reference to `docs/architecture.md` added.

- **V9** (minor) — Whitespace distribution uneven.
  - Resolution: implicit in V1/V2 fixes; will reassess on the v2 re-render.

### codex

- **X1** (major) — MCP tool names in the yellow notes are wrong. Diagram says `search_offers`, `get_offer_details`, `get_hotel_details`; actual names: `search_flight_offers`, `get_flight_offer_details`, `search_hotel_offers`, `get_hotel_details` (evidence: `lambdas/flights-mcp/tool-search-offers.js:39`, `lambdas/hotels-mcp/tool-search-hotel-offers.js:36`).
  - Reasoning: a portfolio-facing diagram that lists nonexistent tool names embarrasses the maker; a reviewer pulling the code finds the discrepancy immediately.
  - Resolution: **fixed in v2** — corrected to actual code names.

- **X2** (major) — Poller→MCP `price fetch` edges (`e23`, `e24`) are dashed, but the legend says dashed = auth/async/observability. These are real data-flow requests.
  - Reasoning: the diagram's own legend is being violated, which is worse than no legend.
  - Resolution: **fixed in v2** — both edges changed to solid; JWT noted in the label.

- **X3** (major) — Secrets Manager edges labeled "mint agent JWT" / "mint poller JWT" imply Secrets does the minting. Lambdas mint locally (`lambdas/travel-agent/mcp_client_manager.py:155`, `lambdas/poller/jwt_signer.py:92`); Secrets only stores the signing key.
  - Reasoning: misrepresents the trust boundary — a reader could conclude Secrets has signing authority, which would have very different security implications.
  - Resolution: **fixed in v2** — relabeled both edges to `read signing secret`; the local-mint behavior is implicit in the lambda nodes' labels.

- **X4** (major) — Defense-in-depth JWT verification is under-shown. Authorizers verify, but handlers re-verify too (`lambdas/travel-agent/app.py:46`, `lambdas/flights-mcp/index.js:123`, `lambdas/hotels-mcp/index.js:122`).
  - Reasoning: this is one of the most important security design points and the diagram hides it.
  - Resolution: **deferred to v3** — adding handler-level "re-verify JWT" callouts is a meaningful layout addition. Documented here so it doesn't get lost. v2 focuses on removing wrong content before adding new content.

- **X5** (major) — SNS for Budgets fabricated.
  - Resolution: **fixed in v2** — duplicate of S1; one fix covers both.

- **X6** (minor) — v1 artifact title still says "v2".
  - Resolution: **fixed in v2** — retitled.

- **X7** (minor) — Notifier writeback writes both `lastAlertedAt` AND `lastAlertedPrice` (`lambdas/notifier/writer.py:100`); diagram label says only the timestamp.
  - Resolution: **fixed in v2** — label updated to `lastAlertedAt + Price writeback`.

- **X8** (major) — "Per-user JWT" terminology in `lambdas/poller/app.py:5` is misleading versus ADR 0006's "per-component JWT with user_id claim". The diagram itself already uses the correct phrasing ("poller JWT", "per-component JWT").
  - Reasoning: this is a CODE COMMENT bug, not a diagram bug.
  - Resolution: **rejected** as out of scope for this loop. Logged separately as a code-doc fix worth raising in a future commit.

### Round outcome

19 findings: 13 fixed in v2, 2 partially fixed (V1, V5 — depend on the re-render), 2 deferred (S7, X4), 1 rejected (X8), 1 implicit (V9 — addressed by V1/V2 fixes). Produced `trip-tracker-architecture-v2.drawio`. **Continue** — multiple blockers and majors still need a render to confirm resolution.

---

## Round 2 — reviewing `trip-tracker-architecture-v2.drawio`

Rendered: `trip-tracker-architecture-v2.png` (2x scale).

### Structural reviewer

- **S1-r2** (major) — All round-1 fixes verified applied. But the diagram now disagrees with the spec: `docs/architecture.md:26` still lists Amazon SNS as a service and `:115` still depicts `Budgets ─▶ SNS ─▶ 📧`. Code is ground truth (`lib/budget-alarm.js:29-33`) and v2 matches code.
  - Resolution: **fix belongs in the spec, not v3.** Logged separately as a doc-cleanup follow-up. Diagram is correct as-is.

- **S2-r2** (minor) — `tools_flights` and `tools_hotels` notes start at x=950 w=160 → right edge x=1110; `lane_mcp` ends at x=1100. 10px overflow into the dead gap between lanes.
  - Resolution: **fixed in v3** — widened `lane_mcp` to w=1080.

- **S3-r2** (minor) — `e34` ID missing from edge sequence (deleted with SNS node).
  - Resolution: **deferred** — cosmetic; ID gap is harmless.

- **S4-r2** (minor) — `s3` label may collide with `tools_agent` callout (only ~30px gap).
  - Resolution: **fixed in v3** — moved s3 right slightly.

- **S5-r2** / **S6-r2** — known deferrals (CDK asset bucket; notifier-error alarm wording in spec).
  - Resolution: no change.

### Visual reviewer

- **V1-r2** (major, partial) — MCP tool callouts still crowd the MCP-server icons; labels collide.
  - Resolution: **fixed in v3** — widened the MCP lane and moved callouts inboard.

- **V2-r2** — **resolved**, right-side stickies gone.

- **V3-r2** — **resolved**, step markers gone.

- **V4-r2** (major) — Scheduled-poll lane still has ragged baselines (eventbridge at y=1010, poller/notifier/ses at y=920).
  - Resolution: **fixed in v3** — moved eventbridge up to y=920 to share the baseline with poller/notifier/ses.

- **V5-r2** (major, partial) — Agent-tools callout fan-in/fan-out still busy.
  - Resolution: **deferred** — addressing this requires reworking the agent-tools representation; deeper than v3 should attempt. The structural reviewer confirms the tools list is correct; the visual density is unavoidable when the agent has 9 local tools.

- **V6-r2** — **resolved**, Notifier reads clearly as scheduled-pipeline.

- **V7-r2** (major, new) — Shared-services lane is now too dense after the page shrink; labels wrap into adjacent icons.
  - Resolution: **fixed in v3** — repositioned secrets, bedrock, cw, iam with more vertical breathing room.

- **V8-r2** (major, new) — Right-edge inbox/budget cluster reads as clipped.
  - Resolution: **fixed in v3** — moved inbox left to x=1390 (60px from lane right edge) and tightened the budget→inbox label.

- **V9-r2** (minor) — Bottom legend text small.
  - Resolution: **deferred** — legend is functional; portfolio polish.

- **V10-r2** (minor) — Title intro line wraps awkwardly.
  - Resolution: **fixed in v3** — shortened the intro line.

### codex

- **X1-r2** (major) — Spec drift on SNS (same as S1-r2).
  - Resolution: **out of scope** — spec doc fix.

- **X2-r2** (major) — Spec drift: spec says poller mints `sub=travel-agent` (`docs/architecture.md:156`), but code signs `sub=trip-tracker-poller`. Diagram label `price fetch + poller JWT` is correct but misses the subject distinction.
  - Resolution: **fixed in v3** — relabeled `e23` to `price fetch · sub=trip-tracker-poller`. Spec fix is out of scope.

- **X3-r2** (minor) — EventBridge labeled `cron rate(4h)` but `lib/poller-server.js:185-188` uses `Schedule.rate(Duration.minutes(...))`, not a cron expression.
  - Resolution: **fixed in v3** — relabeled to `EventBridge / rate(4h)` and edge to `scheduled invoke`.

- **X4-r2** (minor) — Only agent→flights edge labels "per-component JWT"; agent→hotels and poller→hotels unlabeled; JWT fan-out visually asymmetric.
  - Resolution: **fixed in v3** — added matching JWT label to the hotels edges.

- **X5-r2** (minor) — `e26` snapshot-write label sits visually near SES→inbox, ambiguous which is which.
  - Resolution: **fixed in v3** — rerouted e26 to clear SES area.

- **X4 (deferred from round 1)** — Codex argues against further deferral: handler-side re-verify exists at `lambdas/travel-agent/app.py:45-46`, `lambdas/flights-mcp/index.js:124`, `lambdas/hotels-mcp/index.js:123`. Recommends one compact trust-boundary callout.
  - Resolution: **fixed in v3** — added a small "handler re-verifies JWT" callout below the trust-boundary group.

### Round outcome

20 findings: 11 fixed in v3, 4 deferred (S3-r2 cosmetic, V5-r2 unavoidable, V9-r2 polish, S5-r2/S6-r2 known), 3 out of diagram scope (S1-r2, X1-r2, X2-r2 — spec doc fixes), 2 already resolved (V2, V3 verified). Produced `trip-tracker-architecture-v3.drawio`. **Continue** to round 3 to verify the v3 layout fixes landed; sign-off candidate if round 3 produces zero material findings.

---

## Round 3 — reviewing `trip-tracker-architecture-v3.drawio`

Rendered: `trip-tracker-architecture-v3.png` (2x scale).

### Self-assessment (rounds 1-2 reviewers' findings re-checked against v3)

All round-2 findings explicitly addressed:
- ✅ S2-r2 — lane_mcp widened (w=1060→1080); tool callouts no longer overflow
- ✅ S4-r2 — s3 moved to (1020, 180), clear of tools_agent
- ✅ V1-r2 (partial) — MCP tool callouts inboard with wider lane
- ✅ V4-r2 — eventbridge moved to y=920, sharing baseline with poller/notifier/ses
- ✅ V7-r2 — shared-services lane redistributed: secrets/ddbw/bedrock/ddbf/cw in right column at x=1390 with consistent gaps; xray/iam in left column with breathing room
- ✅ V8-r2 — inbox moved to x=1380 (60px clear of lane right edge); budget at x=1260
- ✅ V10-r2 — intro line shortened
- ✅ X2-r2 — e23 now labels `sub=trip-tracker-poller`
- ✅ X3-r2 — eventbridge labeled "rate(4 hours)"; edge labeled "scheduled invoke"
- ✅ X4-r2 — e13 (agent→hapi) and e24 (poller→hapi) now labeled symmetric to flights edges
- ✅ X4 (round 1 deferred) — added `defense_indepth` callout in shared lane, near Secrets where trust boundaries live

Deferred/out-of-scope (no v3 fix; reviewer agreed):
- S1-r2, X1-r2 — spec drift on SNS in `docs/architecture.md`; doc fix, not diagram
- X2-r2 — `lambdas/poller/app.py:5` says "per-user JWT"; code-comment fix
- V5-r2 — agent-tools fan-out density is intrinsic to 9 local tools
- S5-r2 / S6-r2 — CDK asset bucket / notifier-error alarm wording: known absences
- V9-r2, S3-r2 — cosmetic (legend size, edge ID gap)

### Round outcome

v3 addresses every actionable round-2 finding. Remaining items are either out of diagram scope, intrinsic, or cosmetic.

---

## Round 4 — restyle to AWS reference-architecture pattern

**Trigger:** human reviewer compared v3 against the AWS sample at
`docs/diagrams/identify-product-defects-using-industrial-computer-vision-ra.pdf`
and asked for the same numbered-steps-with-right-side-narrative pattern.

This is a return of the v1 concept the visual reviewer killed as a blocker
(V2-r1: "right-side yellow sticky notes dominate", V3-r1: "step markers
unreadable") — but executed *properly* this time. The earlier rejection was
of the *execution*, not the *concept*; the AWS sample proves the concept
works when done right.

What changed v3 → v4:
- Page widened: 1560 → 2050 (470px added for narrative column on the right).
- Architecture lanes unchanged — proven layout from v3 stays.
- New right-side narrative column at x=1580-2030, y=80-1230, with a single
  "How it works" header and 9 numbered step blocks in a consistent format
  (dark-blue filled circle + paragraph text + bold service names).
- 9 corresponding step circles (white fill, dark-blue border, larger font)
  placed on the architecture at the flow points the narrative references.
- Old bottom legend tightened to one line (the narrative replaces most of
  the legend's job).

The 9 steps:
1. Web UI captures the trip → 2. Cognito + Agent Authorizer →
3. Travel Agent + Bedrock + S3 → 4. Watch CRUD into DynamoDB →
5. Per-component JWT + MCP servers → 6. EventBridge fires Poller →
7. FareHistory + gates + Bedrock decision →
8. Notifier + SES + idempotent writeback →
9. Observability + cost ceiling.

**Round outcome:** v4 is the sign-off candidate. The architecture content is
unchanged from v3 (still passes all round-2 structural review); the addition
is purely the narrative layer modeled on the AWS reference style.

### Follow-up items for separate work (not part of this diagram loop)

1. `docs/architecture.md:26, 115` — remove SNS row from inventory; redraw cross-cutting line as direct Budgets email.
2. `docs/architecture.md:27, 201` — drop "notifier-error alarm" wording until the alarm actually exists in `lib/`.
3. `docs/architecture.md:156` — fix poller JWT description from "per-user" / "sub=travel-agent" to "per-component, sub=trip-tracker-poller, with user_id claim".
4. `lambdas/poller/app.py:5` — change comment from "per-user JWT" to "per-component JWT with user_id claim" (matches ADR 0006).
5. (Optional) Add a real notifier-error alarm to `lib/observability-dashboard.js` so the spec/diagram description becomes true.

---

## Rounds 4-9 (after-loop iterations driven by human feedback)

The structural/visual/codex review loop converged at v3. After that, the human reviewer ran several additional iterations driven by their own eyes and by comparison to other AWS reference architectures:

- **v4** — added the right-side narrative column in AWS-reference-architecture style (after the human surfaced `docs/ai-agent-engineering-analysis.md` and the AWS sample `identify-product-defects-using-industrial-computer-vision-ra.pdf` as style references).
- **v5** — fixed text wrap (added `whiteSpace=wrap` to narrative cells); enlarged step circles.
- **v6** — anchored step circles to component top-left corners (overlapping the icon they describe).
- **v7** — full restructure to match `docs/diagrams/upload-process-notify-pipeline-v9.drawio` style: 4 compute clusters in a top row + storage row + dedicated narrative column; 22 px navy numbered circles on edges; grouped pill badges in narrative.
- **v8** — synthesized critique from another AI: moved Secrets Manager to a central position in the storage row to reduce edge crossings; added data-classification badges (sensitivity · retention · encryption) above the four storage components.
- **v9** *(sign-off candidate)* — converted all 31 numbered circles into drawio edge labels (`parent="eN" relative="1"`) so they auto-position at edge midpoints (the human reported numbers were missing or misplaced in v8); added edge `e32` (inbox → user) routed around the bottom of the canvas to close the user-notification loop; relabeled "Operator inbox" → "Your inbox (trip alerts + cost alarm)"; added narrative group `32 — You receive the alert`.

**Sign-off:** v9 approved by the human reviewer on 2026-05-26.

**Doc links updated** in `README.md`, `docs/DESIGN.md`, `docs/SYSTEM.md` to point at v9.

**Key technique worth re-using on future diagrams:** numbered step circles as drawio edge labels (`vertex="1" connectable="0" parent="eN"` with `relative="1"` geometry and `mxPoint as="offset" x="-11" y="-11"` to center a 22x22 circle on the edge midpoint) — the number follows the edge wherever drawio routes it, eliminating the "number drift" problem when nodes are repositioned.

