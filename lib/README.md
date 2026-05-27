# lib Folder Guide

This folder contains the AWS CDK construct modules that define all deployed infrastructure for Trip Tracker.

## How This Folder Is Organized

- One file per construct or concern.
- Most files export a construct class used by the stack.
- The stack entry file wires all constructs together.

## File By File Reference

| File | Primary role | What it provides |
|---|---|---|
| [trip-tracker-stack.js](trip-tracker-stack.js) | Top-level stack composition | Creates and wires Cognito, secrets, MCP servers, data stores, agent, poller, notifier, budget alarm, and observability dashboard |
| [agent.js](agent.js) | Travel agent API plane | Provisions travel-agent Lambda, dependencies layer, session S3 bucket, API Gateway, agent authorizer, and resource-scoped Bedrock IAM grants |
| [cognito.js](cognito.js) | Identity setup | Provisions Cognito user pool, app client, domain, seeded users, and outputs including JWKS and login URLs |
| [secrets.js](secrets.js) | Internal signing keys | Provisions per-component Secrets Manager secrets for agent and poller JWT signing |
| [data-stores.js](data-stores.js) | Persistence layer | Provisions Watches and FareHistory DynamoDB tables, including status-index GSI and TTL on fare history |
| [flights-mcp-server.js](flights-mcp-server.js) | Flights MCP service infra | Provisions flights MCP Lambda, API Gateway endpoint, custom authorizer, and exposes endpoint/function/api refs |
| [hotels-mcp-server.js](hotels-mcp-server.js) | Hotels MCP service infra | Provisions hotels MCP Lambda, API Gateway endpoint, custom authorizer, and exposes endpoint/function/api refs |
| [poller-server.js](poller-server.js) | Scheduled decision pipeline infra | Provisions poller Lambda, EventBridge schedule, Bedrock invoke permission, notifier invoke permission, and data store grants |
| [notifier-server.js](notifier-server.js) | Alert delivery infra | Provisions notifier Lambda, SES send permission scoped to sender identity, and DynamoDB UpdateItem permission for alert writeback |
| [observability-dashboard.js](observability-dashboard.js) | Monitoring dashboard | Provisions CloudWatch dashboard widgets for poller metrics, Lambda health, API errors, SES metrics, and placeholder alarm row |
| [budget-alarm.js](budget-alarm.js) | Cost safety guardrail | Provisions monthly AWS budget with email notifications at 80 percent actual and 100 percent forecasted |

## Construct Contracts And Exposed Properties

### Stack entry: [trip-tracker-stack.js](trip-tracker-stack.js)

This is the orchestrator for the whole deployment. It does not create application logic itself; it composes other constructs and passes references between them.

Notable wiring outcomes:
- MCP endpoints from flights and hotels are injected into the agent and poller.
- Secrets from [secrets.js](secrets.js) are passed to all consumers that mint or verify JWTs.
- Tables from [data-stores.js](data-stores.js) are passed to agent, poller, and notifier.
- All function and API references are passed into [observability-dashboard.js](observability-dashboard.js).

### Agent plane: [agent.js](agent.js)

Main resources:
- travel-agent-on-lambda function
- dependencies Lambda layer for Python runtime packages
- S3 bucket for session state
- travel-agent-api (REST API)
- travel-agent-authorizer function

Exposes:
- this.function
- this.api
- this.authorizerFunction

Important behavior encoded in construct:
- Bedrock model id validation at synth time
- resource-scoped Bedrock IAM grants
- environment wiring for MCP endpoints, Cognito JWKS, table names, and session bucket

### Identity: [cognito.js](cognito.js)

Main resources:
- User pool
- User pool client
- Cognito domain
- Seed users Alice and Bob

Provides:
- cognitoJwksUrl return value to the caller
- CloudFormation outputs for login and OIDC discovery URLs

### Secrets: [secrets.js](secrets.js)

Main resources:
- agent signing secret
- poller signing secret

Provides:
- this.agentJwtSecret
- this.pollerJwtSecret

### Data stores: [data-stores.js](data-stores.js)

Main resources:
- Watches table: partition key userId, sort key watchId
- status-index GSI for active watch enumeration
- FareHistory table: partition key watchId, sort key timestamp, ttl attribute

Provides:
- this.watchesTable
- this.fareHistoryTable
- table name outputs

### Flights MCP infra: [flights-mcp-server.js](flights-mcp-server.js)

Main resources:
- flights-mcp-server function
- flights-mcp-server-authorizer function
- flights-mcp-api with mcp resource and token authorizer

Provides:
- this.endpoint
- this.function
- this.api
- this.authorizerFunction

### Hotels MCP infra: [hotels-mcp-server.js](hotels-mcp-server.js)

Main resources:
- hotels-mcp-server function
- hotels-mcp-server-authorizer function
- hotels-mcp-api with mcp resource and token authorizer

Provides:
- this.endpoint
- this.function
- this.api
- this.authorizerFunction

### Poller infra: [poller-server.js](poller-server.js)

Main resources:
- trip-tracker-poller function
- EventBridge schedule rule

Provides:
- this.function
- this.scheduleRule

Important behavior encoded in construct:
- schedule interval and timeout validation/clamping
- bedrock mode validation and bedrock model id validation
- optional inference-profile ARN validation
- scoped IAM for Bedrock and notifier invoke

### Notifier infra: [notifier-server.js](notifier-server.js)

Main resources:
- trip-tracker-notifier function

Provides:
- this.function
- function and sender outputs

Important behavior encoded in construct:
- sender and recipient email validation
- ses mode validation
- SES send permission scoped to sender identity ARN
- DynamoDB UpdateItem-only style grant for writeback path

### Observability: [observability-dashboard.js](observability-dashboard.js)

Main resources:
- CloudWatch dashboard with deterministic widget order

Provides:
- this.dashboard
- exported constants for poller metric namespace and names

Dashboard sections include:
- poller EMF counters
- lambda invocations, errors, and p99 duration
- API Gateway 4xx and 5xx metrics
- SES send, bounce, complaint metrics

### Budget guardrail: [budget-alarm.js](budget-alarm.js)

Main resources:
- one account-level monthly cost budget

Behavior:
- validates email context at synth
- configures two notifications: 80 percent actual and 100 percent forecasted

## Shared Patterns In This Folder

- Synth-time validation is used heavily to fail early on bad config.
- IAM grants are intentionally narrow and resource-scoped.
- Most constructs expose references through instance properties rather than constructor returns.
- API-facing lambdas use authorizers and x-ray tracing by default.

## Context Values Used Across Constructs

Common CDK context keys consumed in this folder:
- mcpMode
- duffelApiKey
- liteApiKey
- agentBedrockModelId
- bedrockModelId
- bedrockMode
- bedrockInferenceProfileArn
- pollIntervalMinutes
- lambdaTimeoutSeconds
- notifierSenderEmail
- notifierRecipientEmail
- budgetAlarmEmail

If these are missing or malformed, many constructs intentionally throw at synth time to prevent broken deploys.
