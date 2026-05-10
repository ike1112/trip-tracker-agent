/**
 * Unit tests for the fixture client. Uses Node's built-in test runner
 * (`node --test`) — no jest/vitest/mocha dependency.
 *
 * These tests are the production-readiness signal for fixture mode:
 * if they pass, a forking reviewer can run the Lambda end-to-end with
 * no Duffel account.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { searchOffers, getOfferDetails } from '../client-fixture.js';

test('searchOffers returns offers from a matching fixture file', async () => {
    const result = await searchOffers({
        origin: 'SFO',
        destination: 'NRT',
        departDate: '2026-10-15',
    });
    assert.equal(result.source, 'fixture');
    assert.equal(result.fixtureName, 'SFO-NRT-2026-10-15.json');
    assert.ok(result.offers.length >= 1);
    for (const offer of result.offers) {
        assert.ok(typeof offer.id === 'string');
        assert.ok(typeof offer.totalAmount === 'number');
        assert.ok(Array.isArray(offer.slices));
    }
});

test('searchOffers returns empty list on fixture miss', async () => {
    const result = await searchOffers({
        origin: 'XXX',
        destination: 'YYY',
        departDate: '2099-01-01',
    });
    assert.equal(result.source, 'fixture-miss');
    assert.deepEqual(result.offers, []);
});

test('searchOffers handles origin as a list by picking the first code', async () => {
    const result = await searchOffers({
        origin: ['SFO', 'OAK', 'SJC'],
        destination: 'NRT',
        departDate: '2026-10-15',
    });
    assert.equal(result.source, 'fixture');
});

test('getOfferDetails finds an offer across fixture files', async () => {
    const offer = await getOfferDetails({ offerId: 'off_0010BA_LHR_CDG_20261220' });
    assert.ok(offer);
    assert.equal(offer.id, 'off_0010BA_LHR_CDG_20261220');
    assert.equal(offer.owner, 'BA');
    assert.equal(offer.source, 'fixture');
    assert.equal(offer.fixtureName, 'LHR-CDG-2026-12-20.json');
});

test('getOfferDetails returns null for an unknown offer id', async () => {
    const offer = await getOfferDetails({ offerId: 'off_does_not_exist' });
    assert.equal(offer, null);
});
