"""Shared track-matching functions for Yandex→Spotify migration.

Handles normalization, Levenshtein similarity, Cyrillic transliteration,
library indexing, pre-matching, and Spotify search/scoring.
"""

import re
import time
import unicodedata

import spotipy.exceptions

TITLE_MATCH_THRESHOLD = 0.7
CANDIDATES_TO_STORE = 5
DELAY_BETWEEN_REQUESTS = 0

try:
    from transliterate import translit
    HAS_TRANSLIT = True
except ImportError:
    HAS_TRANSLIT = False


def first_artist(artists_str):
    """Extract the first artist name from a comma-separated string."""
    return artists_str.split(",")[0].strip()


def normalize(s):
    s = s.lower().strip()
    s = unicodedata.normalize("NFKD", s)
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s


def _levenshtein(a, b):
    """Compute Levenshtein distance between two strings."""
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(prev[j] + (ca != cb), prev[j + 1] + 1, curr[j] + 1))
        prev = curr
    return prev[-1]


def similarity(a, b):
    na, nb = normalize(a), normalize(b)
    max_len = max(len(na), len(nb))
    if max_len == 0:
        return 1.0
    full = 1 - _levenshtein(na, nb) / max_len
    # Also try truncating the longer string to the shorter's length,
    # so "Yesterday" vs "Yesterday - Remastered 2009" scores 1.0.
    min_len = min(len(na), len(nb))
    if min_len > 0 and max_len > min_len:
        truncated = 1 - _levenshtein(na[:min_len], nb[:min_len]) / min_len
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


def fetch_liked_songs(sp, existing_spotify_ids=None, log=None):
    """Fetch user's Spotify liked songs with pagination.
    If existing_spotify_ids is provided, stops early when >=90% of a page
    already exists (we've reached previously-synced territory)."""
    liked_songs = []
    offset = 0
    limit = 50

    def _log(level, msg):
        if log:
            getattr(log, level)(msg)

    while True:
        try:
            results = sp.current_user_saved_tracks(limit=limit, offset=offset)
        except spotipy.exceptions.SpotifyException as e:
            if e.http_status == 429:
                retry_after = get_retry_after(e)
                _log("warning", f"  Rate limited fetching library, waiting {retry_after}s...")
                time.sleep(retry_after + 5)
                continue
            raise

        items = results.get("items", [])
        if not items:
            break

        page_songs = []
        for item in items:
            track = item["track"]
            if not track:
                continue
            page_songs.append({
                "spotify_id": track["id"],
                "spotify_uri": track["uri"],
                "spotify_name": track["name"],
                "spotify_artists": ", ".join(a["name"] for a in track["artists"]),
            })

        liked_songs.extend(page_songs)

        if len(liked_songs) % 500 < limit:
            _log("info", f"  Fetched {len(liked_songs)} liked songs...")

        # Early-stop: if most of this page is already known, we've reached synced territory
        if existing_spotify_ids and page_songs:
            known = sum(1 for s in page_songs if s["spotify_id"] in existing_spotify_ids)
            if known / len(page_songs) >= 0.9:
                _log("info", f"  Early stop: reached previously synced tracks ({known}/{len(page_songs)} known on this page)")
                break

        if not results.get("next"):
            break
        offset += limit
        time.sleep(DELAY_BETWEEN_REQUESTS)

    _log("info", f"  Fetched {len(liked_songs)} liked songs total.")
    return liked_songs


def build_library_index(liked_songs):
    """Build indexes for fast prematch lookup.

    Returns (title_index, artist_index):
      - title_index: normalized title -> [song, ...] (includes transliterated keys)
      - artist_index: normalized artist -> [song, ...] (includes transliterated keys)

    The title index enables O(1) exact-match lookups for ~96% of tracks.
    The artist index is the fallback for fuzzy similarity matching."""
    title_index = {}
    artist_index = {}

    for song in liked_songs:
        # Title keys: normalized original + transliterated form
        title_keys = set()
        norm_title = normalize(song["spotify_name"])
        title_keys.add(norm_title)
        translit_title = transliterate_text(song["spotify_name"])
        if translit_title:
            title_keys.add(normalize(translit_title))
        for key in title_keys:
            if key not in title_index:
                title_index[key] = []
            title_index[key].append(song)

        # Artist keys: normalized original + transliterated form
        artist_names = [a.strip() for a in song["spotify_artists"].split(",")]
        artist_keys = set()
        for name in artist_names:
            artist_keys.add(normalize(name))
            translit_name = transliterate_text(name)
            if translit_name:
                artist_keys.add(normalize(translit_name))
        for key in artist_keys:
            if key not in artist_index:
                artist_index[key] = []
            artist_index[key].append(song)

    return title_index, artist_index


def _match_entry(t, song, title_score, artist_score):
    """Build a matched prematch entry dict."""
    return {
        "yandex_title": t["title"],
        "yandex_artists": t["artists"],
        "yandex_id": t["id"],
        "spotify_id": song["spotify_id"],
        "spotify_uri": song["spotify_uri"],
        "spotify_name": song["spotify_name"],
        "spotify_artists": song["spotify_artists"],
        "title_score": round(title_score, 3),
        "artist_score": round(artist_score, 3),
        "source": "library_prematch",
    }


def _artist_keys_for_track(t):
    """Return the set of normalized artist lookup keys for a Yandex track."""
    artist = first_artist(t["artists"])
    keys = {normalize(artist)}
    translit_artist = transliterate_text(artist)
    if translit_artist:
        keys.add(normalize(translit_artist))
    return keys


def _artist_similarity(t, song):
    """Compute best artist similarity between a Yandex track and a Spotify song.
    Tries original and transliterated forms of both sides, returns the max."""
    yandex_artist = first_artist(t["artists"])
    yandex_forms = [yandex_artist]
    tr = transliterate_text(yandex_artist)
    if tr:
        yandex_forms.append(tr)

    spotify_artists = [a.strip() for a in song["spotify_artists"].split(",")]
    spotify_forms = list(spotify_artists)
    for a in spotify_artists:
        tr = transliterate_text(a)
        if tr:
            spotify_forms.append(tr)

    best = 0
    for yf in yandex_forms:
        for sf in spotify_forms:
            best = max(best, similarity(yf, sf))
    return best


def _title_similarity(t, song):
    """Compute title similarity between a Yandex track and a Spotify song.
    Tries original and transliterated forms, returns the max."""
    title = t["title"]
    score = similarity(title, song["spotify_name"])
    translit_title = transliterate_text(title)
    if translit_title:
        score = max(score, similarity(translit_title, song["spotify_name"]))
    return score


def _try_title_lookup(t, title_index):
    """Try O(1) exact title match. Returns (song, artist_score) or (None, 0).
    Title score is 1.0 by definition (exact match). Artist score must pass threshold."""
    title_keys = set()
    title_keys.add(normalize(t["title"]))
    translit_title = transliterate_text(t["title"])
    if translit_title:
        title_keys.add(normalize(translit_title))

    best_song = None
    best_artist_score = 0

    for tkey in title_keys:
        for song in title_index.get(tkey, []):
            ascore = _artist_similarity(t, song)
            if ascore >= TITLE_MATCH_THRESHOLD and ascore > best_artist_score:
                best_artist_score = ascore
                best_song = song

    return best_song, best_artist_score


def _try_artist_similarity(t, artist_index):
    """Fallback: find best match by artist bucket + independent scoring.
    Returns (song, title_score, artist_score) or (None, 0, 0).
    Both title and artist scores must be >= threshold."""
    artist_keys = _artist_keys_for_track(t)

    candidates = []
    seen_ids = set()
    for key in artist_keys:
        for song in artist_index.get(key, []):
            if song["spotify_id"] not in seen_ids:
                seen_ids.add(song["spotify_id"])
                candidates.append(song)

    best_match = None
    best_combined = 0
    best_title = 0
    best_artist = 0

    for song in candidates:
        tscore = _title_similarity(t, song)
        ascore = _artist_similarity(t, song)
        combined = min(tscore, ascore)
        if combined >= TITLE_MATCH_THRESHOLD and combined > best_combined:
            best_combined = combined
            best_title = tscore
            best_artist = ascore
            best_match = song

    if best_match:
        return best_match, best_title, best_artist
    return None, 0, 0


def prematch_from_library(yandex_tracks, title_index, artist_index):
    """Match Yandex tracks against the Spotify library.

    Two-phase lookup:
      1. O(1) exact title match via title_index (~96% of matches)
      2. Fuzzy similarity against artist bucket via artist_index (remainder)

    Both phases require min(title_score, artist_score) >= threshold.

    Returns (matched, unmatched) lists."""
    matched = []
    unmatched = []

    for t in yandex_tracks:
        # Phase 1: exact title lookup
        song, artist_score = _try_title_lookup(t, title_index)
        if song:
            matched.append(_match_entry(t, song, 1.0, artist_score))
            continue

        # Phase 2: artist-bucket similarity fallback
        song, title_score, artist_score = _try_artist_similarity(t, artist_index)
        if song:
            matched.append(_match_entry(t, song, title_score, artist_score))
        else:
            unmatched.append(t)

    return matched, unmatched


def spotify_search(sp, query):
    """Execute a Spotify track search, return items."""
    results = sp.search(q=query, type="track", limit=5)
    return results["tracks"]["items"]


def search_track(sp, title, artist):
    """
    Search Spotify for a track by title + artist (single query).
    Scoring handles Cyrillic→Latin transliteration automatically.
    Returns (best_candidate | None, top_candidates_list).
    """
    seen = {}  # spotify_id → candidate, deduplicated keeping highest score

    def run_query(query):
        items = spotify_search(sp, query)
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
