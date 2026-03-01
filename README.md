# Yandex Music to Spotify Migration

CLI tool to migrate liked tracks from Yandex Music to Spotify. Handles Cyrillic artist names via automatic transliteration, scores matches by title similarity, and saves unmatched tracks with Spotify candidates for manual resolution.

All commands are resumable -- safe to interrupt and re-run.

## Requirements

- Python 3.10+
- Spotify Premium account
- A Spotify Developer app (see below)
- A Yandex Music OAuth token (for `--full-sync`; get one via [yandex-music-token](https://github.com/MarshalX/yandex-music-token) browser extension)

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

## Usage

All commands can be run via the wrapper script (handles venv activation):

```bash
# Linux/macOS
./migrate.sh <command> [options]

# Windows
migrate.bat <command> [options]
```

Or directly:

```bash
source venv/bin/activate
python3 spotify_crossref.py <command> [options]
```

### Commands

| Command | Description |
|---------|-------------|
| `--full-sync --token TOKEN` | Full pipeline: fetch new tracks from Yandex, print stats, migrate to Spotify |
| `--full` | Search Spotify for all unprocessed tracks and like those with a good match. Tracks below the certainty threshold are saved with candidates for manual resolution via `--resolve`. |
| `--resolve` | Interactive CLI to manually pick from stored Spotify candidates |
| `--stats` | Print migration progress, unmatched artist breakdown |
| `--test` | Same as `--full` but limited to 10 tracks (useful for first-time setup) |
| `--pending` | Like previously matched tracks without searching (runs implicitly during `--full`) |
| `--force-prematch` | Combined with `--full` or `--test`: refetch entire Spotify library for pre-matching (default: incremental) |

## Rate limits

Spotify's API enforces rate limits. And on your own spotify developer app you probably will get that rate limit ban if trying to migrate > 500-600 tracks at once. Limits come from searching requests, which are not batched, and there exist no batching API for that. This tool is taking rate limit into account, saves state and allows to resume migrating once rate-limit ban is lifted. I personally encountered Spotify lifting a 24h ban, so you'll need some patience to migrate if you have a big collection of tracks.

One potentially viable remedy to this could be to request extended quota for your Spotify app, but I'm not sure that it would help and never tried by myself.

### Typical workflow

```bash
# One command to do everything: fetch from Yandex + migrate to Spotify
./migrate.sh --full-sync --token YOUR_YANDEX_TOKEN

# After migration, manually resolve unmatched tracks
./migrate.sh --resolve

# Check progress at any time
./migrate.sh --stats
```

### Step-by-step (if not using --full-sync)

```bash
# 1. Export liked tracks from Yandex Music
python3 yandex_fetch.py --token YOUR_YANDEX_TOKEN

# 2. Test on 10 tracks first (run on the very first try)
./migrate.sh --test

# 3. Run full migration
./migrate.sh --full

# 4. Manually resolve unmatched tracks
./migrate.sh --resolve
```

## How it works

### Pre-matching against existing library

Before searching the Spotify API, `--full` and `--test` fetch your existing Spotify liked songs and match them locally against the Yandex track list. This avoids burning API search quota on tracks you've already liked.

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

### API search

For tracks not pre-matched from the library, the tool searches Spotify with `track:TITLE artist:ARTIST`. If the artist name contains Cyrillic characters, it also tries a transliterated Latin variant.

### Data files

All data lives in `data/` (gitignored):

| File | Contents |
|------|----------|
| `yandex_music_likes.json` | Source tracks exported from Yandex Music |
| `spotify_found.json` | Successfully matched and liked tracks |
| `spotify_not_found.json` | Unmatched tracks with Spotify candidates |
| `spotify_pending.json` | Matched but not yet liked (intermediate state) |

### Resumability

Progress is tracked via `spotify_found.json` and `spotify_not_found.json`. Any track whose `yandex_id` appears in either file is skipped on re-run. Matched tracks are batched into `spotify_pending.json` before being liked, so even a crash mid-batch loses no search work.


## Resetting

```bash
# Reset migration progress (keeps Yandex export)
rm data/spotify_found.json data/spotify_not_found.json

# Full reset (including OAuth token)
rm data/spotify_found.json data/spotify_not_found.json .spotify_token_cache
```

## Tests

```bash
source venv/bin/activate
python -m pytest tests/ -v
```
