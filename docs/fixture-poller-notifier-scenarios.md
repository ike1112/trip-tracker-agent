# Fixture Poller And Notifier Scenarios

Use these two fixture watches to exercise the scheduled path after a
fixture/stub deploy.

## Scenario 1: Tokyo Snapshot Path

Purpose:

```text
EventBridge/manual poller -> flights MCP -> hotels MCP -> FareHistory write -> decision_made alert=false
```

Chat input:

```text
Watch Tokyo from SFO to NRT, departure window October 15 to October 15, 2026, 5 nights, 1 passenger, max total price $1500.
```

Confirmation input:

```text
Yes, confirmed. Create this watch.
```

Fixture source files:

```text
lambdas/flights-mcp/fixtures/SFO-NRT-2026-10-15.json
lambdas/hotels-mcp/fixtures/Tokyo-2026-10-15.json
```

Expected fixture prices:

```text
Cheapest flight:  $1148.00
Cheapest hotel:   $485.00
Total:           $1633.00
```

Expected result:

```text
snapshot_written
decision_made alert=false reason=no_gate_passed
poll_complete watches_errored=0
```

## Scenario 2: Paris Alert And Notifier Path

Purpose:

```text
EventBridge/manual poller -> flights MCP -> hotels MCP -> FareHistory write -> decision_made alert=true -> async notifier invoke -> real SES attempt -> Watches alert writeback
```

Chat input:

```text
Watch Paris from LHR to CDG, departure window December 20 to December 20, 2026, 3 nights, 1 passenger, max total price $800.
```

Confirmation input:

```text
Yes, confirmed. Create this watch.
```

Fixture source files:

```text
lambdas/flights-mcp/fixtures/LHR-CDG-2026-12-20.json
lambdas/hotels-mcp/fixtures/Paris-2026-12-20.json
```

Expected fixture prices:

```text
Flight:          $142.30
Cheapest hotel:  $410.00
Total:           $552.30
```

Expected poller result:

```text
snapshot_written
decision_made alert=true
poll_complete watches_errored=0
```

Expected notifier result:

```text
trip-tracker-notifier is invoked
SES send is attempted as a real email
Watches row receives lastAlertedAt and lastAlertedPrice
```

## Manual Poller Trigger

Run from `cmd.exe` in the repo root:

```bat
echo {}> empty.json
aws lambda invoke --function-name trip-tracker-poller --payload file://empty.json --cli-binary-format raw-in-base64-out poll.json --region us-east-1
type poll.json
```

## Log Checks

Poller snapshot writes:

```bat
aws logs filter-log-events --log-group-name /aws/lambda/trip-tracker-poller --filter-pattern "snapshot_written" --max-items 20 --region us-east-1
```

Poller decisions:

```bat
aws logs filter-log-events --log-group-name /aws/lambda/trip-tracker-poller --filter-pattern "decision_made" --max-items 20 --region us-east-1
```

Notifier:

```bat
aws logs filter-log-events --log-group-name /aws/lambda/trip-tracker-notifier --max-items 20 --region us-east-1
```
