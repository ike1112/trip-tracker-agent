/**
 * Live Duffel client. Same interface as client-fixture.js so the rest of
 * the Lambda is mode-agnostic — see ADR 0002 for why this split matters.
 *
 * NOT runtime-verified in this repo: there are no Duffel credentials in
 * the test environment, so coverage is via fixture-mode only. The shape
 * of the response objects is normalised here so downstream tool handlers
 * don't have to know about Duffel's wire format.
 *
 * To record fresh fixtures: set `DUFFEL_API_KEY`, set `MCP_MODE=live`, run
 * the agent end-to-end once, and copy the responses into `fixtures/`.
 */
const DUFFEL_API_BASE = 'https://api.duffel.com';
const DUFFEL_API_VERSION = 'v2';

const DUFFEL_API_KEY = process.env.DUFFEL_API_KEY;

async function _post(path, body) {
    if (!DUFFEL_API_KEY) {
        throw new Error('DUFFEL_API_KEY is required for live mode. Set MCP_MODE=fixture to run without a key.');
    }
    const resp = await fetch(`${DUFFEL_API_BASE}${path}`, {
        method: 'POST',
        headers: {
            'Authorization': `Bearer ${DUFFEL_API_KEY}`,
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'Duffel-Version': DUFFEL_API_VERSION,
        },
        body: JSON.stringify(body),
    });
    if (!resp.ok) {
        const text = await resp.text().catch(() => '');
        throw new Error(`Duffel ${path} failed: ${resp.status} ${text.slice(0, 200)}`);
    }
    return resp.json();
}

async function _get(path) {
    if (!DUFFEL_API_KEY) {
        throw new Error('DUFFEL_API_KEY is required for live mode. Set MCP_MODE=fixture to run without a key.');
    }
    const resp = await fetch(`${DUFFEL_API_BASE}${path}`, {
        method: 'GET',
        headers: {
            'Authorization': `Bearer ${DUFFEL_API_KEY}`,
            'Accept': 'application/json',
            'Duffel-Version': DUFFEL_API_VERSION,
        },
    });
    if (!resp.ok) {
        const text = await resp.text().catch(() => '');
        throw new Error(`Duffel ${path} failed: ${resp.status} ${text.slice(0, 200)}`);
    }
    return resp.json();
}

/**
 * Duffel offer searches are a two-step protocol:
 *   1. POST /air/offer_requests with the slice spec → returns offer_request.id
 *   2. GET  /air/offers?offer_request_id=… for the actual offers
 * We collapse this into one client call.
 */
export async function searchOffers({ origin, destination, departDate, returnDate, pax }) {
    const slices = [{ origin, destination, departure_date: departDate }];
    if (returnDate) {
        slices.push({ origin: destination, destination: origin, departure_date: returnDate });
    }
    const passengers = Array.from({ length: pax }, () => ({ type: 'adult' }));

    const requestResp = await _post('/air/offer_requests', {
        data: { slices, passengers, cabin_class: 'economy' },
    });
    const offerRequestId = requestResp?.data?.id;

    const offersResp = await _get(`/air/offers?offer_request_id=${offerRequestId}&limit=10&sort=total_amount`);
    const offers = (offersResp?.data ?? []).map(_normaliseOffer);
    return { offers, source: 'live' };
}

export async function getOfferDetails({ offerId }) {
    const resp = await _get(`/air/offers/${offerId}?return_available_services=true`);
    if (!resp?.data) return null;
    return { ..._normaliseOffer(resp.data), source: 'live' };
}

function _normaliseOffer(raw) {
    return {
        id: raw.id,
        totalAmount: parseFloat(raw.total_amount),
        currency: raw.total_currency,
        owner: raw.owner?.iata_code,
        slices: (raw.slices ?? []).map((s) => ({
            origin: s.origin?.iata_code,
            destination: s.destination?.iata_code,
            duration: s.duration,
            segments: (s.segments ?? []).map((seg) => ({
                airline: seg.marketing_carrier?.iata_code,
                flightNumber: seg.marketing_carrier_flight_number,
                departAt: seg.departing_at,
                arriveAt: seg.arriving_at,
            })),
            stops: Math.max(0, (s.segments?.length ?? 1) - 1),
        })),
        expiresAt: raw.expires_at,
    };
}
