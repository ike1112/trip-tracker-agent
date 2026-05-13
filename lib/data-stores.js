const ddb = require('aws-cdk-lib/aws-dynamodb');
const { RemovalPolicy, CfnOutput } = require('aws-cdk-lib');
const { Construct } = require('constructs');

/**
 * DataStoresConstruct provisions the two DynamoDB tables used by the
 * trip-tracker agent (per spec §3):
 *
 *   Watches       – one row per user-defined trip watch
 *                   PK userId  / SK watchId
 *
 *   FareHistory   – one row per polled price snapshot for a watch
 *                   PK watchId / SK timestamp (ISO; query desc for latest-first)
 *                   90-day TTL via the `ttl` attribute
 *
 * Design notes:
 *
 * - Both tables use on-demand (PAY_PER_REQUEST) billing. For personal-scale
 *   traffic this avoids capacity planning and is the cheapest option below
 *   ~10 RCU/WCU sustained.
 *
 * - The Watches table partitions by userId, which is ideal for per-user
 *   CRUD from the chat agent. The poller needs every active watch across
 *   all users on each tick — that requires a Scan with a FilterExpression
 *   on status. Acceptable while watch counts are small (personal use,
 *   dozens of items). If this ever grows, add a GSI keyed on `status` so
 *   the poller can Query by status="active" instead.
 *
 * - FareHistory's TTL keeps the table bounded automatically. 90 days is
 *   long enough for the 30-day-median anomaly logic in spec §5 with ample
 *   headroom, short enough that storage cost stays trivial.
 *
 * - RemovalPolicy.DESTROY matches the existing stack convention. This is
 *   appropriate for a personal/dev project; switch to RETAIN before any
 *   production deploy that holds real user data.
 */
class DataStoresConstruct extends Construct {
    constructor(scope, id, props) {
        super(scope, id, props);

        // -----------------------------------------------------------------------
        // Watches table — user-defined trip watches.
        // PK userId  / SK watchId  → fast per-user Query for chat (list/refine).
        // -----------------------------------------------------------------------
        this.watchesTable = new ddb.Table(this, 'WatchesTable', {
            partitionKey: { name: 'userId', type: ddb.AttributeType.STRING },
            sortKey:      { name: 'watchId', type: ddb.AttributeType.STRING },
            billingMode:  ddb.BillingMode.PAY_PER_REQUEST,
            removalPolicy: RemovalPolicy.DESTROY,
        });

        // -----------------------------------------------------------------------
        // FareHistory table — time-series of price snapshots per watch.
        // PK watchId / SK timestamp (ISO) → Query latest-N descending.
        // TTL on `ttl` (unix epoch seconds) auto-prunes rows after 90 days.
        // -----------------------------------------------------------------------
        this.fareHistoryTable = new ddb.Table(this, 'FareHistoryTable', {
            partitionKey: { name: 'watchId',   type: ddb.AttributeType.STRING },
            sortKey:      { name: 'timestamp', type: ddb.AttributeType.STRING },
            billingMode:  ddb.BillingMode.PAY_PER_REQUEST,
            timeToLiveAttribute: 'ttl',
            removalPolicy: RemovalPolicy.DESTROY,
        });

        // Surface the auto-generated table names so `cdk deploy` output and
        // cdk-outputs.json show what got created. Matches the CfnOutput
        // pattern used elsewhere in this stack.
        new CfnOutput(this, 'WatchesTableName',     { value: this.watchesTable.tableName });
        new CfnOutput(this, 'FareHistoryTableName', { value: this.fareHistoryTable.tableName });
    }
}

module.exports = DataStoresConstruct;
