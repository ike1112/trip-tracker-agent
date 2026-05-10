import { z } from 'zod';
import { client } from './client.js';

const description = (
    'Search for hotel offers in a destination city across a check-in/check-out ' +
    'window. Returns the top offers sorted by total price with star rating, ' +
    'refundability, and a booking deep link. Use get_hotel_details after this ' +
    'for amenities + photos of a specific hotel.'
);

const schema = {
    city: z.string().describe('Destination city (e.g. "Tokyo", "Paris")'),
    checkin: z.string().describe('Check-in date in YYYY-MM-DD'),
    checkout: z.string().describe('Check-out date in YYYY-MM-DD'),
    pax: z.number().int().positive().default(1),
    minStars: z.number().int().min(1).max(5).optional(),
};

async function handler({ city, checkin, checkout, pax, minStars }) {
    const result = await client.searchHotels({ city, checkin, checkout, pax, minStars });
    let hotels = result.hotels ?? [];
    // Defense in depth — the live client also filters server-side, but the
    // fixture client doesn't, and we want the tool semantics to be identical
    // either way.
    if (typeof minStars === 'number') {
        hotels = hotels.filter((h) => (h.stars ?? 0) >= minStars);
    }
    return {
        content: [
            { type: 'text', text: JSON.stringify({ source: result.source, hotels }, null, 2) },
        ],
    };
}

// Globally-unique tool name — see ADR 0002 + slice 4 commit message.
export default ['search_hotel_offers', description, schema, handler];
