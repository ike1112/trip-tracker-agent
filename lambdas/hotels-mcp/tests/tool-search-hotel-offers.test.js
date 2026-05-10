// MCP_MODE is set in tests/setup.js (loaded via node --import).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import tool from '../tool-search-hotel-offers.js';

const [name, _description, _schema, handler] = tool;

test('tool is named search_hotel_offers (disambiguated from flights-mcp)', () => {
    assert.equal(name, 'search_hotel_offers');
});

test('handler returns content envelope with all fixture hotels when minStars is unset', async () => {
    const result = await handler({
        city: 'Tokyo',
        checkin: '2026-10-15',
        checkout: '2026-10-20',
        pax: 1,
    });
    assert.equal(result.content[0].type, 'text');
    const body = JSON.parse(result.content[0].text);
    assert.equal(body.source, 'fixture');
    assert.ok(body.hotels.length >= 1);
});

test('handler filters by minStars', async () => {
    const result = await handler({
        city: 'Tokyo',
        checkin: '2026-10-15',
        checkout: '2026-10-20',
        pax: 1,
        minStars: 4,
    });
    const body = JSON.parse(result.content[0].text);
    for (const h of body.hotels) {
        assert.ok(h.stars >= 4, `expected stars >= 4, got ${h.stars} for ${h.hotelName}`);
    }
    // Both 4* and 5* should survive; the 3* should not.
    const names = body.hotels.map((h) => h.hotelName).sort();
    assert.ok(names.includes('Park Central Tokyo'));
    assert.ok(names.includes('Imperial Grand Tokyo'));
    assert.ok(!names.includes('Shibuya Business Hotel'));
});

test('handler returns empty hotels on a fixture miss', async () => {
    const result = await handler({
        city: 'Atlantis',
        checkin: '2099-01-01',
        checkout: '2099-01-05',
        pax: 1,
    });
    const body = JSON.parse(result.content[0].text);
    assert.equal(body.source, 'fixture-miss');
    assert.deepEqual(body.hotels, []);
});
