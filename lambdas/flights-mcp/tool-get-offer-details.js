import { z } from 'zod';
import { client } from './client.js';

const description = (
    'Return the full details of a single flight offer, including segment-by-segment ' +
    'timings and the booking link. Use this after search_flight_offers when the ' +
    'user wants to act on a specific result.'
);

const schema = {
    offerId: z.string(),
};

async function handler({ offerId }) {
    const details = await client.getOfferDetails({ offerId });
    if (!details) {
        return {
            content: [{ type: 'text', text: JSON.stringify({ error: 'offer_not_found', offerId }) }],
            isError: true,
        };
    }
    return {
        content: [{ type: 'text', text: JSON.stringify(details, null, 2) }],
    };
}

// Renamed from `get_offer_details` to disambiguate from the hotels-mcp tool. (slice 4)
export default ['get_flight_offer_details', description, schema, handler];
