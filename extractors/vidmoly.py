import re
from urllib.parse import urljoin, urlparse
from extractors.base import BaseExtractor, ExtractorError

class VidmolyExtractor(BaseExtractor):
    """Vidmoly URL extractor."""

    def __init__(self, request_headers: dict, proxies: list = None):
        super().__init__(request_headers, proxies, extractor_name="vidmoly")

    async def extract(self, url: str, **kwargs) -> dict:
        """Extract Vidmoly URL."""
        parsed = urlparse(url)
        if not parsed.hostname or "vidmoly" not in parsed.hostname:
            raise ExtractorError("VIDMOLY: Invalid domain")

        headers = {
            "User-Agent": self.base_headers["User-Agent"],
            "Referer": url,
            "Sec-Fetch-Dest": "iframe",
        }

        # --- Fetch embed page ---
        resp = await self._make_request(url, headers=headers)
        html = resp.text

        # --- Extract master m3u8 ---
        match = re.search(r'sources:\s*\[{file:"([^"]+)', html)
        if not match:
            raise ExtractorError("VIDMOLY: Stream URL not found")

        master_url = match.group(1)

        if not master_url.startswith("http"):
            master_url = urljoin(url, master_url)

        # --- Validate stream (prevents Stremio timeout) ---
        try:
            await self._make_request(master_url, headers=headers)
        except ExtractorError as e:
            raise ExtractorError(f"VIDMOLY: Stream unavailable or timed out: {e}")

        # Return MASTER playlist, not variant
        # Let MediaFlow Proxy handle variants
        return {
            "destination_url": master_url,
            "request_headers": headers,
            "mediaflow_endpoint": self.mediaflow_endpoint,
        }

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
