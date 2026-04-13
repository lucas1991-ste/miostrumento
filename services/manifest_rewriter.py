import logging
import re
import urllib.parse
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

# Conditional import for DLHD detection
# (Rimosso perche non serve piu logica speciale per DLHD nel rewriter)
try:
    DLHDExtractor = None  # Placeholder per compatibilita se servisse in futuro
except ImportError:
    pass


class ManifestRewriter:
    @staticmethod
    def rewrite_mpd_manifest(
        manifest_content: str,
        base_url: str,
        proxy_base: str,
        stream_headers: dict,
        clearkey_param: str = None,
        api_password: str = None,
    ) -> str:
        """Riscrive i manifest MPD (DASH) per passare attraverso il proxy."""
        try:
            # Aggiungiamo il namespace di default se non presente, per ET
            if "xmlns" not in manifest_content:
                manifest_content = manifest_content.replace(
                    "<MPD", '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"', 1
                )

            root = ET.fromstring(manifest_content)
            ns = {
                "mpd": "urn:mpeg:dash:schema:mpd:2011",
                "cenc": "urn:mpeg:cenc:2013",
                "dashif": "http://dashif.org/guidelines/clearKey",
            }

            # Registra i namespace per evitare prefissi ns0
            ET.register_namespace("", ns["mpd"])
            ET.register_namespace("cenc", ns["cenc"])
            ET.register_namespace("dashif", ns["dashif"])

            # Includiamo tutti gli header rilevanti passati dall'estrattore
            header_params = "".join(
                [
                    f"&h_{urllib.parse.quote(key)}={urllib.parse.quote(value)}"
                    for key, value in stream_headers.items()
                ]
            )

            if api_password:
                header_params += f"&api_password={api_password}"

            def create_proxy_url(relative_url):
                # Skip proxying if URL contains DASH template variables - player must resolve these
                if "$" in relative_url:
                    # Just make it absolute without proxying
                    return urljoin(base_url, relative_url)
                absolute_url = urljoin(base_url, relative_url)
                encoded_url = urllib.parse.quote(absolute_url, safe="")
                return f"{proxy_base}/proxy/mpd/manifest.m3u8?d={encoded_url}{header_params}"

            # --- GESTIONE CLEARKEY STATICA ---
            if clearkey_param:
                try:
                    # Support multiple keys separated by comma
                    # Format: KID1:KEY1,KID2:KEY2
                    key_pairs = clearkey_param.split(",")

                    # Usa il primo KID come default per cenc:default_KID se disponibile
                    first_kid_hex = None
                    if key_pairs:
                        first_pair = key_pairs[0]
                        if ":" in first_pair:
                            first_kid_hex = first_pair.split(":")[0]

                    # Crea l'elemento ContentProtection per ClearKey
                    cp_element = ET.Element("ContentProtection")
                    cp_element.set(
                        "schemeIdUri",
                        "urn:uuid:e2719d58-a985-b3c9-781a-007147f192ec",
                    )
                    cp_element.set("value", "ClearKey")

                    # Puntiamo al nostro endpoint /license
                    license_url = f"{proxy_base}/license?clearkey={clearkey_param}"
                    if api_password:
                        license_url += f"&api_password={api_password}"

                    # 1. Laurl standard (namespace MPD)
                    laurl_element = ET.SubElement(
                        cp_element, "{urn:mpeg:dash:schema:mpd:2011}Laurl"
                    )
                    laurl_element.text = license_url

                    # 2. dashif:Laurl (namespace DashIF)
                    laurl_dashif = ET.SubElement(
                        cp_element, "{http://dashif.org/guidelines/clearKey}Laurl"
                    )
                    laurl_dashif.text = license_url

                    # 3. Aggiungi cenc:default_KID
                    if first_kid_hex and len(first_kid_hex) == 32:
                        kid_guid = (
                            f"{first_kid_hex[:8]}-{first_kid_hex[8:12]}-"
                            f"{first_kid_hex[12:16]}-{first_kid_hex[16:20]}-"
                            f"{first_kid_hex[20:]}"
                        )
                        cp_element.set(
                            "{urn:mpeg:cenc:2013}default_KID", kid_guid
                        )

                    # Inietta ContentProtection
                    adaptation_sets = root.findall(".//mpd:AdaptationSet", ns)
                    logger.info(
                        f"Found {len(adaptation_sets)} AdaptationSet in manifest."
                    )

                    for adaptation_set in adaptation_sets:
                        # RIMUOVI altri ContentProtection (es. Widevine)
                        for cp in adaptation_set.findall("mpd:ContentProtection", ns):
                            scheme = cp.get("schemeIdUri", "").lower()
                            if "e2719d58-a985-b3c9-781a-007147f192ec" not in scheme:
                                adaptation_set.remove(cp)
                                logger.info(
                                    f"Removed conflicting ContentProtection: {scheme}"
                                )

                        # Verifica se esiste gia ClearKey
                        existing_cp = False
                        for cp in adaptation_set.findall("mpd:ContentProtection", ns):
                            if (
                                cp.get("schemeIdUri")
                                == "urn:uuid:e2719d58-a985-b3c9-781a-007147f192ec"
                            ):
                                existing_cp = True
                                break

                        if not existing_cp:
                            adaptation_set.insert(0, cp_element)
                            logger.info(
                                "Injected static ClearKey ContentProtection in AdaptationSet"
                            )

                except Exception as e:
                    logger.error(f"Error parsing clearkey parameter: {e}")

            # --- GESTIONE PROXY LICENZE ESISTENTI ---
            for cp in root.findall(".//mpd:ContentProtection", ns):
                for child in cp:
                    if "Laurl" in child.tag and child.text:
                        original_license_url = child.text
                        encoded_license_url = urllib.parse.quote(
                            original_license_url, safe=""
                        )
                        proxy_license_url = (
                            f"{proxy_base}/license?url={encoded_license_url}{header_params}"
                        )
                        child.text = proxy_license_url
                        logger.info(
                            f"Redirected License URL: {original_license_url} -> {proxy_license_url}"
                        )

            # Riscrive gli attributi URL
            for template_tag in root.findall(".//mpd:SegmentTemplate", ns):
                for attr in ["media", "initialization"]:
                    if template_tag.get(attr):
                        template_tag.set(attr, create_proxy_url(template_tag.get(attr)))

            for seg_url_tag in root.findall(".//mpd:SegmentURL", ns):
                if seg_url_tag.get("media"):
                    seg_url_tag.set("media", create_proxy_url(seg_url_tag.get("media")))

            for base_url_tag in root.findall(".//mpd:BaseURL", ns):
                if base_url_tag.text:
                    base_url_tag.text = create_proxy_url(base_url_tag.text)

            return ET.tostring(root, encoding="unicode", method="xml")

        except Exception as e:
            logger.error(f"Error during MPD manifest rewrite: {e}")
            return manifest_content

    @staticmethod
    async def rewrite_manifest_urls(
        manifest_content: str,
        base_url: str,
        proxy_base: str,
        stream_headers: dict,
        original_channel_url: str = "",
        api_password: str = None,
        get_extractor_func=None,
        no_bypass: bool = False,
    ) -> str:
        """Riscrive gli URL nei manifest HLS per passare attraverso il proxy."""
        lines = manifest_content.split("\n")
        rewritten_lines = []

        # Determina se occorre filtrare e selezionare la massima qualita
        filter_highest_quality = False
        logger.info(f"Manifest rewriter called with base_url: {base_url}")

        try:
            if get_extractor_func:
                original_request_url = (
                    stream_headers.get("referer")
                    or stream_headers.get("Referer")
                    or base_url
                )
                extractor = await get_extractor_func(original_request_url, {})

                if hasattr(extractor, "is_vixsrc") and extractor.is_vixsrc:
                    filter_highest_quality = True
                    logger.info("Detected VixSrc stream: enabling quality filter.")
                elif hasattr(extractor, "is_city") and extractor.is_city:
                    filter_highest_quality = True
                    logger.info("Detected City stream: enabling quality filter.")
        except Exception as e:
            logger.error(f"Error in extractor detection: {e}")

        # no_bypass e mantenuto per compatibilita, ma il rewriter ora proxa sempre.
        _ = no_bypass

        # Logica speciale per il filtro qualita
        if filter_highest_quality:
            streams = []
            for i, line in enumerate(lines):
                if line.startswith("#EXT-X-STREAM-INF:"):
                    bandwidth_match = re.search(r"BANDWIDTH=(\d+)", line)
                    if bandwidth_match and i + 1 < len(lines):
                        bandwidth = int(bandwidth_match.group(1))
                        streams.append(
                            {
                                "bandwidth": bandwidth,
                                "inf": line,
                                "url": lines[i + 1],
                            }
                        )

            if streams:
                highest_quality_stream = max(streams, key=lambda x: x["bandwidth"])
                logger.info(
                    f"Quality Filter: Selected bandwidth {highest_quality_stream['bandwidth']}."
                )
                header_params = "".join(
                    [
                        f"&h_{urllib.parse.quote(key, safe='')}={urllib.parse.quote(str(value), safe='')}"
                        for key, value in stream_headers.items()
                    ]
                )
                if api_password:
                    header_params += f"&api_password={api_password}"

                absolute_stream_url = urljoin(base_url, highest_quality_stream["url"])
                encoded_stream_url = urllib.parse.quote(absolute_stream_url, safe="")
                proxied_stream_url = (
                    f"{proxy_base}/proxy/hls/manifest.m3u8?d={encoded_stream_url}{header_params}"
                )

                proxied_media_lines = []
                for line in lines:
                    if not line.startswith("#EXT-X-MEDIA:") or 'URI="' not in line:
                        continue

                    uri_start = line.find('URI="') + 5
                    uri_end = line.find('"', uri_start)
                    if uri_start <= 4 or uri_end <= uri_start:
                        proxied_media_lines.append(line)
                        continue

                    absolute_media_url = urljoin(base_url, line[uri_start:uri_end])
                    encoded_media_url = urllib.parse.quote(absolute_media_url, safe="")
                    proxy_media_url = (
                        f"{proxy_base}/proxy/hls/manifest.m3u8?d={encoded_media_url}{header_params}"
                    )
                    proxied_media_lines.append(
                        line[:uri_start] + proxy_media_url + line[uri_end:]
                    )

                # Aggiungi i tag globali (es. #EXT-X-VERSION) ma filtra quelli che riscriveremo
                for line in lines:
                    line = line.strip()
                    if not line or line == "#EXTM3U":
                        continue
                    if line.startswith("#EXT-X-MEDIA:") or line.startswith("#EXT-X-STREAM-INF:") or not line.startswith("#"):
                        continue
                    rewritten_lines.append(line)

                # Aggiunge i media proxati (sottotitoli, audio) e lo stream scelto
                rewritten_lines.extend(proxied_media_lines)
                rewritten_lines.append(highest_quality_stream["inf"])
                rewritten_lines.append(proxied_stream_url)
                
                # Assicura che inizi con #EXTM3U
                final_content = "#EXTM3U\n" + "\n".join(rewritten_lines)
                return final_content

        # --- Logica Standard ---
        header_params = "".join(
            [
                f"&h_{urllib.parse.quote(key, safe='')}={urllib.parse.quote(str(value), safe='')}"
                for key, value in stream_headers.items()
            ]
        )

        if api_password:
            header_params += f"&api_password={api_password}"

        # Estrai query params dal base_url per ereditarli se necessario
        base_parsed = urllib.parse.urlparse(base_url)
        base_query = base_parsed.query

        for line in lines:
            line = line.strip()

            # 1. GESTIONE CHIAVI AES-128
            if line.startswith("#EXT-X-KEY:") and 'URI=' in line:
                uri_start = line.find('URI="') + 5
                uri_end = line.find('"', uri_start)

                if uri_start > 4 and uri_end > uri_start:
                    original_key_url = line[uri_start:uri_end]
                    absolute_key_url = urljoin(base_url, original_key_url)

                    encoded_key_url = urllib.parse.quote(absolute_key_url, safe="")
                    encoded_original_channel_url = urllib.parse.quote(
                        original_channel_url, safe=""
                    )

                    # Proxy KEY URL
                    proxy_key_url = (
                        f"{proxy_base}/key?key_url={encoded_key_url}"
                        f"&original_channel_url={encoded_original_channel_url}"
                    )

                    # Aggiungi header
                    key_header_params = "".join(
                        [
                            f"&h_{urllib.parse.quote(key, safe='')}={urllib.parse.quote(str(value), safe='')}"
                            for key, value in stream_headers.items()
                        ]
                    )
                    proxy_key_url += key_header_params

                    if api_password:
                        proxy_key_url += f"&api_password={api_password}"

                    new_line = line[:uri_start] + proxy_key_url + line[uri_end:]
                    rewritten_lines.append(new_line)
                    logger.info(
                        f"Redirected AES key: {absolute_key_url} -> {proxy_key_url}"
                    )
                else:
                    rewritten_lines.append(line)

            # 2. GESTIONE MEDIA (Sottotitoli, Audio secondario)
            elif line.startswith("#EXT-X-MEDIA:") and 'URI=' in line:
                uri_start = line.find('URI="') + 5
                uri_end = line.find('"', uri_start)

                if uri_start > 4 and uri_end > uri_start:
                    original_media_url = line[uri_start:uri_end]
                    absolute_media_url = urljoin(base_url, original_media_url)
                    encoded_media_url = urllib.parse.quote(absolute_media_url, safe="")

                    # Usa endpoint manifest
                    proxy_media_url = (
                        f"{proxy_base}/proxy/hls/manifest.m3u8?d={encoded_media_url}{header_params}"
                    )
                    new_line = line[:uri_start] + proxy_media_url + line[uri_end:]
                    rewritten_lines.append(new_line)
                    logger.info(
                        f"Redirected Media URL: {absolute_media_url} -> {proxy_media_url}"
                    )
                else:
                    rewritten_lines.append(line)

            # 3. GESTIONE MAP (Init Segment per fMP4)
            elif line.startswith("#EXT-X-MAP:") and 'URI=' in line:
                uri_start = line.find('URI="') + 5
                uri_end = line.find('"', uri_start)

                if uri_start > 4 and uri_end > uri_start:
                    original_map_url = line[uri_start:uri_end]
                    absolute_map_url = urljoin(base_url, original_map_url)
                    encoded_map_url = urllib.parse.quote(absolute_map_url, safe="")

                    # Usa endpoint segment.mp4
                    proxy_map_url = (
                        f"{proxy_base}/proxy/hls/segment.mp4?d={encoded_map_url}{header_params}"
                    )

                    new_line = line[:uri_start] + proxy_map_url + line[uri_end:]
                    rewritten_lines.append(new_line)
                    logger.info(
                        f"Redirected MAP URL: {absolute_map_url} -> {proxy_map_url}"
                    )
                else:
                    rewritten_lines.append(line)

            # 4. GESTIONE SEGMENTI E SUB-MANIFEST
            elif line and not line.startswith("#"):
                absolute_url = urljoin(base_url, line) if not line.startswith("http") else line

                # Eredita query params (es. token)
                if base_query and "?" not in absolute_url:
                    absolute_url += f"?{base_query}"

                encoded_url = urllib.parse.quote(absolute_url, safe="")

                # Se e .m3u8 usa /proxy/manifest.m3u8, altrimenti determina estensione
                if ".m3u8" in absolute_url:
                    proxy_url = (
                        f"{proxy_base}/proxy/manifest.m3u8?url={encoded_url}{header_params}"
                    )
                else:
                    # Se l'URL originale ha estensione mp4/m4s, usa .mp4, altrimenti default a .ts
                    path = urllib.parse.urlparse(absolute_url).path
                    ext = ".ts"
                    if (
                        path.endswith(".m4s")
                        or path.endswith(".mp4")
                        or path.endswith(".m4v")
                    ):
                        ext = ".mp4"

                    proxy_url = (
                        f"{proxy_base}/proxy/hls/segment{ext}?d={encoded_url}{header_params}"
                    )

                rewritten_lines.append(proxy_url)

            else:
                # Tutti gli altri tag (es. #EXTINF, #EXT-X-ENDLIST)
                rewritten_lines.append(line)

        return "\n".join(rewritten_lines)
