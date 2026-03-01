"""
Sync Yandex Music playlists to Spotify.

Matches playlist tracks to Spotify (reusing favs matches), creates Spotify playlists,
and keeps them in sync on re-runs. Add-only: never removes tracks from Spotify playlists.

Tracks that also appear in yandex_music_likes.json are automatically liked on Spotify.

Usage:
  python3 playlist_sync.py --test              # First playlist only
  python3 playlist_sync.py --full              # All playlists
  python3 playlist_sync.py --full --filter-playlist "Rock" --filter-playlist "Jazz"  # Only matching playlists
  python3 playlist_sync.py --resolve           # Manual resolution for unmatched
  python3 playlist_sync.py --stats             # Show sync status
"""

import json
import time
import sys
import argparse
import os
import tempfile
import logging
from logging.handlers import TimedRotatingFileHandler

import spotipy.exceptions

from spotify_client import create_client, PLAYLIST_SCOPES
from matching import (
    first_artist, search_track, score_items, get_retry_after,
    fetch_liked_songs, build_library_index, prematch_from_library,
    TITLE_MATCH_THRESHOLD, CANDIDATES_TO_STORE,
)

DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = f"{DIR}/data"
LOG_DIR = f"{DIR}/logs"

os.makedirs(LOG_DIR, exist_ok=True)

log = logging.getLogger("playlist_sync")
log.setLevel(logging.DEBUG)

_log_fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

_console = logging.StreamHandler()
_console.setLevel(logging.INFO)
_console.setFormatter(logging.Formatter("%(message)s"))
log.addHandler(_console)

_latest = logging.FileHandler(f"{LOG_DIR}/playlist_sync_latest.log", mode="w", encoding="utf-8")
_latest.setLevel(logging.DEBUG)
_latest.setFormatter(_log_fmt)
log.addHandler(_latest)

_daily = TimedRotatingFileHandler(
    f"{LOG_DIR}/playlist_sync.log", when="midnight", backupCount=0, encoding="utf-8",
)
_daily.setLevel(logging.DEBUG)
_daily.setFormatter(_log_fmt)
_daily.namer = lambda name: name.replace(".log.", ".") + ".log"
log.addHandler(_daily)

# Data files
YANDEX_PLAYLISTS_FILE = f"{DATA_DIR}/yandex_playlists.json"
YANDEX_LIKES_FILE = f"{DATA_DIR}/yandex_music_likes.json"
POOL_FILE = f"{DATA_DIR}/playlist_track_pool.json"
MAPPING_FILE = f"{DATA_DIR}/playlist_mapping.json"
FOUND_FILE = f"{DATA_DIR}/spotify_found.json"

DELAY_BETWEEN_REQUESTS = 0
LIKE_BATCH_SIZE = 40
PLAYLIST_ADD_BATCH_SIZE = 100

sp = create_client(extra_scopes=PLAYLIST_SCOPES)


# --- File I/O ---

def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def atomic_write_json(path, data):
    """Write JSON atomically: write to temp file then rename."""
    fd, tmp_path = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.rename(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def like_tracks(spotify_ids):
    """Save tracks to library using the PUT /me/library endpoint."""
    import requests as _req
    uris = [f"spotify:track:{tid}" for tid in spotify_ids]
    token = sp.auth_manager.get_access_token(as_dict=False)
    r = _req.put(
        "https://api.spotify.com/v1/me/library",
        headers={"Authorization": f"Bearer {token}"},
        params={"uris": ",".join(uris)},
    )
    if r.status_code not in (200, 201):
        raise spotipy.exceptions.SpotifyException(
            r.status_code, -1, f"{r.url}: {r.text}", headers=r.headers,
        )


# --- Phase 1: Match tracks ---

def collect_unique_tracks(playlists):
    """Collect all unique tracks across playlists, keyed by yandex_id."""
    tracks = {}
    for pl in playlists:
        for t in pl.get("tracks", []):
            yid = str(t["id"])
            if yid not in tracks:
                tracks[yid] = t
    return tracks


def match_tracks(playlists, test_mode=False):
    """Match all playlist tracks to Spotify. Saves pool after each batch."""
    pool = load_json(POOL_FILE, {})
    found = load_json(FOUND_FILE, [])
    found_by_yid = {str(e["yandex_id"]): e for e in found if e.get("yandex_id")}

    all_tracks = collect_unique_tracks(playlists)
    log.info(f"Total unique tracks across playlists: {len(all_tracks)}")

    # Determine which tracks still need matching
    to_match = {}
    already_matched = 0
    for yid, t in all_tracks.items():
        if yid in pool:
            already_matched += 1
            continue
        if yid in found_by_yid:
            # Already matched via favs migration — copy to pool
            e = found_by_yid[yid]
            pool[yid] = {
                "spotify_id": e["spotify_id"],
                "spotify_uri": e.get("spotify_uri", f"spotify:track:{e['spotify_id']}"),
                "title_score": e.get("title_score", 1.0),
                "artist_score": e.get("artist_score", 1.0),
                "source": "favs_crossref",
            }
            already_matched += 1
            continue
        to_match[yid] = t

    if already_matched:
        log.info(f"Already matched: {already_matched}")
    if not to_match:
        log.info("All tracks already matched.")
        atomic_write_json(POOL_FILE, pool)
        return pool

    log.info(f"Need to match: {len(to_match)} tracks")

    # Pre-match against Spotify library
    log.info("Fetching Spotify library for pre-matching...")
    liked_songs = fetch_liked_songs(sp, log=log)
    prematched_count = 0
    if liked_songs:
        title_index, artist_index = build_library_index(liked_songs)
        to_match_list = [{"id": yid, "title": t["title"], "artists": t["artists"]} for yid, t in to_match.items()]
        matched, unmatched = prematch_from_library(to_match_list, title_index, artist_index)

        for m in matched:
            pool[m["yandex_id"]] = {
                "spotify_id": m["spotify_id"],
                "spotify_uri": m["spotify_uri"],
                "title_score": m["title_score"],
                "artist_score": m["artist_score"],
                "source": "library_prematch",
            }
            prematched_count += 1

        # Update to_match to only unmatched
        unmatched_ids = {t["id"] for t in unmatched}
        to_match = {yid: t for yid, t in to_match.items() if yid in unmatched_ids}

        if prematched_count:
            log.info(f"Pre-matched {prematched_count} tracks from library.")
            atomic_write_json(POOL_FILE, pool)

    # Search Spotify API for remaining
    if to_match:
        log.info(f"Searching Spotify for {len(to_match)} remaining tracks...")
        if test_mode:
            items = list(to_match.items())[:10]
            log.info("*** TEST MODE: searching up to 10 tracks ***")
        else:
            items = list(to_match.items())

        for i, (yid, t) in enumerate(items):
            artist = first_artist(t["artists"])
            try:
                best, candidates = search_track(sp, t["title"], artist)
            except spotipy.exceptions.SpotifyException as e:
                if e.http_status == 429:
                    retry_after = get_retry_after(e)
                    log.warning(f"Rate limited, waiting {retry_after}s...")
                    atomic_write_json(POOL_FILE, pool)
                    time.sleep(retry_after + 5)
                    try:
                        best, candidates = search_track(sp, t["title"], artist)
                    except Exception:
                        log.error("Still failing after retry. Saving and exiting.")
                        atomic_write_json(POOL_FILE, pool)
                        sys.exit(1)
                else:
                    log.error(f"Spotify error: {e}")
                    pool[yid] = None  # confirmed no-match
                    continue

            if best and best["title_score"] >= TITLE_MATCH_THRESHOLD:
                pool[yid] = {
                    "spotify_id": best["spotify_id"],
                    "spotify_uri": best["spotify_uri"],
                    "title_score": best["title_score"],
                    "source": "api_search",
                    "candidates": candidates,
                }
                status = f"OK    score={best['title_score']:.2f} → {best['spotify_name']}"
            else:
                pool[yid] = None if not candidates else {
                    "matched": False,
                    "candidates": candidates,
                }
                reason = "no_results" if not best else f"best={best['title_score']:.2f}"
                status = f"MISS  {reason}"

            log.info(f"[{i+1}/{len(items)}] {status} | {artist} — {t['title']}")

            # Save pool periodically
            if (i + 1) % 20 == 0:
                atomic_write_json(POOL_FILE, pool)

    atomic_write_json(POOL_FILE, pool)
    matched_count = sum(1 for v in pool.values() if v is not None and v.get("spotify_id"))
    log.info(f"Pool: {len(pool)} total, {matched_count} matched, {len(pool) - matched_count} unmatched")
    return pool


# --- Phase 1b: Like tracks that are also in Yandex likes ---

def like_playlist_tracks_in_likes(pool):
    """Like playlist tracks that also appear in yandex_music_likes.json."""
    yandex_likes = load_json(YANDEX_LIKES_FILE, [])
    liked_yandex_ids = {str(t["id"]) for t in yandex_likes}

    found = load_json(FOUND_FILE, [])
    found_yids = {str(e["yandex_id"]) for e in found if e.get("yandex_id")}

    to_like = []
    for yid, match in pool.items():
        if match is None or not match.get("spotify_id"):
            continue
        if yid in liked_yandex_ids and yid not in found_yids:
            to_like.append((yid, match))

    if not to_like:
        return

    log.info(f"Liking {len(to_like)} playlist tracks that are also in Yandex likes...")
    for chunk_start in range(0, len(to_like), LIKE_BATCH_SIZE):
        chunk = to_like[chunk_start:chunk_start + LIKE_BATCH_SIZE]
        ids = [m["spotify_id"] for _, m in chunk]
        try:
            like_tracks(ids)
        except spotipy.exceptions.SpotifyException as e:
            if e.http_status == 429:
                retry_after = get_retry_after(e)
                log.warning(f"Rate limited liking tracks, waiting {retry_after}s...")
                time.sleep(retry_after + 5)
                try:
                    like_tracks(ids)
                except Exception:
                    log.error(f"Failed to like batch starting at {chunk_start}. Continuing...")
                    continue
            else:
                log.error(f"Error liking tracks: {e}")
                continue

        # Add to found
        yandex_likes_by_id = {str(t["id"]): t for t in yandex_likes}
        for yid, match in chunk:
            yt = yandex_likes_by_id.get(yid, {})
            found.append({
                "yandex_title": yt.get("title", ""),
                "yandex_artists": yt.get("artists", ""),
                "yandex_id": yid,
                "spotify_id": match["spotify_id"],
                "spotify_uri": match["spotify_uri"],
                "title_score": match.get("title_score", 0),
                "source": "playlist_crosslike",
            })
        log.info(f"  Liked {len(chunk)} tracks")

    atomic_write_json(FOUND_FILE, found)
    log.info(f"Updated {FOUND_FILE} with {len(to_like)} cross-liked tracks.")


# --- Phase 2: Sync playlists to Spotify ---

def sync_playlists(playlists, pool, test_mode=False):
    """Create/update Spotify playlists. Add-only: never removes tracks."""
    mapping = load_json(MAPPING_FILE, {})

    if test_mode:
        playlists = playlists[:1]
        log.info("*** TEST MODE: syncing first playlist only ***")

    for pl in playlists:
        pl_id = str(pl["playlist_id"])
        pl_name = pl["name"]
        pl_tracks = pl.get("tracks", [])

        if not pl_tracks:
            log.info(f"  {pl_name}: empty, skipping")
            continue

        # Collect matched spotify URIs for this playlist
        desired_uris = []
        desired_yids = []
        unmatched = 0
        for t in pl_tracks:
            yid = str(t["id"])
            match = pool.get(yid)
            if match and match.get("spotify_uri"):
                desired_uris.append(match["spotify_uri"])
                desired_yids.append(yid)
            else:
                unmatched += 1

        if not desired_uris:
            log.info(f"  {pl_name}: no matched tracks ({unmatched} unmatched)")
            continue

        # Get or create Spotify playlist
        pl_map = mapping.get(pl_id, {})
        spotify_pl_id = pl_map.get("spotify_playlist_id")

        if not spotify_pl_id:
            # Create new Spotify playlist
            log.info(f"  {pl_name}: creating on Spotify...")
            try:
                user_id = sp.current_user()["id"]
                result = sp.user_playlist_create(user_id, pl_name)
                spotify_pl_id = result["id"]
                log.info(f"  Created playlist: {spotify_pl_id}")
            except spotipy.exceptions.SpotifyException as e:
                log.error(f"  Failed to create playlist '{pl_name}': {e}")
                continue

        # Compute diff: only add new tracks (never remove)
        last_synced = set(pl_map.get("last_synced_track_ids", []))
        new_yids = [yid for yid in desired_yids if yid not in last_synced]

        if not new_yids:
            log.info(f"  {pl_name}: up to date ({len(desired_uris)} tracks, {unmatched} unmatched)")
            # Still update mapping in case name changed
            mapping[pl_id] = {
                "yandex_name": pl_name,
                "spotify_playlist_id": spotify_pl_id,
                "last_synced_track_ids": list(set(desired_yids) | last_synced),
            }
            atomic_write_json(MAPPING_FILE, mapping)
            continue

        # Map new yids to URIs
        new_uris = []
        for yid in new_yids:
            match = pool.get(yid)
            if match and match.get("spotify_uri"):
                new_uris.append(match["spotify_uri"])

        log.info(f"  {pl_name}: adding {len(new_uris)} new tracks ({len(desired_uris)} total matched, {unmatched} unmatched)")

        # Add in batches of 100
        added = 0
        try:
            for batch_start in range(0, len(new_uris), PLAYLIST_ADD_BATCH_SIZE):
                batch = new_uris[batch_start:batch_start + PLAYLIST_ADD_BATCH_SIZE]
                sp.playlist_add_items(spotify_pl_id, batch)
                added += len(batch)
                time.sleep(DELAY_BETWEEN_REQUESTS)
        except spotipy.exceptions.SpotifyException as e:
            if e.http_status == 429:
                retry_after = get_retry_after(e)
                log.warning(f"  Rate limited, waiting {retry_after}s...")
                time.sleep(retry_after + 5)
                # Retry remaining
                try:
                    remaining_uris = new_uris[added:]
                    for batch_start in range(0, len(remaining_uris), PLAYLIST_ADD_BATCH_SIZE):
                        batch = remaining_uris[batch_start:batch_start + PLAYLIST_ADD_BATCH_SIZE]
                        sp.playlist_add_items(spotify_pl_id, batch)
                        added += len(batch)
                        time.sleep(DELAY_BETWEEN_REQUESTS)
                except Exception:
                    log.error(f"  Failed after adding {added}/{len(new_uris)} tracks")
            else:
                log.error(f"  Error adding tracks: {e}")

        # Only update mapping AFTER successful Spotify operations
        if added > 0:
            synced_yids = list(set(desired_yids) | last_synced)
            mapping[pl_id] = {
                "yandex_name": pl_name,
                "spotify_playlist_id": spotify_pl_id,
                "last_synced_track_ids": synced_yids,
            }
            atomic_write_json(MAPPING_FILE, mapping)
            log.info(f"  → added {added} tracks to '{pl_name}'")
        else:
            log.warning(f"  → no tracks added to '{pl_name}' (errors occurred)")


# --- Commands ---

def filter_playlists(playlists, names):
    """Filter playlists by exact name match. Returns filtered list."""
    filtered = [pl for pl in playlists if pl["name"] in names]
    found_names = {pl["name"] for pl in filtered}
    for name in names:
        if name not in found_names:
            log.warning(f"  Filter: no playlist named '{name}' found")
    return filtered


def cmd_sync(test_mode=False, filter_names=None):
    """Match all playlist tracks and sync playlists to Spotify."""
    playlists = load_json(YANDEX_PLAYLISTS_FILE, [])
    if not playlists:
        log.error(f"No playlists found. Run: python3 yandex_fetch.py --playlists --token TOKEN")
        sys.exit(1)

    if filter_names:
        playlists = filter_playlists(playlists, filter_names)
        if not playlists:
            log.error("No playlists matched the filter.")
            sys.exit(1)
        log.info(f"Filtered to {len(playlists)} playlist(s): {', '.join(pl['name'] for pl in playlists)}")

    if test_mode:
        log.info(f"*** TEST MODE ***")

    log.info(f"=== Phase 1: Matching tracks ===")
    pool = match_tracks(playlists, test_mode=test_mode)

    log.info(f"\n=== Phase 1b: Cross-liking ===")
    like_playlist_tracks_in_likes(pool)

    log.info(f"\n=== Phase 2: Syncing playlists ===")
    sync_playlists(playlists, pool, test_mode=test_mode)

    log.info("\nDone!")


def cmd_resolve():
    """Manual resolution for unmatched playlist tracks."""
    pool = load_json(POOL_FILE, {})
    playlists = load_json(YANDEX_PLAYLISTS_FILE, [])
    all_tracks = collect_unique_tracks(playlists)

    resolvable = []
    for yid, match in pool.items():
        if match is None:
            continue
        if isinstance(match, dict) and not match.get("spotify_id") and match.get("candidates"):
            t = all_tracks.get(yid)
            if t:
                resolvable.append((yid, t, match["candidates"]))

    if not resolvable:
        unmatched = sum(1 for v in pool.values() if v is None or (isinstance(v, dict) and not v.get("spotify_id")))
        print(f"No tracks with candidates to resolve. ({unmatched} tracks unmatched total)")
        return

    print(f"{len(resolvable)} tracks have Spotify candidates for manual review.")
    print("\nFor each track, enter:")
    print("  Number to select a candidate")
    print("  's' to skip (keep for later)")
    print("  'n' to mark as no match")
    print("  'q' to quit\n")

    resolved = 0
    for yid, t, candidates in resolvable:
        print(f"\n--- {t['artists']} — {t['title']}")
        for j, c in enumerate(candidates):
            name = c.get('spotify_name', '?')[:45]
            artists = c.get('spotify_artists', '?')[:30]
            score = c.get('title_score', 0)
            print(f"  [{j}] {name:45s}  by {artists:30s}  score={score:.2f}")

        choice = input("  Choice: ").strip().lower()
        if choice == "q":
            break
        elif choice == "s":
            continue
        elif choice == "n":
            pool[yid] = None
            resolved += 1
        elif choice.isdigit() and int(choice) < len(candidates):
            picked = candidates[int(choice)]
            pool[yid] = {
                "spotify_id": picked["spotify_id"],
                "spotify_uri": picked["spotify_uri"],
                "title_score": picked["title_score"],
                "source": "manual_resolve",
            }
            resolved += 1
            log.info(f"  → resolved: {picked.get('spotify_name', '?')}")
        else:
            print("  → skipped (invalid input)")
            continue

        atomic_write_json(POOL_FILE, pool)

    if resolved:
        log.info(f"Resolved {resolved} tracks. Run --full to sync playlists.")


def cmd_stats():
    """Show playlist sync status."""
    playlists = load_json(YANDEX_PLAYLISTS_FILE, [])
    pool = load_json(POOL_FILE, {})
    mapping = load_json(MAPPING_FILE, {})

    if not playlists:
        log.info("No playlists found. Run: python3 yandex_fetch.py --playlists --token TOKEN")
        return

    all_tracks = collect_unique_tracks(playlists)
    matched = sum(1 for v in pool.values() if v is not None and isinstance(v, dict) and v.get("spotify_id"))
    unmatched = len(all_tracks) - matched
    with_candidates = sum(1 for v in pool.values()
                         if v is not None and isinstance(v, dict)
                         and not v.get("spotify_id") and v.get("candidates"))

    log.info(f"Yandex playlists:      {len(playlists)}")
    log.info(f"Unique tracks:         {len(all_tracks)}")
    log.info(f"Matched:               {matched}")
    log.info(f"Unmatched:             {unmatched} ({with_candidates} with candidates)")
    log.info(f"Synced to Spotify:     {len(mapping)}")

    for pl in playlists:
        pl_id = str(pl["playlist_id"])
        pl_name = pl["name"]
        pl_tracks = len(pl.get("tracks", []))
        pl_map = mapping.get(pl_id, {})
        synced = len(pl_map.get("last_synced_track_ids", []))
        spotify_id = pl_map.get("spotify_playlist_id", "not created")
        log.info(f"  {pl_name}: {pl_tracks} tracks, {synced} synced → {spotify_id}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync Yandex playlists to Spotify")
    parser.add_argument("--test", action="store_true", help="Test with first playlist only")
    parser.add_argument("--full", action="store_true", help="Sync all playlists")
    parser.add_argument("--filter-playlist", action="append", metavar="NAME", help="Only sync playlists matching this name (exact match, repeatable)")
    parser.add_argument("--resolve", action="store_true", help="Manually resolve unmatched tracks")
    parser.add_argument("--stats", action="store_true", help="Show sync status")
    args = parser.parse_args()

    if args.test:
        cmd_sync(test_mode=True, filter_names=args.filter_playlist)
    elif args.full:
        cmd_sync(test_mode=False, filter_names=args.filter_playlist)
    elif args.resolve:
        cmd_resolve()
    elif args.stats:
        cmd_stats()
    else:
        print("Usage:")
        print("  python3 playlist_sync.py --test                                    # Test with first playlist")
        print("  python3 playlist_sync.py --full                                    # Sync all playlists")
        print("  python3 playlist_sync.py --full --filter-playlist 'My Playlist'    # Sync specific playlist(s)")
        print("  python3 playlist_sync.py --resolve                                 # Manually resolve unmatched tracks")
        print("  python3 playlist_sync.py --stats                                   # Show sync status")
