import asyncio
import logging
import re
import time
import base64
from urllib.parse import urlparse, urljoin, urlencode

import aiohttp
from bs4 import BeautifulSoup, SoupStrainer

from config import FLARESOLVERR_URL, FLARESOLVERR_TIMEOUT, get_proxy_for_url, TRANSPORT_ROUTES, get_solver_proxy_url, GLOBAL_PROXIES, FLARESOLVERR_WARM_SESSIONS
from utils.cookie_cache import CookieCache
from utils.solver_manager import solver_manager

logger = logging.getLogger(__name__)

class ExtractorError(Exception):
    pass

class Settings:
    flaresolverr_url = FLARESOLVERR_URL
    flaresolverr_timeout = FLARESOLVERR_TIMEOUT

settings = Settings()

class DeltabitExtractor:
    def __init__(self, request_headers: dict = None, proxies: list = None, bypass_warp: bool = False):
        self.request_headers = request_headers or {}
        self.base_headers = self.request_headers.copy()
        if "User-Agent" not in self.base_headers and "user-agent" not in self.base_headers:
             self.base_headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        self.proxies = proxies or GLOBAL_PROXIES
        self.cache = CookieCache("universal")
        self.mediaflow_endpoint = "proxy_stream_endpoint"
        self.bypass_warp_active = bypass_warp
        self.session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(headers=self.base_headers)
        return self.session

    async def _request_flaresolverr(self, cmd: str, url: str = None, post_data: str = None, session_id: str = None, wait: int = 0) -> dict:
        endpoint = f"{settings.flaresolverr_url.rstrip('/')}/v1"
        payload = {"cmd": cmd, "maxTimeout": (settings.flaresolverr_timeout + 60) * 1000}
        if wait > 0: payload["wait"] = wait
        fs_headers = {}
        if url: 
            payload["url"] = url
            proxy = get_proxy_for_url(url, TRANSPORT_ROUTES, self.proxies, bypass_warp=self.bypass_warp_active)
            if proxy:
                payload["proxy"] = {"url": proxy}
                fs_headers["X-Proxy-Server"] = get_solver_proxy_url(proxy)
        if post_data: payload["postData"] = post_data
        if session_id: payload["session"] = session_id
        async with aiohttp.ClientSession() as fs_session:
            async with fs_session.post(endpoint, json=payload, headers=fs_headers, timeout=settings.flaresolverr_timeout + 95) as resp:
                data = await resp.json()
        if data.get("status") != "ok": raise ExtractorError(f"FlareSolverr: {data.get('message')}")
        return data

    async def extract(self, url: str, **kwargs) -> dict:
        proxy = get_proxy_for_url(url, TRANSPORT_ROUTES, self.proxies, self.bypass_warp_active)
        session_id, is_persistent = await solver_manager.get_session(proxy)
        try:
            # 1. Hybrid Solver for Redirector (FAST)
            if any(d in url.lower() for d in ["safego.cc", "clicka.cc", "clicka"]):
                url, ua, _ = await self._solve_redirector_hybrid(url, session_id)

            if "deltabit.co" in url.lower(): url = url.replace("deltabit.co/ ", "deltabit.co/")
            
            # 2. Final page fetch (FlareSolverr for stability)
            res = await self._request_flaresolverr("request.get", url, session_id=session_id, wait=0)
            solution = res.get("solution", {})
            html, ua = solution.get("response", ""), solution.get("userAgent", self.base_headers.get("User-Agent"))
            
            soup = BeautifulSoup(html, 'lxml')
            form_data = {inp.get('name'): inp.get('value', '') for inp in soup.find_all('input') if inp.get('name')}
            if not form_data.get("op"):
                link_match = re.search(r'sources:\s*\["([^"]+)"', html) or re.search(r'file:\s*["\']([^"\']+)["\']', html)
                if link_match: return self._build_result(link_match.group(1), url, ua)
                raise ExtractorError("Deltabit: Form not found")

            # 3. Final POST via FlareSolverr (STABLE)
            form_data['imhuman'], form_data['referer'] = "", url
            await asyncio.sleep(2.5) 
            
            post_res = await self._request_flaresolverr("request.post", url, urlencode(form_data), session_id=session_id, wait=0)
            post_html = post_res.get("solution", {}).get("response", "")

            link_match = re.search(r'sources:\s*\["([^"]+)"', post_html) or re.search(r'file:\s*["\']([^"\']+)["\']', post_html)
            if not link_match: raise ExtractorError("Deltabit: Video source not found")
            return self._build_result(link_match.group(1), url, ua)
        finally:
            if session_id: await solver_manager.release_session(session_id, is_persistent)

    async def _solve_redirector_hybrid(self, url: str, session_id: str) -> tuple:
        res = await self._request_flaresolverr("request.get", url, session_id=session_id)
        solution = res.get("solution", {})
        ua, cookies = solution.get("userAgent"), {c["name"]: c["value"] for c in solution.get("cookies", [])}
        html, current_url = solution.get("response", ""), solution.get("url", url)
        headers, session = {"User-Agent": ua, "Referer": url}, await self._get_session()
        async def light_fetch(target_url, post_data=None):
            try:
                if post_data:
                    async with session.post(target_url, data=post_data, cookies=cookies, headers=headers, timeout=10) as r:
                        return await r.text(), str(r.url)
                else:
                    async with session.get(target_url, cookies=cookies, headers=headers, timeout=10) as r:
                        return await r.text(), str(r.url)
            except Exception: return None, target_url
        for step in range(5):
            if not any(d in current_url.lower() for d in ["safego.cc", "clicka.cc", "clicka"]): break
            soup = BeautifulSoup(html, "lxml")
            img_tag = soup.find("img", src=re.compile(r'data:image/png;base64,'))
            if img_tag:
                import ddddocr
                ocr = ddddocr.DdddOcr(show_ad=False)
                captcha = re.sub(r'[^0-9]', '', ocr.classification(base64.b64decode(img_tag["src"].split(",")[1])).replace('o','0').replace('l','1'))
                form = soup.find("form")
                post_fields = {inp.get("name"): inp.get("value", "") for inp in form.find_all("input") if inp.get("name")} if form else {}
                for key in ["code", "captch5"]:
                    if key in post_fields or (form and form.find("input", {"name": key})):
                        post_fields[key] = captcha
                        break
                else: post_fields["code"] = captcha
                html, current_url = await light_fetch(current_url, post_data=post_fields)
                if not html: break
                soup = BeautifulSoup(html, "lxml")
            next_url = None
            for attempt in range(12):
                for a_tag in soup.find_all(["a", "button"], href=True) or soup.find_all(["a", "button"]):
                    txt = a_tag.get_text().lower()
                    if any(x in txt for x in ["proceed", "continue", "prosegui", "avanti", "click here", "clicca qui"]):
                        next_url = urljoin(current_url, a_tag.get("href", ""))
                        break
                if next_url and next_url != current_url:
                    current_url = next_url
                    html, current_url = await light_fetch(current_url)
                    if html: soup = BeautifulSoup(html, "lxml")
                    break
                if attempt < 11:
                    await asyncio.sleep(1.0)
                    html, current_url = await light_fetch(current_url)
                    if html: soup = BeautifulSoup(html, "lxml")
            if not next_url: break
        return current_url, ua, cookies

    def _build_result(self, video_url: str, referer: str, ua: str) -> dict:
        headers = {"Referer": referer, "User-Agent": ua, "Origin": f"https://{urlparse(referer).netloc}"}
        return {"destination_url": video_url, "request_headers": headers, "mediaflow_endpoint": self.mediaflow_endpoint, "bypass_warp": self.bypass_warp_active}

    async def close(self):
        if self.session and not self.session.closed: await self.session.close()
