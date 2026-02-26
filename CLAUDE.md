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

Key dependencies: `spotipy`, `transliterate` (optional, for Cyrillic→Latin conversion), `difflib` (stdlib).

Spotify OAuth credentials are stored in environment or configured in the scripts. The `.cache` file (auto-generated) holds the OAuth token.

## Migration Pipeline

```bash
# Step 1: Test on 10 tracks
python3 spotify_crossref.py --test

# Step 2: Full migration (resumable — safe to re-run after interruption)
python3 spotify_crossref.py --full

# Step 3: Manually resolve unmatched tracks (no re-fetching needed)
python3 spotify_crossref.py --resolve
```

`artist_mapper.py` is obsolete and can be deleted.

## Architecture

### `spotify_crossref.py`
Searches Spotify directly for each track — no pre-built artist mapping required.

- `--test` / `--full`: For each track, searches `track:TITLE artist:ARTIST`. If the artist name is Cyrillic, also tries a transliterated Latin variant. Best result across both queries is scored by title similarity (≥ 0.7 → liked immediately). Progress is tracked via `spotify_found.json` + `spotify_not_found.json` (resume picks up any track whose `yandex_id` isn't in either file).
- `--resolve`: Interactive CLI for unmatched tracks. Candidates are already stored in `spotify_not_found.json` with their `spotify_id`, so the user picks a number and the track is liked instantly — no API search needed.

### Output files (in `data/`)
| File | Contents |
|------|----------|
| `data/spotify_found.json` | Matched + liked tracks |
| `data/spotify_not_found.json` | Unmatched tracks; entries include `candidates` list with `spotify_id` for `--resolve` |

### Data Flow
```
data/yandex_music_likes.json
    → spotify_crossref.py --full
    → data/spotify_found.json
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

Commit on any essential checkpoint (e.g. after finishing a feature, fixing a bug, or adding tests) without waiting to be asked. Always `git push` after every commit.

When working with external APIs (Spotify, Yandex Music, etc.), always check the official documentation first as the source of truth. Do not guess how an API works — fetch and read the actual docs before writing or debugging API calls.
