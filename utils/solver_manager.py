import logging
import asyncio
import aiohttp
from config import FLARESOLVERR_URL, FLARESOLVERR_TIMEOUT, FLARESOLVERR_WARM_SESSIONS

logger = logging.getLogger(__name__)

class SolverSessionManager:
    """
    Gestore intelligente delle sessioni FlareSolverr.
    Supporta sessioni persistenti (Warm Mode) per risparmiare RAM o sessioni temporanee per risparmiare RAM.
    """
    _instance = None
    _warm_session_id = None
    _lock = asyncio.Lock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(SolverSessionManager, cls).__new__(cls)
        return cls._instance

    async def get_session(self, proxy: str = None) -> tuple[str, bool]:
        """
        Ottiene una sessione FlareSolverr.
        Ritorna una tupla (session_id, is_persistent).
        """
        if not FLARESOLVERR_URL:
            return None, False

        # Se non c'è proxy e la modalità Warm è attiva, usiamo la sessione persistente
        if proxy is None and FLARESOLVERR_WARM_SESSIONS:
            async with self._lock:
                if self._warm_session_id:
                    return self._warm_session_id, True
                
                logger.info("FlareSolverr: Creazione sessione persistente (WARM_SESSIONS=true)")
                self._warm_session_id = await self._create_session(None)
                if self._warm_session_id:
                    return self._warm_session_id, True
        
        # Altrimenti creiamo una sessione temporanea (sarà distrutta dopo l'uso)
        session_id = await self._create_session(proxy)
        return session_id, False

    async def _create_session(self, proxy: str = None) -> str:
        endpoint = f"{FLARESOLVERR_URL.rstrip('/')}/v1"
        payload = {
            "cmd": "sessions.create",
            "maxTimeout": (FLARESOLVERR_TIMEOUT + 60) * 1000,
        }
        if proxy:
            # FlareSolverr/Chromium preferisce socks5:// invece di socks5h://
            solver_proxy = proxy.replace("socks5h://", "socks5://") if proxy.startswith("socks5h://") else proxy
            payload["proxy"] = {"url": solver_proxy}
            
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(
                    endpoint, 
                    json=payload, 
                    timeout=aiohttp.ClientTimeout(total=60)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("status") == "ok":
                            return data.get("session")
            except Exception as e:
                logger.error(f"FlareSolverr: Errore creazione sessione: {e}")
        return None

    async def release_session(self, session_id: str, is_persistent: bool):
        """Chiude la sessione se non è persistente."""
        if not session_id or is_persistent or not FLARESOLVERR_URL:
            return
            
        endpoint = f"{FLARESOLVERR_URL.rstrip('/')}/v1"
        payload = {"cmd": "sessions.destroy", "session": session_id}
        async with aiohttp.ClientSession() as session:
            try:
                await session.post(endpoint, json=payload, timeout=10)
            except Exception:
                pass

    async def report_invalid(self, session_id: str):
        """Invalida la sessione persistente se riscontra errori."""
        if not session_id:
            return
        async with self._lock:
            if session_id == self._warm_session_id:
                logger.warning(f"FlareSolverr: Sessione persistente {session_id} non valida. Rimozione.")
                self._warm_session_id = None
                # Tentiamo di distruggerla sul server per sicurezza
                await self.release_session(session_id, False)

# Singleton
solver_manager = SolverSessionManager()
