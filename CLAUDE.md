# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Golden Rules

- **Never break existing functionality to add new features.** If a new requirement conflicts with existing behavior (e.g., ordering vs batching performance), find a solution that preserves both — don't degrade one for the other.

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
| `migrate.py` | Unified CLI orchestrator — routes to the right scripts |
| `matching.py` | Pure matching functions: normalization, Levenshtein similarity, transliteration, library indexing, pre-matching, Spotify search/scoring |
| `spotify_client.py` | Shared Spotify OAuth client setup with configurable scopes |
| `spotify_crossref.py` | Liked tracks migration (search, resolve, pending, stats) |
| `playlist_sync.py` | Playlist migration (match, sync, resolve, stats). Supports `--filter-playlist NAME` for exact name filtering |
| `yandex_fetch.py` | Fetch liked tracks and playlists from Yandex Music |

## CLI (via migrate.py / migrate.sh / migrate.bat)

```bash
migrate.sh <flow> [options]
```

### Flows (positional, required)

| Flow | Description |
|------|-------------|
| `liked` | Migrate liked tracks only (search + like on Spotify) |
| `playlists` | Sync playlists only (match tracks + create/update Spotify playlists) |
| `all` | Liked + playlists (liked runs first) |
| `resolve` | Interactively resolve unmatched tracks (both liked + playlists) — shows candidates, pick/skip/mark no match |
| `retry` | Re-search Spotify for all not_found tracks — useful when Spotify catalog changes or after fixing matching logic |
| `stats` | Show migration progress (matched/unmatched/pending counts for both) |
| `pending` | Like tracks in `spotify_pending.json` without new searching |

### Options

| Option | Works with | Description |
|--------|-----------|-------------|
| `--test` | `liked`, `playlists`, `all` | Test mode — 10 tracks (liked) / first playlist (playlists) |
| `--filter-playlist NAME [NAME ...]` | `playlists`, `all` | Only sync playlists matching these Yandex names (exact, case-sensitive, space-separated) |
| `--artist-on-spotify` | `retry` | Only retry tracks whose artist was found on Spotify |
| `--force-prematch` | `liked`, `playlists`, `all` | Refetch entire Spotify library for pre-matching (instead of incremental) |
| `--sync` | `liked`, `playlists`, `all` | Fetch fresh data from Yandex Music before migrating. Requires `--token` |
| `--token TOKEN` | `liked`, `playlists`, `all` | Yandex Music OAuth token (required with `--sync`). Also reads `YANDEX_MUSIC_TOKEN` env var |

### Examples

```bash
# --- Full migration ---
./migrate.sh all                                       # Migrate liked tracks + all playlists
./migrate.sh all --test                                # Test run: 10 liked tracks + first playlist
./migrate.sh all --sync --token TOKEN                  # Fetch from Yandex first, then migrate everything
./migrate.sh all --force-prematch                      # Full migration with complete library refetch
./migrate.sh all --filter-playlist "Rock" "Jazz"       # Liked tracks + only "Rock" and "Jazz" playlists

# --- Liked tracks only ---
./migrate.sh liked                                     # Migrate all liked tracks
./migrate.sh liked --test                              # Test with 10 tracks
./migrate.sh liked --test --force-prematch             # Test with full library refetch
./migrate.sh liked --sync --token TOKEN                # Fetch likes from Yandex, then migrate

# --- Playlists only ---
./migrate.sh playlists                                 # Sync all playlists
./migrate.sh playlists --test                          # Test with first playlist
./migrate.sh playlists --filter-playlist "Rock" "Jazz" # Sync only "Rock" and "Jazz" playlists
./migrate.sh playlists --sync --token TOKEN            # Fetch playlists from Yandex, then sync

# --- Maintenance ---
./migrate.sh retry                                     # Re-search Spotify for unmatched tracks
./migrate.sh retry --artist-on-spotify                 # Retry only tracks whose artist exists on Spotify
./migrate.sh resolve                                   # Interactively resolve unmatched tracks
./migrate.sh stats                                     # Show migration progress
./migrate.sh pending                                   # Like pending matched tracks
```

**Add-only by design:** playlist sync never removes tracks from Spotify playlists. If you need to remove tracks, do it manually in Spotify.

**Cross-liking:** Playlist tracks that also appear in `yandex_music_likes.json` are automatically liked on Spotify during sync.

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

**Ordering caveat:** Tracks are processed oldest-first so that the overall Spotify liked order mirrors Yandex. However, `PUT /me/library` does not guarantee ordering within a single batch (up to 40 tracks) — tracks in the same batch may appear in arbitrary order on Spotify. The Spotify API has no mechanism (like `timestamped_ids`) on this endpoint to control within-batch ordering.

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

**Always update README.md** when making substantial changes (new features, changed CLI flags, new data files, architectural changes). Keep it in sync with CLAUDE.md — README.md is user-facing, CLAUDE.md is for the AI assistant.
