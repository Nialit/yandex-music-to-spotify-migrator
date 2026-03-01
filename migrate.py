#!/usr/bin/env python3
"""
Unified CLI for Yandex Music → Spotify migration.

Orchestrates liked tracks migration and playlist sync.

Usage:
  python3 migrate.py --full                              # Likes + playlists
  python3 migrate.py --full --no-playlists               # Likes only
  python3 migrate.py --full-sync --token TOKEN            # Fetch from Yandex + migrate all
  python3 migrate.py --test                              # Test likes (10 tracks) + playlists (1 playlist)
  python3 migrate.py --resolve                           # Resolve unmatched (likes then playlists)
  python3 migrate.py --stats                             # Show all stats

  python3 migrate.py playlist --full                     # Playlists only
  python3 migrate.py playlist --full --filter-playlist "Rock"  # Specific playlists
  python3 migrate.py playlist --resolve                  # Resolve unmatched playlist tracks
  python3 migrate.py playlist --stats                    # Playlist stats only
"""

import argparse
import os
import sys
import subprocess

DIR = os.path.dirname(os.path.abspath(__file__))


def run(script, args):
    """Run a script with arguments. Exits on failure."""
    cmd = [sys.executable, os.path.join(DIR, script)] + args
    result = subprocess.run(cmd, cwd=DIR)
    if result.returncode != 0:
        sys.exit(result.returncode)


def main():
    # Check if first arg is "playlist" subcommand
    if len(sys.argv) > 1 and sys.argv[1] == "playlist":
        # Pass everything after "playlist" directly to playlist_sync.py
        run("playlist_sync.py", sys.argv[2:])
        return

    parser = argparse.ArgumentParser(
        description="Yandex Music → Spotify migration",
        usage="%(prog)s [command] [options]\n       %(prog)s playlist [command] [options]",
    )
    parser.add_argument("--test", action="store_true", help="Test mode: 10 liked tracks + first playlist")
    parser.add_argument("--full", action="store_true", help="Full migration: likes + playlists")
    parser.add_argument("--full-sync", action="store_true", help="Fetch from Yandex + full migration")
    parser.add_argument("--resolve", action="store_true", help="Resolve unmatched tracks (likes then playlists)")
    parser.add_argument("--stats", action="store_true", help="Show migration stats")
    parser.add_argument("--pending", action="store_true", help="Like only pending tracks")
    parser.add_argument("--no-playlists", action="store_true", help="Skip playlist sync")
    parser.add_argument("--force-prematch", action="store_true", help="Refetch entire Spotify library for pre-matching")
    parser.add_argument("--filter-playlist", action="append", metavar="NAME", help="Only sync playlists matching this name (exact match, repeatable)")
    parser.add_argument("--token", help="Yandex Music OAuth token (for --full-sync)")
    args = parser.parse_args()

    do_playlists = not args.no_playlists

    if args.full_sync:
        token = args.token or os.environ.get("YANDEX_MUSIC_TOKEN")
        if not token:
            print("Error: --token is required for --full-sync")
            print("  python3 migrate.py --full-sync --token YOUR_TOKEN")
            sys.exit(1)

        # Fetch from Yandex
        fetch_args = ["--token", token]
        if do_playlists:
            fetch_args.append("--playlists")
        run("yandex_fetch.py", fetch_args)

        # Migrate likes
        run("spotify_crossref.py", ["--full"])

        # Sync playlists
        if do_playlists:
            pl_args = ["--full"]
            if args.filter_playlist:
                for name in args.filter_playlist:
                    pl_args.extend(["--filter-playlist", name])
            run("playlist_sync.py", pl_args)

    elif args.full:
        # Migrate likes
        likes_args = ["--full"]
        if args.force_prematch:
            likes_args.append("--force-prematch")
        run("spotify_crossref.py", likes_args)

        # Sync playlists
        if do_playlists:
            pl_args = ["--full"]
            if args.filter_playlist:
                for name in args.filter_playlist:
                    pl_args.extend(["--filter-playlist", name])
            run("playlist_sync.py", pl_args)

    elif args.test:
        # Test likes
        likes_args = ["--test"]
        if args.force_prematch:
            likes_args.append("--force-prematch")
        run("spotify_crossref.py", likes_args)

        # Test playlists
        if do_playlists:
            pl_args = ["--test"]
            if args.filter_playlist:
                for name in args.filter_playlist:
                    pl_args.extend(["--filter-playlist", name])
            run("playlist_sync.py", pl_args)

    elif args.resolve:
        # Resolve likes
        run("spotify_crossref.py", ["--resolve"])

        # Resolve playlists
        if do_playlists:
            run("playlist_sync.py", ["--resolve"])

    elif args.stats:
        run("spotify_crossref.py", ["--stats"])
        if do_playlists:
            print()
            run("playlist_sync.py", ["--stats"])

    elif args.pending:
        run("spotify_crossref.py", ["--pending"])

    else:
        print("Usage:")
        print("  python3 migrate.py --full                                    # Likes + playlists")
        print("  python3 migrate.py --full --no-playlists                     # Likes only")
        print("  python3 migrate.py --full-sync --token TOKEN                 # Fetch + migrate all")
        print("  python3 migrate.py --test                                    # Test (10 likes + 1 playlist)")
        print("  python3 migrate.py --resolve                                 # Resolve unmatched")
        print("  python3 migrate.py --stats                                   # Show all stats")
        print("  python3 migrate.py --full --filter-playlist 'Rock Classics'  # Filter playlists")
        print()
        print("Playlists only:")
        print("  python3 migrate.py playlist --full                           # Sync all playlists")
        print("  python3 migrate.py playlist --full --filter-playlist 'Rock'  # Specific playlists")
        print("  python3 migrate.py playlist --resolve                        # Resolve playlist tracks")
        print("  python3 migrate.py playlist --stats                          # Playlist stats")


if __name__ == "__main__":
    main()
