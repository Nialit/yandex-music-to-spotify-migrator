"""Tests for yandex_fetch.py — mocks all Yandex Music API calls and file I/O."""

import json
import os
import sys
from unittest.mock import MagicMock, patch, call

# Add parent dir to path so we can import the modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def write_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


def read_json(path):
    with open(path) as f:
        return json.load(f)


# We import yandex_fetch inside tests with yandex_music mocked,
# to avoid requiring the real yandex_music package.

@pytest.fixture
def yf(monkeypatch):
    """Import yandex_fetch with yandex_music mocked."""
    mock_ym = MagicMock()
    monkeypatch.setitem(sys.modules, "yandex_music", mock_ym)
    # Remove cached module so it re-imports with the mock
    if "yandex_fetch" in sys.modules:
        del sys.modules["yandex_fetch"]
    import yandex_fetch
    yield yandex_fetch
    del sys.modules["yandex_fetch"]


# ---------------------------------------------------------------------------
# 1. get_synced_yandex_ids()
# ---------------------------------------------------------------------------

class TestGetSyncedYandexIds:
    def test_both_files_exist(self, yf, tmp_path, monkeypatch):
        monkeypatch.setattr(yf, "FOUND_FILE", str(tmp_path / "found.json"))
        monkeypatch.setattr(yf, "NOT_FOUND_FILE", str(tmp_path / "not_found.json"))
        write_json(str(tmp_path / "found.json"), [{"yandex_id": "1"}, {"yandex_id": "2"}])
        write_json(str(tmp_path / "not_found.json"), [{"yandex_id": "3"}])

        ids = yf.get_synced_yandex_ids()
        assert ids == {"1", "2", "3"}

    def test_files_missing(self, yf, tmp_path, monkeypatch):
        monkeypatch.setattr(yf, "FOUND_FILE", str(tmp_path / "nope1.json"))
        monkeypatch.setattr(yf, "NOT_FOUND_FILE", str(tmp_path / "nope2.json"))

        ids = yf.get_synced_yandex_ids()
        assert ids == set()

    def test_entries_missing_yandex_id(self, yf, tmp_path, monkeypatch):
        monkeypatch.setattr(yf, "FOUND_FILE", str(tmp_path / "found.json"))
        monkeypatch.setattr(yf, "NOT_FOUND_FILE", str(tmp_path / "not_found.json"))
        write_json(str(tmp_path / "found.json"), [{"yandex_id": "1"}, {"other_field": "x"}])
        write_json(str(tmp_path / "not_found.json"), [])

        ids = yf.get_synced_yandex_ids()
        assert ids == {"1"}


# ---------------------------------------------------------------------------
# 2. main() — full sync flow
# ---------------------------------------------------------------------------

class TestMain:
    def _make_client_mock(self, track_ids, track_details):
        """Create a mock Client that returns given track IDs and details."""
        mock_client = MagicMock()
        mock_client.me.account.login = "testuser"
        mock_client.base_url = "https://api.music.yandex.net"

        # users_likes_tracks returns TrackShort-like objects
        likes = [MagicMock(track_id=tid) for tid in track_ids]
        mock_client.users_likes_tracks.return_value = likes

        # _request.post returns raw dicts
        mock_client._request.post.return_value = track_details
        mock_client.init.return_value = mock_client
        return mock_client

    @patch("sys.argv", ["yandex_fetch.py", "--token", "test_token"])
    def test_first_run_fetches_all(self, yf, tmp_path, monkeypatch):
        likes_file = str(tmp_path / "likes.json")
        monkeypatch.setattr(yf, "LIKES_FILE", likes_file)
        monkeypatch.setattr(yf, "FOUND_FILE", str(tmp_path / "found.json"))
        monkeypatch.setattr(yf, "NOT_FOUND_FILE", str(tmp_path / "nf.json"))

        track_details = [
            {"id": 100, "title": "Song A", "artists": [{"name": "Artist A"}]},
            {"id": 200, "title": "Song B", "artists": [{"name": "Artist B"}]},
        ]
        mock_client = self._make_client_mock(["100", "200"], track_details)
        monkeypatch.setattr(yf, "Client", lambda token: mock_client)

        yf.main()

        data = read_json(likes_file)
        assert len(data) == 2
        assert data[0]["title"] == "Song A"
        assert data[0]["id"] == "100"

    @patch("sys.argv", ["yandex_fetch.py", "--token", "test_token"])
    def test_append_only_new_tracks(self, yf, tmp_path, monkeypatch):
        likes_file = str(tmp_path / "likes.json")
        monkeypatch.setattr(yf, "LIKES_FILE", likes_file)
        monkeypatch.setattr(yf, "FOUND_FILE", str(tmp_path / "found.json"))
        monkeypatch.setattr(yf, "NOT_FOUND_FILE", str(tmp_path / "nf.json"))

        # Existing track
        write_json(likes_file, [{"title": "Old Song", "artists": "Old Artist", "id": "100"}])

        track_details = [
            {"id": 200, "title": "New Song", "artists": [{"name": "New Artist"}]},
        ]
        mock_client = self._make_client_mock(["100", "200"], track_details)
        monkeypatch.setattr(yf, "Client", lambda token: mock_client)

        yf.main()

        data = read_json(likes_file)
        assert len(data) == 2
        # New track prepended
        assert data[0]["title"] == "New Song"
        assert data[1]["title"] == "Old Song"

    @patch("sys.argv", ["yandex_fetch.py", "--token", "test_token"])
    def test_no_new_tracks_no_write(self, yf, tmp_path, monkeypatch):
        likes_file = str(tmp_path / "likes.json")
        monkeypatch.setattr(yf, "LIKES_FILE", likes_file)
        monkeypatch.setattr(yf, "FOUND_FILE", str(tmp_path / "found.json"))
        monkeypatch.setattr(yf, "NOT_FOUND_FILE", str(tmp_path / "nf.json"))

        write_json(likes_file, [{"title": "Song", "artists": "Artist", "id": "100"}])
        mtime_before = os.path.getmtime(likes_file)

        mock_client = self._make_client_mock(["100"], [])
        monkeypatch.setattr(yf, "Client", lambda token: mock_client)

        yf.main()

        # File should not have been rewritten
        mtime_after = os.path.getmtime(likes_file)
        assert mtime_before == mtime_after

    @patch("sys.argv", ["yandex_fetch.py", "--token", ""])
    def test_empty_token_exits(self, yf, tmp_path, monkeypatch):
        monkeypatch.setattr(yf, "LIKES_FILE", str(tmp_path / "likes.json"))
        monkeypatch.setattr("builtins.input", lambda _: "")
        with pytest.raises(SystemExit) as exc_info:
            yf.main()
        assert exc_info.value.code == 1

    @patch("sys.argv", ["yandex_fetch.py", "--token", "test_token"])
    def test_composite_track_ids(self, yf, tmp_path, monkeypatch):
        """Track IDs with colons (e.g. '12345:6789') — only part before colon is the real ID."""
        likes_file = str(tmp_path / "likes.json")
        monkeypatch.setattr(yf, "LIKES_FILE", likes_file)
        monkeypatch.setattr(yf, "FOUND_FILE", str(tmp_path / "found.json"))
        monkeypatch.setattr(yf, "NOT_FOUND_FILE", str(tmp_path / "nf.json"))

        track_details = [
            {"id": 100, "title": "Song", "artists": [{"name": "Artist"}]},
        ]
        mock_client = self._make_client_mock(["100:999"], track_details)
        monkeypatch.setattr(yf, "Client", lambda token: mock_client)

        yf.main()

        data = read_json(likes_file)
        assert len(data) == 1
        assert data[0]["id"] == "100"

    @patch("sys.argv", ["yandex_fetch.py", "--token", "test_token"])
    def test_batch_fetching(self, yf, tmp_path, monkeypatch):
        """More than 100 tracks should be fetched in batches."""
        likes_file = str(tmp_path / "likes.json")
        monkeypatch.setattr(yf, "LIKES_FILE", likes_file)
        monkeypatch.setattr(yf, "FOUND_FILE", str(tmp_path / "found.json"))
        monkeypatch.setattr(yf, "NOT_FOUND_FILE", str(tmp_path / "nf.json"))

        track_ids = [str(i) for i in range(150)]

        # Return details in two batches
        def make_details(ids):
            return [{"id": int(tid), "title": f"Song {tid}", "artists": [{"name": f"Artist {tid}"}]} for tid in ids]

        mock_client = MagicMock()
        mock_client.me.account.login = "testuser"
        mock_client.base_url = "https://api.music.yandex.net"
        likes = [MagicMock(track_id=tid) for tid in track_ids]
        mock_client.users_likes_tracks.return_value = likes

        # _request.post called twice (batch of 100, batch of 50)
        mock_client._request.post.side_effect = [
            make_details(track_ids[:100]),
            make_details(track_ids[100:]),
        ]
        mock_client.init.return_value = mock_client
        monkeypatch.setattr(yf, "Client", lambda token: mock_client)

        yf.main()

        data = read_json(likes_file)
        assert len(data) == 150
        assert mock_client._request.post.call_count == 2


# ---------------------------------------------------------------------------
# 3. fetch_playlists()
# ---------------------------------------------------------------------------

class TestFetchPlaylists:
    def _make_playlist_mock(self, kind, title, track_ids):
        """Create a mock playlist with tracks."""
        pl = MagicMock()
        pl.kind = kind
        pl.title = title
        return pl

    def _make_full_playlist_mock(self, track_ids):
        """Create a mock full playlist with track shorts."""
        full = MagicMock()
        full.tracks = [MagicMock(track_id=tid) for tid in track_ids]
        return full

    @patch("sys.argv", ["yandex_fetch.py", "--token", "test_token", "--playlists"])
    def test_fetches_playlists(self, yf, tmp_path, monkeypatch):
        playlists_file = str(tmp_path / "playlists.json")
        monkeypatch.setattr(yf, "PLAYLISTS_FILE", playlists_file)
        monkeypatch.setattr(yf, "LIKES_FILE", str(tmp_path / "likes.json"))
        monkeypatch.setattr(yf, "FOUND_FILE", str(tmp_path / "found.json"))
        monkeypatch.setattr(yf, "NOT_FOUND_FILE", str(tmp_path / "nf.json"))

        mock_client = MagicMock()
        mock_client.me.account.login = "testuser"
        mock_client.me.account.uid = 12345
        mock_client.base_url = "https://api.music.yandex.net"
        mock_client.init.return_value = mock_client

        # Likes (empty)
        mock_client.users_likes_tracks.return_value = []

        # One playlist with two tracks
        pl_mock = self._make_playlist_mock(1, "My Playlist", ["100", "200"])
        mock_client.users_playlists_list.return_value = [pl_mock]

        # Raw API returns playlist with track IDs
        mock_client._request.get.return_value = {
            "tracks": [{"id": 100}, {"id": 200}],
        }

        track_details = [
            {"id": 100, "title": "Song A", "artists": [{"name": "Artist A"}]},
            {"id": 200, "title": "Song B", "artists": [{"name": "Artist B"}]},
        ]
        mock_client._request.post.return_value = track_details

        monkeypatch.setattr(yf, "Client", lambda token: mock_client)

        yf.main()

        data = read_json(playlists_file)
        assert len(data) == 1
        assert data[0]["name"] == "My Playlist"
        assert len(data[0]["tracks"]) == 2

    @patch("sys.argv", ["yandex_fetch.py", "--token", "test_token", "--playlists"])
    def test_incremental_skips_unchanged(self, yf, tmp_path, monkeypatch):
        playlists_file = str(tmp_path / "playlists.json")
        monkeypatch.setattr(yf, "PLAYLISTS_FILE", playlists_file)
        monkeypatch.setattr(yf, "LIKES_FILE", str(tmp_path / "likes.json"))
        monkeypatch.setattr(yf, "FOUND_FILE", str(tmp_path / "found.json"))
        monkeypatch.setattr(yf, "NOT_FOUND_FILE", str(tmp_path / "nf.json"))

        # Pre-existing playlist data
        write_json(playlists_file, [{
            "playlist_id": "1",
            "name": "My Playlist",
            "tracks": [
                {"title": "Song A", "artists": "Artist A", "id": "100"},
                {"title": "Song B", "artists": "Artist B", "id": "200"},
            ],
        }])

        mock_client = MagicMock()
        mock_client.me.account.login = "testuser"
        mock_client.me.account.uid = 12345
        mock_client.base_url = "https://api.music.yandex.net"
        mock_client.init.return_value = mock_client
        mock_client.users_likes_tracks.return_value = []

        pl_mock = self._make_playlist_mock(1, "My Playlist", ["100", "200"])
        mock_client.users_playlists_list.return_value = [pl_mock]

        # Same tracks as before (raw API)
        mock_client._request.get.return_value = {
            "tracks": [{"id": 100}, {"id": 200}],
        }

        monkeypatch.setattr(yf, "Client", lambda token: mock_client)

        yf.main()

        # _request.post should NOT be called for track details (playlist unchanged)
        mock_client._request.post.assert_not_called()

        data = read_json(playlists_file)
        assert len(data) == 1
        assert len(data[0]["tracks"]) == 2
