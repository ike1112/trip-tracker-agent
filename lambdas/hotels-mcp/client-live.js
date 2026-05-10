/**
 * Live LiteAPI client. Same interface as client-fixture.js; see ADR 0002 for
 * why this split exists.
 *
 * Design considerations baked in here:
 *
 * - Latency budget: a 20s AbortSignal timeout on every LiteAPI fetch. Lambda
 *   timeout is 30s; this leaves 10s headroom for serialization + X-Ray flush
 *   even on a worst-case cold start. Hotel searches are slower than flight
 *   searches (multi-property, multi-rate-plan), so this matters.
 *
 * - Currency strictness: requestCurrency is hard-coded to USD, and we throw
 *   if the response comes back in a different currency. The watch system
 *   tracks USD totals; silent unit conversion would corrupt FareHistory
 *   forever.
 *
 * - Pagination: limit=10 on the wire, top 5 returned. Match the agent's
 *   chat surface — the user sees a headline + a few comparisons, not 100
 *   listings.
 *
 * - Response normalisation: live results are flattened to {id, hotelName,
 *   totalAmount, currency, stars, address, checkin, checkout, refundable,
 *   bookingDeepLink}. The shape of LiteAPI's wire format is the live
 *   client's concern; nothing downstream knows.
 *
 * NOT runtime-verified — no test LiteAPI key in this env. Coverage is via
 * fixture mode only. To record fresh fixtures: set LITEAPI_API_KEY, set
 * MCP_MODE=live, run the agent end-to-end, copy responses into fixtures/.
 */
const LITEAPI_BASE = 'https://api.liteapi.travel/v3.0';
const LITEAPI_TIMEOUT_MS = 20_000;
const TOP_N = 5;

const LITEAPI_API_KEY = process.env.LITEAPI_API_KEY;

async function _get(path, params) {
    if (!LITEAPI_API_KEY) {
        throw new Error('LITEAPI_API_KEY is required for live mode. Set MCP_MODE=fixture to run without a key.');
    }
    const url = new URL(`${LITEAPI_BASE}${path}`);
    for (const [k, v] of Object.entries(params)) {
        if (v !== undefined && v !== null) url.searchParams.set(k, String(v));
    }
    const resp = await fetch(url, {
        method: 'GET',
        signal: AbortSignal.timeout(LITEAPI_TIMEOUT_MS),
        headers: {
            'X-API-Key': LITEAPI_API_KEY,
            'Accept': 'application/json',
        },
    });
    if (!resp.ok) {
        const text = await resp.text().catch(() => '');
        throw new Error(`LiteAPI ${path} failed: ${resp.status} ${text.slice(0, 200)}`);
    }
    return resp.json();
}

/**
 * Two-step LiteAPI dance:
 *   1. /hotels — list properties matching the city + filters
 *   2. /hotels/rates — current rates for the candidate hotelIds
 * We do both in series and return the cheapest top-N composed offers.
 */
export async function searchHotels({ city, checkin, checkout, pax, minStars }) {
    const hotelsResp = await _get('/data/hotels', {
        cityName: city,
        countryCode: undefined,        // LiteAPI accepts cityName alone for popular cities
        limit: 25,
        minRating: minStars ?? undefined,
    });
    const candidates = (hotelsResp?.data ?? []).slice(0, 25).map((h) => h.id);
    if (candidates.length === 0) return { hotels: [], source: 'live' };

    const ratesResp = await _get('/hotels/rates', {
        hotelIds: candidates.join(','),
        checkin,
        checkout,
        adults: pax,
        currency: 'USD',
    });

    const hotels = (ratesResp?.data ?? [])
        .map((r) => _normaliseHotel(r, checkin, checkout))
        .filter((h) => h !== null)
        .sort((a, b) => a.totalAmount - b.totalAmount)
        .slice(0, TOP_N);

    return { hotels, source: 'live' };
}

export async function getHotelDetails({ hotelId }) {
    const resp = await _get(`/data/hotels/${hotelId}`, {});
    if (!resp?.data) return null;
    const d = resp.data;
    return {
        id: d.id,
        hotelName: d.name,
        stars: d.starRating ?? null,
        address: d.address ?? null,
        amenities: d.amenities ?? [],
        photos: (d.images ?? []).slice(0, 5),
        source: 'live',
    };
}

function _normaliseHotel(rate, checkin, checkout) {
    const cheapestRate = (rate.roomTypes ?? [])
        .flatMap((rt) => rt.rates ?? [])
        .sort((a, b) => (a.retailRate?.total?.[0]?.amount ?? Infinity)
                      - (b.retailRate?.total?.[0]?.amount ?? Infinity))[0];
    if (!cheapestRate) return null;

    const total = cheapestRate.retailRate?.total?.[0];
    if (!total) return null;

    // Currency strictness. Silent conversion = silent data corruption.
    if (total.currency !== 'USD') {
        throw new Error(`LiteAPI returned non-USD currency ${total.currency} for hotelId=${rate.hotelId}; refusing to convert silently`);
    }

    return {
        id: rate.hotelId,
        hotelName: rate.hotelInfo?.name ?? rate.hotelId,
        totalAmount: total.amount,
        currency: 'USD',
        stars: rate.hotelInfo?.starRating ?? null,
        address: rate.hotelInfo?.address ?? null,
        checkin,
        checkout,
        refundable: Boolean(cheapestRate.cancellationPolicies?.refundableTag === 'RFN'),
        bookingDeepLink: cheapestRate.bookingDeepLink ?? null,
    };
}
