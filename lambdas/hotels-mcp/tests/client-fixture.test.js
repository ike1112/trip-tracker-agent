/**
 * Fixture-client unit tests. If these pass, a forking reviewer can run
 * the Lambda end-to-end without a LiteAPI account.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { searchHotels, getHotelDetails } from '../client-fixture.js';

test('searchHotels returns hotels from a matching fixture', async () => {
    const result = await searchHotels({
        city: 'Tokyo',
        checkin: '2026-10-15',
        checkout: '2026-10-20',
        pax: 1,
    });
    assert.equal(result.source, 'fixture');
    assert.equal(result.fixtureName, 'Tokyo-2026-10-15.json');
    assert.ok(result.hotels.length >= 1);
    for (const h of result.hotels) {
        assert.ok(typeof h.id === 'string');
        assert.ok(typeof h.hotelName === 'string');
        assert.ok(typeof h.totalAmount === 'number');
        assert.equal(h.currency, 'USD');
    }
});

test('searchHotels returns empty list on fixture miss', async () => {
    const result = await searchHotels({
        city: 'Atlantis',
        checkin: '2099-01-01',
        checkout: '2099-01-05',
        pax: 1,
    });
    assert.equal(result.source, 'fixture-miss');
    assert.deepEqual(result.hotels, []);
});

test('getHotelDetails finds a hotel across fixture files', async () => {
    const hotel = await getHotelDetails({ hotelId: 'lp_paris_left_bank' });
    assert.ok(hotel);
    assert.equal(hotel.id, 'lp_paris_left_bank');
    assert.equal(hotel.source, 'fixture');
    assert.equal(hotel.fixtureName, 'Paris-2026-12-20.json');
});

test('getHotelDetails returns null for an unknown hotel id', async () => {
    const hotel = await getHotelDetails({ hotelId: 'lp_does_not_exist' });
    assert.equal(hotel, null);
});
