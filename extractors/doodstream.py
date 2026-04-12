import logging
import random
import re
import time
import string
from urllib.parse import urlparse
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from aiohttp_socks import ProxyConnector
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

class ExtractorError(Exception):
    pass

class DoodStreamExtractor:
    """DoodStream URL extractor."""

    def __init__(
        self,
        request_headers: dict,
        proxies: list = None,
    ):
        self.request_headers = request_headers
        self.base_headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        self.session = None
        self.mediaflow_endpoint = "proxy_stream_endpoint"
        self.proxies = proxies or []

    def _get_random_proxy(self):
        return random.choice(self.proxies) if self.proxies else None

    @staticmethod
    def _random_suffix(length: int = 10) -> str:
        alphabet = string.ascii_letters + string.digits
        return "".join(random.choice(alphabet) for _ in range(length))

    @staticmethod
    def _extract_pass_and_token(text: str) -> tuple[str | None, str | None]:
        pass_match = re.search(r"(/pass_md5/[^\"'\s]+)", text)
        token_match = re.search(r"\?token=([^\"'&\s]+)&expiry=", text)
        pass_path = pass_match.group(1) if pass_match else None
        token = token_match.group(1) if token_match else None

        if not token and pass_path:
            path_parts = [part for part in pass_path.split("/") if part]
            if len(path_parts) >= 2 and path_parts[0] == "pass_md5":
                token = path_parts[-1]

        return pass_path, token

    async def _fetch_player_data_via_browser(
        self, url: str
    ) -> tuple[str | None, str | None, str | None, str, str]:
        last_result = (None, None, None, url, "")
        browser_proxy = self._get_random_proxy()

        for attempt in range(1, 4):
            async with async_playwright() as playwright:
                launch_options = {
                    "headless": True,
                    "args": [
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--autoplay-policy=no-user-gesture-required",
                        "--disable-dev-shm-usage",
                    ],
                }
                if browser_proxy:
                    launch_options["proxy"] = {"server": browser_proxy}
                    if attempt == 1:
                        logger.info("DoodStream browser fallback using proxy: %s", browser_proxy)

                browser = await playwright.chromium.launch(**launch_options)
                context = await browser.new_context(
                    user_agent=self.base_headers["user-agent"],
                    locale="en-US",
                    viewport={"width": 1366, "height": 768},
                )
                await context.add_init_script(
                    """
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4] });
                    window.chrome = window.chrome || { runtime: {} };
                    """
                )

                page = await context.new_page()
                pass_path: str | None = None
                token: str | None = None
                pass_body: str | None = None
                captured_media_url: str | None = None
                candidate_urls: set[str] = set()

                async def handle_response(response):
                    nonlocal pass_path, token, pass_body, captured_media_url
                    response_url = response.url
                    if any(marker in response_url for marker in ("cloudatacdn.com", "doodcdn", ".mp4")):
                        captured_media_url = captured_media_url or response_url
                        candidate_urls.add(response_url)
                    if "/pass_md5/" not in response_url:
                        return
                    if not pass_path:
                        parsed = urlparse(response_url)
                        pass_path = parsed.path
                    if not token:
                        token_match = re.search(r"[?&]token=([^&]+)", response_url)
                        if token_match:
                            token = token_match.group(1)
                    if not token and pass_path:
                        path_parts = [part for part in pass_path.split("/") if part]
                        if len(path_parts) >= 2 and path_parts[0] == "pass_md5":
                            token = path_parts[-1]
                    if pass_body is None:
                        try:
                            pass_body = await response.text()
                        except Exception:
                            pass

                async def handle_request(request):
                    request_url = request.url
                    if any(marker in request_url for marker in ("pass_md5", "cloudatacdn.com", "doodcdn", ".mp4")):
                        candidate_urls.add(request_url)

                page.on("request", handle_request)
                page.on("response", handle_response)
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)

                click_selectors = [
                    ".captcha_l",
                    "#video_player",
                    ".videoplayer",
                    "button",
                ]

                async def try_clicks_in_frame(frame):
                    for selector in click_selectors:
                        if pass_path and token:
                            break
                        try:
                            locator = frame.locator(selector).first
                            if await locator.count():
                                await locator.click(timeout=3000)
                                await page.wait_for_timeout(1500)
                        except Exception:
                            continue

                await try_clicks_in_frame(page)

                for frame in page.frames:
                    if frame == page.main_frame:
                        continue
                    frame_url = frame.url or ""
                    if frame_url:
                        logger.info("DoodStream browser fallback inspecting frame: %s", frame_url)
                    await try_clicks_in_frame(frame)

                for _ in range(8):
                    if pass_path and token:
                        break
                    await page.wait_for_timeout(1500)

                html = await page.content()
                final_url = page.url
                if not pass_path or not token:
                    html_pass_path, html_token = self._extract_pass_and_token(html)
                    pass_path = pass_path or html_pass_path
                    token = token or html_token

                if not pass_path and captured_media_url:
                    media_match = re.search(
                        r"(?P<base>https?://[^\"'\s]+?)(?:/|%2F)(?P<tail>[^/?\"'\s]+)\?token=(?P<token>[^&]+)&expiry=(?P<expiry>\d+)",
                        captured_media_url,
                    )
                    if media_match:
                        logger.info(
                            "DoodStream browser fallback captured direct media URL candidate: %s",
                            captured_media_url,
                        )
                        pass_body = media_match.group("base").rstrip("/") + "/"
                        token = token or media_match.group("token")

                last_result = (pass_path, token, pass_body, final_url, html)

                await context.close()
                await browser.close()

                if pass_path and token:
                    if attempt > 1:
                        logger.info("DoodStream browser fallback succeeded on retry %s", attempt)
                    return last_result

                if attempt < 3:
                    logger.info("DoodStream browser fallback retry %s/3", attempt + 1)

        return last_result

    async def _get_session(self):
        if self.session is None or self.session.closed:
            timeout = ClientTimeout(total=60, connect=30, sock_read=30)
            proxy = self._get_random_proxy()
            if proxy:
                connector = ProxyConnector.from_url(proxy)
            else:
                connector = TCPConnector(limit=0, limit_per_host=0, keepalive_timeout=60, enable_cleanup_closed=True, force_close=False, use_dns_cache=True)
            self.session = ClientSession(timeout=timeout, connector=connector, headers={'User-Agent': self.base_headers["user-agent"]})
        return self.session

    async def extract(self, url: str, **kwargs) -> dict:
        """Extract DoodStream URL."""
        session = await self._get_session()

        async with session.get(url) as response:
            text = await response.text()
            response_url = str(response.url)

        pass_path, token = self._extract_pass_and_token(text)
        pass_body = None

        if not pass_path or not token:
            logger.info("DoodStream: direct HTML parse failed, trying browser fallback")
            try:
                pass_path, token, pass_body, response_url, text = await self._fetch_player_data_via_browser(url)
            except Exception as exc:
                logger.warning("DoodStream: browser fallback failed: %s", exc)

        if not pass_path or not token:
            logger.warning(
                "DoodStream extraction failed after browser fallback: response_url=%s pass_path=%s token_found=%s pass_body_found=%s",
                response_url,
                bool(pass_path),
                bool(token),
                bool(pass_body),
            )
            raise ExtractorError("Failed to extract URL pattern")

        parsed_response_url = urlparse(response_url)
        base_url = f"{parsed_response_url.scheme}://{parsed_response_url.netloc}"
        pass_url = f"{base_url}{pass_path}"
        referer = f"{base_url}/"
        headers = {"range": "bytes=0-", "referer": referer}

        response_text = pass_body
        if response_text is None:
            async with session.get(pass_url, headers=headers) as response:
                response_text = await response.text()
        
        timestamp_ms = str(int(time.time() * 1000))
        final_url = (
            f"{response_text}{self._random_suffix()}?token="
            f"{token}&expiry={timestamp_ms}"
        )

        return {
            "destination_url": final_url,
            "request_headers": {
                **self.base_headers,
                "referer": referer,
            },
            "mediaflow_endpoint": self.mediaflow_endpoint,
        }

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
