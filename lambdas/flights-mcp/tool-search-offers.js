import { z } from 'zod';
import { client } from './client.js';

const description = (
    'Search for flight offers between an origin and destination on specific dates. ' +
    'Returns the top offers sorted by total price with airline, stops, and timing. ' +
    'Origin may be a single airport code or a list (e.g. ["SFO","OAK","SJC"]). ' +
    'Destination should be a single airport code. Use get_flight_offer_details for ' +
    'the full fare rules and booking link for a specific offer.'
);

const schema = {
    origin: z.union([z.string(), z.array(z.string())]),
    destination: z.string(),
    departDate: z.string().describe('Departure date in YYYY-MM-DD'),
    returnDate: z.string().optional().describe('Return date in YYYY-MM-DD (omit for one-way)'),
    pax: z.number().int().positive().default(1),
    maxStops: z.number().int().min(0).optional(),
};

async function handler({ origin, destination, departDate, returnDate, pax, maxStops }) {
    const result = await client.searchOffers({ origin, destination, departDate, returnDate, pax });
    let offers = result.offers ?? [];
    if (typeof maxStops === 'number') {
        offers = offers.filter((o) =>
            (o.slices ?? []).every((s) => (s.stops ?? 0) <= maxStops)
        );
    }
    return {
        content: [
            { type: 'text', text: JSON.stringify({ source: result.source, offers }, null, 2) },
        ],
    };
}

// Renamed from `search_offers` to disambiguate from the hotels-mcp tool of the
// same shape — the agent merges both servers' tool lists into one toolbox, so
// the names must be globally unique.
export default ['search_flight_offers', description, schema, handler];
