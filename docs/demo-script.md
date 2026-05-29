# Demo Script

Use this as a short recording outline after a clean fixture or live run.

## Goal

Show that this is more than a chat demo: the agent creates a structured watch,
the scheduled path can poll prices, and the operator can verify behavior in AWS.

## 60-90 Second Flow

1. Open the web UI and log in as `Alice`.
2. Create a watch in chat:

   ```text
   Watch Tokyo from SFO to NRT, departure window October 15 to October 15, 2026, 5 nights, 1 passenger, max total price $1500.
   ```

3. Confirm the watch when the agent echoes the details.
4. Ask for status:

   ```text
   What's happening with my watches?
   ```

5. Show the CloudWatch dashboard or Lambda logs for the poller.
6. If using the Paris fixture scenario, show the alert email and the
   `lastAlertedAt` / `lastAlertedPrice` writeback.

## Evidence To Capture

- Web UI conversation.
- CloudWatch dashboard with timestamp visible.
- Poller log lines for `snapshot_written`, `decision_made`, and `poll_complete`.
- Notifier log line for `notification_sent` if the alert path was triggered.
- DynamoDB watch row with sensitive IDs redacted.

See [`fixture-poller-notifier-scenarios.md`](./fixture-poller-notifier-scenarios.md)
for the deterministic scheduled-path scenarios.
