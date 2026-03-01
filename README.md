# Yandex Music to Spotify Migration

CLI tool to migrate liked tracks and playlists from Yandex Music to Spotify. Handles Cyrillic artist names via automatic transliteration, scores matches by title similarity, and saves unmatched tracks with Spotify candidates for manual resolution.

All commands are resumable -- safe to interrupt and re-run.

## Requirements

- Python 3.10+
- Spotify Premium account
- A Spotify Developer app (see below)
- A Yandex Music OAuth token (for `--sync` or `yandex_fetch.py`; get one via [yandex-music-token](https://github.com/MarshalX/yandex-music-token) browser extension)

## Spotify Developer Setup

1. Go to [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) and log in with your Premium account
2. Create a new app — select **Web API** when asked which API you plan to use
3. In the app settings, add a Redirect URI: `http://127.0.0.1:8888/callback`
4. Note your **Client ID** and **Client Secret** from the app's settings page

## Setup

```bash
# Clone and create virtual environment
git clone <repo-url> && cd spotify-migration
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Create config file from template
cp config.example.py config.py
```

**Important:** Open `config.py` and fill in your Spotify app credentials — `CLIENT_ID`, `CLIENT_SECRET`, and `REDIRECT_URI` from the previous step. You can use the same redirect URI: `http://127.0.0.1:8888/callback`.

On first run, a browser window opens for Spotify OAuth. The token is cached in `.spotify_token_cache`.

**Note:** If you've been using this tool before the playlist feature was added, delete `.spotify_token_cache` to re-auth with the new playlist scopes.

## Usage

All commands go through `migrate.py` (or the wrapper scripts `migrate.sh` / `migrate.bat` which handle venv activation):

```bash
# Linux/macOS
./migrate.sh <flow> [options]

# Windows
migrate.bat <flow> [options]

# Or directly
source venv/bin/activate
python3 migrate.py <flow> [options]
```

### Flows

The first positional argument selects what to migrate:

| Flow | Description |
|------|-------------|
| `all` | Migrate liked tracks + sync playlists (liked runs first) |
| `liked` | Migrate liked tracks only (search + like on Spotify) |
| `playlists` | Sync playlists only (match tracks + create/update Spotify playlists) |
| `resolve` | Interactively resolve unmatched tracks — shows Spotify candidates and lets you pick the right one, mark as no match, or skip. Runs for both liked tracks and playlists |
| `stats` | Show migration progress: matched/unmatched/pending counts for both liked tracks and playlists |
| `pending` | Like previously matched tracks that are waiting in `spotify_pending.json` without doing any new searching |

### Options

| Option | Works with | Description |
|--------|-----------|-------------|
| `--test` | `liked`, `playlists`, `all` | Test mode — limit to 10 tracks (liked) / first playlist (playlists). Use this for a dry-run before committing to a full migration |
| `--filter-playlist NAME [NAME ...]` | `playlists`, `all` | Only sync playlists whose Yandex name matches one of the given names (exact match, case-sensitive). Accepts one or more space-separated names |
| `--force-prematch` | `liked`, `playlists`, `all` | Refetch your entire Spotify library for pre-matching instead of doing an incremental fetch. Useful if you've liked many tracks on Spotify since the last run |
| `--sync` | `liked`, `playlists`, `all` | Fetch fresh data from Yandex Music before migrating. Requires `--token` |
| `--token TOKEN` | `liked`, `playlists`, `all` | Yandex Music OAuth token (required with `--sync`). Can also be set via `YANDEX_MUSIC_TOKEN` env var |

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
./migrate.sh resolve                                   # Interactively resolve unmatched tracks
./migrate.sh stats                                     # Show migration progress
./migrate.sh pending                                   # Like pending matched tracks
```

### Yandex fetch (standalone)

Fetch data from Yandex Music separately (if not using `--sync`):

```bash
python3 yandex_fetch.py --token TOKEN              # Liked tracks only
python3 yandex_fetch.py --token TOKEN --playlists   # Liked tracks + playlists
```

## Rate limits

Spotify's API enforces rate limits. And on your own spotify developer app you probably will get that rate limit ban if trying to migrate > 500-600 tracks at once. Limits come from searching requests, which are not batched, and there exist no batching API for that. This tool is taking rate limit into account, saves state and allows to resume migrating once rate-limit ban is lifted. I personally encountered Spotify lifting a 24h ban, so you'll need some patience to migrate if you have a big collection of tracks.

One potentially viable remedy to this could be to request extended quota for your Spotify app, but I'm not sure that it would help and never tried by myself.

### Typical workflow

```bash
# One command to do everything
./migrate.sh all --sync --token YOUR_YANDEX_TOKEN

# Resolve unmatched tracks interactively
./migrate.sh resolve

# Check progress
./migrate.sh stats
```

## How it works

### Module structure

| Module | Purpose |
|--------|---------|
| `migrate.py` | Unified CLI orchestrator — routes to the right scripts |
| `matching.py` | Shared matching logic: normalization, Levenshtein similarity, transliteration, library indexing, pre-matching, search scoring |
| `spotify_client.py` | Shared Spotify OAuth client with configurable scopes |
| `spotify_crossref.py` | Liked tracks migration engine |
| `playlist_sync.py` | Playlist migration engine |
| `yandex_fetch.py` | Yandex Music data fetcher (likes + playlists) |

### Pre-matching against existing library

Before searching the Spotify API, the tool fetches your existing Spotify liked songs and matches them locally against the Yandex track list. This avoids burning API search quota on tracks you've already liked.

- **First run** (no `spotify_found.json`): fetches your entire Spotify library
- **Subsequent runs**: fetches incrementally (newest-first), stopping early when it reaches previously-synced tracks (≥90% of a page already known)
- **`--force-prematch`**: refetches the entire library regardless

Matching uses a two-phase lookup:
1. **Title index** (O(1)): normalized title exact match, then verify artist similarity — handles ~96% of matches
2. **Artist index** (fallback): for each Yandex artist, scan all songs by that artist and score by title similarity

Pre-matching also resolves previously-unmatched tracks (`not_found`) and pending tracks against newly fetched library songs.

### Scoring

Similarity is computed using **Levenshtein distance** with truncation handling (e.g. "Yesterday" matches "Yesterday - Remastered 2009"). Artist and title are scored independently — both must meet the threshold.

- `min(title_score, artist_score)` >= 0.7: automatically liked on Spotify
- Below threshold: saved to `not_found` with top 5 candidates for manual resolution

### Ordering

Tracks are processed oldest-first so that the overall Spotify liked order mirrors Yandex (most recently liked on top). However, the Spotify `PUT /me/library` endpoint does not guarantee ordering within a single batch (up to 40 tracks) — tracks in the same batch may appear in arbitrary order. There is no API mechanism on this endpoint to control within-batch ordering.

### API search

For tracks not pre-matched from the library, the tool searches Spotify with `track:TITLE artist:ARTIST`. If the artist name contains Cyrillic characters, it also tries a transliterated Latin variant.

### Playlist sync

Playlist sync works in two phases:

1. **Match tracks**: Collects all unique tracks across playlists, reuses matches from liked tracks migration (`spotify_found.json`), pre-matches against the Spotify library, then searches the API for the rest. Results are saved to a shared pool (`playlist_track_pool.json`).

2. **Sync to Spotify**: For each Yandex playlist, creates a corresponding Spotify playlist (if new) and adds new tracks. **Add-only by design** — tracks are never removed from Spotify playlists. If you need to remove tracks, do it manually in Spotify.

**Cross-liking**: Playlist tracks that also appear in your Yandex liked tracks are automatically liked on Spotify during sync.

### Data files

All data lives in `data/` (gitignored):

| File | Contents |
|------|----------|
| `yandex_music_likes.json` | Source liked tracks from Yandex Music |
| `yandex_playlists.json` | Source playlists from Yandex Music |
| `spotify_found.json` | Successfully matched and liked tracks |
| `spotify_not_found.json` | Unmatched tracks with Spotify candidates |
| `spotify_pending.json` | Matched but not yet liked (intermediate state) |
| `playlist_track_pool.json` | Matched playlist tracks (shared pool, keyed by yandex_id) |
| `playlist_mapping.json` | Yandex→Spotify playlist ID mapping + synced track sets |

### Resumability

Progress is tracked via JSON files on disk. Any track whose `yandex_id` appears in the found/not_found/pool files is skipped on re-run. Matched tracks are batched before being liked, so even a crash mid-batch loses no search work. Playlist mapping is updated only after successful Spotify operations.


## Resetting

```bash
# Reset liked tracks migration (keeps Yandex export)
rm data/spotify_found.json data/spotify_not_found.json

# Reset playlist sync
rm data/playlist_track_pool.json data/playlist_mapping.json

# Full reset (including OAuth token — will need to re-auth)
rm data/spotify_found.json data/spotify_not_found.json data/playlist_track_pool.json data/playlist_mapping.json .spotify_token_cache
```

## Tests

```bash
source venv/bin/activate
python -m pytest tests/ -v
```
