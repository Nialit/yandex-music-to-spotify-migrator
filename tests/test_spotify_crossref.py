"""Tests for spotify_crossref.py — mocks all Spotify API calls and file I/O."""

import json
import os
import sys
import time
from unittest.mock import MagicMock, patch, call

# Add parent dir to path so we can import the modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import spotipy.exceptions

# Patch config and spotipy BEFORE importing the modules
with patch.dict("sys.modules", {"config": MagicMock(CLIENT_ID="x", CLIENT_SECRET="y", REDIRECT_URI="http://localhost")}):
    with patch("spotipy.Spotify"):
        with patch("spotipy.oauth2.SpotifyOAuth"):
            import matching
            import spotify_crossref as sc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_spotify_item(track_id, name, artists=None):
    """Create a fake Spotify track item dict."""
    if artists is None:
        artists = ["Artist"]
    return {
        "id": track_id,
        "uri": f"spotify:track:{track_id}",
        "name": name,
        "artists": [{"name": a} for a in artists],
    }


def write_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


def read_json(path):
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 1. normalize()
# ---------------------------------------------------------------------------

class TestNormalize:
    def test_lowercase_and_strip(self):
        assert matching.normalize("  Hello World  ") == "hello world"

    def test_removes_punctuation(self):
        assert matching.normalize("rock'n'roll!") == "rocknroll"

    def test_collapses_spaces(self):
        assert matching.normalize("a   b    c") == "a b c"

    def test_unicode_nfkd(self):
        # NFKD decomposes e.g. ligatures: ﬁ → fi
        assert matching.normalize("ﬁne") == "fine"

    def test_empty_string(self):
        assert matching.normalize("") == ""


# ---------------------------------------------------------------------------
# 2. similarity()
# ---------------------------------------------------------------------------

class TestSimilarity:
    def test_identical(self):
        assert matching.similarity("Yesterday", "Yesterday") == 1.0

    def test_completely_different(self):
        assert matching.similarity("abc", "xyz") < 0.3

    def test_truncation_remastered(self):
        score = matching.similarity("Yesterday", "Yesterday - Remastered 2009")
        assert score == 1.0

    def test_truncation_prefix(self):
        score = matching.similarity("Hello", "Hello World Extended Mix")
        assert score == 1.0

    def test_no_truncation_same_length(self):
        score = matching.similarity("abcde", "abcdf")
        assert score < 1.0

    def test_empty_string(self):
        score = matching.similarity("", "something")
        assert score == 0.0

    def test_both_empty(self):
        score = matching.similarity("", "")
        assert score == 1.0


# ---------------------------------------------------------------------------
# 3. is_cyrillic() + transliterate_text()
# ---------------------------------------------------------------------------

class TestCyrillic:
    def test_latin_not_cyrillic(self):
        assert matching.is_cyrillic("Hello World") is False

    def test_cyrillic_detected(self):
        assert matching.is_cyrillic("Привет") is True

    def test_mixed_text(self):
        assert matching.is_cyrillic("Hello Привет") is True

    def test_empty_string(self):
        assert matching.is_cyrillic("") is False


class TestTransliterate:
    @pytest.mark.skipif(not matching.HAS_TRANSLIT, reason="transliterate not installed")
    def test_cyrillic_transliterated(self):
        from transliterate import translit
        result = translit("Привет", "ru", reversed=True)
        assert result is not None
        assert matching.is_cyrillic(result) is False

    @pytest.mark.skipif(not matching.HAS_TRANSLIT, reason="transliterate not installed")
    def test_transliterate_text_with_cyrillic(self):
        result = matching.transliterate_text("Тест")
        if result is not None:
            assert matching.is_cyrillic(result) is False

    def test_latin_returns_none(self):
        assert matching.transliterate_text("Hello") is None

    def test_empty_returns_none(self):
        assert matching.transliterate_text("") is None


# ---------------------------------------------------------------------------
# 4. score_items()
# ---------------------------------------------------------------------------

class TestScoreItems:
    def test_scores_latin_title(self):
        items = [make_spotify_item("1", "Yesterday")]
        scored = matching.score_items(items, "Yesterday")
        assert len(scored) == 1
        assert scored[0]["title_score"] == 1.0
        assert scored[0]["spotify_id"] == "1"

    @pytest.mark.skipif(not matching.HAS_TRANSLIT, reason="transliterate not installed")
    def test_scores_cyrillic_title(self):
        with patch.object(matching, "transliterate_text", return_value="Privet"):
            items = [make_spotify_item("1", "Privet")]
            scored = matching.score_items(items, "Привет")
            assert len(scored) == 1
            assert scored[0]["title_score"] == 1.0

    def test_empty_items(self):
        assert matching.score_items([], "anything") == []

    def test_includes_artist_info(self):
        items = [make_spotify_item("1", "Song", ["Artist A", "Artist B"])]
        scored = matching.score_items(items, "Song")
        assert scored[0]["spotify_artists"] == "Artist A, Artist B"

    def test_max_of_original_and_translit(self):
        items = [make_spotify_item("1", "Song Title")]
        scored = matching.score_items(items, "Song Title")
        assert scored[0]["title_score"] == 1.0


# ---------------------------------------------------------------------------
# 5. search_track()
# ---------------------------------------------------------------------------

class TestSearchTrack:
    @patch.object(matching, "spotify_search", return_value=[])
    @patch.object(matching, "DELAY_BETWEEN_REQUESTS", 0)
    def test_no_results(self, mock_search):
        best, candidates = matching.search_track(MagicMock(), "NonExistent", "Nobody")
        assert best is None
        assert candidates == []

    @patch.object(matching, "DELAY_BETWEEN_REQUESTS", 0)
    @patch.object(matching, "spotify_search")
    def test_results_below_threshold(self, mock_search):
        mock_search.return_value = [make_spotify_item("1", "Totally Different Song")]
        best, candidates = matching.search_track(MagicMock(), "My Song", "Artist")
        assert best is not None  # still returns best
        assert len(candidates) >= 1

    @patch.object(matching, "DELAY_BETWEEN_REQUESTS", 0)
    @patch.object(matching, "spotify_search")
    def test_results_above_threshold(self, mock_search):
        mock_search.return_value = [make_spotify_item("1", "Yesterday")]
        best, candidates = matching.search_track(MagicMock(), "Yesterday", "Beatles")
        assert best is not None
        assert best["title_score"] == 1.0

    @patch.object(matching, "DELAY_BETWEEN_REQUESTS", 0)
    @patch.object(matching, "spotify_search")
    def test_deduplication_keeps_highest_score(self, mock_search):
        mock_search.return_value = [
            make_spotify_item("1", "Song"),
            make_spotify_item("1", "Song - Remix"),
        ]
        best, candidates = matching.search_track(MagicMock(), "Song", "Artist")
        ids = [c["spotify_id"] for c in candidates]
        assert ids.count("1") == 1
        assert candidates[0]["title_score"] == 1.0

    @patch.object(matching, "DELAY_BETWEEN_REQUESTS", 0)
    @patch.object(matching, "spotify_search")
    def test_limited_to_candidates_to_store(self, mock_search):
        items = [make_spotify_item(str(i), f"Song {i}") for i in range(10)]
        mock_search.return_value = items
        _, candidates = matching.search_track(MagicMock(), "Song 0", "Artist")
        assert len(candidates) <= matching.CANDIDATES_TO_STORE


# ---------------------------------------------------------------------------
# 6. flush_pending()
# ---------------------------------------------------------------------------

class TestFlushPending:
    def setup_method(self):
        self._orig_dir = sc.DIR
        self._orig_data_dir = sc.DATA_DIR
        self._orig_found = sc.FOUND_FILE
        self._orig_not_found = sc.NOT_FOUND_FILE
        self._orig_pending = sc.PENDING_FILE

    def teardown_method(self):
        sc.DIR = self._orig_dir
        sc.DATA_DIR = self._orig_data_dir
        sc.FOUND_FILE = self._orig_found
        sc.NOT_FOUND_FILE = self._orig_not_found
        sc.PENDING_FILE = self._orig_pending

    def _setup_paths(self, tmp_path):
        sc.DIR = str(tmp_path)
        sc.DATA_DIR = str(tmp_path)
        sc.FOUND_FILE = str(tmp_path / "found.json")
        sc.NOT_FOUND_FILE = str(tmp_path / "not_found.json")
        sc.PENDING_FILE = str(tmp_path / "pending.json")

    @patch.object(sc, "like_tracks")
    def test_no_pending_file(self, mock_like, tmp_path):
        self._setup_paths(tmp_path)
        found, num = sc.flush_pending([])
        assert num == 0
        assert found == []
        mock_like.assert_not_called()

    @patch.object(sc, "like_tracks")
    @patch.object(sc, "DELAY_AFTER_LIKE", 0)
    def test_success_likes_all(self, mock_like, tmp_path):
        self._setup_paths(tmp_path)
        pending = [
            {"spotify_id": "a", "yandex_id": "1", "spotify_uri": "spotify:track:a"},
            {"spotify_id": "b", "yandex_id": "2", "spotify_uri": "spotify:track:b"},
        ]
        write_json(sc.PENDING_FILE, pending)
        found, num = sc.flush_pending([])
        assert num == 2
        assert len(found) == 2
        mock_like.assert_called_once_with(["a", "b"])
        assert not os.path.exists(sc.PENDING_FILE)

    @patch.object(sc, "like_tracks")
    @patch.object(sc, "DELAY_AFTER_LIKE", 0)
    @patch("time.sleep")
    def test_429_then_retry_succeeds(self, mock_sleep, mock_like, tmp_path):
        self._setup_paths(tmp_path)
        pending = [{"spotify_id": "a", "yandex_id": "1", "spotify_uri": "spotify:track:a"}]
        write_json(sc.PENDING_FILE, pending)

        mock_like.side_effect = [
            spotipy.exceptions.SpotifyException(429, -1, "rate limited", headers={"Retry-After": "1"}),
            None,  # retry succeeds
        ]
        found, num = sc.flush_pending([])
        assert num == 1
        assert len(found) == 1

    @patch.object(sc, "like_tracks")
    @patch.object(sc, "DELAY_AFTER_LIKE", 0)
    @patch("time.sleep")
    def test_429_then_retry_fails(self, mock_sleep, mock_like, tmp_path):
        self._setup_paths(tmp_path)
        pending = [{"spotify_id": "a", "yandex_id": "1", "spotify_uri": "spotify:track:a"}]
        write_json(sc.PENDING_FILE, pending)

        mock_like.side_effect = [
            spotipy.exceptions.SpotifyException(429, -1, "rate limited", headers={"Retry-After": "1"}),
            Exception("still failing"),
        ]
        found, num = sc.flush_pending([])
        assert num == 0
        # Pending saved to disk
        assert os.path.exists(sc.PENDING_FILE)

    @patch.object(sc, "like_tracks")
    @patch.object(sc, "DELAY_AFTER_LIKE", 0)
    def test_403_saves_remaining(self, mock_like, tmp_path):
        self._setup_paths(tmp_path)
        pending = [{"spotify_id": "a", "yandex_id": "1", "spotify_uri": "spotify:track:a"}]
        write_json(sc.PENDING_FILE, pending)

        mock_like.side_effect = spotipy.exceptions.SpotifyException(403, -1, "forbidden", headers={})
        found, num = sc.flush_pending([])
        assert num == 0
        assert os.path.exists(sc.PENDING_FILE)

    @patch.object(sc, "like_tracks")
    @patch.object(sc, "DELAY_AFTER_LIKE", 0)
    def test_batching_over_limit(self, mock_like, tmp_path):
        self._setup_paths(tmp_path)
        batch_size = sc.LIKE_BATCH_SIZE  # 40 per API limit
        total = batch_size + 15
        pending = [{"spotify_id": str(i), "yandex_id": str(i), "spotify_uri": f"spotify:track:{i}"} for i in range(total)]
        write_json(sc.PENDING_FILE, pending)

        found, num = sc.flush_pending([])
        assert num == total
        assert mock_like.call_count == 2
        assert len(mock_like.call_args_list[0][0][0]) == batch_size
        assert len(mock_like.call_args_list[1][0][0]) == 15


# ---------------------------------------------------------------------------
# 7. cmd_migrate()
# ---------------------------------------------------------------------------

class TestCmdMigrate:
    def setup_method(self):
        self._orig_dir = sc.DIR
        self._orig_data_dir = sc.DATA_DIR
        self._orig_found = sc.FOUND_FILE
        self._orig_not_found = sc.NOT_FOUND_FILE
        self._orig_pending = sc.PENDING_FILE

    def teardown_method(self):
        sc.DIR = self._orig_dir
        sc.DATA_DIR = self._orig_data_dir
        sc.FOUND_FILE = self._orig_found
        sc.NOT_FOUND_FILE = self._orig_not_found
        sc.PENDING_FILE = self._orig_pending

    def _setup_paths(self, tmp_path):
        sc.DIR = str(tmp_path)
        sc.DATA_DIR = str(tmp_path)
        sc.FOUND_FILE = str(tmp_path / "found.json")
        sc.NOT_FOUND_FILE = str(tmp_path / "not_found.json")
        sc.PENDING_FILE = str(tmp_path / "pending.json")

    def _write_yandex(self, tmp_path, tracks):
        write_json(str(tmp_path / "yandex_music_likes.json"), tracks)

    @patch.object(sc, "fetch_liked_songs", return_value=[])
    @patch.object(sc, "flush_pending", return_value=([], 0))
    @patch.object(sc, "search_track")
    @patch.object(sc, "DELAY_BETWEEN_REQUESTS", 0)
    @patch.object(sc, "DELAY_BETWEEN_BATCHES", 0)
    def test_test_mode_limits_to_10(self, mock_search, mock_flush, mock_fetch, tmp_path):
        self._setup_paths(tmp_path)
        tracks = [{"title": f"Song {i}", "artists": f"Artist {i}", "id": str(i)} for i in range(20)]
        self._write_yandex(tmp_path, tracks)

        mock_search.return_value = (None, [])
        # flush_pending called initially (returns ([], 0)), then at end (with pending)
        mock_flush.side_effect = lambda found: (found, 0)

        sc.cmd_migrate(test_mode=True)
        assert mock_search.call_count == 10

    @patch.object(sc, "fetch_liked_songs", return_value=[])
    @patch.object(sc, "flush_pending")
    @patch.object(sc, "search_track")
    @patch.object(sc, "DELAY_BETWEEN_REQUESTS", 0)
    def test_matched_track_goes_to_pending(self, mock_search, mock_flush, mock_fetch, tmp_path):
        self._setup_paths(tmp_path)
        tracks = [{"title": "Yesterday", "artists": "Beatles", "id": "1"}]
        self._write_yandex(tmp_path, tracks)

        best = {"spotify_id": "sp1", "spotify_uri": "spotify:track:sp1",
                "spotify_name": "Yesterday", "spotify_artists": "Beatles", "title_score": 1.0}
        mock_search.return_value = (best, [best])

        # Simulate successful liking: read pending from disk, extend found, clear file.
        def fake_flush(found):
            pending = sc.load_json(sc.PENDING_FILE, [])
            if not pending:
                return found, 0
            found = found + pending
            sc.clear_pending()
            return found, len(pending)

        mock_flush.side_effect = fake_flush

        sc.cmd_migrate(test_mode=True)
        # flush_pending called from inner flush() at end
        assert mock_flush.call_count >= 1

    @patch.object(sc, "fetch_liked_songs", return_value=[])
    @patch.object(sc, "flush_pending", return_value=([], 0))
    @patch.object(sc, "search_track")
    @patch.object(sc, "DELAY_BETWEEN_REQUESTS", 0)
    def test_unmatched_goes_to_not_found(self, mock_search, mock_flush, mock_fetch, tmp_path):
        self._setup_paths(tmp_path)
        tracks = [{"title": "Unknown Song", "artists": "Nobody", "id": "1"}]
        self._write_yandex(tmp_path, tracks)

        mock_search.return_value = (None, [])
        mock_flush.side_effect = lambda found: (found, 0)

        sc.cmd_migrate(test_mode=True)
        not_found = read_json(sc.NOT_FOUND_FILE)
        assert len(not_found) == 1
        assert not_found[0]["yandex_id"] == "1"
        assert not_found[0]["reason"] == "no_results"

    @patch.object(sc, "fetch_liked_songs", return_value=[])
    @patch.object(sc, "flush_pending")
    @patch.object(sc, "search_track")
    @patch.object(sc, "DELAY_BETWEEN_REQUESTS", 0)
    def test_skips_already_processed(self, mock_search, mock_flush, mock_fetch, tmp_path):
        self._setup_paths(tmp_path)
        tracks = [{"title": "Song", "artists": "Artist", "id": "1"}]
        self._write_yandex(tmp_path, tracks)
        write_json(sc.FOUND_FILE, [{"yandex_id": "1"}])

        mock_flush.return_value = ([{"yandex_id": "1"}], 0)

        sc.cmd_migrate(test_mode=True)
        mock_search.assert_not_called()


# ---------------------------------------------------------------------------
# 8. cmd_resolve()
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# first_artist() + update_artist_met_status()
# ---------------------------------------------------------------------------

class TestFirstArtist:
    def test_single_artist(self):
        assert matching.first_artist("Beatles") == "Beatles"

    def test_multiple_artists(self):
        assert matching.first_artist("Beatles, Wings, Solo") == "Beatles"

    def test_strips_whitespace(self):
        assert matching.first_artist("  Beatles , Wings") == "Beatles"

    def test_empty_string(self):
        assert matching.first_artist("") == ""


class TestUpdateArtistMetStatus:
    def test_artist_found_in_found_list(self):
        found = [{"yandex_artists": "Beatles, Wings"}]
        not_found = [{"yandex_artists": "Beatles", "yandex_title": "X"}]
        not_found, changed = sc.update_artist_met_status(found, not_found)
        assert changed is True
        assert not_found[0]["artist_met_on_spotify"] is True

    def test_artist_not_in_found_list(self):
        found = [{"yandex_artists": "Beatles"}]
        not_found = [{"yandex_artists": "Fedorov", "yandex_title": "X"}]
        not_found, changed = sc.update_artist_met_status(found, not_found)
        assert changed is True
        assert not_found[0]["artist_met_on_spotify"] is False

    def test_no_change_when_already_set(self):
        found = [{"yandex_artists": "Beatles"}]
        not_found = [{"yandex_artists": "Beatles", "artist_met_on_spotify": True}]
        not_found, changed = sc.update_artist_met_status(found, not_found)
        assert changed is False
        assert not_found[0]["artist_met_on_spotify"] is True

    def test_empty_found(self):
        not_found = [{"yandex_artists": "Beatles"}]
        not_found, changed = sc.update_artist_met_status([], not_found)
        assert changed is True
        assert not_found[0]["artist_met_on_spotify"] is False

    def test_empty_not_found(self):
        found = [{"yandex_artists": "Beatles"}]
        not_found, changed = sc.update_artist_met_status(found, [])
        assert changed is False

    def test_mixed_artists(self):
        found = [{"yandex_artists": "Beatles"}, {"yandex_artists": "Queen"}]
        not_found = [
            {"yandex_artists": "Beatles", "yandex_title": "A"},
            {"yandex_artists": "Fedorov", "yandex_title": "B"},
            {"yandex_artists": "Queen, May", "yandex_title": "C"},
        ]
        not_found, changed = sc.update_artist_met_status(found, not_found)
        assert changed is True
        assert not_found[0]["artist_met_on_spotify"] is True
        assert not_found[1]["artist_met_on_spotify"] is False
        assert not_found[2]["artist_met_on_spotify"] is True


class TestCmdResolve:
    def setup_method(self):
        self._orig_dir = sc.DIR
        self._orig_data_dir = sc.DATA_DIR
        self._orig_found = sc.FOUND_FILE
        self._orig_not_found = sc.NOT_FOUND_FILE
        self._orig_pending = sc.PENDING_FILE

    def teardown_method(self):
        sc.DIR = self._orig_dir
        sc.DATA_DIR = self._orig_data_dir
        sc.FOUND_FILE = self._orig_found
        sc.NOT_FOUND_FILE = self._orig_not_found
        sc.PENDING_FILE = self._orig_pending

    def _setup_paths(self, tmp_path):
        sc.DIR = str(tmp_path)
        sc.DATA_DIR = str(tmp_path)
        sc.FOUND_FILE = str(tmp_path / "found.json")
        sc.NOT_FOUND_FILE = str(tmp_path / "not_found.json")
        sc.PENDING_FILE = str(tmp_path / "pending.json")

    def _make_not_found_entry(self, yandex_id="1"):
        return {
            "yandex_title": "Song",
            "yandex_artists": "Artist",
            "yandex_id": yandex_id,
            "reason": "title_mismatch best=0.50",
            "candidates": [{
                "spotify_id": "sp1",
                "spotify_uri": "spotify:track:sp1",
                "spotify_name": "The Song",
                "spotify_artists": "The Artist",
                "title_score": 0.5,
            }],
        }

    @patch.object(sc, "like_tracks")
    @patch("builtins.input", return_value="0")
    def test_select_candidate_liked(self, mock_input, mock_like, tmp_path):
        self._setup_paths(tmp_path)
        write_json(sc.FOUND_FILE, [])
        write_json(sc.NOT_FOUND_FILE, [self._make_not_found_entry()])

        sc.cmd_resolve()

        mock_like.assert_called_once_with(["sp1"])
        found = read_json(sc.FOUND_FILE)
        assert len(found) == 1
        assert found[0]["manually_resolved"] is True

    @patch("builtins.input", return_value="s")
    def test_skip_unchanged(self, mock_input, tmp_path):
        self._setup_paths(tmp_path)
        entry = self._make_not_found_entry()
        write_json(sc.FOUND_FILE, [])
        write_json(sc.NOT_FOUND_FILE, [entry])

        sc.cmd_resolve()

        not_found = read_json(sc.NOT_FOUND_FILE)
        assert len(not_found) == 1
        assert len(not_found[0]["candidates"]) == 1  # unchanged

    @patch("builtins.input", return_value="n")
    def test_mark_no_match(self, mock_input, tmp_path):
        self._setup_paths(tmp_path)
        write_json(sc.FOUND_FILE, [])
        write_json(sc.NOT_FOUND_FILE, [self._make_not_found_entry()])

        sc.cmd_resolve()

        not_found = read_json(sc.NOT_FOUND_FILE)
        assert len(not_found) == 1
        assert not_found[0]["candidates"] == []

    @patch("builtins.input", return_value="q")
    def test_quit_stops_loop(self, mock_input, tmp_path):
        self._setup_paths(tmp_path)
        entries = [self._make_not_found_entry("1"), self._make_not_found_entry("2")]
        write_json(sc.FOUND_FILE, [])
        write_json(sc.NOT_FOUND_FILE, entries)

        sc.cmd_resolve()
        # input called only once (quit on first entry)
        assert mock_input.call_count == 1

    @patch.object(sc, "like_tracks", side_effect=Exception("API error"))
    @patch("builtins.input", return_value="0")
    def test_like_failure_continues(self, mock_input, mock_like, tmp_path):
        self._setup_paths(tmp_path)
        write_json(sc.FOUND_FILE, [])
        write_json(sc.NOT_FOUND_FILE, [self._make_not_found_entry()])

        sc.cmd_resolve()

        found = read_json(sc.FOUND_FILE)
        assert len(found) == 0  # not added due to error
