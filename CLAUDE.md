# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A two-stage CLI tool to migrate 1,623 liked tracks from Yandex Music to Spotify Liked Songs. The core challenge is that artist names may be in Cyrillic and differ between services.

**Source data**: `data/yandex_music_likes.json` — tracks with `title`, `artists`, and `id` fields.

## Environment Setup

Python 3.13 with a local `venv/`. Activate before running any script:

```bash
source venv/bin/activate
```

Key dependencies: `spotipy`, `transliterate` (optional, for Cyrillic→Latin conversion).

Spotify OAuth credentials are stored in environment or configured in the scripts. The `.cache` file (auto-generated) holds the OAuth token.

## Migration Pipeline

```bash
# Step 1: Test on 10 tracks (prematch runs first, then searches remainder)
python3 spotify_crossref.py --test

# Step 2: Full migration (resumable — safe to re-run after interruption)
python3 spotify_crossref.py --full

# Step 3: Manually resolve unmatched tracks (no re-fetching needed)
python3 spotify_crossref.py --resolve

# Optional: force full library rescan (e.g. after manually liking tracks on Spotify)
python3 spotify_crossref.py --full --force-prematch
```

`artist_mapper.py` is obsolete and can be deleted.

## Architecture

### `spotify_crossref.py`
Matches Yandex tracks to Spotify using a two-stage approach: first pre-matching against the user's existing Spotify library (no API search needed), then searching Spotify for the remainder.

#### Pre-matching (library scan)
Before any API searches, the tool fetches the user's Spotify liked songs and matches them locally against Yandex tracks. This resolves tracks that are already in the user's library without spending search API quota.

**Two-phase lookup:**
1. **Title index** (O(1) exact match) — normalized title → songs multimap. Handles ~96% of matches. Titles are indexed under both original and Cyrillic→Latin transliterated forms.
2. **Artist-bucket similarity** (fallback) — for titles that don't match exactly, candidates are gathered from the matching artist bucket and scored by fuzzy similarity.

**Scoring:** Title and artist are scored independently using Levenshtein distance (normalized to 0–1). Both `title_score` and `artist_score` must be ≥ 0.7 (`min(title, artist) >= threshold`). Truncation is applied (shorter string length) so "Yesterday" matches "Yesterday - Remastered 2009".

**Incremental fetch:** On subsequent runs, liked songs are fetched newest-first. If ≥90% of a page already exists in `spotify_found.json`, fetching stops early. Use `--force-prematch` to refetch the entire library.

**Cross-list resolution:** Pre-matching also checks `not_found` and `pending` entries — if the user liked a track on Spotify since the last run, it gets resolved automatically.

#### Search (API)
- `--test` / `--full`: For each remaining track, searches `track:TITLE artist:ARTIST`. Best result is scored by Levenshtein similarity (≥ 0.7 → liked immediately). Progress is tracked via `spotify_found.json` + `spotify_not_found.json` (resume picks up any track whose `yandex_id` isn't in either file).
- `--resolve`: Interactive CLI for unmatched tracks. Candidates are already stored in `spotify_not_found.json` with their `spotify_id`, so the user picks a number and the track is liked instantly — no API search needed.
- `--force-prematch`: Combinable with `--test`/`--full`. Refetches the entire Spotify library for pre-matching instead of using incremental early-stop.

### Output files (in `data/`)
| File | Contents |
|------|----------|
| `data/spotify_found.json` | Matched + liked tracks |
| `data/spotify_not_found.json` | Unmatched tracks; entries include `candidates` list with `spotify_id` for `--resolve` |

### Data Flow
```
data/yandex_music_likes.json
    → spotify_crossref.py --full
        1. Fetch Spotify liked songs (incremental, early-stop)
        2. Pre-match against library (title index + artist fallback)
           Also resolves not_found/pending entries
        3. Search Spotify API for remaining unmatched tracks
    → data/spotify_found.json   (matched tracks, includes "source": "library_prematch")
    → data/spotify_not_found.json  (candidates stored with spotify_ids)
    → spotify_crossref.py --resolve
    → data/spotify_found.json (updated), data/spotify_not_found.json (updated)
```

## Resetting Progress

```bash
# Reset migration
rm data/spotify_found.json data/spotify_not_found.json

# Full reset (including OAuth token)
rm data/spotify_found.json data/spotify_not_found.json .spotify_token_cache
```

## Rate Limiting

Both scripts respect Spotify API rate limits: ~1.5s between requests with automatic retry on 429 responses. The `--build` step also adds batch pauses. Do not remove these delays.

## Workflow

Squash commits before pushing. Before pushing, always double-check that no confidential data (tokens, secrets, client IDs, personal data) is leaked in the commit.

When working with external APIs (Spotify, Yandex Music, etc.), always check the official documentation first as the source of truth. Do not guess how an API works — fetch and read the actual docs before writing or debugging API calls.
