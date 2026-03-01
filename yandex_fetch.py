#!/usr/bin/env python3
"""Sync liked tracks and playlists from Yandex Music."""

import argparse
import json
import os
import sys
from pathlib import Path

from yandex_music import Client

DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = f"{DIR}/data"

LIKES_FILE = f"{DATA_DIR}/yandex_music_likes.json"
PLAYLISTS_FILE = f"{DATA_DIR}/yandex_playlists.json"
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


def fetch_track_details(client, track_ids):
    """Bulk fetch track details using raw JSON workaround."""
    tracks = []
    batch_size = 100
    for i in range(0, len(track_ids), batch_size):
        batch = track_ids[i : i + batch_size]
        url = f"{client.base_url}/tracks"
        result = client._request.post(url, {"track-ids": batch})
        for t in result:
            artist_names = [a.get("name", "") for a in (t.get("artists") or [])]
            artists = ", ".join(n for n in artist_names if n)
            tracks.append({
                "title": t.get("title", ""),
                "artists": artists,
                "id": str(t.get("id", "")),
            })
        print(f"  {len(tracks)}/{len(track_ids)}")
    return tracks


def fetch_likes(client):
    """Fetch liked tracks, append new ones to existing file."""
    existing = load_json(LIKES_FILE)
    known_ids = {str(t["id"]) for t in existing}
    print(f"Loaded {len(existing)} existing tracks from {LIKES_FILE}")

    print("Fetching liked track IDs...")
    likes = client.users_likes_tracks()
    track_ids = [t.track_id for t in likes]
    print(f"Found {len(track_ids)} liked tracks on Yandex")

    new_ids = [tid for tid in track_ids if str(tid).split(":")[0] not in known_ids]

    if not new_ids:
        print(f"\nNo new tracks to add (all {len(track_ids)} already known)")
    else:
        print(f"Fetching details for {len(new_ids)} new tracks...")
        new_tracks = fetch_track_details(client, new_ids)

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


def fetch_playlists(client):
    """Fetch all user playlists and their tracks, save to yandex_playlists.json.

    Incremental: on re-run, only refetches playlists whose track ID set changed."""
    existing = load_json(PLAYLISTS_FILE)
    existing_by_id = {str(p["playlist_id"]): p for p in existing}

    print("Fetching playlist list...")
    playlists = client.users_playlists_list()
    print(f"Found {len(playlists)} playlists on Yandex")

    updated = []
    fetched_count = 0
    skipped_count = 0

    for pl in playlists:
        pl_id = str(pl.kind)
        pl_name = pl.title or f"Playlist {pl_id}"

        # Get short track list from playlist to check IDs
        # Fetch full playlist to get track_ids
        full_pl = client.users_playlists(pl.kind)
        if not full_pl or not full_pl.tracks:
            print(f"  {pl_name}: empty, skipping")
            updated.append({
                "playlist_id": pl_id,
                "name": pl_name,
                "tracks": [],
            })
            continue

        current_track_ids = set()
        raw_ids = []
        for t_short in full_pl.tracks:
            tid = str(t_short.track_id).split(":")[0]
            current_track_ids.add(tid)
            raw_ids.append(t_short.track_id)

        # Check if we can skip (same track ID set as last time)
        existing_pl = existing_by_id.get(pl_id)
        if existing_pl:
            old_track_ids = {str(t["id"]) for t in existing_pl.get("tracks", [])}
            if old_track_ids == current_track_ids:
                print(f"  {pl_name}: unchanged ({len(current_track_ids)} tracks)")
                updated.append(existing_pl)
                skipped_count += 1
                continue

        # Track set changed or new playlist — fetch full details
        print(f"  {pl_name}: fetching {len(raw_ids)} tracks...")
        tracks = fetch_track_details(client, raw_ids)

        updated.append({
            "playlist_id": pl_id,
            "name": pl_name,
            "tracks": tracks,
        })
        fetched_count += 1

    with open(PLAYLISTS_FILE, "w", encoding="utf-8") as f:
        json.dump(updated, f, ensure_ascii=False, indent=2)

    print(f"\nSaved {len(updated)} playlists to {PLAYLISTS_FILE}")
    print(f"  {fetched_count} fetched, {skipped_count} unchanged (skipped)")


def main():
    parser = argparse.ArgumentParser(description="Sync Yandex Music liked tracks and playlists")
    parser.add_argument("--token", help="Yandex Music OAuth token")
    parser.add_argument("--playlists", action="store_true", help="Also fetch playlists")
    args = parser.parse_args()

    token = args.token or os.environ.get("YANDEX_MUSIC_TOKEN")
    if not token:
        token = input("Paste your Yandex Music OAuth token: ").strip()
    if not token:
        print("Error: empty token")
        sys.exit(1)

    # Authenticate
    print("Authenticating...")
    client = Client(token).init()
    print(f"Logged in as: {client.me.account.login}")

    # Always fetch likes
    fetch_likes(client)

    # Optionally fetch playlists
    if args.playlists:
        print("\n=== Fetching playlists ===")
        fetch_playlists(client)


if __name__ == "__main__":
    main()
