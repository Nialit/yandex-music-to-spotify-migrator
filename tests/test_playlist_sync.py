"""Tests for playlist_sync.py — mocks all Spotify API calls and file I/O."""

import json
import os
import sys
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import spotipy.exceptions

# Patch config and spotipy BEFORE importing the modules
with patch.dict("sys.modules", {"config": MagicMock(CLIENT_ID="x", CLIENT_SECRET="y", REDIRECT_URI="http://localhost")}):
    with patch("spotipy.Spotify"):
        with patch("spotipy.oauth2.SpotifyOAuth"):
            import playlist_sync as ps
            import matching


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def write_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


def read_json(path):
    with open(path) as f:
        return json.load(f)


def make_playlist(pl_id, name, tracks):
    return {
        "playlist_id": pl_id,
        "name": name,
        "tracks": [{"title": t[0], "artists": t[1], "id": t[2]} for t in tracks],
    }


# ---------------------------------------------------------------------------
# collect_unique_tracks()
# ---------------------------------------------------------------------------

class TestCollectUniqueTracks:
    def test_deduplicates_across_playlists(self):
        pl1 = make_playlist("1", "P1", [("Song A", "Artist", "100"), ("Song B", "Artist", "200")])
        pl2 = make_playlist("2", "P2", [("Song A", "Artist", "100"), ("Song C", "Artist", "300")])
        tracks = ps.collect_unique_tracks([pl1, pl2])
        assert len(tracks) == 3
        assert "100" in tracks
        assert "200" in tracks
        assert "300" in tracks

    def test_empty_playlists(self):
        assert ps.collect_unique_tracks([]) == {}

    def test_empty_tracks(self):
        pl = make_playlist("1", "P1", [])
        assert ps.collect_unique_tracks([pl]) == {}


# ---------------------------------------------------------------------------
# match_tracks()
# ---------------------------------------------------------------------------

class TestMatchTracks:
    def setup_method(self):
        self._orig_data_dir = ps.DATA_DIR
        self._orig_pool = ps.POOL_FILE
        self._orig_found = ps.FOUND_FILE
        self._orig_likes = ps.YANDEX_LIKES_FILE

    def teardown_method(self):
        ps.DATA_DIR = self._orig_data_dir
        ps.POOL_FILE = self._orig_pool
        ps.FOUND_FILE = self._orig_found
        ps.YANDEX_LIKES_FILE = self._orig_likes

    def _setup_paths(self, tmp_path):
        ps.DATA_DIR = str(tmp_path)
        ps.POOL_FILE = str(tmp_path / "pool.json")
        ps.FOUND_FILE = str(tmp_path / "found.json")
        ps.YANDEX_LIKES_FILE = str(tmp_path / "likes.json")

    @patch.object(ps, "fetch_liked_songs", return_value=[])
    @patch.object(ps, "search_track")
    @patch.object(matching, "DELAY_BETWEEN_REQUESTS", 0)
    def test_new_track_searched_and_matched(self, mock_search, mock_fetch, tmp_path):
        self._setup_paths(tmp_path)
        pl = make_playlist("1", "P1", [("Yesterday", "Beatles", "100")])

        best = {"spotify_id": "sp1", "spotify_uri": "spotify:track:sp1",
                "spotify_name": "Yesterday", "spotify_artists": "Beatles", "title_score": 1.0}
        mock_search.return_value = (best, [best])

        pool = ps.match_tracks([pl])
        assert "100" in pool
        assert pool["100"]["spotify_id"] == "sp1"

    @patch.object(ps, "fetch_liked_songs", return_value=[])
    @patch.object(ps, "search_track")
    def test_already_in_pool_skipped(self, mock_search, mock_fetch, tmp_path):
        self._setup_paths(tmp_path)
        write_json(ps.POOL_FILE, {"100": {"spotify_id": "sp1", "spotify_uri": "u", "title_score": 1.0}})
        pl = make_playlist("1", "P1", [("Yesterday", "Beatles", "100")])

        pool = ps.match_tracks([pl])
        mock_search.assert_not_called()
        assert pool["100"]["spotify_id"] == "sp1"

    @patch.object(ps, "fetch_liked_songs", return_value=[])
    @patch.object(ps, "search_track")
    def test_already_in_found_copied_to_pool(self, mock_search, mock_fetch, tmp_path):
        self._setup_paths(tmp_path)
        write_json(ps.FOUND_FILE, [{"yandex_id": "100", "spotify_id": "sp1", "spotify_uri": "u", "title_score": 0.9}])
        pl = make_playlist("1", "P1", [("Yesterday", "Beatles", "100")])

        pool = ps.match_tracks([pl])
        mock_search.assert_not_called()
        assert pool["100"]["spotify_id"] == "sp1"
        assert pool["100"]["source"] == "favs_crossref"

    @patch.object(ps, "fetch_liked_songs", return_value=[])
    @patch.object(ps, "search_track")
    @patch.object(matching, "DELAY_BETWEEN_REQUESTS", 0)
    def test_no_match_stored_as_none(self, mock_search, mock_fetch, tmp_path):
        self._setup_paths(tmp_path)
        pl = make_playlist("1", "P1", [("Unknown", "Nobody", "100")])
        mock_search.return_value = (None, [])

        pool = ps.match_tracks([pl])
        assert pool["100"] is None


# ---------------------------------------------------------------------------
# sync_playlists()
# ---------------------------------------------------------------------------

class TestSyncPlaylists:
    def setup_method(self):
        self._orig_data_dir = ps.DATA_DIR
        self._orig_mapping = ps.MAPPING_FILE

    def teardown_method(self):
        ps.DATA_DIR = self._orig_data_dir
        ps.MAPPING_FILE = self._orig_mapping

    def _setup_paths(self, tmp_path):
        ps.DATA_DIR = str(tmp_path)
        ps.MAPPING_FILE = str(tmp_path / "mapping.json")

    @patch.object(ps, "sp")
    def test_creates_new_playlist_and_adds_tracks(self, mock_sp, tmp_path):
        self._setup_paths(tmp_path)
        pl = make_playlist("1", "My Playlist", [("Song", "Artist", "100")])
        pool = {"100": {"spotify_id": "sp1", "spotify_uri": "spotify:track:sp1", "title_score": 1.0}}

        mock_sp.current_user.return_value = {"id": "user1"}
        mock_sp.user_playlist_create.return_value = {"id": "spotify_pl_1"}

        ps.sync_playlists([pl], pool)

        mock_sp.user_playlist_create.assert_called_once_with("user1", "My Playlist")
        mock_sp.playlist_add_items.assert_called_once_with("spotify_pl_1", ["spotify:track:sp1"])

        mapping = read_json(ps.MAPPING_FILE)
        assert "1" in mapping
        assert mapping["1"]["spotify_playlist_id"] == "spotify_pl_1"
        assert "100" in mapping["1"]["last_synced_track_ids"]

    @patch.object(ps, "sp")
    def test_existing_playlist_adds_only_new_tracks(self, mock_sp, tmp_path):
        self._setup_paths(tmp_path)
        write_json(ps.MAPPING_FILE, {
            "1": {
                "yandex_name": "My Playlist",
                "spotify_playlist_id": "spotify_pl_1",
                "last_synced_track_ids": ["100"],
            }
        })
        pl = make_playlist("1", "My Playlist", [
            ("Song A", "Artist", "100"),
            ("Song B", "Artist", "200"),
        ])
        pool = {
            "100": {"spotify_id": "sp1", "spotify_uri": "spotify:track:sp1", "title_score": 1.0},
            "200": {"spotify_id": "sp2", "spotify_uri": "spotify:track:sp2", "title_score": 0.9},
        }

        ps.sync_playlists([pl], pool)

        # Only the new track should be added
        mock_sp.playlist_add_items.assert_called_once_with("spotify_pl_1", ["spotify:track:sp2"])
        mock_sp.user_playlist_create.assert_not_called()

    @patch.object(ps, "sp")
    def test_up_to_date_playlist_no_api_calls(self, mock_sp, tmp_path):
        self._setup_paths(tmp_path)
        write_json(ps.MAPPING_FILE, {
            "1": {
                "yandex_name": "My Playlist",
                "spotify_playlist_id": "spotify_pl_1",
                "last_synced_track_ids": ["100"],
            }
        })
        pl = make_playlist("1", "My Playlist", [("Song", "Artist", "100")])
        pool = {"100": {"spotify_id": "sp1", "spotify_uri": "spotify:track:sp1", "title_score": 1.0}}

        ps.sync_playlists([pl], pool)

        mock_sp.playlist_add_items.assert_not_called()
        mock_sp.user_playlist_create.assert_not_called()

    @patch.object(ps, "sp")
    def test_unmatched_tracks_skipped(self, mock_sp, tmp_path):
        self._setup_paths(tmp_path)
        pl = make_playlist("1", "My Playlist", [
            ("Song", "Artist", "100"),
            ("Unknown", "Nobody", "200"),
        ])
        pool = {
            "100": {"spotify_id": "sp1", "spotify_uri": "spotify:track:sp1", "title_score": 1.0},
            "200": None,  # unmatched
        }

        mock_sp.current_user.return_value = {"id": "user1"}
        mock_sp.user_playlist_create.return_value = {"id": "spotify_pl_1"}

        ps.sync_playlists([pl], pool)

        # Only the matched track should be added
        mock_sp.playlist_add_items.assert_called_once_with("spotify_pl_1", ["spotify:track:sp1"])

    @patch.object(ps, "sp")
    def test_empty_playlist_skipped(self, mock_sp, tmp_path):
        self._setup_paths(tmp_path)
        pl = make_playlist("1", "Empty", [])

        ps.sync_playlists([pl], {})

        mock_sp.user_playlist_create.assert_not_called()
        mock_sp.playlist_add_items.assert_not_called()


# ---------------------------------------------------------------------------
# like_playlist_tracks_in_likes()
# ---------------------------------------------------------------------------

class TestCrossLike:
    def setup_method(self):
        self._orig_data_dir = ps.DATA_DIR
        self._orig_found = ps.FOUND_FILE
        self._orig_likes = ps.YANDEX_LIKES_FILE

    def teardown_method(self):
        ps.DATA_DIR = self._orig_data_dir
        ps.FOUND_FILE = self._orig_found
        ps.YANDEX_LIKES_FILE = self._orig_likes

    def _setup_paths(self, tmp_path):
        ps.DATA_DIR = str(tmp_path)
        ps.FOUND_FILE = str(tmp_path / "found.json")
        ps.YANDEX_LIKES_FILE = str(tmp_path / "likes.json")

    @patch.object(ps, "like_tracks")
    def test_likes_track_in_both_pool_and_yandex_likes(self, mock_like, tmp_path):
        self._setup_paths(tmp_path)
        write_json(ps.FOUND_FILE, [])
        write_json(ps.YANDEX_LIKES_FILE, [{"title": "Song", "artists": "Artist", "id": "100"}])

        pool = {"100": {"spotify_id": "sp1", "spotify_uri": "spotify:track:sp1", "title_score": 1.0}}
        ps.like_playlist_tracks_in_likes(pool)

        mock_like.assert_called_once_with(["sp1"])
        found = read_json(ps.FOUND_FILE)
        assert len(found) == 1
        assert found[0]["source"] == "playlist_crosslike"

    @patch.object(ps, "like_tracks")
    def test_skips_already_in_found(self, mock_like, tmp_path):
        self._setup_paths(tmp_path)
        write_json(ps.FOUND_FILE, [{"yandex_id": "100", "spotify_id": "sp1"}])
        write_json(ps.YANDEX_LIKES_FILE, [{"title": "Song", "artists": "Artist", "id": "100"}])

        pool = {"100": {"spotify_id": "sp1", "spotify_uri": "spotify:track:sp1", "title_score": 1.0}}
        ps.like_playlist_tracks_in_likes(pool)

        mock_like.assert_not_called()

    @patch.object(ps, "like_tracks")
    def test_skips_track_not_in_yandex_likes(self, mock_like, tmp_path):
        self._setup_paths(tmp_path)
        write_json(ps.FOUND_FILE, [])
        write_json(ps.YANDEX_LIKES_FILE, [])  # track 100 not in likes

        pool = {"100": {"spotify_id": "sp1", "spotify_uri": "spotify:track:sp1", "title_score": 1.0}}
        ps.like_playlist_tracks_in_likes(pool)

        mock_like.assert_not_called()

    @patch.object(ps, "like_tracks")
    def test_skips_unmatched_pool_entries(self, mock_like, tmp_path):
        self._setup_paths(tmp_path)
        write_json(ps.FOUND_FILE, [])
        write_json(ps.YANDEX_LIKES_FILE, [{"title": "Song", "artists": "Artist", "id": "100"}])

        pool = {"100": None}  # unmatched
        ps.like_playlist_tracks_in_likes(pool)

        mock_like.assert_not_called()


# ---------------------------------------------------------------------------
# cmd_stats()
# ---------------------------------------------------------------------------

class TestCmdStats:
    def setup_method(self):
        self._orig_data_dir = ps.DATA_DIR
        self._orig_playlists = ps.YANDEX_PLAYLISTS_FILE
        self._orig_pool = ps.POOL_FILE
        self._orig_mapping = ps.MAPPING_FILE

    def teardown_method(self):
        ps.DATA_DIR = self._orig_data_dir
        ps.YANDEX_PLAYLISTS_FILE = self._orig_playlists
        ps.POOL_FILE = self._orig_pool
        ps.MAPPING_FILE = self._orig_mapping

    def test_shows_stats(self, tmp_path):
        ps.DATA_DIR = str(tmp_path)
        ps.YANDEX_PLAYLISTS_FILE = str(tmp_path / "playlists.json")
        ps.POOL_FILE = str(tmp_path / "pool.json")
        ps.MAPPING_FILE = str(tmp_path / "mapping.json")

        pl = make_playlist("1", "P1", [("Song", "Artist", "100")])
        write_json(ps.YANDEX_PLAYLISTS_FILE, [pl])
        write_json(ps.POOL_FILE, {"100": {"spotify_id": "sp1", "spotify_uri": "u", "title_score": 1.0}})
        write_json(ps.MAPPING_FILE, {"1": {"yandex_name": "P1", "spotify_playlist_id": "spl1", "last_synced_track_ids": ["100"]}})

        # Should not raise
        ps.cmd_stats()
