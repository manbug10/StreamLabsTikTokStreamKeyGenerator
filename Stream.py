import requests


class Stream:
    def __init__(self, token):
        self.s = requests.session()
        self.id = None
        self.s.headers.update({
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) StreamlabsDesktop/1.17.0 Chrome/122.0.6261.156 Electron/29.3.1 Safari/537.36",
            "authorization": f"Bearer {token}"
        })

    def search(self, game):
        if not game:
            return []
        game = game[:25]  # If the game name exceeds 25 characters, the API will return error 500
        url = "https://streamlabs.com/api/v5/slobs/tiktok/info"
        response = self.s.get(url, params={"category": game}, timeout=15)
        response.raise_for_status()
        info = response.json()
        categories = info.get("categories")
        if not isinstance(categories, list):
            raise ValueError(f"Unexpected API response while searching categories: {info}")
        categories.append({"full_name": "Other", "game_mask_id": ""})
        return categories

    def start(self, title, category, audience_type='0'):
        url = "https://streamlabs.com/api/v5/slobs/tiktok/stream/start"
        # NOTE: "win32" is intentional — it is the value the Streamlabs Desktop
        # client sends and the only one proven to work on every OS build.
        files = (
            ('title', (None, title)),
            ('device_platform', (None, 'win32')),
            ('category', (None, category)),
            ('audience_type', (None, audience_type)),
        )
        response = self.s.post(url, files=files, timeout=30)
        response.raise_for_status()
        data = response.json()
        self.id = data.get("id")
        return data.get("rtmp"), data.get("key")

    def end(self):
        if not self.id:
            # end() was called before a successful start() or the Stream
            # object was recreated — there is nothing to end.
            return False
        url = f"https://streamlabs.com/api/v5/slobs/tiktok/stream/{self.id}/end"
        response = self.s.post(url, timeout=15)
        response.raise_for_status()
        return bool(response.json().get("success", False))

    def getInfo(self):
        url = "https://streamlabs.com/api/v5/slobs/tiktok/info"
        response = self.s.get(url, timeout=15)
        response.raise_for_status()
        return response.json()
