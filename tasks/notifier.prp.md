# PRP: Alert notifier — SES Lambda + idempotent writeback + ADR 0005

**Prior commits this builds on:**
- `8f0fe70` — `bedrock_decide` parser hardened (rejects HTML / control / bidi chars in `reason`). The notifier inherits this safety net for whatever the model emits.
- `ad77796` — `decision.py` returns `{alert: bool, reason: str, bedrock_called: bool}`. Notifier consumes this shape.
- `1a455f1` — poller writes `FareHistory` snapshot rows; the notifier reads from this on the read path.

**Confidence:** **8/10** for one-pass execution. Main unknown: the user's SES sender identity is verified outside this repo (AWS console). The PRP assumes a verified address is passed via CDK context.

---

## 1. Summary

A second Lambda (`lambdas/notifier/`) that converts a `{snapshot, watch, decision}` triple into an alert email via SES and writes `lastAlertedAt` + `lastAlertedPrice` back to `Watches` so the dedup gate can do its job on the next poll. The poller invokes the notifier asynchronously (`InvocationType=Event`) when `decision.alert == True`, decoupling polling latency from email delivery. ADR 0005 documents the at-least-once / price-proximity-dedup safety story.

## 2. Problem statement

`decision.decide` produces alert verdicts but nothing consumes them. The dedup gate's `lastAlertedAt` field has never been populated, so a real deploy would alert on every poll where the threshold/anomaly fires — flooding the user. Closing the loop requires:
1. Reading the decision output
2. Composing an email the user can act on
3. Sending via SES
4. Writing back the dedup state

## 3. Solution shape

Direct async Lambda invoke from the poller. The poller adds one line after the decision: if `decision.alert and decision.bedrock_called`, async-invoke the notifier with the payload. The notifier:

1. Receives `{snapshot, watch, decision}` via the event payload (no DDB re-read — the poller already has the data).
2. Composes a plain-text email body from a template (no HTML — the model's `reason` is already pre-validated by `bedrock_decide` to be free of HTML/control/bidi chars; plain text avoids the entire HTML-injection class anyway).
3. Calls SES `send_email` with the verified sender from env, recipient from CDK context (single-recipient v1).
4. On success: DDB `UpdateItem` with `lastAlertedAt = utc_now()` and `lastAlertedPrice = snapshot.totalPrice`. Conditional on `attribute_not_exists(lastAlertedAt) OR lastAlertedAt < :now` so an out-of-order retry can't backdate the dedup state.
5. On SES failure: raise. Lambda async retry handles it; DLQ catches what retry can't.
6. On DDB failure after SES success: log warning + return success. The alert was delivered; if a duplicate happens on the next poll, the price-proximity check in the dedup gate (5% band) catches identical-price snapshots.

Stub mode (`SES_MODE=stub`) for tests — mirrors `BEDROCK_MODE`.

## 4. Files to create

| File | Purpose |
|---|---|
| `lambdas/notifier/__init__.py` | empty |
| `lambdas/notifier/app.py` | Lambda handler; orchestrates compose → send → writeback |
| `lambdas/notifier/ses_client.py` | boto3 SES wrapper; stub mode + defensive fallback |
| `lambdas/notifier/email_template.py` | Pure function: `(snapshot, watch, decision) -> (subject, body)` |
| `lambdas/notifier/writer.py` | Idempotent DDB UpdateItem on `Watches` |
| `lambdas/notifier/requirements.txt` | `aws-lambda-powertools==3.4.0`, `aws-xray-sdk==2.14.0`, `boto3==1.35.49` |
| `lambdas/notifier/dev-requirements.txt` | `pytest==8.3.3`, `moto[dynamodb,ses]==5.0.20` |
| `lambdas/notifier/tests/__init__.py` | empty |
| `lambdas/notifier/tests/conftest.py` | `SES_MODE=stub` + moto fixtures + builders mirroring `lambdas/poller/tests/conftest.py:150-301` |
| `lambdas/notifier/tests/test_email_template.py` | template tests |
| `lambdas/notifier/tests/test_ses_client.py` | SES wrapper tests |
| `lambdas/notifier/tests/test_writer.py` | DDB writeback tests |
| `lambdas/notifier/tests/test_handler.py` | handler integration tests |
| `lambdas/notifier/tests/test_handler_e2e.py` | end-to-end with stub SES + moto DDB |
| `lib/notifier-server.js` | CDK construct: Lambda + IAM + env vars |
| `docs/adr/0005-after-ses-idempotency.md` | ADR |

## 5. Files to update

| File | Change |
|---|---|
| `lambdas/poller/app.py` | After `decide()`, async-invoke notifier if `decision["alert"]` |
| `lambdas/poller/requirements.txt` | (no new deps — boto3 already pinned) |
| `lib/poller-server.js` | Grant poller `lambda:InvokeFunction` on the notifier ARN; add `NOTIFIER_FUNCTION_NAME` env var |
| `lib/strands-agent-on-lambda-stack.js` | Wire `NotifierServerConstruct` and pass its function name into the poller |
| `docs/adr/README.md` | Flip ADR 0005 row from `(planned)` to `Accepted` |
| `docs/threat-model.md` | Append row `[7] Notifier -> SES` boundary; cross-reference `[4]` IAM row |

## 6. Locked decisions

1. **Trigger:** poller invokes notifier directly via `lambda:InvokeFunction` async. NOT SNS (avoids new infrastructure for v1; SNS upgrade path noted in ADR 0005).
2. **Email format:** plain text only. NOT HTML or multipart. Plain text removes the HTML-injection class entirely. ADR 0005 notes the upgrade path.
3. **Recipient:** single recipient from CDK context (`-c notifierRecipientEmail=...`). Personal-use v1. ADR 0005 notes the multi-user upgrade path (look up email from Cognito by `userId`).
4. **Sender:** verified SES identity from CDK context (`-c notifierSenderEmail=...`). Synth-time validation that it's a valid-looking email. **Out of scope:** the actual SES verification step (manual AWS console / SES `VerifyEmailIdentity` API call).
5. **SES_MODE:** env var (`live`/`stub`). Stub mode returns a deterministic `{"MessageId": "stub-<sha8>"}` shape. Conftest sets stub.
6. **Writeback timing:** AFTER SES success. Conditional update guards against out-of-order retries.
7. **Idempotency under retry:** Lambda async retry on SES failure re-invokes the notifier with the same payload. The handler re-composes the email + re-sends; the second SES send is a duplicate alert, but the dedup gate at the next poll catches the steady state (it only fires when price moves >5%). For the in-cycle duplicate-during-retry, document the risk in ADR 0005.

## 7. NOT building (explicit)

- HTML email / multipart MIME — plain text only for v1.
- Cognito-driven multi-user recipient lookup — single CDK-context recipient.
- Bounce/complaint SNS feedback handling — out of scope; rely on SES verified-identity guardrails.
- SES sender domain verification — manual AWS console step, documented in README + ADR 0005.
- Slack / SMS / webhook fan-out — SNS upgrade path noted but not built.
- DLQ for Lambda async failures — Lambda's default async retry semantics are enough for v1; explicit DLQ is the next slice's work alongside CloudWatch alarms.
- A second eval framework for email rendering — the template is pure-function + tested with byte-exact assertions; doesn't need a model-driven eval.

## 8. Test matrix (locked by the test-engineer gate before writing test code)

Concrete groups every test file must cover. Add more if the engineer identifies gaps.

### `test_email_template.py`
- **Group A — template structure:** subject contains "trip-tracker alert" + destination; body contains the model's `reason` string verbatim; body contains the snapshot's totalPrice + flightPrice + hotelPrice; body contains a humanly-readable date range; body has no HTML tags (we're plain text); body has no markdown syntax characters that render weirdly in plain-text mail clients (avoid backticks around prices).
- **Group B — determinism:** two renders of identical input produce byte-identical output. The render function has no wall-clock or random state.
- **Group C — reason string safety:** the upstream parser already rejects HTML/control/bidi, but the template defends-in-depth — if a `reason` somehow contains `<script>`, the plain-text render emits the literal characters with no special handling (no autoescaping, since plain text is its own escape).
- **Group D — missing fields:** snapshot without `bestOfferBlob.bookingDeepLink` produces a clean message saying so; missing hotelName falls back to "(unknown hotel)" not crashes.

### `test_ses_client.py`
- **Group A — stub mode:** returns `{"MessageId": "stub-<sha8>"}` deterministically; never imports boto3; deterministic message id derived from `(sender, recipient, subject, body)`.
- **Group B — live-mode wiring:** boto3 `send_email` called with `Source`, `Destination.ToAddresses`, `Message.Subject.Data`, `Message.Body.Text.Data` matching the inputs; `Charset='UTF-8'` on both Subject and Body.
- **Group C — error mapping:** `botocore.exceptions.ClientError` raises `SesSendError` with the error code in the message; transient errors are NOT swallowed (the Lambda's async retry handles them); the test does NOT assert any specific retry behavior at the SDK layer — Lambda runtime owns retries.
- **Group D — mode selection at import:** `SES_MODE=live` default when env absent; unknown value raises ImportError; mode is read once at import.

### `test_writer.py`
- **Group A — happy path:** call writer; read back via DDB Query; assert `lastAlertedAt` is ISO UTC, `lastAlertedPrice` is Decimal matching snapshot.
- **Group B — conditional update:** pre-existing `lastAlertedAt` BEFORE now → update succeeds (overwrites); pre-existing `lastAlertedAt` IN THE FUTURE → conditional check fails, raises `WritebackConflictError`.
- **Group C — Decimal precision:** lastAlertedPrice is Decimal not float (DDB rejects float).
- **Group D — partial writeback:** writer only touches `lastAlertedAt` + `lastAlertedPrice`. All other Watches fields (status, maxTotalPrice, preferences, …) are untouched. Verified by round-trip read.

### `test_handler.py`
- **Group A — happy path:** event with `alert=True` invokes ses_client.send + writer.write; returns 200.
- **Group B — alert=False shouldn't reach here:** if event payload has `alert=False`, handler logs WARNING and returns early (the poller shouldn't have invoked us); no SES call, no DDB write.
- **Group C — SES failure:** ses_client raises → handler raises (Lambda async retry takes over); writer NOT called.
- **Group D — DDB writeback failure after SES success:** writer raises → handler logs WARNING + returns 200 (alert was delivered; the next poll's dedup gate handles duplicates via price proximity).
- **Group E — malformed event payload:** missing snapshot / watch / decision → 4xx-style structured log + raise.
- **Group F — log structure:** `notification_sent` event includes `watch_id`, `user_id_prefix`, `message_id`. No PII (full email) in logs. `user_id_prefix` is first 8 chars only — mirrors the poller convention.

### `test_handler_e2e.py`
- **Group A — full pipeline:** moto DDB + stub SES → handler with realistic payload → assert email body contains reason, lastAlertedAt is now-ish, lastAlertedPrice == snapshot total.
- **Group B — out-of-order retry guard:** seed watches row with `lastAlertedAt` in the future → handler attempt → writer's conditional update fails → handler still returns 200 (SES already sent) but writer raised `WritebackConflictError` — log captures the conflict for debugging.

## 9. Validation gates (Ralph runs each; all must pass)

### Gate 1 — Unit tests of the notifier package
```
"C:/Users/isabe/Downloads/trip-tracker-agent/.venv-tests/Scripts/python.exe" \
  -m pytest C:/Users/isabe/Downloads/trip-tracker-agent/lambdas/notifier/tests/ -q
```
EXPECT: all new tests pass. No skipped.

### Gate 2 — No regression
```
"C:/Users/isabe/Downloads/trip-tracker-agent/.venv-tests/Scripts/python.exe" \
  -m pytest C:/Users/isabe/Downloads/trip-tracker-agent/lambdas/poller/tests/ \
            C:/Users/isabe/Downloads/trip-tracker-agent/evals/tests/ -q
```
EXPECT: 294 still passing.

### Gate 3 — Comment-cleanliness (global rule)
```
rg -n --no-heading 'slice[ -_]?\d|\bT[1-9]\b|\bTask [1-9]\b|Checkpoint [A-Z]\b' \
  C:/Users/isabe/Downloads/trip-tracker-agent/lambdas/notifier/ \
  C:/Users/isabe/Downloads/trip-tracker-agent/lib/notifier-server.js \
  C:/Users/isabe/Downloads/trip-tracker-agent/docs/adr/0005-after-ses-idempotency.md
rg -n --no-heading -w 'basically|simply|obviously|essentially|merely' \
  C:/Users/isabe/Downloads/trip-tracker-agent/lambdas/notifier/ \
  C:/Users/isabe/Downloads/trip-tracker-agent/lib/notifier-server.js \
  C:/Users/isabe/Downloads/trip-tracker-agent/docs/adr/0005-after-ses-idempotency.md
```
EXPECT: zero matches in both.

### Gate 4 — CDK synth doesn't break
```
cd C:/Users/isabe/Downloads/trip-tracker-agent && npx cdk synth --quiet
```
This may still fail on AgentConstruct's DependenciesLayer bundling (the no-Docker issue documented in slice 5's Ralph state). If it fails for THAT reason, run a direct-construct check instead:
```
node -e "const {Stack, App} = require('aws-cdk-lib'); const C = require('./lib/notifier-server.js'); const app = new App(); const stack = new Stack(app, 'T'); new C(stack, 'N', { watchesTable: { tableArn:'arn:aws:dynamodb:us-east-1:1:table/W', grantWriteData:()=>{}}, senderEmail:'a@b.com', recipientEmail:'c@d.com' }); console.log('ok')"
```
EXPECT: either `cdk synth` succeeds, OR the node-eval fallback prints `ok`.

### Gate 5 — Synth-time validation of sender email
Test that an invalid `notifierSenderEmail` context throws at synth:
```
cd C:/Users/isabe/Downloads/trip-tracker-agent && npx cdk synth -c notifierSenderEmail="not-an-email" 2>&1 | grep -i "invalid\|sender"
```
EXPECT: non-zero exit + error message naming the invalid context.

### Gate 6 — ADR 0005 well-formed
- Exists at `docs/adr/0005-after-ses-idempotency.md`.
- Has `## Context`, `## Decision`, `## Consequences` sections.
- Length: 3500-7000 chars (peer to ADR 0004's 6401).
- Status line: `Accepted`.

### Gate 7 — Threat model + ADR README updated
- `docs/adr/README.md` row for 0005 says `Accepted`, not `(planned)`.
- `docs/threat-model.md` has a new `[7] Notifier -> SES` section with at least 4 rows in its threat table.

## 10. Constraints inherited

- Multi-model test-engineer gate BEFORE writing test code (Task #10 in the parent's task list). Locked per memory `feedback_multi_model_workflow`.
- All tests assert real behaviour; no placeholder / does-not-raise. Per memory `feedback_meaningful_tests`.
- **Zero `slice/T/Task/Checkpoint` refs** in any new file. Per the global CLAUDE.md installed at `~/.claude/CLAUDE.md`.
- Zero nonsense filler.
- Use `.venv-tests/Scripts/python.exe` for pytest.
- Sequential reviewer subagents — code-reviewer → security-auditor → test-engineer → comments-focused code-reviewer.

## 11. Step-by-step (Ralph executes top-to-bottom)

1. **Test-design gate.** Spawn `agent-skills:test-engineer` (Sonnet) with the matrix in §8. Receive concrete `test_<name>` function names per group. Identify any shared helpers needed in `lambdas/notifier/tests/conftest.py`.
2. **Implement `ses_client.py`** — stub + live + error mapping. Mirror `bedrock_decide.py:1-237` structure: module docstring (Owns + Modes), `_resolve_mode()` at import, lazy boto3 client, defensive fallback raising `SesSendError` only on programmer error (not transient). Pin `DEFAULT_CHARSET = "UTF-8"`.
3. **Implement `email_template.py`** — pure function, no IO. Plain text. Subject and body returned as a 2-tuple. Reason string interpolated verbatim. Sentinel test in test_email_template confirms no HTML autoescape happens.
4. **Implement `writer.py`** — DDB `UpdateItem` with `UpdateExpression="SET lastAlertedAt = :now, lastAlertedPrice = :price"` and `ConditionExpression="attribute_not_exists(lastAlertedAt) OR lastAlertedAt < :now"`. `_now()` test seam mirroring `snapshot._now()`. Raises `WritebackConflictError` on `ConditionalCheckFailedException`.
5. **Implement `app.py`** — handler reads event, validates shape, calls template → ses_client → writer in order, structured logs at each step.
6. **Write all test files** following the matrix from step 1. Use the autouse env-reset fixture pattern from `tests/test_bedrock_decide.py:52-62`.
7. **Implement `lib/notifier-server.js`** — Lambda construct with `SES_MODE`, sender + recipient env vars, X-Ray ACTIVE, reserved-concurrency = 5 (notifier handles its own concurrency — async invocations can fan out), `ses:SendEmail` resource-scoped to the verified sender identity ARN, DDB `UpdateItem` grant on Watches.
8. **Wire poller integration** — `lib/poller-server.js` adds `lambda:InvokeFunction` grant + `NOTIFIER_FUNCTION_NAME` env var; `lambdas/poller/app.py` reads the env var and async-invokes when `decision["alert"]` is True. Don't call from `_poll_one` directly — keep it in the outer handler so a notifier-invoke failure can't kill the rest of the poll.
9. **Write `docs/adr/0005-after-ses-idempotency.md`** — Context (why writeback matters; what the dedup gate needs); Decision (after-SES order + conditional update + plain-text email + direct-invoke trigger); Consequences (at-least-once semantics; price-proximity safety net; DLQ deferred). Length matched to ADR 0004.
10. **Update threat model** — new `[7]` section. Append a row to `[4]` IAM section referencing the new SES + DDB grants.
11. **Update ADR README** — flip 0005 from `(planned)` to `Accepted` + link.
12. **Run all 7 validation gates.** Fix anything failing.
13. **STOP.** Parent agent runs the 4-reviewer gate.

## 12. Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| SES sandbox restrictions block live testing | HIGH | LOW | Stub mode is the default in tests; live mode is opt-in via env. README documents the SES verification step + sandbox limits. |
| Duplicate alerts during Lambda async retry | MED | LOW | Documented in ADR 0005 + acknowledged in threat model. Steady-state dedup gate (5% price band) catches the next-poll case. |
| `lastAlertedPrice` Decimal precision drift | LOW | MED | Writer round-trips through `Decimal(str(value))`. Test in writer Group C pins this. |
| Plain-text email is ugly in some clients | LOW | LOW | Acceptable for v1. HTML upgrade path noted in ADR. |
| Poller invokes notifier before notifier is deployed | LOW | LOW | The poller reads `NOTIFIER_FUNCTION_NAME` from env; the stack wires both constructs in the same `cdk deploy`. If the env var is missing, poller logs WARNING and skips invoke (alert is lost; price proximity dedup means next poll re-attempts). |
| Notifier writes lastAlertedAt after a manual user reset | MED | LOW | Conditional update with `lastAlertedAt < :now` accepts the write only if the existing value is older. A manual reset to null is allowed by `attribute_not_exists`. |

---

## What "done" looks like

- 14 new files under `lambdas/notifier/`, 1 new construct in `lib/`, 1 new ADR, 1 threat-model section.
- 2 files updated (poller integration).
- All 7 gates green.
- Working tree changes confined to the above paths.
- Ready to commit as `add alert notifier: SES Lambda + idempotent lastAlertedAt writeback + ADR 0005`.
