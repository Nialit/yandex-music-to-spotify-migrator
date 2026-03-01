# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A CLI tool to migrate liked tracks and playlists from Yandex Music to Spotify. The core challenge is that artist names may be in Cyrillic and differ between services.

**Source data**: `data/yandex_music_likes.json` (liked tracks), `data/yandex_playlists.json` (playlists).

## Environment Setup

Python 3.13 with a local `venv/`. Activate before running any script:

```bash
source venv/bin/activate
```

Key dependencies: `spotipy`, `transliterate` (optional, for Cyrillic→Latin conversion).

Spotify OAuth credentials are stored in `config.py`. The `.spotify_token_cache` file (auto-generated) holds the OAuth token. After adding playlist scopes, delete `.spotify_token_cache` to re-auth.

## Module Structure

| Module | Purpose |
|--------|---------|
| `matching.py` | Pure matching functions: normalization, Levenshtein similarity, transliteration, library indexing, pre-matching, Spotify search/scoring |
| `spotify_client.py` | Shared Spotify OAuth client setup with configurable scopes |
| `spotify_crossref.py` | Liked tracks migration (search, resolve, pending, stats) |
| `playlist_sync.py` | Playlist migration (match, sync, resolve, stats) |
| `yandex_fetch.py` | Fetch liked tracks and playlists from Yandex Music |

## Liked Tracks Migration

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

## Playlist Migration

```bash
# Step 1: Fetch playlists from Yandex Music
python3 yandex_fetch.py --playlists --token YOUR_TOKEN

# Step 2: Test sync with first playlist only
python3 playlist_sync.py --test

# Step 3: Full sync — match tracks, cross-like, create/update Spotify playlists
python3 playlist_sync.py --full

# Step 4: Manually resolve unmatched playlist tracks
python3 playlist_sync.py --resolve

# Check status
python3 playlist_sync.py --stats
```

**Add-only by design:** playlist sync never removes tracks from Spotify playlists. If you need to remove tracks, do it manually in Spotify.

**Cross-liking:** Playlist tracks that also appear in `yandex_music_likes.json` are automatically liked on Spotify during sync.

## Full Sync (both likes + playlists)

```bash
python3 spotify_crossref.py --full-sync --token YOUR_TOKEN --playlists
```

`artist_mapper.py` is obsolete and can be deleted.

## Architecture

### Matching (`matching.py`)

Shared by both `spotify_crossref.py` and `playlist_sync.py`.

**Two-phase library pre-matching:**
1. **Title index** (O(1) exact match) — normalized title → songs multimap. Handles ~96% of matches. Titles are indexed under both original and Cyrillic→Latin transliterated forms.
2. **Artist-bucket similarity** (fallback) — for titles that don't match exactly, candidates are gathered from the matching artist bucket and scored by fuzzy similarity.

**Scoring:** Title and artist are scored independently using Levenshtein distance (normalized to 0–1). Both `title_score` and `artist_score` must be ≥ 0.7 (`min(title, artist) >= threshold`). Truncation is applied (shorter string length) so "Yesterday" matches "Yesterday - Remastered 2009".

### Liked Tracks (`spotify_crossref.py`)

Imports matching functions from `matching.py` and client from `spotify_client.py`.

**Incremental fetch:** On subsequent runs, liked songs are fetched newest-first. If ≥90% of a page already exists in `spotify_found.json`, fetching stops early. Use `--force-prematch` to refetch the entire library.

**Cross-list resolution:** Pre-matching also checks `not_found` and `pending` entries — if the user liked a track on Spotify since the last run, it gets resolved automatically.

### Playlist Sync (`playlist_sync.py`)

Two-phase pipeline:

**Phase 1 — Match tracks:** Collects all unique tracks across playlists, checks existing matches (pool + favs found), pre-matches against library, searches Spotify API for remainder. Saves to `playlist_track_pool.json` (resumable).

**Phase 2 — Sync playlists:** For each Yandex playlist, creates a Spotify playlist (if new) and adds only new tracks (diff against `last_synced_track_ids`). Updates `playlist_mapping.json` only after successful Spotify operations.

### Output files (in `data/`)
| File | Contents |
|------|----------|
| `spotify_found.json` | Matched + liked tracks |
| `spotify_not_found.json` | Unmatched tracks with `candidates` for `--resolve` |
| `yandex_playlists.json` | Yandex playlists with tracks |
| `playlist_track_pool.json` | Global pool of matched playlist tracks (deduped, keyed by yandex_id) |
| `playlist_mapping.json` | Yandex playlist → Spotify playlist ID mapping + synced track set |

### Data Flow
```
data/yandex_music_likes.json
    → spotify_crossref.py --full
        1. Fetch Spotify liked songs (incremental, early-stop)
        2. Pre-match against library (title index + artist fallback)
        3. Search Spotify API for remaining unmatched tracks
    → data/spotify_found.json + data/spotify_not_found.json

data/yandex_playlists.json
    → playlist_sync.py --full
        1. Match tracks (reuse favs matches from spotify_found.json)
        2. Cross-like tracks also in yandex_music_likes.json
        3. Create/update Spotify playlists (add-only)
    → data/playlist_track_pool.json + data/playlist_mapping.json
```

## Resetting Progress

```bash
# Reset liked tracks migration
rm data/spotify_found.json data/spotify_not_found.json

# Reset playlist sync
rm data/playlist_track_pool.json data/playlist_mapping.json

# Full reset (including OAuth token — will need to re-auth)
rm data/spotify_found.json data/spotify_not_found.json data/playlist_track_pool.json data/playlist_mapping.json .spotify_token_cache
```

## Rate Limiting

All scripts respect Spotify API rate limits with automatic retry on 429 responses. Do not remove these delays.

## Workflow

Squash commits before pushing. Before pushing, always double-check that no confidential data (tokens, secrets, client IDs, personal data) is leaked in the commit.

When working with external APIs (Spotify, Yandex Music, etc.), always check the official documentation first as the source of truth. Do not guess how an API works — fetch and read the actual docs before writing or debugging API calls.
