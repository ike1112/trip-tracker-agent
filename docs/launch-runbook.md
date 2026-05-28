# Launch Runbook: Fixture Rehearsal and Live Evidence Run

This runbook has two separate tracks:

- **Track A: Fixture/Stub Rehearsal** deploys the stack with fixture travel
  providers, stubbed poller Bedrock decisions, and real SES sends. Use it to
  verify infrastructure, Cognito, the web UI, and fixture-mode behavior before
  spending time on live evidence.
- **Track B: Live Launch** deploys with real Duffel, LiteAPI, Bedrock poller
  decisions, SES email, and the 7-day price-watch evidence run.

All command blocks are written for Windows `cmd.exe`. Replace every
`PUT_..._HERE` value before running a command.

## What The Stack Deploys

Both tracks deploy the same CDK stack, `TripTrackerStack`, defined by
`bin/trip-tracker.js` and `lib/trip-tracker-stack.js`.

The stack creates:

- Cognito demo users and hosted-login configuration for the web UI.
- API Gateway endpoints for the chat agent and MCP servers.
- Lambda functions for the travel agent, authorizers, flights MCP, hotels MCP,
  poller, and notifier.
- DynamoDB `Watches` and `FareHistory` tables.
- Secrets Manager signing secrets for per-component JWTs.
- EventBridge schedule for the poller.
- CloudWatch dashboard and Lambda logs.
- AWS Budget named `trip-tracker-monthly-cost`.
- SES send permission for the configured sender email.

`--require-approval never` makes CDK non-interactive after the command starts.
Use it only after you have reviewed the command, AWS account, region, and
context values.

## Shared Phase 0: Local Setup

Run this once before either track.

- [ ] Open `cmd.exe` in the repo root, the directory containing `package.json`
  and `cdk.json`.

  ```bat
  cd
  dir package.json cdk.json
  ```

- [ ] Confirm tool versions:

  ```bat
  node --version
  python --version
  docker ps
  cdk --version
  aws --version
  ```

  Expected:

  - Node.js: `v22.x`
  - Python: `3.12.x`
  - Docker daemon reachable without elevation

- [ ] Confirm AWS identity and region:

  ```bat
  aws sts get-caller-identity
  aws configure get region
  ```

  Record:

  - AWS account ID: `____________________`
  - AWS region: `____________________`

- [ ] Bootstrap CDK in the deploy account and region if needed:

  ```bat
  aws cloudformation describe-stacks --stack-name CDKToolkit
  ```

  If the stack does not exist:

  ```bat
  cdk bootstrap aws://PUT_ACCOUNT_ID_HERE/PUT_REGION_HERE
  ```

- [ ] Install Node dependencies:

  ```bat
  npm ci
  npm --prefix lambdas/agent-authorizer ci
  npm --prefix lambdas/mcp-authorizer ci
  npm --prefix lambdas/flights-mcp ci
  npm --prefix lambdas/hotels-mcp ci
  ```

- [ ] Install the pinned Python test environment:

  ```bat
  python -m venv .venv-tests
  .venv-tests\Scripts\activate.bat
  python -m pip install --upgrade pip
  python -m pip install -r requirements-test.txt
  ```

- [ ] Create a local deploy scratch file:

  ```bat
  copy .env.example .env
  notepad .env
  ```

  CDK does not read `.env`; it is a gitignored scratch pad for deploy values.

## Shared Phase 1: Pre-Deploy Tests

All commands run from the repo root.

- [ ] CDK construct tests:

  ```bat
  npm test
  ```

- [ ] Node Lambda and authorizer tests:

  ```bat
  npm --prefix lambdas/agent-authorizer test
  npm --prefix lambdas/mcp-authorizer test
  npm --prefix lambdas/flights-mcp test
  npm --prefix lambdas/hotels-mcp test
  ```

- [ ] Python Lambda, web, and eval tests:

  ```bat
  .venv-tests\Scripts\python.exe -m pytest lambdas/poller/tests -q
  cd lambdas\notifier
  ..\..\.venv-tests\Scripts\python.exe -m pytest tests -q
  cd ..\..
  .venv-tests\Scripts\python.exe -m pytest lambdas/travel-agent/tests -q
  cd web
  ..\.venv-tests\Scripts\python.exe -m pytest tests -q
  cd ..
  cd evals
  ..\.venv-tests\Scripts\python.exe -m pytest tests -q
  cd ..
  ```

Stop here if any gate fails.

## Track A: Fixture/Stub Rehearsal

Use this track for a deploy rehearsal. It validates CDK, Cognito, the web app,
fixture MCP responses, the poller decision stub, real SES email sends from the
notifier, the dashboard, and the budget construct.

Important caveat: the chat agent still uses Bedrock live by default, even in
fixture/stub deploys. Enable access for the agent model in Bedrock unless you
also change the agent implementation.

### Fixture Phase A0: Access Requirements

- [ ] Bedrock access is enabled for the chat agent model:
  `us.anthropic.claude-sonnet-4-5-20250929-v1:0`.
- [ ] AWS Marketplace access for Anthropic models is cleared through the
  Bedrock console **Manage model access** flow.
- [ ] `notifierSenderEmail`, `notifierRecipientEmail`, and `budgetAlarmEmail`
  are valid addresses. `notifierSenderEmail` must be verified in SES; if your
  SES account is still in sandbox, `notifierRecipientEmail` must be verified too.

### Fixture Phase A1: Deploy Fixture Stack

Run from the repo root:

```bat
cdk deploy ^
  --require-approval never ^
  -c mcpMode=fixture ^
  -c bedrockMode=stub ^
  -c notifierSenderEmail=isabelkeyan@gmail.com ^
  -c notifierRecipientEmail=isabelkeyan@gmail.com ^
  -c budgetAlarmEmail=isabelkeyan@gmail.com
```

### Fixture Phase A2: Save Outputs

```bat
aws cloudformation describe-stacks ^
  --stack-name TripTrackerStack ^
  --query "Stacks[0].Outputs" ^
  --output table
```

```bat
for /f "usebackq delims=" %A in (`aws cloudformation describe-stacks --stack-name TripTrackerStack --query "Stacks[0].Outputs[?contains(OutputKey, 'PollerFunctionName')]|[0].OutputValue" --output text`) do set POLLER_FN_NAME=%A
for /f "usebackq delims=" %A in (`aws cloudformation describe-stacks --stack-name TripTrackerStack --query "Stacks[0].Outputs[?contains(OutputKey, 'FareHistoryTableName')]|[0].OutputValue" --output text`) do set FARE_HISTORY_TABLE_NAME=%A
for /f "usebackq delims=" %A in (`aws cloudformation describe-stacks --stack-name TripTrackerStack --query "Stacks[0].Outputs[?contains(OutputKey, 'WatchesTableName')]|[0].OutputValue" --output text`) do set WATCHES_TABLE_NAME=%A
echo %POLLER_FN_NAME%
echo %FARE_HISTORY_TABLE_NAME%
echo %WATCHES_TABLE_NAME%
```

### Fixture Phase A3: Web Smoke Test

- [ ] Prepare the web app. `prep-web.sh` is a POSIX shell script; from
  `cmd.exe`, run it through `bash` if Git Bash or WSL provides `bash` on your
  PATH. It writes `web\.env`, including a generated `SESSION_SECRET_KEY` for
  the web session cookie:

  ```bat
  bash ./prep-web.sh
  ```

- [ ] Start the web UI in a separate `cmd.exe` terminal:

  ```bat
  cd web
  python -m venv .venv
  .venv\Scripts\activate.bat
  python -m pip install --upgrade pip
  python -m pip install -r requirements.txt
  python app.py
  ```

- [ ] Open `http://localhost:8000/chat/` and log in as `Alice`.
- [ ] Run this exact fixture chat script.

  Input 1:

  ```text
  Hi
  ```

  Expected output: the agent greets `Alice` and describes the trip-price
  tracking workflow.

  Input 2:

  ```text
  Watch Tokyo from SFO to NRT, departure window October 15 to October 15, 2026, 5 nights, 1 passenger, max total price $1500.
  ```

  Expected output: the agent echoes the full Tokyo watch in plain English and
  asks for confirmation before saving.

  Input 3:

  ```text
  Yes, confirmed. Create this watch.
  ```

  Expected output: the agent confirms the watch was created. Verify one new
  active Tokyo row exists in DynamoDB.

  Input 4:

  ```text
  What's happening with my watches?
  ```

  Expected output: one readable headline for the active Tokyo watch.

  Input 5:

  ```text
  How much is Tokyo right now for SFO to NRT on October 15, 2026 for 5 nights?
  ```

  Expected output: a fixture-backed flight + hotel price for Tokyo.

### Fixture Phase A4: Optional Fixture Cleanup

Destroy the fixture stack if you are done rehearsing and do not want it to
remain in the account.

```bat
cdk destroy
```

## Track B: Live Launch

Use this track only after the shared tests pass and fixture rehearsal is good.
This is the evidence-producing path: real travel providers, real poller
Bedrock decision, real SES email, and a 7-day `FareHistory` trend.

### Live Phase B0: Live Access Requirements

- [ ] Bedrock model access is enabled in the deploy region.

  Current defaults:

  - Chat agent: `us.anthropic.claude-sonnet-4-5-20250929-v1:0`
    from `lib/agent.js`.
  - Poller decision: `claude-haiku-4-5-20251001`
    from `lib/poller-server.js` and `lambdas/poller/bedrock_decide.py`.

  For the chat agent's `us.` inference profile, enable the underlying
  foundation model in `us-east-1`, `us-east-2`, and `us-west-2`.

- [ ] AWS Marketplace access for Anthropic Bedrock models is cleared through
  the Bedrock console **Manage model access** flow.
- [ ] Duffel live access token is available. It must not be a test token.
- [ ] LiteAPI production key is available. It must not be sandbox/canned data.
- [ ] SES sender and recipient identities are verified in the deploy region.
  Staying in the SES sandbox is acceptable for this launch if both identities
  are verified.
- [ ] `.env` contains:

  - `duffelApiKey`
  - `liteApiKey`
  - `notifierSenderEmail`
  - `notifierRecipientEmail`
  - `budgetAlarmEmail` if different from the alert recipient

### Live Phase B1: Deploy Live Stack

Run from the repo root:

```bat
cdk deploy ^
  --require-approval never ^
  -c mcpMode=live ^
  -c duffelApiKey=PUT_DUFFEL_LIVE_TOKEN_HERE ^
  -c liteApiKey=PUT_LITEAPI_PROD_KEY_HERE ^
  -c bedrockMode=live ^
  -c notifierSenderEmail=PUT_VERIFIED_SES_SENDER_EMAIL_HERE ^
  -c notifierRecipientEmail=PUT_VERIFIED_SES_RECIPIENT_EMAIL_HERE ^
  -c budgetAlarmEmail=PUT_BUDGET_ALARM_EMAIL_HERE
```

### Live Phase B2: Save Outputs And Confirm Budget

```bat
aws cloudformation describe-stacks ^
  --stack-name TripTrackerStack ^
  --query "Stacks[0].Outputs" ^
  --output table
```

```bat
for /f "usebackq delims=" %A in (`aws cloudformation describe-stacks --stack-name TripTrackerStack --query "Stacks[0].Outputs[?contains(OutputKey, 'PollerFunctionName')]|[0].OutputValue" --output text`) do set POLLER_FN_NAME=%A
for /f "usebackq delims=" %A in (`aws cloudformation describe-stacks --stack-name TripTrackerStack --query "Stacks[0].Outputs[?contains(OutputKey, 'FareHistoryTableName')]|[0].OutputValue" --output text`) do set FARE_HISTORY_TABLE_NAME=%A
for /f "usebackq delims=" %A in (`aws cloudformation describe-stacks --stack-name TripTrackerStack --query "Stacks[0].Outputs[?contains(OutputKey, 'WatchesTableName')]|[0].OutputValue" --output text`) do set WATCHES_TABLE_NAME=%A
echo %POLLER_FN_NAME%
echo %FARE_HISTORY_TABLE_NAME%
echo %WATCHES_TABLE_NAME%
```

```bat
aws budgets describe-budget ^
  --account-id PUT_ACCOUNT_ID_HERE ^
  --budget-name trip-tracker-monthly-cost
```

Confirm the budget notification email is received by `budgetAlarmEmail` or
`notifierRecipientEmail` before starting any live polling.

### Live Phase B3: Live Smoke Test

The 7-day clock does not start until every item in this phase is complete.

- [ ] Prepare the web app:

  ```bat
  bash ./prep-web.sh
  ```

  This writes `web\.env`, including a generated `SESSION_SECRET_KEY` for the
  web session cookie. If you already have a `web\.env` from an older run and
  only need to add the missing key:

  ```bat
  python -c "import secrets; print('SESSION_SECRET_KEY=' + secrets.token_urlsafe(48))" >> web\.env
  ```

- [ ] Start the web UI in a separate `cmd.exe` terminal:

  ```bat
  cd web
  python -m venv .venv
  .venv\Scripts\activate.bat
  python -m pip install --upgrade pip
  python -m pip install -r requirements.txt
  python app.py
  ```

- [ ] Open `http://localhost:8000/chat/` and log in as `Alice`.

- [ ] Choose the trend-watch trip.

  Criteria:

  - high-liquidity origin/destination
  - departure 4 to 10 weeks out
  - non-holiday date window
  - both flight and hotel prices returned in USD

  Record:

  - Origin: `__________`
  - Destination: `__________`
  - Departure window: `__________`
  - Nights: `__________`

- [ ] Create the trend watch through chat.

  Input:

  ```text
  Watch DESTINATION from ORIGIN to DESTINATION_AIRPORT, departure window EARLIEST_DEPART_DATE to LATEST_DEPART_DATE, NIGHTS nights, 1 passenger, max total price MAX_TOTAL_PRICE.
  ```

  Example:

  ```text
  Watch Tokyo from SFO to NRT, departure window October 15 to October 15, 2026, 5 nights, 1 passenger, max total price $1500.
  ```

  Expected output: the agent echoes the full watch in plain English and asks
  for confirmation before saving.

  Confirmation input:

  ```text
  Yes, confirmed. Create this watch.
  ```

  Expected output: the agent confirms the watch was created.

  The structured watch response proves:

  `web -> API Gateway -> travel-agent Lambda -> Bedrock -> DynamoDB`

- [ ] Verify live provider search in chat.

  ```text
  How much is DESTINATION right now for ORIGIN to DESTINATION_AIRPORT on EARLIEST_DEPART_DATE for NIGHTS nights?
  ```

  Expected output: a real non-zero price.

- [ ] Invoke the poller manually:

  ```bat
  echo {}> empty.json
  aws lambda invoke ^
    --function-name %POLLER_FN_NAME% ^
    --payload file://empty.json ^
    --cli-binary-format raw-in-base64-out ^
    poll.json
  type poll.json
  ```

- [ ] Check poller logs:

  ```bat
  aws logs filter-log-events ^
    --log-group-name /aws/lambda/%POLLER_FN_NAME% ^
    --filter-pattern "snapshot_written" ^
    --max-items 10

  aws logs filter-log-events ^
    --log-group-name /aws/lambda/%POLLER_FN_NAME% ^
    --filter-pattern "poll_complete" ^
    --max-items 10
  ```

  Required:

  - `snapshot_written` exists
  - `flight_price` is numeric and non-null
  - `hotel_price` is numeric and non-null
  - `poll_complete` shows `watches_errored: 0`

  If logs show `snapshot_skipped`, choose a better route/date window and retry.
  Do not start the 7-day clock on a skipped snapshot.

- [ ] Record the trend watch ID:

  ```bat
  echo {"#s":"status"}> expr-names.json
  aws dynamodb scan ^
    --table-name %WATCHES_TABLE_NAME% ^
    --projection-expression "userId, watchId, #s, origin, destination, departDate, returnDate, maxTotalPrice" ^
    --expression-attribute-names file://expr-names.json ^
    --output table
  ```

  Watch TREND ID: `____________________`

- [ ] Confirm the first `FareHistory` row:

  ```bat
  set WATCH_TREND_ID=PUT_WATCH_TREND_ID_HERE
  echo {":w":{"S":"%WATCH_TREND_ID%"}}> watch-key.json
  aws dynamodb query ^
    --table-name %FARE_HISTORY_TABLE_NAME% ^
    --key-condition-expression "watchId = :w" ^
    --expression-attribute-values file://watch-key.json ^
    --output table
  ```

  Required: at least one row with numeric `flightPrice`, `hotelPrice`, and
  `totalPrice`.

### Live Phase B4: Run The 7-Day Trend Watch

Use two separate watches.

**Watch TREND**

- [ ] Set `maxTotalPrice` well below the current live total so this watch does
  not alert.
- [ ] Start timestamp: `____________________`
- [ ] Target evidence timestamp, 7 days later: `____________________`
- [ ] Expected cadence: every 240 minutes unless `pollIntervalMinutes` was
  changed at deploy.
- [ ] Expected rows over 7 days: about 42.
- [ ] Minimum acceptable rows: 38, all with numeric non-null `flightPrice`,
  `hotelPrice`, and `totalPrice`.

Daily gate:

```bat
aws dynamodb query ^
  --table-name %FARE_HISTORY_TABLE_NAME% ^
  --key-condition-expression "watchId = :w" ^
  --expression-attribute-values file://watch-key.json ^
  --select COUNT
```

Check at the end of Day 1 and Day 2. If rows are missing, sparse, or null on
either price leg, stop the clock, fix the issue, reset the start timestamp, and
restart the 7-day run.

Likely causes:

- route/date window has poor inventory
- provider key expired or is not live-enabled
- EventBridge rule disabled
- Bedrock/model access failure
- fixture-vs-live parsing drift

**Watch ALERT**

- [ ] Create this watch near demo-recording day.
- [ ] Set `maxTotalPrice` just above the watch's current live total.
- [ ] Let the next poll send one alert.
- [ ] Screenshot or save the email with the model-written reason visible.
- [ ] Archive this watch after capture.

Never use Watch ALERT rows for the 7-day trend curve.

### Live Phase B5: Documentation During The Wait

Documentation changes are safe while the 7-day clock runs.

- [ ] Update README first-screen proof block:

  1. one-line pitch
  2. fixture-mode quickstart
  3. architecture diagram link
  4. proof bullets with links to eval results and `docs/evidence/`
  5. production-readiness delta
  6. deeper links: specs, ADRs, threat model

- [ ] Add a short line near Bedrock/ADR 0004 references explaining why the
  model decision earns its runtime call: it is backed by eval
  decision-quality evidence.
- [ ] Draft `docs/demo-script.md` for a 60-90 second recording.

### Live Phase B6: Export Evidence Before Teardown

Run from the repo root after the 7-day window completes.

- [ ] Create the evidence directory:

  ```bat
  if not exist docs\evidence mkdir docs\evidence
  ```

- [ ] Export Watch TREND rows:

  ```bat
  set WATCH_TREND_ID=PUT_WATCH_TREND_ID_HERE
  echo {":w":{"S":"%WATCH_TREND_ID%"}}> watch-key.json
  aws dynamodb query ^
    --table-name %FARE_HISTORY_TABLE_NAME% ^
    --key-condition-expression "watchId = :w" ^
    --expression-attribute-values file://watch-key.json ^
    --output json > docs\evidence\fare-history-trend.json
  ```

  Redact or truncate `userId` values before committing. Keep `watchId`; it is
  needed to correlate rows, screenshots, and logs.

- [ ] Export poller log evidence:

  ```bat
  aws logs filter-log-events ^
    --log-group-name /aws/lambda/%POLLER_FN_NAME% ^
    --filter-pattern "snapshot_written" ^
    --output json > docs\evidence\poller-snapshot-written.json

  aws logs filter-log-events ^
    --log-group-name /aws/lambda/%POLLER_FN_NAME% ^
    --filter-pattern "decision_made" ^
    --output json > docs\evidence\poller-decision-made.json

  aws logs filter-log-events ^
    --log-group-name /aws/lambda/%POLLER_FN_NAME% ^
    --filter-pattern "poll_complete" ^
    --output json > docs\evidence\poller-poll-complete.json
  ```

- [ ] Capture screenshots:

  - trend chart or status view, with timestamp and watch ID visible
  - Watch ALERT email, with model-written reason visible
  - CloudWatch dashboard, with timestamp visible and account number redacted
  - any demo stills/GIF fallback if using an external recording link

- [ ] Commit evidence:

  ```bat
  git status docs/evidence/
  git add docs/evidence/
  git commit -m "Add launch evidence exports"
  git log -1 --stat -- docs/evidence/
  ```

### Live Phase B7: Demo And Teardown

- [ ] Record the demo from `docs/demo-script.md`.
- [ ] Add README links to:

  - committed evidence files
  - screenshots
  - demo recording or fallback stills
  - latest passing GitHub Actions run on `main`, or a status badge

- [ ] Mark the corresponding production-readiness checklist items complete.
- [ ] Destroy only after evidence is committed and screenshots are captured:

  ```bat
  cdk destroy
  ```

## Cost Guardrails

Expected live-run cost is under $1:

- Bedrock Haiku/Sonnet calls: low volume, cents-scale
- Duffel/LiteAPI search: no booking, search-only
- SES: one alert email
- DynamoDB/EventBridge/Lambda: personal-scale usage

The practical cost risk is a runaway poll loop or misconfigured cadence. Keep
the `trip-tracker-monthly-cost` AWS Budget in place before live polling, and
run `cdk destroy` after evidence capture.
