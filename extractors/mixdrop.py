import asyncio
import logging
import re
import time
import base64
import os
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

class MixdropExtractor:
    def __init__(self, request_headers: dict = None, proxies: list = None, bypass_warp: bool = False):
        self.request_headers = request_headers or {}
        self.base_headers = self.request_headers.copy()
        if "User-Agent" not in self.base_headers and "user-agent" not in self.base_headers:
             self.base_headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        self.proxies = proxies or GLOBAL_PROXIES
        self.cookie_cache = CookieCache("universal")
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

    def _unpack(self, packed_js: str) -> str:
        try:
            match = re.search(r'}\(\'(.*)\',(\d+),(\d+),\'(.*)\'\.split\(\'\|\'\)', packed_js)
            if not match: return packed_js
            p, a, c, k = match.groups()
            a, c, k = int(a), int(c), k.split('|')
            def e(c):
                res = ""
                if c >= a: res = e(c // a)
                return res + "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"[c % a]
            d = {e(i): (k[i] if k[i] else e(i)) for i in range(c)}
            return re.sub(r'\b(\w+)\b', lambda m: d.get(m.group(1), m.group(1)), p)
        except Exception: return packed_js

    async def extract(self, url: str, **kwargs) -> dict:
        proxy = get_proxy_for_url(url, TRANSPORT_ROUTES, self.proxies, self.bypass_warp_active)
        session_id, is_persistent = await solver_manager.get_session(proxy)
        try:
            # 1. Hybrid Solver for Redirectors
            ua, cookies = self.base_headers.get("User-Agent"), {}
            if any(d in url.lower() for d in ["safego.cc", "clicka.cc", "clicka"]):
                url, ua, cookies = await self._solve_redirector_hybrid(url, session_id)

            if "/f/" in url: url = url.replace("/f/", "/e/")
            
            # 2. Fast Path Extraction
            for _ in range(2):
                # Try aiohttp first (Fast Path)
                html = ""
                try:
                    headers = {"User-Agent": ua, "Referer": url}
                    session = await self._get_session()
                    async with session.get(url, cookies=cookies, headers=headers, timeout=5) as r:
                        if r.status == 200: html = await r.text()
                except Exception: pass

                # Fallback to FlareSolverr if Fast Path fails
                if not html or "Cloudflare" in html:
                    res = await self._request_flaresolverr("request.get", url, session_id=session_id, wait=0)
                    solution = res.get("solution", {})
                    html, ua = solution.get("response", ""), solution.get("userAgent", ua)
                    cookies.update({c["name"]: c["value"] for c in solution.get("cookies", [])})
                
                if "eval(function(p,a,c,k,e,d)" in html:
                    for block in re.findall(r'eval\(function\(p,a,c,k,e,d\).*?\}\(.*\)\)', html, re.S):
                        html += "\n" + self._unpack(block)

                patterns = [r'(?:MDCore|vsConfig)\.wurl\s*=\s*["\']([^"\']+)["\']', r'source\s*src\s*=\s*["\']([^"\']+)["\']', r'file:\s*["\']([^"\']+)["\']', r'["\'](https?://[^\s"\']+\.(?:mp4|m3u8)[^\s"\']*)["\']']
                for p in patterns:
                    match = re.search(p, html)
                    if match:
                        v_url = match.group(1)
                        if v_url.startswith("//"): v_url = "https:" + v_url
                        return self._build_result(v_url, url, ua)

                soup = BeautifulSoup(html, "lxml")
                iframe = soup.find("iframe", src=re.compile(r'/e/|/emb', re.I))
                if iframe:
                    url = urljoin(url, iframe["src"])
                    continue
                break
            raise ExtractorError("Mixdrop: Video source not found")
        finally:
            if session_id: await solver_manager.release_session(session_id, is_persistent)

    async def _solve_redirector_hybrid(self, url: str, session_id: str) -> tuple:
        res = await self._request_flaresolverr("request.get", url, session_id=session_id)
        solution = res.get("solution", {})
        ua, cookies = solution.get("userAgent"), {c["name"]: c["value"] for c in solution.get("cookies", [])}
        html, current_url = solution.get("response", ""), solution.get("url", url)
        
        headers, session = {"User-Agent": ua, "Referer": url}, await self._get_session()
        async def light_fetch(target_url, post_data=None):
            for _ in range(2): # Retry once with FS if CF detected
                try:
                    if post_data:
                        async with session.post(target_url, data=post_data, cookies=cookies, headers=headers, timeout=12) as r:
                            text = await r.text()
                            if "cf-challenge" in text or "ray id" in text.lower() or "checking your browser" in text.lower():
                                logger.info(f"Cloudflare detected in redirect step for {target_url}, using FlareSolverr...")
                                fs_res = await self._request_flaresolverr("request.post", target_url, urlencode(post_data), session_id=session_id)
                                sol = fs_res.get("solution", {})
                                cookies.update({c["name"]: c["value"] for c in sol.get("cookies", [])})
                                return sol.get("response", ""), sol.get("url", target_url)
                            return text, str(r.url)
                    else:
                        async with session.get(target_url, cookies=cookies, headers=headers, timeout=12) as r:
                            text = await r.text()
                            if "cf-challenge" in text or "ray id" in text.lower() or "checking your browser" in text.lower():
                                logger.info(f"Cloudflare detected in redirect step for {target_url}, using FlareSolverr...")
                                fs_res = await self._request_flaresolverr("request.get", target_url, session_id=session_id)
                                sol = fs_res.get("solution", {})
                                cookies.update({c["name"]: c["value"] for c in sol.get("cookies", [])})
                                return sol.get("response", ""), sol.get("url", target_url)
                            return text, str(r.url)
                except Exception as e:
                    logger.debug(f"Light fetch failed: {e}, falling back to FlareSolverr...")
                    try:
                        fs_cmd = "request.post" if post_data else "request.get"
                        fs_res = await self._request_flaresolverr(fs_cmd, target_url, urlencode(post_data) if post_data else None, session_id=session_id)
                        sol = fs_res.get("solution", {})
                        cookies.update({c["name"]: c["value"] for c in sol.get("cookies", [])})
                        return sol.get("response", ""), sol.get("url", target_url)
                    except: return None, target_url
            return None, target_url

        for step in range(6):
            if not any(d in current_url.lower() for d in ["safego.cc", "clicka.cc", "clicka", "uprot.net"]): break
            soup = BeautifulSoup(html, "lxml")
            
            # 1. Handle CAPTCHA if present
            img_tag = soup.find("img", src=re.compile(r'data:image/png;base64,|captcha\.php'))
            if img_tag:
                import ddddocr
                ocr = ddddocr.DdddOcr(show_ad=False)
                if "base64," in img_tag["src"]:
                    captcha_data = base64.b64decode(img_tag["src"].split(",")[1])
                else:
                    # Download image
                    c_url = urljoin(current_url, img_tag["src"])
                    _, c_img_data = await light_fetch(c_url) # We need binary here, but light_fetch returns text... 
                    # Re-fetching binary for simplicity since ddddocr needs it
                    async with session.get(c_url, cookies=cookies, headers=headers) as r:
                        captcha_data = await r.read()
                
                captcha = re.sub(r'[^0-9]', '', ocr.classification(captcha_data)).replace('o','0').replace('l','1')
                form = soup.find("form")
                post_fields = {inp.get("name"): inp.get("value", "") for inp in form.find_all("input") if inp.get("name")} if form else {}
                for key in ["code", "captch5", "captcha"]:
                    if key in post_fields or (form and form.find("input", {"name": key})):
                        post_fields[key] = captcha
                        break
                else: post_fields["code"] = captcha
                html, current_url = await light_fetch(current_url, post_data=post_fields)
                if not html: break
                soup = BeautifulSoup(html, "lxml")

            # 2. Handle "Step" buttons and "Proceed" buttons
            next_url = None
            # Search for any button or link that looks like a progression button
            button_markers = ["proceed", "continue", "prosegui", "avanti", "click here", "clicca qui", "step", "passaggio", "vai al"]
            
            for attempt in range(15):
                # Check for Meta Refresh first (common in clicka/safego)
                meta_refresh = soup.find("meta", attrs={"http-equiv": "refresh"})
                if meta_refresh and "url=" in meta_refresh.get("content", "").lower():
                    next_url = urljoin(current_url, meta_refresh["content"].lower().split("url=")[1].strip())
                    break

                for a_tag in soup.find_all(["a", "button", "div"], href=True) or soup.find_all(["a", "button", "div"]):
                    txt = a_tag.get_text().strip().lower()
                    if not txt and a_tag.get("value"): txt = a_tag.get("value").lower()
                    
                    if any(x in txt for x in button_markers):
                        href = a_tag.get("href")
                        if not href and a_tag.name == "button":
                            # Check for onclick redirect
                            onclick = a_tag.get("onclick", "")
                            oc_match = re.search(r'location\.href\s*=\s*["\']([^"\']+)["\']', onclick)
                            if oc_match: href = oc_match.group(1)
                        
                        if href:
                            next_url = urljoin(current_url, href)
                            break
                
                if next_url and next_url != current_url and "uprot.net" not in next_url:
                    current_url = next_url
                    html, current_url = await light_fetch(current_url)
                    if html: soup = BeautifulSoup(html, "lxml")
                    break
                
                if attempt < 14:
                    await asyncio.sleep(1.0)
                    # Re-fetch page to see if it changed (some have countdowns)
                    html, current_url = await light_fetch(current_url)
                    if html: soup = BeautifulSoup(html, "lxml")
            
            if not next_url: break
        return current_url, ua, cookies

    def _build_result(self, video_url: str, referer: str, ua: str) -> dict:
        headers = {"Referer": referer, "User-Agent": ua, "Origin": f"https://{urlparse(referer).netloc}"}
        return {"destination_url": video_url, "request_headers": headers, "mediaflow_endpoint": self.mediaflow_endpoint, "bypass_warp": self.bypass_warp_active}

    async def close(self):
        if self.session and not self.session.closed: await self.session.close()
