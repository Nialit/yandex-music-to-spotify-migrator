#!/usr/bin/env python3
"""
Unified CLI for Yandex Music → Spotify migration.

Usage:
  python3 migrate.py all                                       # Liked + playlists
  python3 migrate.py all --test                                # Test: 10 liked + 10 playlist tracks
  python3 migrate.py all --sync --token TOKEN                  # Yandex fetch + migrate all
  python3 migrate.py liked                                     # Liked tracks only
  python3 migrate.py liked --test --force-prematch             # Test liked with library refetch
  python3 migrate.py playlists --filter-playlist "Rock" "Jazz" # Specific playlists
  python3 migrate.py playlists --test                          # Test playlists
  python3 migrate.py resolve                                   # Resolve unmatched (liked + playlists)
  python3 migrate.py stats                                     # Show stats
  python3 migrate.py pending                                   # Like pending tracks
"""

import argparse
import os
import sys
import subprocess

from log_setup import reset_latest

DIR = os.path.dirname(os.path.abspath(__file__))


def run(script, args):
    """Run a script with arguments. Exits on failure."""
    cmd = [sys.executable, os.path.join(DIR, script)] + args
    result = subprocess.run(cmd, cwd=DIR)
    if result.returncode != 0:
        sys.exit(result.returncode)


def main():
    reset_latest()

    class HelpOnErrorParser(argparse.ArgumentParser):
        def error(self, message):
            self.print_help(sys.stderr)
            sys.stderr.write(f"\nerror: {message}\n")
            sys.exit(2)

    parser = HelpOnErrorParser(
        description="Yandex Music → Spotify migration",
        usage="%(prog)s <flow> [options]",
    )
    parser.add_argument(
        "flow",
        choices=["liked", "playlists", "all", "resolve", "retry", "stats", "pending"],
        help="What to migrate: liked, playlists, all, resolve, retry, stats, pending",
    )
    parser.add_argument("--test", action="store_true", help="Limit to 10 tracks")
    parser.add_argument("--filter-playlist", nargs="+", metavar="NAME", help="Filter playlists by Yandex name")
    parser.add_argument("--force-prematch", action="store_true", help="Refetch entire Spotify library for pre-matching")
    parser.add_argument("--artist-on-spotify", action="store_true", help="With retry: only retry tracks whose artist exists on Spotify")
    parser.add_argument("--sync", action="store_true", help="Fetch from Yandex first, then migrate")
    parser.add_argument("--token", help="Yandex Music OAuth token (for --sync)")
    args = parser.parse_args()

    do_liked = args.flow in ("liked", "all")
    do_playlists = args.flow in ("playlists", "all")

    # Yandex fetch if --sync
    if args.sync:
        token = args.token or os.environ.get("YANDEX_MUSIC_TOKEN")
        if not token:
            print("Error: --token is required for --sync")
            print("  python3 migrate.py all --sync --token YOUR_TOKEN")
            sys.exit(1)
        fetch_args = ["--token", token]
        if not do_liked:
            fetch_args.append("--no-likes")
        if do_playlists:
            fetch_args.append("--playlists")
            if args.filter_playlist:
                fetch_args.append("--filter-playlist")
                fetch_args.extend(args.filter_playlist)
        run("yandex_fetch.py", fetch_args)

    if args.flow in ("liked", "playlists", "all"):
        mode = "--test" if args.test else "--full"

        if do_liked:
            likes_args = [mode]
            if args.force_prematch:
                likes_args.append("--force-prematch")
            run("spotify_crossref.py", likes_args)

        if do_playlists:
            pl_args = [mode]
            if args.force_prematch:
                pl_args.append("--force-prematch")
            if args.filter_playlist:
                pl_args.append("--filter-playlist")
                pl_args.extend(args.filter_playlist)
            run("playlist_sync.py", pl_args)

    elif args.flow == "resolve":
        run("spotify_crossref.py", ["--resolve"])
        run("playlist_sync.py", ["--resolve"])

    elif args.flow == "retry":
        retry_args = ["--retry"]
        if args.artist_on_spotify:
            retry_args.append("--artist-on-spotify")
        run("spotify_crossref.py", retry_args)

    elif args.flow == "stats":
        run("spotify_crossref.py", ["--stats"])
        print()
        run("playlist_sync.py", ["--stats"])

    elif args.flow == "pending":
        run("spotify_crossref.py", ["--pending"])


if __name__ == "__main__":
    main()
