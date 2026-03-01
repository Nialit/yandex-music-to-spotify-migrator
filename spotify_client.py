"""Shared Spotify OAuth client setup.

Both spotify_crossref.py and playlist_sync.py import from here.
"""

import os

import requests as _requests
import spotipy
from spotipy.oauth2 import SpotifyOAuth

from config import CLIENT_ID, CLIENT_SECRET, REDIRECT_URI

DIR = os.path.dirname(os.path.abspath(__file__))

BASE_SCOPES = "user-library-modify user-library-read"
PLAYLIST_SCOPES = "playlist-modify-public playlist-modify-private playlist-read-private"


def create_client(extra_scopes=None):
    """Create and return a configured spotipy.Spotify instance.

    Args:
        extra_scopes: Additional OAuth scopes (space-separated string).
                      Pass PLAYLIST_SCOPES for playlist operations.
    """
    scopes = BASE_SCOPES
    if extra_scopes:
        scopes = f"{scopes} {extra_scopes}"

    session = _requests.Session()
    session.mount("https://", _requests.adapters.HTTPAdapter(max_retries=0))

    return spotipy.Spotify(
        auth_manager=SpotifyOAuth(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            redirect_uri=REDIRECT_URI,
            scope=scopes,
            cache_path=f"{DIR}/.spotify_token_cache",
        ),
        requests_session=session,
    )
