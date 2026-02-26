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
"""

import json
import time
import sys
import re
import argparse
import unicodedata
import os
import tempfile
from difflib import SequenceMatcher
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from config import CLIENT_ID, CLIENT_SECRET, REDIRECT_URI

DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = f"{DIR}/data"

FOUND_FILE = f"{DATA_DIR}/spotify_found.json"
NOT_FOUND_FILE = f"{DATA_DIR}/spotify_not_found.json"
PENDING_FILE = f"{DATA_DIR}/spotify_pending.json"

DELAY_BETWEEN_REQUESTS = 0
LIKE_BATCH_SIZE = 40        # max track URIs per PUT /me/library call (API limit)
DELAY_AFTER_LIKE = 0
DELAY_BETWEEN_BATCHES = 0
TITLE_MATCH_THRESHOLD = 0.7
CANDIDATES_TO_STORE = 5

try:
    from transliterate import translit
    HAS_TRANSLIT = True
except ImportError:
    HAS_TRANSLIT = False
    print("Note: transliterate not installed, skipping Cyrillic→Latin fallback searches")

import requests as _requests

_session = _requests.Session()
# No retry adapter — we handle 429s ourselves
_session.mount("https://", _requests.adapters.HTTPAdapter(max_retries=0))

sp = spotipy.Spotify(
    auth_manager=SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope="user-library-modify user-library-read",
        cache_path=f"{DIR}/.spotify_token_cache",
    ),
    requests_session=_session,
)


def first_artist(artists_str):
    """Extract the first artist name from a comma-separated string."""
    return artists_str.split(",")[0].strip()


def normalize(s):
    s = s.lower().strip()
    s = unicodedata.normalize("NFKD", s)
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s


def similarity(a, b):
    na, nb = normalize(a), normalize(b)
    full = SequenceMatcher(None, na, nb).ratio()
    # Also try truncating the longer string to the shorter's length,
    # so "Yesterday" vs "Yesterday - Remastered 2009" scores 1.0.
    min_len = min(len(na), len(nb))
    if min_len > 0 and max(len(na), len(nb)) > min_len:
        truncated = SequenceMatcher(None, na[:min_len], nb[:min_len]).ratio()
        return max(full, truncated)
    return full


def is_cyrillic(text):
    return any("\u0400" <= c <= "\u04ff" for c in text)


def transliterate_text(text):
    """Transliterate Cyrillic text to Latin. Returns None if not applicable."""
    if not HAS_TRANSLIT or not is_cyrillic(text):
        return None
    try:
        return translit(text, "ru", reversed=True)
    except Exception:
        return None


def get_retry_after(e):
    """Extract Retry-After from a SpotifyException."""
    if e.headers and "Retry-After" in e.headers:
        return int(e.headers["Retry-After"])
    return 60  # conservative default


def spotify_search(query):
    """Execute a Spotify track search, return items. Caller handles rate limits."""
    results = sp.search(q=query, type="track", limit=5)
    return results["tracks"]["items"]


def score_items(items, title):
    """Score search result items by title similarity.
    If the title contains Cyrillic, also tries transliterated comparison
    and keeps the higher score."""
    translit_title = transliterate_text(title)
    scored = []
    for item in items:
        score = similarity(title, item["name"])
        if translit_title:
            score = max(score, similarity(translit_title, item["name"]))
        scored.append({
            "spotify_id": item["id"],
            "spotify_uri": item["uri"],
            "spotify_name": item["name"],
            "spotify_artists": ", ".join(a["name"] for a in item["artists"]),
            "title_score": round(score, 3),
        })
    return scored


def search_track(title, artist):
    """
    Search Spotify for a track by title + artist (single query).
    Scoring handles Cyrillic→Latin transliteration automatically.
    Returns (best_candidate | None, top_candidates_list).
    """
    seen = {}  # spotify_id → candidate, deduplicated keeping highest score

    def run_query(query):
        items = spotify_search(query)
        time.sleep(DELAY_BETWEEN_REQUESTS)
        for c in score_items(items, title):
            sid = c["spotify_id"]
            if sid not in seen or c["title_score"] > seen[sid]["title_score"]:
                seen[sid] = c

    run_query(f"track:{title} artist:{artist}")

    if not seen:
        return None, []

    ranked = sorted(seen.values(), key=lambda c: c["title_score"], reverse=True)
    best = ranked[0]
    return best, ranked[:CANDIDATES_TO_STORE]


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

    print(f"Liking {len(pending)} pending tracks...")
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
                    print(f"  → rate limited ({retry_after}s), saving and exiting.")
                    save_pending(pending[chunk_start:])
                    save_found(found)
                    return found, liked
                print(f"  → rate limited, waiting {retry_after}s...")
                time.sleep(retry_after + 5)
                try:
                    like_tracks(ids)
                except Exception:
                    print(f"  → still failing. {liked} liked, {len(pending) - liked} remain.")
                    save_pending(pending[chunk_start:])
                    save_found(found)
                    return found, liked
            elif e.http_status == 403:
                print(f"\n*** 403 Forbidden. Pending {len(pending) - liked} tracks saved to disk. ***")
                print("Likely Spotify Development Mode write rate limit.")
                save_pending(pending[chunk_start:])
                save_found(found)
                return found, liked
            else:
                print(f"  → error {e.http_status}. {liked} liked, {len(pending) - liked} remain.")
                save_pending(pending[chunk_start:])
                save_found(found)
                return found, liked
        found.extend(chunk)
        liked += len(chunk)
        print(f"  → liked {len(chunk)} tracks (total: {liked}/{len(pending)})")
        time.sleep(DELAY_AFTER_LIKE)

    clear_pending()
    save_found(found)
    return found, liked


# --- Commands ---

def cmd_migrate(test_mode):
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
        print(f"Recovered {total_recovered} pending likes from previous run.")

    pending_on_disk = load_json(PENDING_FILE, [])
    done_ids = set(e["yandex_id"] for e in found) | set(e["yandex_id"] for e in not_found) | set(e["yandex_id"] for e in pending_on_disk)

    # Reverse order: add last Yandex track first so Spotify liked list mirrors Yandex order
    # (Spotify shows most recently liked at top)
    remaining = [t for t in reversed(all_tracks) if t["id"] not in done_ids]

    if len(all_tracks) - len(remaining) > 0:
        print(f"Resuming: {len(all_tracks) - len(remaining)} already processed, {len(remaining)} remaining")

    if test_mode:
        remaining = remaining[:10]
        print(f"\n*** TEST MODE: processing up to 10 tracks ***\n")

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
                best, candidates = search_track(t["title"], artist)
            except spotipy.exceptions.SpotifyException as e:
                if e.http_status == 429:
                    retry_after = get_retry_after(e)
                    if retry_after > 60:
                        print(f"\n*** Rate limited ({retry_after}s). Flushing pending and exiting. ***")
                        flush()
                        save_not_found(not_found)
                        sys.exit(1)
                    print(f"\n*** Rate limited on search! Flushing, saving and waiting {retry_after}s ***")
                    flush()
                    save_not_found(not_found)
                    time.sleep(retry_after + 5)
                    try:
                        best, candidates = search_track(t["title"], artist)
                    except Exception:
                        print("*** Still failing after retry, saving and exiting. Run again to resume. ***")
                        save_pending(pending_likes)
                        save_not_found(not_found)
                        sys.exit(1)
                else:
                    print(f"  Spotify error: {e}")
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
            print(f"[{global_idx}/{len(all_tracks)}] {status} | {artist} — {t['title']}  ({liked} liked, {pct:.0f}%)")

    except KeyboardInterrupt:
        print(f"\n\n*** Interrupted! Flushing {len(pending_likes)} pending likes and saving progress... ***")
        try:
            flush()
        except Exception as e:
            print(f"  → flush failed ({e}), saving pending to disk for next run")
            save_pending(pending_likes)
        save_not_found(not_found)
        print(f"Saved: {len(found)} found, {len(not_found)} not found. Run again to resume.")
        sys.exit(0)

    flush()  # send any remaining pending likes

    not_found, met_changed = update_artist_met_status(found, not_found)
    if met_changed:
        save_not_found(not_found)

    total = len(found) + len(not_found)
    pct = 100 * len(found) / total if total else 0
    print(f"\n=== RESULTS ===")
    print(f"Total Yandex tracks: {len(all_tracks)}")
    print(f"Processed:           {total}")
    print(f"Found & liked:       {len(found)} ({pct:.1f}%)")
    print(f"Not found:           {len(not_found)}")
    if not_found:
        with_candidates = sum(1 for e in not_found if e.get("candidates"))
        print(f"  → {with_candidates} have Spotify candidates for manual resolution")
        print(f"  → run --resolve to pick manually (no re-fetching needed)")

    if test_mode:
        print(f"\n*** TEST COMPLETE — check Spotify Liked Songs, then run --full ***")


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
            print("  → marked as no match")
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
                print(f"  → liked: {picked['spotify_name']}")
            except Exception as e:
                print(f"  → ERROR liking track: {e}")
                continue
        else:
            print("  → skipped (invalid input)")
            continue

        # Save after every decision (like, no-match) so nothing is lost on crash
        save_found(found)
        save_not_found(not_found)

    print(f"\nTotal liked: {len(found)}")
    remaining_resolvable = sum(1 for e in not_found if e.get("candidates"))
    print(f"Remaining with candidates: {remaining_resolvable}")
    print(f"Remaining without candidates: {len(not_found) - remaining_resolvable}")


def cmd_pending():
    """Like only the tracks in spotify_pending.json, no searching."""
    found = load_json(FOUND_FILE, [])
    found, liked = flush_pending(found)
    if liked:
        print(f"\nDone! Total liked: {len(found)}")
    else:
        print("No pending tracks to like.")


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

    print(f"Total Yandex tracks:  {total}")
    print(f"Found & liked:        {len(found)} ({pct:.1f}%)")
    print(f"Not found:            {len(not_found)} ({with_candidates} with candidates)")
    print(f"Pending:              {len(pending)}")
    print(f"Finding overlap:      {overlap_pct:.1f}%")
    print(f"Remaining to process: {remaining}")
    if not_on_spotify:
        artist_counts = []
        for a in not_on_spotify:
            count = sum(1 for e in not_found
                        if first_artist(e.get("yandex_artists", "")) == a)
            artist_counts.append((a, count))
        artist_counts.sort(key=lambda x: x[1], reverse=True)
        print(f"\nArtists not found on Spotify ({len(not_on_spotify)}):")
        for a, count in artist_counts:
            print(f"  {a} ({count})")
    return remaining


def cmd_full_sync(yandex_token):
    """Full pipeline: fetch from Yandex, print stats, migrate if tracks remain."""
    import subprocess
    print("=== Fetching from Yandex Music ===\n")
    result = subprocess.run(
        [sys.executable, f"{DIR}/yandex_fetch.py", "--token", yandex_token],
        cwd=DIR,
    )
    if result.returncode != 0:
        print("\n*** Yandex fetch failed, aborting. ***")
        sys.exit(1)

    print(f"\n=== Migration Stats ===\n")
    remaining = cmd_stats()

    if remaining > 0:
        print(f"\n=== Migrating {remaining} tracks to Spotify ===\n")
        cmd_migrate(test_mode=False)
    else:
        print("\nAll tracks already processed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="Test on first 10 unprocessed tracks")
    parser.add_argument("--full", action="store_true", help="Process all tracks (resumable)")
    parser.add_argument("--full-sync", action="store_true", help="Fetch from Yandex + migrate to Spotify")
    parser.add_argument("--resolve", action="store_true", help="Manually resolve unmatched tracks using stored candidates")
    parser.add_argument("--pending", action="store_true", help="Like only pending tracks (no searching)")
    parser.add_argument("--stats", action="store_true", help="Print current migration statistics")
    parser.add_argument("--token", help="Yandex Music OAuth token (required for --full-sync)")
    args = parser.parse_args()

    if args.test:
        cmd_migrate(test_mode=True)
    elif args.full:
        cmd_stats()
        print()
        cmd_migrate(test_mode=False)
    elif args.full_sync:
        token = args.token or os.environ.get("YANDEX_MUSIC_TOKEN")
        if not token:
            print("Error: --token is required for --full-sync")
            print("  python3 spotify_crossref.py --full-sync --token YOUR_TOKEN")
            sys.exit(1)
        cmd_full_sync(token)
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
        print("  python3 spotify_crossref.py --resolve                 # Manually pick from stored candidates")
        print("  python3 spotify_crossref.py --stats                   # Print migration statistics")
        print("  python3 spotify_crossref.py --pending  # Like only pending tracks (no searching)")
