/**
 * Tool-level tests for search_offers. Exercises the tool wiring + the
 * maxStops post-filter in addition to the underlying client.
 *
 * Forces fixture mode by setting MCP_MODE before any module loads — the
 * client.js selector reads the env var at cold start.
 */
// MCP_MODE is set in tests/setup.js (loaded via node --import).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import tool from '../tool-search-offers.js';

const [name, _description, _schema, handler] = tool;

test('tool is named search_flight_offers (disambiguated from hotels-mcp)', () => {
    assert.equal(name, 'search_flight_offers');
});

test('handler returns content envelope with all fixture offers when maxStops is unset', async () => {
    const result = await handler({
        origin: 'SFO',
        destination: 'NRT',
        departDate: '2026-10-15',
        pax: 1,
    });
    assert.equal(result.content[0].type, 'text');
    const body = JSON.parse(result.content[0].text);
    assert.equal(body.source, 'fixture');
    assert.equal(body.offers.length, 2);
});

test('handler filters out multi-stop offers when maxStops=0', async () => {
    const result = await handler({
        origin: 'SFO',
        destination: 'NRT',
        departDate: '2026-10-15',
        pax: 1,
        maxStops: 0,
    });
    const body = JSON.parse(result.content[0].text);
    // One offer in the fixture is non-stop both ways; the other has a stop on the outbound.
    assert.equal(body.offers.length, 1);
    for (const offer of body.offers) {
        for (const slice of offer.slices) {
            assert.ok(slice.stops <= 0);
        }
    }
});

test('handler returns empty offers on a fixture miss', async () => {
    const result = await handler({
        origin: 'XXX',
        destination: 'YYY',
        departDate: '2099-01-01',
        pax: 1,
    });
    const body = JSON.parse(result.content[0].text);
    assert.equal(body.source, 'fixture-miss');
    assert.deepEqual(body.offers, []);
});
