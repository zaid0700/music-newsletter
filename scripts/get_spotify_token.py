"""
One-time Spotify login. Run this ONCE on your own computer to get the
SPOTIFY_REFRESH_TOKEN secret. It only asks for permission to read your playlists.

  1. In your app at https://developer.spotify.com/dashboard → Settings, add this
     Redirect URI exactly:  http://127.0.0.1:8888/callback
  2. Run:  python3 scripts/get_spotify_token.py
  3. Paste your Client ID and Client Secret when asked.
  4. Your browser opens; log in to Spotify and click Agree.
  5. Copy the refresh token it prints into a GitHub secret named SPOTIFY_REFRESH_TOKEN.

Uses only Python's standard library, so there's nothing to install.
"""

import base64
import json
import secrets
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 8888
REDIRECT_URI = f"http://127.0.0.1:{PORT}/callback"
SCOPES = "playlist-read-private playlist-read-collaborative"


def main() -> None:
    client_id = input("Client ID: ").strip()
    client_secret = input("Client Secret: ").strip()
    state = secrets.token_urlsafe(16)
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if query.get("state", [""])[0] == state:
                result.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>Done. You can close this tab and go back to the terminal.</h2>")

        def log_message(self, *args):
            pass

    auth_url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
    })
    print("\nOpening your browser to log in to Spotify...")
    print(f"If it doesn't open, paste this link into your browser:\n{auth_url}\n")
    webbrowser.open(auth_url)

    server = HTTPServer(("127.0.0.1", PORT), Handler)
    while "code" not in result and "error" not in result:
        server.handle_request()
    server.server_close()

    if "error" in result:
        raise SystemExit(f"Spotify said: {result['error']}")

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=urllib.parse.urlencode({
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": REDIRECT_URI,
        }).encode(),
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req) as resp:
        tokens = json.load(resp)

    print("\nSuccess! Add this as a GitHub secret named SPOTIFY_REFRESH_TOKEN:\n")
    print(tokens["refresh_token"])
    print("\nKeep it private: it lets the newsletter read your playlists.")


if __name__ == "__main__":
    main()
