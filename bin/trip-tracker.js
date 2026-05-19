#!/usr/bin/env node

const cdk = require('aws-cdk-lib');
const { TripTrackerStack } = require('../lib/trip-tracker-stack');

const app = new cdk.App();
new TripTrackerStack(app, 'TripTrackerStack');