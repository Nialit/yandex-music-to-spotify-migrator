"""
Migrate Yandex Music liked tracks to Spotify Liked Songs.

Searches Spotify directly for each track — no pre-built artist mappings needed.
Scoring handles Cyrillic→Latin transliteration automatically.
Unmatched tracks are saved with Spotify candidates (including IDs) for manual resolution.

Usage:
  python3 spotify_crossref.py --test     # Test on first 10 unprocessed tracks
  python3 spotify_crossref.py --full     # Process all tracks (resumable)
  python3 spotify_crossref.py --resolve  # Manually resolve unmatched tracks
  python3 spotify_crossref.py --pending  # Like only pending tracks (no searching)
  python3 spotify_crossref.py --full --force-prematch  # Refetch entire Spotify library for pre-matching
"""

import json
import time
import sys
import argparse
import os
import tempfile
import logging
from logging.handlers import TimedRotatingFileHandler
import spotipy

from spotify_client import create_client
from matching import (
    first_artist, normalize, similarity, is_cyrillic, transliterate_text,
    score_items, fetch_liked_songs, build_library_index, prematch_from_library,
    search_track, get_retry_after,
    TITLE_MATCH_THRESHOLD, CANDIDATES_TO_STORE, HAS_TRANSLIT,
)

DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = f"{DIR}/data"
LOG_DIR = f"{DIR}/logs"

os.makedirs(LOG_DIR, exist_ok=True)

log = logging.getLogger("spotify_crossref")
log.setLevel(logging.DEBUG)

_log_fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

_console = logging.StreamHandler()
_console.setLevel(logging.INFO)
_console.setFormatter(logging.Formatter("%(message)s"))
log.addHandler(_console)

_latest = logging.FileHandler(f"{LOG_DIR}/latest.log", mode="w", encoding="utf-8")
_latest.setLevel(logging.DEBUG)
_latest.setFormatter(_log_fmt)
log.addHandler(_latest)

_daily = TimedRotatingFileHandler(
    f"{LOG_DIR}/spotify_crossref.log", when="midnight", backupCount=0, encoding="utf-8",
)
_daily.setLevel(logging.DEBUG)
_daily.setFormatter(_log_fmt)
_daily.namer = lambda name: name.replace(".log.", ".") + ".log"
log.addHandler(_daily)

FOUND_FILE = f"{DATA_DIR}/spotify_found.json"
NOT_FOUND_FILE = f"{DATA_DIR}/spotify_not_found.json"
PENDING_FILE = f"{DATA_DIR}/spotify_pending.json"

DELAY_BETWEEN_REQUESTS = 0
LIKE_BATCH_SIZE = 40        # max track URIs per PUT /me/library call (API limit)
DELAY_AFTER_LIKE = 0
DELAY_BETWEEN_BATCHES = 0

if not HAS_TRANSLIT:
    log.info("Note: transliterate not installed, skipping Cyrillic→Latin fallback searches")

sp = create_client()


def like_tracks(spotify_ids):
    """Save tracks to library using the PUT /me/library endpoint (Feb 2026).
    Accepts a list of Spotify track IDs, converts to URIs internally.
    URIs passed as query parameter per API spec (max 40 per request)."""
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


def update_artist_met_status(found, not_found):
    """Set artist_met_on_spotify flag on not_found entries.
    Returns (not_found, changed) where changed is True if any entry was updated."""
    met_artists = {first_artist(e["yandex_artists"]) for e in found if e.get("yandex_artists")}
    changed = False
    for e in not_found:
        artist = first_artist(e.get("yandex_artists", ""))
        met = artist in met_artists
        if e.get("artist_met_on_spotify") != met:
            e["artist_met_on_spotify"] = met
            changed = True
    return not_found, changed


def save_found(found):
    atomic_write_json(FOUND_FILE, found)


def save_not_found(not_found):
    atomic_write_json(NOT_FOUND_FILE, not_found)


def save_pending(pending):
    atomic_write_json(PENDING_FILE, pending)


def clear_pending():
    try:
        os.unlink(PENDING_FILE)
    except FileNotFoundError:
        pass


# --- Core: flush pending likes to Spotify ---

def flush_pending(found):
    """Like all tracks in spotify_pending.json, move them to found, clear pending.
    Returns (found, num_liked). Handles rate limiting with one retry."""
    pending = load_json(PENDING_FILE, [])
    if not pending:
        return found, 0

    log.info(f"Liking {len(pending)} pending tracks...")
    liked = 0
    for chunk_start in range(0, len(pending), LIKE_BATCH_SIZE):
        chunk = pending[chunk_start:chunk_start + LIKE_BATCH_SIZE]
        ids = [e["spotify_id"] for e in chunk]
        try:
            like_tracks(ids)
        except spotipy.exceptions.SpotifyException as e:
            if e.http_status == 429:
                retry_after = get_retry_after(e)
                if retry_after > 60:
                    log.warning(f"  → rate limited ({retry_after}s), saving and exiting.")
                    save_pending(pending[chunk_start:])
                    save_found(found)
                    return found, liked
                log.warning(f"  → rate limited, waiting {retry_after}s...")
                time.sleep(retry_after + 5)
                try:
                    like_tracks(ids)
                except Exception:
                    log.error(f"  → still failing. {liked} liked, {len(pending) - liked} remain.")
                    save_pending(pending[chunk_start:])
                    save_found(found)
                    return found, liked
            elif e.http_status == 403:
                log.error(f"*** 403 Forbidden. Pending {len(pending) - liked} tracks saved to disk. ***")
                log.error("Likely Spotify Development Mode write rate limit.")
                save_pending(pending[chunk_start:])
                save_found(found)
                return found, liked
            else:
                log.error(f"  → error {e.http_status}. {liked} liked, {len(pending) - liked} remain.")
                save_pending(pending[chunk_start:])
                save_found(found)
                return found, liked
        found.extend(chunk)
        liked += len(chunk)
        log.info(f"  → liked {len(chunk)} tracks (total: {liked}/{len(pending)})")
        time.sleep(DELAY_AFTER_LIKE)

    clear_pending()
    save_found(found)
    return found, liked


# --- Commands ---

def cmd_migrate(test_mode, force_prematch=False):
    with open(f"{DATA_DIR}/yandex_music_likes.json") as f:
        all_tracks = json.load(f)

    found = load_json(FOUND_FILE, [])
    not_found = load_json(NOT_FOUND_FILE, [])

    # Flush all pending likes from previous runs, retrying until exhausted
    total_recovered = 0
    while load_json(PENDING_FILE, []):
        found, recovered = flush_pending(found)
        if recovered:
            total_recovered += recovered
        else:
            break  # flush returned 0 — stuck on error, stop retrying
    if total_recovered:
        log.info(f"Recovered {total_recovered} pending likes from previous run.")

    pending_on_disk = load_json(PENDING_FILE, [])
    done_ids = set(e["yandex_id"] for e in found) | set(e["yandex_id"] for e in not_found) | set(e["yandex_id"] for e in pending_on_disk)

    # Reverse order: add last Yandex track first so Spotify liked list mirrors Yandex order
    # (Spotify shows most recently liked at top)
    remaining = [t for t in reversed(all_tracks) if t["id"] not in done_ids]

    # --- Pre-match against existing Spotify library ---
    if remaining or not_found or pending_on_disk:
        log.info("Fetching Spotify liked songs for pre-matching...")
        existing_spotify_ids = None
        if found and not force_prematch:
            existing_spotify_ids = {e["spotify_id"] for e in found if e.get("spotify_id")}
        liked_songs = fetch_liked_songs(sp, existing_spotify_ids, log=log)

        if liked_songs:
            new_songs = [s for s in liked_songs if s["spotify_id"] not in (existing_spotify_ids or set())]
            if existing_spotify_ids:
                log.info(f"  {len(new_songs)} new tracks in Spotify library since last sync.")

            title_index, artist_index = build_library_index(liked_songs)

            # Prematch remaining yandex tracks
            prematched_remaining = []
            if remaining:
                prematched_remaining, remaining = prematch_from_library(remaining, title_index, artist_index)
                if prematched_remaining:
                    found.extend(prematched_remaining)
                    done_ids.update(e["yandex_id"] for e in prematched_remaining)

            # Prematch not_found entries (user may have liked them on Spotify manually)
            prematched_not_found = []
            if not_found:
                nf_as_yandex = [{"id": e["yandex_id"], "title": e["yandex_title"], "artists": e["yandex_artists"]} for e in not_found]
                matched_nf, _ = prematch_from_library(nf_as_yandex, title_index, artist_index)
                if matched_nf:
                    prematched_not_found = matched_nf
                    resolved_ids = {e["yandex_id"] for e in matched_nf}
                    not_found = [e for e in not_found if e["yandex_id"] not in resolved_ids]
                    found.extend(matched_nf)
                    done_ids.update(resolved_ids)

            # Prematch pending entries
            prematched_pending = []
            if pending_on_disk:
                pd_as_yandex = [{"id": e["yandex_id"], "title": e["yandex_title"], "artists": e["yandex_artists"]} for e in pending_on_disk]
                matched_pd, _ = prematch_from_library(pd_as_yandex, title_index, artist_index)
                if matched_pd:
                    prematched_pending = matched_pd
                    resolved_ids = {e["yandex_id"] for e in matched_pd}
                    pending_on_disk = [e for e in pending_on_disk if e["yandex_id"] not in resolved_ids]
                    found.extend(matched_pd)
                    done_ids.update(resolved_ids)
                    save_pending(pending_on_disk if pending_on_disk else [])
                    if not pending_on_disk:
                        clear_pending()

            total_prematched = len(prematched_remaining) + len(prematched_not_found) + len(prematched_pending)
            if total_prematched:
                save_found(found)
                if prematched_not_found:
                    save_not_found(not_found)
                parts = []
                if prematched_remaining:
                    parts.append(f"{len(prematched_remaining)} from unprocessed")
                if prematched_not_found:
                    parts.append(f"{len(prematched_not_found)} from not_found")
                if prematched_pending:
                    parts.append(f"{len(prematched_pending)} from pending")
                log.info(f"Pre-matched {total_prematched} tracks from existing library ({', '.join(parts)}).")
                if remaining:
                    log.info(f"  {len(remaining)} remaining to search.")
            else:
                log.info("No pre-matches found in existing library.")
        else:
            log.info("No liked songs in Spotify library (or fetch returned empty).")

    if len(all_tracks) - len(remaining) > 0:
        log.info(f"Resuming: {len(all_tracks) - len(remaining)} already processed, {len(remaining)} remaining")

    if test_mode:
        remaining = remaining[:10]
        log.info("*** TEST MODE: processing up to 10 tracks ***")

    pending_likes = []

    def flush():
        """Write pending to disk and flush to Spotify."""
        nonlocal found, pending_likes
        if not pending_likes:
            return
        # Save to disk first so a crash doesn't lose search results
        save_pending(pending_likes)
        pending_likes = []
        found, n = flush_pending(found)
        if n == 0:
            # flush_pending failed completely — exit so user can retry
            save_not_found(not_found)
            sys.exit(1)

    try:
        for i, t in enumerate(remaining):
            artist = first_artist(t["artists"])
            global_idx = len(done_ids) + i + 1

            try:
                best, candidates = search_track(sp, t["title"], artist)
            except spotipy.exceptions.SpotifyException as e:
                if e.http_status == 429:
                    retry_after = get_retry_after(e)
                    if retry_after > 60:
                        log.error(f"*** Rate limited ({retry_after}s). Flushing pending and exiting. ***")
                        flush()
                        save_not_found(not_found)
                        sys.exit(1)
                    log.warning(f"*** Rate limited on search! Flushing, saving and waiting {retry_after}s ***")
                    flush()
                    save_not_found(not_found)
                    time.sleep(retry_after + 5)
                    try:
                        best, candidates = search_track(sp, t["title"], artist)
                    except Exception:
                        log.error("*** Still failing after retry, saving and exiting. Run again to resume. ***")
                        save_pending(pending_likes)
                        save_not_found(not_found)
                        sys.exit(1)
                else:
                    log.error(f"  Spotify error: {e}")
                    not_found.append({
                        "yandex_title": t["title"], "yandex_artists": t["artists"],
                        "yandex_id": t["id"], "reason": "api_error", "candidates": [],
                    })
                    save_not_found(not_found)
                    continue

            if best and best["title_score"] >= TITLE_MATCH_THRESHOLD:
                pending_likes.append({
                    "yandex_title": t["title"], "yandex_artists": t["artists"], "yandex_id": t["id"],
                    **best,
                })
                status = f"OK    score={best['title_score']:.2f} → {best['spotify_name']}"
                if len(pending_likes) >= LIKE_BATCH_SIZE:
                    flush()
            else:
                reason = "no_results" if not best else f"title_mismatch best={best['title_score']:.2f}"
                not_found.append({
                    "yandex_title": t["title"], "yandex_artists": t["artists"], "yandex_id": t["id"],
                    "reason": reason,
                    "candidates": candidates,
                })
                status = f"MISS  {reason}"
                save_not_found(not_found)

            liked = len(found) + len(pending_likes)
            total_done = liked + len(not_found)
            pct = 100 * liked / total_done if total_done else 0
            log.info(f"[{global_idx}/{len(all_tracks)}] {status} | {artist} — {t['title']}  ({liked} liked, {pct:.0f}%)")

    except KeyboardInterrupt:
        log.warning(f"*** Interrupted! Flushing {len(pending_likes)} pending likes and saving progress... ***")
        try:
            flush()
        except Exception as e:
            log.error(f"  → flush failed ({e}), saving pending to disk for next run")
            save_pending(pending_likes)
        save_not_found(not_found)
        log.info(f"Saved: {len(found)} found, {len(not_found)} not found. Run again to resume.")
        sys.exit(0)

    flush()  # send any remaining pending likes

    not_found, met_changed = update_artist_met_status(found, not_found)
    if met_changed:
        save_not_found(not_found)

    total = len(found) + len(not_found)
    pct = 100 * len(found) / total if total else 0
    log.info("=== RESULTS ===")
    log.info(f"Total Yandex tracks: {len(all_tracks)}")
    log.info(f"Processed:           {total}")
    log.info(f"Found & liked:       {len(found)} ({pct:.1f}%)")
    log.info(f"Not found:           {len(not_found)}")
    if not_found:
        with_candidates = sum(1 for e in not_found if e.get("candidates"))
        log.info(f"  → {with_candidates} have Spotify candidates for manual resolution")
        log.info(f"  → run --resolve to pick manually (no re-fetching needed)")

    if test_mode:
        log.info("*** TEST COMPLETE — check Spotify Liked Songs, then run --full ***")


def cmd_resolve():
    not_found = load_json(NOT_FOUND_FILE, [])
    found = load_json(FOUND_FILE, [])

    resolvable = [e for e in not_found if e.get("candidates")]
    if not resolvable:
        no_cand = len([e for e in not_found if not e.get("candidates")])
        print(f"No tracks with candidates to resolve. ({no_cand} tracks had no Spotify results at all.)")
        return

    no_candidates_count = len(not_found) - len(resolvable)
    print(f"{len(resolvable)} tracks have Spotify candidates for manual review.")
    if no_candidates_count:
        print(f"{no_candidates_count} tracks had no Spotify results (nothing to resolve for those).")
    print("\nFor each track, enter:")
    print("  Number to select a candidate and add it to Liked Songs")
    print("  's' to skip (keep for later)")
    print("  'n' to mark as no match (clears candidates)")
    print("  'q' to quit\n")

    for entry in resolvable:
        print(f"\n--- {entry['yandex_artists']} — {entry['yandex_title']}")
        print(f"  Reason: {entry.get('reason', '?')}")
        for j, c in enumerate(entry["candidates"]):
            print(f"  [{j}] {c['spotify_name'][:45]:45s}  by {c['spotify_artists'][:30]:30s}  score={c['title_score']:.2f}")

        choice = input("  Choice: ").strip().lower()
        if choice == "q":
            break
        elif choice == "s":
            continue
        elif choice == "n":
            entry["candidates"] = []
            log.info(f"  → marked as no match: {entry['yandex_artists']} — {entry['yandex_title']}")
        elif choice.isdigit() and int(choice) < len(entry["candidates"]):
            picked = entry["candidates"][int(choice)]
            try:
                like_tracks([picked["spotify_id"]])
                found.append({
                    "yandex_title": entry["yandex_title"],
                    "yandex_artists": entry["yandex_artists"],
                    "yandex_id": entry["yandex_id"],
                    "spotify_id": picked["spotify_id"],
                    "spotify_uri": picked["spotify_uri"],
                    "spotify_name": picked["spotify_name"],
                    "spotify_artists": picked["spotify_artists"],
                    "title_score": picked["title_score"],
                    "manually_resolved": True,
                })
                # Remove this entry from not_found
                not_found = [e for e in not_found if e["yandex_id"] != entry["yandex_id"]]
                log.info(f"  → liked: {picked['spotify_name']}")
            except Exception as e:
                log.error(f"  → ERROR liking track: {e}")
                continue
        else:
            print("  → skipped (invalid input)")
            continue

        # Save after every decision (like, no-match) so nothing is lost on crash
        save_found(found)
        save_not_found(not_found)

    log.info(f"Total liked: {len(found)}")
    remaining_resolvable = sum(1 for e in not_found if e.get("candidates"))
    log.info(f"Remaining with candidates: {remaining_resolvable}")
    log.info(f"Remaining without candidates: {len(not_found) - remaining_resolvable}")


def cmd_pending():
    """Like only the tracks in spotify_pending.json, no searching."""
    found = load_json(FOUND_FILE, [])
    found, liked = flush_pending(found)
    if liked:
        log.info(f"Done! Total liked: {len(found)}")
    else:
        log.info("No pending tracks to like.")


def cmd_stats():
    """Print current migration statistics. Returns remaining count."""
    all_tracks = load_json(f"{DATA_DIR}/yandex_music_likes.json", [])
    found = load_json(FOUND_FILE, [])
    not_found = load_json(NOT_FOUND_FILE, [])
    pending = load_json(PENDING_FILE, [])

    total = len(all_tracks)
    done = len(found) + len(not_found) + len(pending)
    remaining = total - done
    with_candidates = sum(1 for e in not_found if e.get("candidates"))
    pct = 100 * len(found) / total if total else 0

    not_found, met_changed = update_artist_met_status(found, not_found)
    if met_changed:
        save_not_found(not_found)

    not_on_spotify = sorted({first_artist(e.get("yandex_artists", ""))
                             for e in not_found
                             if e.get("yandex_artists") and not e.get("artist_met_on_spotify")})

    processed = len(found) + len(pending) + len(not_found)
    overlap_pct = 100 * (len(found) + len(pending)) / processed if processed else 0

    log.info(f"Total Yandex tracks:  {total}")
    log.info(f"Found & liked:        {len(found)} ({pct:.1f}%)")
    log.info(f"Not found:            {len(not_found)} ({with_candidates} with candidates)")
    log.info(f"Pending:              {len(pending)}")
    log.info(f"Finding overlap:      {overlap_pct:.1f}%")
    log.info(f"Remaining to process: {remaining}")
    if not_on_spotify:
        artist_counts = []
        for a in not_on_spotify:
            count = sum(1 for e in not_found
                        if first_artist(e.get("yandex_artists", "")) == a)
            artist_counts.append((a, count))
        artist_counts.sort(key=lambda x: x[1], reverse=True)
        log.info(f"Artists not found on Spotify ({len(not_on_spotify)}):")
        for a, count in artist_counts:
            log.info(f"  {a} ({count})")
    return remaining


def cmd_full_sync(yandex_token, include_playlists=False):
    """Full pipeline: fetch from Yandex, print stats, migrate if tracks remain."""
    import subprocess
    log.info("=== Fetching from Yandex Music ===")
    fetch_args = [sys.executable, f"{DIR}/yandex_fetch.py", "--token", yandex_token]
    if include_playlists:
        fetch_args.append("--playlists")
    result = subprocess.run(fetch_args, cwd=DIR)
    if result.returncode != 0:
        log.error("*** Yandex fetch failed, aborting. ***")
        sys.exit(1)

    log.info("=== Migration Stats ===")
    remaining = cmd_stats()

    if remaining > 0:
        log.info(f"=== Migrating {remaining} tracks to Spotify ===")
        cmd_migrate(test_mode=False)
    else:
        log.info("All tracks already processed.")

    if include_playlists:
        log.info("=== Syncing playlists ===")
        result = subprocess.run(
            [sys.executable, f"{DIR}/playlist_sync.py", "--full"],
            cwd=DIR,
        )
        if result.returncode != 0:
            log.error("*** Playlist sync failed. ***")
            sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="Test on first 10 unprocessed tracks")
    parser.add_argument("--full", action="store_true", help="Process all tracks (resumable)")
    parser.add_argument("--full-sync", action="store_true", help="Fetch from Yandex + migrate to Spotify")
    parser.add_argument("--resolve", action="store_true", help="Manually resolve unmatched tracks using stored candidates")
    parser.add_argument("--pending", action="store_true", help="Like only pending tracks (no searching)")
    parser.add_argument("--stats", action="store_true", help="Print current migration statistics")
    parser.add_argument("--force-prematch", action="store_true", help="Refetch entire Spotify library for pre-matching (ignore early-stop)")
    parser.add_argument("--token", help="Yandex Music OAuth token (required for --full-sync)")
    parser.add_argument("--playlists", action="store_true", help="Include playlist sync in --full-sync")
    args = parser.parse_args()

    if args.test:
        cmd_migrate(test_mode=True, force_prematch=args.force_prematch)
    elif args.full:
        cmd_stats()
        print()
        cmd_migrate(test_mode=False, force_prematch=args.force_prematch)
    elif args.full_sync:
        token = args.token or os.environ.get("YANDEX_MUSIC_TOKEN")
        if not token:
            log.error("Error: --token is required for --full-sync")
            log.error("  python3 spotify_crossref.py --full-sync --token YOUR_TOKEN")
            sys.exit(1)
        cmd_full_sync(token, include_playlists=args.playlists)
    elif args.resolve:
        cmd_resolve()
    elif args.pending:
        cmd_pending()
    elif args.stats:
        cmd_stats()
    else:
        print("Usage:")
        print("  python3 spotify_crossref.py --test                    # Test on 10 tracks")
        print("  python3 spotify_crossref.py --full                    # Process all tracks (resumable)")
        print("  python3 spotify_crossref.py --full-sync --token TOKEN # Yandex fetch + migrate")
        print("  python3 spotify_crossref.py --full-sync --token TOKEN --playlists  # Include playlists")
        print("  python3 spotify_crossref.py --resolve                 # Manually pick from stored candidates")
        print("  python3 spotify_crossref.py --stats                   # Print migration statistics")
        print("  python3 spotify_crossref.py --pending                 # Like only pending tracks (no searching)")
        print("  python3 spotify_crossref.py --full --force-prematch   # Refetch full library for pre-matching")
