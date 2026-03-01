# Copy this file to config.py and fill in your Spotify app credentials.
# Create an app at https://developer.spotify.com/dashboard
# Set Redirect URI to: http://127.0.0.1:8888/callback
#
# Scopes used:
#   - user-library-modify user-library-read (liked songs migration)
#   - playlist-modify-public playlist-modify-private playlist-read-private (playlist sync)
#
# After adding playlist scopes, delete .spotify_token_cache to re-auth.

CLIENT_ID = "your_client_id_here"
CLIENT_SECRET = "your_client_secret_here"
REDIRECT_URI = "http://127.0.0.1:8888/callback"
