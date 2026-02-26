#!/usr/bin/env python3
"""Sync liked tracks from Yandex Music into yandex_music_likes.json (append-only)."""

import argparse
import json
import os
import sys
from pathlib import Path

from yandex_music import Client

DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = f"{DIR}/data"

LIKES_FILE = f"{DATA_DIR}/yandex_music_likes.json"
FOUND_FILE = f"{DATA_DIR}/spotify_found.json"
NOT_FOUND_FILE = f"{DATA_DIR}/spotify_not_found.json"


def load_json(path):
    """Load a JSON file, returning [] if it doesn't exist."""
    p = Path(path)
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def get_synced_yandex_ids():
    """Return set of yandex IDs already processed by spotify_crossref.py."""
    ids = set()
    for entry in load_json(FOUND_FILE):
        if "yandex_id" in entry:
            ids.add(str(entry["yandex_id"]))
    for entry in load_json(NOT_FOUND_FILE):
        if "yandex_id" in entry:
            ids.add(str(entry["yandex_id"]))
    return ids


def main():
    parser = argparse.ArgumentParser(description="Sync Yandex Music liked tracks")
    parser.add_argument("--token", help="Yandex Music OAuth token")
    args = parser.parse_args()

    token = args.token or os.environ.get("YANDEX_MUSIC_TOKEN")
    if not token:
        token = input("Paste your Yandex Music OAuth token: ").strip()
    if not token:
        print("Error: empty token")
        sys.exit(1)

    # Load existing tracks
    existing = load_json(LIKES_FILE)
    known_ids = {str(t["id"]) for t in existing}
    print(f"Loaded {len(existing)} existing tracks from {LIKES_FILE}")

    # Authenticate and fetch from Yandex
    print("Authenticating...")
    client = Client(token).init()
    print(f"Logged in as: {client.me.account.login}")

    print("Fetching liked track IDs...")
    likes = client.users_likes_tracks()
    track_ids = [t.track_id for t in likes]
    print(f"Found {len(track_ids)} liked tracks on Yandex")

    # Find new track IDs
    new_ids = [tid for tid in track_ids if str(tid).split(":")[0] not in known_ids]

    if not new_ids:
        print(f"\nNo new tracks to add (all {len(track_ids)} already known)")
    else:
        # Bulk fetch details for new tracks only
        print(f"Fetching details for {len(new_ids)} new tracks...")
        new_tracks = []
        batch_size = 100
        for i in range(0, len(new_ids), batch_size):
            batch = new_ids[i : i + batch_size]
            # Call the API directly and parse raw JSON ourselves,
            # bypassing the library's buggy Artist deserialization.
            url = f"{client.base_url}/tracks"
            result = client._request.post(url, {"track-ids": batch})
            for t in result:
                artist_names = [a.get("name", "") for a in (t.get("artists") or [])]
                artists = ", ".join(n for n in artist_names if n)
                new_tracks.append({
                    "title": t.get("title", ""),
                    "artists": artists,
                    "id": str(t.get("id", "")),
                })
            print(f"  {len(new_tracks)}/{len(new_ids)}")

        # Prepend new tracks (newest first)
        merged = new_tracks + existing
        with open(LIKES_FILE, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)

        print(f"\nAdded {len(new_tracks)} new tracks → {len(merged)} total in {LIKES_FILE}")

    # Report pending Spotify sync
    synced_ids = get_synced_yandex_ids()
    all_tracks = load_json(LIKES_FILE)
    pending = sum(1 for t in all_tracks if str(t["id"]) not in synced_ids)
    print(f"Pending Spotify sync: {pending} tracks (run spotify_crossref.py --full)")


if __name__ == "__main__":
    main()
