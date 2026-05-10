import { z } from 'zod';
import { client } from './client.js';

const description = (
    'Return the full details of a single hotel: star rating, address, ' +
    'amenities, and photos. Use this after search_hotel_offers when the ' +
    'user wants to know more about a specific listing before deciding.'
);

const schema = {
    hotelId: z.string(),
};

async function handler({ hotelId }) {
    const details = await client.getHotelDetails({ hotelId });
    if (!details) {
        return {
            content: [{ type: 'text', text: JSON.stringify({ error: 'hotel_not_found', hotelId }) }],
            isError: true,
        };
    }
    return {
        content: [{ type: 'text', text: JSON.stringify(details, null, 2) }],
    };
}

export default ['get_hotel_details', description, schema, handler];
