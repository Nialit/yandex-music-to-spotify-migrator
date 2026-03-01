#!/usr/bin/env python3
"""Sync liked tracks and playlists from Yandex Music."""

import argparse
import json
import os
import sys
from pathlib import Path

from yandex_music import Client

from log_setup import get_logger

DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = f"{DIR}/data"

LIKES_FILE = f"{DATA_DIR}/yandex_music_likes.json"
PLAYLISTS_FILE = f"{DATA_DIR}/yandex_playlists.json"
FOUND_FILE = f"{DATA_DIR}/spotify_found.json"
NOT_FOUND_FILE = f"{DATA_DIR}/spotify_not_found.json"

log = get_logger("yandex")


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
        log.info(f"  {len(tracks)}/{len(track_ids)}")
    return tracks


def fetch_likes(client):
    """Fetch liked tracks, append new ones to existing file."""
    existing = load_json(LIKES_FILE)
    known_ids = {str(t["id"]) for t in existing}
    log.info(f"Loaded {len(existing)} existing tracks from {LIKES_FILE}")

    log.info("Fetching liked track IDs...")
    likes = client.users_likes_tracks()
    track_ids = [t.track_id for t in likes]
    log.info(f"Found {len(track_ids)} liked tracks on Yandex")

    new_ids = [tid for tid in track_ids if str(tid).split(":")[0] not in known_ids]

    if not new_ids:
        log.info(f"No new tracks to add (all {len(track_ids)} already known)")
    else:
        log.info(f"Fetching details for {len(new_ids)} new tracks...")
        new_tracks = fetch_track_details(client, new_ids)

        # Prepend new tracks (newest first)
        merged = new_tracks + existing
        with open(LIKES_FILE, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)

        log.info(f"Added {len(new_tracks)} new tracks -> {len(merged)} total in {LIKES_FILE}")

    # Report pending Spotify sync
    synced_ids = get_synced_yandex_ids()
    all_tracks = load_json(LIKES_FILE)
    pending = sum(1 for t in all_tracks if str(t["id"]) not in synced_ids)
    log.info(f"Pending Spotify sync: {pending} tracks (run spotify_crossref.py --full)")


def fetch_playlist_track_ids(client, uid, kind):
    """Fetch track IDs for a playlist via raw API (bypasses buggy Artist deserialization)."""
    url = f"{client.base_url}/users/{uid}/playlists/{kind}"
    data = client._request.get(url)
    tracks = data.get("tracks") or []
    ids = []
    for t in tracks:
        tid = t.get("id") or (t.get("track", {}) or {}).get("id")
        if tid:
            ids.append(str(tid).split(":")[0])
    return ids


def fetch_playlists(client, filter_names=None):
    """Fetch all user playlists and their tracks, save to yandex_playlists.json.

    Incremental: on re-run, only refetches playlists whose track ID set changed.
    If filter_names is set, only fetch playlists matching those names (exact, case-sensitive)."""
    existing = load_json(PLAYLISTS_FILE)
    existing_by_id = {str(p["playlist_id"]): p for p in existing}

    uid = client.me.account.uid

    log.info("Fetching playlist list...")
    playlists = client.users_playlists_list()
    log.info(f"Found {len(playlists)} playlists on Yandex")

    if filter_names:
        filter_set = set(filter_names)
        playlists = [pl for pl in playlists if (pl.title or "") in filter_set]
        log.info(f"Filtered to {len(playlists)} playlists matching: {', '.join(filter_names)}")

    # Preserve existing playlists not in the current fetch set
    fetched_ids = set()
    updated = []
    fetched_count = 0
    skipped_count = 0

    for pl in playlists:
        pl_id = str(pl.kind)
        pl_name = pl.title or f"Playlist {pl_id}"
        fetched_ids.add(pl_id)

        # Fetch track IDs via raw API to avoid library deserialization bugs
        raw_ids = fetch_playlist_track_ids(client, uid, pl.kind)
        if not raw_ids:
            log.info(f"  {pl_name}: empty, skipping")
            updated.append({
                "playlist_id": pl_id,
                "name": pl_name,
                "tracks": [],
            })
            continue

        current_track_ids = set(raw_ids)

        # Check if we can skip (same track ID set as last time)
        existing_pl = existing_by_id.get(pl_id)
        if existing_pl:
            old_track_ids = {str(t["id"]) for t in existing_pl.get("tracks", [])}
            if old_track_ids == current_track_ids:
                log.info(f"  {pl_name}: unchanged ({len(current_track_ids)} tracks)")
                updated.append(existing_pl)
                skipped_count += 1
                continue

        # Track set changed or new playlist — fetch full details
        log.info(f"  {pl_name}: fetching {len(raw_ids)} tracks...")
        tracks = fetch_track_details(client, raw_ids)

        updated.append({
            "playlist_id": pl_id,
            "name": pl_name,
            "tracks": tracks,
        })
        fetched_count += 1

    # Preserve playlists not in current fetch (e.g. when using --filter-playlist)
    for ep in existing:
        if str(ep["playlist_id"]) not in fetched_ids:
            updated.append(ep)

    with open(PLAYLISTS_FILE, "w", encoding="utf-8") as f:
        json.dump(updated, f, ensure_ascii=False, indent=2)

    log.info(f"Saved {len(updated)} playlists to {PLAYLISTS_FILE}")
    log.info(f"  {fetched_count} fetched, {skipped_count} unchanged (skipped)")


def main():
    parser = argparse.ArgumentParser(description="Sync Yandex Music liked tracks and playlists")
    parser.add_argument("--token", help="Yandex Music OAuth token")
    parser.add_argument("--playlists", action="store_true", help="Also fetch playlists")
    parser.add_argument("--no-likes", action="store_true", help="Skip fetching liked tracks")
    parser.add_argument("--filter-playlist", nargs="+", metavar="NAME", help="Only fetch playlists matching these names")
    args = parser.parse_args()

    token = args.token or os.environ.get("YANDEX_MUSIC_TOKEN")
    if not token:
        token = input("Paste your Yandex Music OAuth token: ").strip()
    if not token:
        log.error("Error: empty token")
        sys.exit(1)

    # Authenticate
    log.info("Authenticating...")
    client = Client(token).init()
    log.info(f"Logged in as: {client.me.account.login}")

    # Fetch likes unless skipped
    if not args.no_likes:
        fetch_likes(client)

    # Optionally fetch playlists
    if args.playlists:
        log.info("=== Fetching playlists ===")
        fetch_playlists(client, filter_names=args.filter_playlist)


if __name__ == "__main__":
    main()
