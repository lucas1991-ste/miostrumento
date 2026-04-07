import random
import logging
import ssl
import urllib.parse
from urllib.parse import urlparse
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from aiohttp_socks import ProxyConnector

logger = logging.getLogger(__name__)

class ExtractorError(Exception):
    """Eccezione personalizzata per errori di estrazione"""
    pass

class GenericHLSExtractor:
    def __init__(self, request_headers, proxies=None):
        self.request_headers = request_headers
        self.base_headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        self.session = None
        self.proxies = proxies or []

    def _get_random_proxy(self):
        """Restituisce un proxy casuale dalla lista."""
        return random.choice(self.proxies) if self.proxies else None

    async def _get_session(self):
        if self.session is None or self.session.closed:
            proxy = self._get_random_proxy()
            if proxy:
                logging.info(f"Utilizzo del proxy {proxy} per la sessione generica.")
                connector = ProxyConnector.from_url(proxy)
            else:
                # Create SSL context that doesn't verify certificates
                ssl_context = ssl.create_default_context()
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE
                
                connector = TCPConnector(
                    limit=0, limit_per_host=0, 
                    keepalive_timeout=60, enable_cleanup_closed=True, 
                    force_close=False, use_dns_cache=True,
                    ssl=ssl_context
                )

            timeout = ClientTimeout(total=60, connect=30, sock_read=30)
            self.session = ClientSession(
                timeout=timeout, connector=connector, 
                headers={'user-agent': self.base_headers['user-agent']}
            )
        return self.session

    async def extract(self, url, **kwargs):
        # ✅ AGGIORNATO: Rimossa validazione estensioni su richiesta utente.
        parsed_url = urlparse(url)
        origin = f"{parsed_url.scheme}://{parsed_url.netloc}"
        
        # DEBUG INSIDE EXTRACTOR
        # logger.debug(f"[GenericHLSExtractor] Extracting {url}")
        # logger.debug(f"[GenericHLSExtractor] self.request_headers: {self.request_headers}")

        # Inizializza headers con User-Agent di default (in minuscolo)
        headers = {"user-agent": self.base_headers["user-agent"]}
        
        # ✅ FIX: Non sovrascrivere Referer/Origin se già presenti in request_headers (es. passati via h_ params)
        # Cerchiamo in modo case-insensitive
        has_referer = False
        has_origin = False
        for k, v in self.request_headers.items():
            if k.lower() == 'referer':
                has_referer = True
                headers["referer"] = v # Usa quello passato
            elif k.lower() == 'origin':
                has_origin = True
                headers["origin"] = v # Usa quello passato

        if not has_referer:
            headers["referer"] = f"{origin}/"
        if not has_origin:
            headers["origin"] = origin

        # Applica altri header passati dal proxy (h_ params)
        for h, v in self.request_headers.items():
            h_lower = h.lower()
            
            # ✅ FIX DLHD: Accetta User-Agent passato via h_ (browser vero)
            if h_lower == "user-agent":
                if "chrome" in v.lower() or "applewebkit" in v.lower():
                    headers["user-agent"] = v
                continue
            
            if h_lower in ["referer", "origin"]:
                continue # Già gestiti sopra

            # Filtra e aggiunge solo gli header necessari/sicuri
            if h_lower in ["authorization", "x-api-key", "x-auth-token", "cookie", "x-channel-key", "accept"]:
                # Sovrascrive garantendo che non ci siano duplicati grazie alla chiave minuscola
                headers[h_lower] = v
            
            # Blocca esplicitamente header di tracciamento IP/Proxy
            if h_lower in ["x-forwarded-for", "x-real-ip", "forwarded", "via", "host"]:
                continue

        return {
            "destination_url": url, 
            "request_headers": headers, 
            "mediaflow_endpoint": "hls_proxy"
        }

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
