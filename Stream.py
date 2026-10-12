import requests
from urllib.parse import quote

# Shared user-agent across all modules to keep API expectations consistent.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) StreamlabsDesktop/1.20.4 Chrome/122.0.6261.156 "
    "Electron/29.3.1 Safari/537.36"
)

REQUEST_TIMEOUT = 15  # seconds — never let a request hang the GUI indefinitely.


class Stream:
    def __init__(self, token):
        self.s = requests.Session()
        self.s.headers.update({
            "user-agent": USER_AGENT,
            "authorization": f"Bearer {token}"
        })
        # Stream id of the currently active stream (None until start() succeeds).
        self.id = None

    def _get_json(self, response):
        """Validate HTTP status before parsing JSON."""
        try:
            response.raise_for_status()
        except requests.HTTPError as e:
            raise RuntimeError(f"API request failed (HTTP {response.status_code}): {e}") from e
        try:
            return response.json()
        except ValueError as e:
            raise RuntimeError("API returned a non-JSON response.") from e

    def search(self, game):
        if not game:
            return []
        # If the game name exceeds 25 characters, the API returns error 500.
        query = game[:25]
        url = f"https://streamlabs.com/api/v5/slobs/tiktok/info?category={quote(query)}"
        info = self._get_json(self.s.get(url, timeout=REQUEST_TIMEOUT))
        categories = info.get("categories") or []
        categories.append({"full_name": "Other", "game_mask_id": ""})
        return categories

    def start(self, title, category, audience_type='0'):
        url = "https://streamlabs.com/api/v5/slobs/tiktok/stream/start"
        payload = {
            "title": title,
            "device_platform": "win32",
            "category": category,
            "audience_type": audience_type,
        }
        response = self._get_json(self.s.post(url, data=payload, timeout=REQUEST_TIMEOUT))
        try:
            self.id = response["id"]
            return response["rtmp"], response["key"]
        except KeyError:
            print("Error: ", response)
            self.id = None
            return None, None

    def end(self):
        if not self.id:
            raise RuntimeError("No active stream to end.")
        url = f"https://streamlabs.com/api/v5/slobs/tiktok/stream/{self.id}/end"
        response = self._get_json(self.s.post(url, timeout=REQUEST_TIMEOUT))
        result = response.get("success", False)
        if result:
            self.id = None
        return result

    def getInfo(self):
        url = "https://streamlabs.com/api/v5/slobs/tiktok/info"
        return self._get_json(self.s.get(url, timeout=REQUEST_TIMEOUT))