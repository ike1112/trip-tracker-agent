/**
 * Fixture client — reads recorded LiteAPI responses from `fixtures/`. Mirrors
 * the live client's interface so the rest of the Lambda is mode-agnostic.
 *
 * Lookup strategy for searchHotels: deterministic filename
 *   `{city}-{checkin}.json`
 * Mirrors the flights-mcp pattern: the natural key of the search request is
 * the filename, so a reviewer reading the fixtures dir can predict exactly
 * what the tool will return.
 *
 * Lookup strategy for getHotelDetails: scan loaded fixtures for a hotel with
 * the requested id. Small dataset, no indexing needed.
 *
 * See ADR 0002.
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

export async function searchHotels({ city, checkin }) {
    const name = `${city}-${checkin}.json`;
    const data = await _readFixture(name);
    if (!data) return { hotels: [], source: 'fixture-miss', fixtureName: name };
    return { hotels: data.hotels ?? [], source: 'fixture', fixtureName: name };
}

export async function getHotelDetails({ hotelId }) {
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
        const match = (data?.hotels ?? []).find((h) => h.id === hotelId);
        if (match) return { ...match, source: 'fixture', fixtureName: f };
    }
    return null;
}
