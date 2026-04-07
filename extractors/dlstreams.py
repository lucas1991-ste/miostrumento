import logging
import re
import random
import time
import asyncio
from urllib.parse import urlparse
from typing import Dict, Any
import aiohttp
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from aiohttp_socks import ProxyConnector
from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright

logger = logging.getLogger(__name__)

# Easy to change in one place if the entry domain changes again.
DLSTREAMS_ENTRY_ORIGIN = "https://dlhd.dad"

class ExtractorError(Exception):
    """Custom exception for extraction errors."""
    pass

class DLStreamsExtractor:
    """Extractor for dlhd.dad / dlstreams streams."""

    def __init__(self, request_headers: dict = None, proxies: list = None):
        self.request_headers = request_headers or {}
        self.entry_origin = DLSTREAMS_ENTRY_ORIGIN
        self.base_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        }
        self.session = None
        self.mediaflow_endpoint = "hls_manifest_proxy"
        self.proxies = proxies or []
        self._verified_channels: dict[str, float] = {}
        self._browser_key_cache: dict[str, tuple[bytes, float]] = {}
        self._browser_manifest_cache: dict[str, tuple[str, float]] = {}
        self._browser_failure_cache: dict[str, float] = {}
        self._browser_channel_locks: dict[str, asyncio.Lock] = {}

    def _get_browser_lock(self, channel_key: str) -> asyncio.Lock:
        lock = self._browser_channel_locks.get(channel_key)
        if lock is None:
            lock = asyncio.Lock()
            self._browser_channel_locks[channel_key] = lock
        return lock

    def _is_browser_cooldown_active(self, channel_key: str) -> bool:
        retry_after = self._browser_failure_cache.get(channel_key, 0)
        return retry_after > time.time()

    def _mark_browser_failure(self, channel_key: str, cooldown_seconds: int = 60) -> None:
        self._browser_failure_cache[channel_key] = time.time() + cooldown_seconds

    def _clear_browser_failure(self, channel_key: str) -> None:
        self._browser_failure_cache.pop(channel_key, None)

    def _get_random_proxy(self):
        return random.choice(self.proxies) if self.proxies else None

    def _get_header(self, name: str, default: str | None = None) -> str | None:
        for key, value in self.request_headers.items():
            if key.lower() == name.lower():
                return value
        return default

    def _get_cookie_header_for_url(self, url: str) -> str | None:
        if not self.session or self.session.closed or not self.session.cookie_jar:
            return None

        parsed = urlparse(url)
        cookies = self.session.cookie_jar.filter_cookies(
            f"{parsed.scheme}://{parsed.netloc}/"
        )
        cookie_header = "; ".join(f"{key}={morsel.value}" for key, morsel in cookies.items())
        return cookie_header or None

    @staticmethod
    def _extract_channel_id(url: str) -> str:
        match_id = re.search(r"id=(\d+)", url)
        channel_id = match_id.group(1) if match_id else str(url)
        if not channel_id.isdigit():
            channel_id = channel_id.replace("premium", "")
        return channel_id

    async def _prime_dlstreams_session(
        self,
        session: ClientSession,
        watch_url: str,
        channel_id: str,
    ) -> None:
        warmup_headers = {
            "User-Agent": self.base_headers["User-Agent"],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": self._get_header("Accept-Language", "en-US,en;q=0.9"),
        }
        source_referer = self._get_header("Referer")
        if source_referer:
            warmup_headers["Referer"] = source_referer

        warmup_urls = [
            watch_url,
            f"{self.entry_origin}/stream/stream-{channel_id}.php",
        ]

        for warmup_url in warmup_urls:
            try:
                async with session.get(warmup_url, headers=warmup_headers) as resp:
                    await resp.read()
                warmup_headers["Referer"] = warmup_url
            except Exception as exc:
                logger.debug("DLStreams warm-up failed for %s: %s", warmup_url, exc)

    async def _browser_prime_verification(self, watch_url: str, channel_key: str) -> bool:
        cached_until = self._verified_channels.get(channel_key, 0)
        now = time.time()
        if cached_until > now:
            logger.debug("DLStreams browser verification cache hit for %s", channel_key)
            return True
        if self._is_browser_cooldown_active(channel_key):
            logger.info("DLStreams browser verification skipped during cooldown for %s", channel_key)
            return False

        logger.info("DLStreams browser verification starting for %s", channel_key)
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    headless=False,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                    ],
                )
                context = await browser.new_context(
                    user_agent=self.base_headers["User-Agent"],
                    viewport={"width": 1366, "height": 768},
                )
                page = await context.new_page()
                verify_seen = False

                async def on_response(response):
                    nonlocal verify_seen
                    if response.url.startswith("https://sec.ai-hls.site/verify") and response.status == 200:
                        verify_seen = True

                page.on("response", on_response)
                await page.goto(watch_url, wait_until="domcontentloaded", timeout=20000)

                deadline = time.time() + 20
                while time.time() < deadline and not verify_seen:
                    await page.wait_for_timeout(250)

                await context.close()
                await browser.close()

                if verify_seen:
                    self._verified_channels[channel_key] = now + 15 * 60
                    self._clear_browser_failure(channel_key)
                    logger.info("DLStreams browser verification succeeded for %s", channel_key)
                    return True

        except PlaywrightTimeoutError as exc:
            logger.warning("DLStreams browser verification timed out for %s: %s", channel_key, exc)
        except Exception as exc:
            logger.warning("DLStreams browser verification failed for %s: %s", channel_key, exc)

        self._mark_browser_failure(channel_key)
        return False

    async def fetch_key_via_browser(self, key_url: str, original_url: str) -> bytes | None:
        cached = self._browser_key_cache.get(key_url)
        now = time.time()
        if cached and cached[1] > now:
            return cached[0]

        channel_id = self._extract_channel_id(original_url)
        await self._capture_browser_session_state(channel_id)

        cached = self._browser_key_cache.get(key_url)
        if cached and cached[1] > time.time():
            return cached[0]

        channel_key = f"premium{channel_id}"
        watch_url = f"{self.entry_origin}/watch.php?id={channel_id}"
        if self._is_browser_cooldown_active(channel_key):
            logger.info("DLStreams browser key fetch skipped during cooldown for %s", channel_key)
            return None

        logger.info("DLStreams browser key fetch starting for %s", key_url)
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    headless=False,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                    ],
                )
                context = await browser.new_context(
                    user_agent=self.base_headers["User-Agent"],
                    viewport={"width": 1366, "height": 768},
                )
                page = await context.new_page()
                key_bytes: bytes | None = None
                verify_seen = False

                async def on_response(response):
                    nonlocal key_bytes, verify_seen
                    try:
                        if response.url.startswith("https://sec.ai-hls.site/verify") and response.status == 200:
                            verify_seen = True
                        if response.url == key_url and response.status == 200 and key_bytes is None:
                            key_bytes = await response.body()
                    except Exception as exc:
                        logger.debug("DLStreams browser response hook failed for %s: %s", response.url, exc)

                page.on("response", on_response)
                await page.goto(watch_url, wait_until="domcontentloaded", timeout=30000)

                deadline = time.time() + 20
                while time.time() < deadline and key_bytes is None:
                    await page.wait_for_timeout(250)

                await context.close()
                await browser.close()

                if verify_seen:
                    self._verified_channels[channel_key] = now + 15 * 60
                if key_bytes:
                    self._browser_key_cache[key_url] = (key_bytes, now + 30)
                    self._clear_browser_failure(channel_key)
                    logger.info("DLStreams browser key fetch succeeded for %s", key_url)
                    return key_bytes
        except PlaywrightTimeoutError as exc:
            logger.warning("DLStreams browser key fetch timed out for %s: %s", key_url, exc)
        except Exception as exc:
            logger.warning("DLStreams browser key fetch failed for %s: %s", key_url, exc)

        self._mark_browser_failure(channel_key)
        return None

    async def get_manifest_via_browser(self, original_url: str) -> str | None:
        channel_id = self._extract_channel_id(original_url)
        channel_key = f"premium{channel_id}"
        cached = self._browser_manifest_cache.get(channel_key)
        now = time.time()
        if cached and cached[1] > now:
            return cached[0]

        await self._capture_browser_session_state(channel_id)
        cached = self._browser_manifest_cache.get(channel_key)
        if cached and cached[1] > time.time():
            return cached[0]
        return None

    async def _capture_browser_session_state(self, channel_id: str) -> None:
        channel_key = f"premium{channel_id}"
        now = time.time()
        cached_manifest = self._browser_manifest_cache.get(channel_key)
        if cached_manifest and cached_manifest[1] > now:
            return
        if self._is_browser_cooldown_active(channel_key):
            logger.info("DLStreams browser session capture skipped during cooldown for %s", channel_key)
            return

        lock = self._get_browser_lock(channel_key)
        async with lock:
            now = time.time()
            cached_manifest = self._browser_manifest_cache.get(channel_key)
            if cached_manifest and cached_manifest[1] > now:
                return
            if self._is_browser_cooldown_active(channel_key):
                logger.info("DLStreams browser session capture skipped during cooldown for %s", channel_key)
                return

            watch_url = f"{self.entry_origin}/watch.php?id={channel_id}"
            logger.info("DLStreams browser session capture starting for %s", channel_key)
            try:
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch(
                        headless=False,
                        args=[
                            "--disable-blink-features=AutomationControlled",
                            "--no-sandbox",
                            "--autoplay-policy=no-user-gesture-required",
                        ],
                    )
                    context = await browser.new_context(
                        user_agent=self.base_headers["User-Agent"],
                        viewport={"width": 1366, "height": 768},
                    )
                    page = await context.new_page()
                    manifest_text: str | None = None
                    key_ttl = now + 30
                    manifest_ttl = now + 30

                    async def on_response(response):
                        nonlocal manifest_text
                        try:
                            if (
                                response.url.endswith(f"/proxy/wind/{channel_key}/mono.css")
                                or f"/proxy/top1/cdn/{channel_key}/mono.css" in response.url
                                or f"/proxy/" in response.url and f"/{channel_key}/mono.css" in response.url
                            ) and response.status == 200:
                                body = await response.body()
                                decoded = body.decode("utf-8", errors="ignore")
                                if decoded.lstrip().startswith("#EXTM3U"):
                                    manifest_text = decoded
                                    self._browser_manifest_cache[channel_key] = (
                                        manifest_text,
                                        manifest_ttl,
                                    )
                            if "sec.ai-hls.site/key/" in response.url and response.status == 200:
                                body = await response.body()
                                self._browser_key_cache[response.url] = (body, key_ttl)
                        except Exception as exc:
                            logger.debug("DLStreams browser capture hook failed for %s: %s", response.url, exc)

                    context.on("response", on_response)
                    await page.goto(watch_url, wait_until="domcontentloaded", timeout=30000)

                    deadline = time.time() + 20
                    while time.time() < deadline:
                        cached_manifest = self._browser_manifest_cache.get(channel_key)
                        has_manifest = cached_manifest and cached_manifest[1] > time.time()
                        has_key = any(
                            key.startswith("https://sec.ai-hls.site/key/")
                            and expiry > time.time()
                            for key, (_, expiry) in self._browser_key_cache.items()
                        )
                        if has_manifest and has_key:
                            break
                        await page.wait_for_timeout(250)

                    await context.close()
                    await browser.close()

                    cached_manifest = self._browser_manifest_cache.get(channel_key)
                    has_manifest = cached_manifest and cached_manifest[1] > time.time()
                    if has_manifest:
                        self._clear_browser_failure(channel_key)
                    else:
                        self._mark_browser_failure(channel_key)

                    logger.info("DLStreams browser session capture completed for %s", channel_key)
            except Exception as exc:
                self._mark_browser_failure(channel_key)
                logger.warning("DLStreams browser session capture failed for %s: %s", channel_key, exc)

    async def _get_session(self):
        if self.session is None or self.session.closed:
            # DLStreams keys and segments appear to be tied to a consistent
            # egress/session context. Using rotating/global proxies here can
            # produce a different AES key than the browser receives.
            connector = TCPConnector(limit=0, limit_per_host=0)
            
            timeout = ClientTimeout(total=30, connect=10)
            self.session = ClientSession(
                timeout=timeout,
                connector=connector,
                headers=self.base_headers,
                cookie_jar=aiohttp.CookieJar(unsafe=True),
            )
        return self.session

    async def extract(self, url: str, **kwargs) -> Dict[str, Any]:
        """Extracts the M3U8 URL and headers bypassing the public watch page."""
        try:
            # Extract ID from URL or use as is if numeric
            channel_id = self._extract_channel_id(url)

            channel_key = f"premium{channel_id}"
            session = await self._get_session()
            watch_url = f"{self.entry_origin}/watch.php?id={channel_id}"

            await self._prime_dlstreams_session(session, watch_url, channel_id)
            await self._browser_prime_verification(watch_url, channel_key)
            captured_manifest = await self.get_manifest_via_browser(url)

            # --- SPEED BYPASS ---
            # Current iframe host (user can update this manually here)
            iframe_host = "embedkclx.sbs"
            iframe_origin = f"https://{iframe_host}"

            # 1. SERVER LOOKUP: Fetch dynamic server_key
            lookup_url = f"https://sec.ai-hls.site/server_lookup?channel_id={channel_key}"
            logger.info(f"Looking up server key for: {channel_key} (Bypassing {self.entry_origin})")
            
            lookup_headers = {
                "Referer": f"{iframe_origin}/",
                "User-Agent": self.base_headers["User-Agent"]
            }
            
            try:
                async with session.get(lookup_url, headers=lookup_headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        server_key = data.get("server_key", "wind")
                        logger.info(f"Found server_key: {server_key}")
                    else:
                        logger.warning(f"Lookup failed (HTTP {resp.status}), using default key.")
                        server_key = "wind"
            except Exception as e:
                logger.warning(f"Error during lookup: {e}, using default key.")
                server_key = "wind"

            # 2. Construct M3U8 URL
            m3u8_url = f"https://sec.ai-hls.site/proxy/{server_key}/{channel_key}/mono.css"

            # 3. Setup headers for playback/proxying
            playback_headers = {
                "Referer": f"{iframe_origin}/",
                "Origin": iframe_origin,
                "User-Agent": self.base_headers["User-Agent"],
                "Accept": "*/*",
                "X-Direct-Connection": "1",
            }
            cookie_header = self._get_cookie_header_for_url(m3u8_url)
            if cookie_header:
                playback_headers["Cookie"] = cookie_header

            logger.info(f"Extracted M3U8: {m3u8_url}")

            return {
                "destination_url": m3u8_url,
                "request_headers": playback_headers,
                "mediaflow_endpoint": self.mediaflow_endpoint,
                "captured_manifest": captured_manifest,
            }

        except Exception as e:
            logger.exception(f"DLStreams extraction failed for {url}")
            raise ExtractorError(f"Extraction failed: {str(e)}")

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None
