# ADR 0005 — After-SES idempotent writeback for `lastAlertedAt`

**Date:** 2026-05-13
**Status:** Accepted

## Context

The poller's decision call (ADR 0004) produces
`{alert: bool, reason: str, bedrock_called: bool}` but nothing
consumed it. The `lastAlertedAt` and `lastAlertedPrice` fields on
the `Watches` row had no writer — meaning the dedup gate
(`is_dedup_eligible`) had nothing to read, so a real deploy would
fire an alert on every poll where the threshold or anomaly gates
passed, flooding the user.

Closing the loop requires:
1. A path from `decision.alert == True` to an outgoing email.
2. A writeback of `lastAlertedAt` + `lastAlertedPrice` after the
   email is sent, so the dedup gate's "have we already alerted
   recently at a similar price" check has data to work with.

Two design axes mattered:

**Trigger mechanism.** Three options — direct async Lambda
invoke (no new infra; Lambda runtime owns retries), SNS topic
(cleaner decoupling, enables future Slack/SMS fan-out, one extra
Construct), EventBridge bus (similar to SNS, more infra).

**Writeback ordering.** Before-SES sets dedup state pre-emptively
but blocks user-visible retry if SES fails. After-SES only sets
state on confirmed delivery but opens a duplicate-alert window if
the DDB write fails after a successful SES send. The dedup gate's
5% price-proximity check caps that duplicate window at one email
per cycle.

## Decision

- **Trigger: direct async Lambda invoke.** Poller calls notifier
  with `InvocationType=Event` on `decision.alert == True`. Lambda
  async runtime owns retry. No SNS or EventBridge today;
  upgrade path documented when fan-out matters.
- **Order: after-SES + conditional writeback.** `ses.send_email`
  first, then `writer.write_alert_state` with
  `ConditionExpression="attribute_not_exists(lastAlertedAt) OR
  lastAlertedAt < :now"`. SES failure raises; writer is never
  reached, next poll retries. DDB failure after SES success logs
  WARN and returns 200 — the alert was delivered, the next poll's
  dedup band handles any duplicate. The conditional protects
  against out-of-order retries backdating the dedup state.
- **Plain text only.** The `reason` interpolates verbatim with no
  HTML escape (none needed — plain text IS the escape).
  Defence-in-depth across the parser hardening in
  `bedrock_decide` (boundary [6]) + the template's CR/LF/control
  strip on the subject + no HTML body part in the SES call.
- **Single recipient, verified sender, both from CDK context.**
  `notifierRecipientEmail` and `notifierSenderEmail` pattern-
  validated at synth time. `ses:SendEmail` IAM grant
  resource-scoped to the sender identity ARN. The identity is
  verified out-of-band (AWS console); this construct only grants
  permission to send AS it. Multi-user lookup via Cognito is the
  upgrade path. **Email identity, not domain identity.** The regex
  rejects bare-domain input on purpose: an `identity/example.com`
  ARN grant would let the Lambda send as ANY address in the
  domain. Domain-wide grants must come through a separate
  construct that documents the wider blast radius.
- **SES_MODE env (`live` / `stub`)** mirrors `BEDROCK_MODE`.
  Tests pin stub; synth-time validation catches typos.

## Consequences

**Good:**

- **The dedup gate finally has data to read.** A future-me running
  the live system gets exactly one alert per genuine price move
  rather than one alert per poll.
- **At-least-once delivery with a bounded duplicate window.**
  Lambda async retry handles transient SES failures
  transparently. The duplicate-during-DDB-fail-after-SES-success
  case is bounded to one duplicate at the same price; the
  price-proximity dedup band (5%) catches it at the next poll.
- **HTML-injection class is closed by construction.** Plain text
  + upstream parser hardening + no autoescape in the template =
  no path from a malicious `reason` to a rendered HTML payload
  in the user's mail client. Defence in depth across three
  layers.
- **IAM stays tight.** `ses:SendEmail` resource-scoped to the
  sender identity ARN. `dynamodb:UpdateItem` scoped to the
  Watches table only — no put, no delete, no scan. Notifier
  cannot send AS another identity or write to FareHistory.
- **Test discipline matches the rest of the codebase.**
  `SES_MODE=stub` keeps the entire test suite cost-free.
  Conditional-update branches are pinned by tests in `test_writer.py`
  group B. The end-to-end test in `test_handler_e2e.py` exercises
  template → SES → writer in a single moto-backed call.

**Cost:**

- **Duplicate emails are possible.** Lambda async retry on SES
  failure can resend, and the after-SES writeback gap means a
  DDB hiccup leaves the dedup state stale. Both windows are
  bounded — the 5% price-proximity dedup band catches identical
  prices at the next poll — but a user could see up to one
  duplicate per cycle in pathological cases.
- **One more external dependency.** SES is the second external
  service in the per-watch path (Bedrock being the first). A
  full SES outage means alerts are queued in Lambda's async
  retry buffer; after retry exhaustion, the alert is lost.
  Acceptable for v1 personal-use; a production deploy would
  add a DLQ + CloudWatch alarm.
- **Plain-text email is ugly.** No bold prices, no embedded
  links, no images. Acceptable trade-off for v1 — HTML upgrade
  path noted; the template's output is already structured so
  the upgrade is mostly markup.
- **Sender domain is a manual step.** A clean deploy needs the
  user to have verified the sender identity in SES before
  running `cdk deploy`. README documents this. SES sandbox
  (the default for new accounts) limits recipients to verified
  addresses — fine for personal-use.

**Not chosen — and why:**

- **SNS topic between poller and notifier.** Rejected for v1 —
  adds a Construct + a topic resource for zero v1 benefit. The
  decoupling is a notional advantage that only pays off when
  fan-out matters (Slack, SMS, multi-user). Upgrade path
  documented; current code is one localised change away.
- **Transactional writeback (DynamoDB transactional write + SES
  in one atomic operation).** Impossible — DynamoDB transactions
  don't span services. The duplicate window is the genuine cost
  of bridging the two.
- **HTML email.** Rejected for v1. Once HTML lands, the
  template MUST autoescape every interpolation; the
  `reason`-string hardening at `bedrock_decide` is one of three
  needed defences, not all three. Plain text is cheaper to keep
  safe.
- **Cognito-driven multi-user recipient lookup.** Out of v1
  scope. The construct accepts a single recipient via context;
  the lookup path is a clean extension when multi-user lands.
- **Bounce / complaint feedback via SNS.** Out of v1 scope.
  Production-readiness will revisit alongside DLQ + budget
  alarm.

## References

- Design spec §5 (alert email flow):
  `docs/superpowers/specs/2026-05-08-trip-tracker-agent-design.md`.
- ADR 0004 — Bedrock decision call (the source of `decision.alert`
  + `decision.reason` the notifier consumes).
- `lambdas/notifier/` — the only call site.
- `lib/notifier-server.js` — IAM grant + env vars.
- `lib/poller-server.js` — `lambda:InvokeFunction` grant + the
  `NOTIFIER_FUNCTION_NAME` env var.
- Threat model boundary `[7]` — Notifier → SES + DDB.
