/**
 * Fixture client — same interface as client-live.js, but reads recorded
 * JSON files from `fixtures/` instead of calling Duffel. This is what makes
 * the repo end-to-end runnable for a reviewer with no Duffel account
 * (see ADR 0002).
 *
 * Lookup strategy for search_offers: deterministic filename pattern
 * `{origin}-{destination}-{departDate}.json`. If no file matches, return
 * an empty offers list — the agent surfaces "no offers found" rather than
 * crashing.
 *
 * Lookup strategy for get_offer_details: scan loaded fixtures for an offer
 * with the requested id. Tiny dataset, no need for indexing.
 */
import { readFile, readdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const FIXTURES_DIR = join(dirname(fileURLToPath(import.meta.url)), 'fixtures');

async function _readFixture(name) {
    try {
        const raw = await readFile(join(FIXTURES_DIR, name), 'utf8');
        return JSON.parse(raw);
    } catch (e) {
        if (e.code === 'ENOENT') return null;
        throw e;
    }
}

export async function searchOffers({ origin, destination, departDate }) {
    // Normalise origin (which may be a list) to a single airport code for the
    // filename. Reviewers reading the fixtures dir expect a flat name.
    const o = Array.isArray(origin) ? origin[0] : origin;
    const name = `${o}-${destination}-${departDate}.json`;
    const data = await _readFixture(name);
    if (!data) return { offers: [], source: 'fixture-miss', fixtureName: name };
    return { offers: data.offers ?? [], source: 'fixture', fixtureName: name };
}

export async function getOfferDetails({ offerId }) {
    let files;
    try {
        files = await readdir(FIXTURES_DIR);
    } catch (e) {
        if (e.code === 'ENOENT') return null;
        throw e;
    }
    for (const f of files) {
        if (!f.endsWith('.json')) continue;
        const data = await _readFixture(f);
        const match = (data?.offers ?? []).find((o) => o.id === offerId);
        if (match) return { ...match, source: 'fixture', fixtureName: f };
    }
    return null;
}
