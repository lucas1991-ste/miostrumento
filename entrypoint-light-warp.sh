#!/bin/bash
export PYTHONPATH=/app

# --- Cloudflare WARP Setup ---
if [ "$ENABLE_WARP" = "true" ]; then
    echo "🌐 Starting Cloudflare WARP..."

    # Avvia warp-svc in background (sopprime warning hardware/dbus)
    warp-svc --accept-tos > /var/log/warp-svc.log 2>&1 &

    # Attendi che warp-svc sia pronto (max 15 secondi)
    MAX_RETRIES=15
    COUNT=0
    while ! warp-cli --accept-tos status > /dev/null 2>&1; do
        echo "⏳ Waiting for warp-svc... ($COUNT/$MAX_RETRIES)"
        sleep 1
        COUNT=$((COUNT+1))
        if [ $COUNT -ge $MAX_RETRIES ]; then
            echo "❌ Failed to start warp-svc"
            break
        fi
    done

    if [ $COUNT -lt $MAX_RETRIES ]; then
        # Registrazione (se necessaria)
        if ! warp-cli --accept-tos status | grep -q "Registration Name"; then
            echo "📝 Registering WARP..."
            # Delete old registration to avoid "Old registration is still around" error
            warp-cli --accept-tos registration delete > /dev/null 2>&1 || true
            warp-cli --accept-tos registration new
        fi

        # Chiave licenza (se fornita)
        if [ -n "$WARP_LICENSE_KEY" ]; then
            echo "🔑 Setting WARP license key..."
            warp-cli --accept-tos registration license "$WARP_LICENSE_KEY"
        fi

        # Esclusioni domini che bloccano WARP o richiedono IP reale
        # Allineato con config.py WARP_EXCLUDE_DOMAINS (v2.5.71)
        # Fallback: prova sia il comando nuovo (v2024+) sia quello vecchio
        for domain in cinemacity.cc cccdn.net vavoo.to vavoo.tv lokke.app mediahubmx.cc strem.fun real-debrid.com realdebrid.com api.real-debrid.com premiumize.me www.premiumize.me alldebrid.com api.alldebrid.com debrid-link.com debridlink.com api.debrid-link.com torbox.app api.torbox.app offcloud.com api.offcloud.com put.io api.put.io; do
            (warp-cli --accept-tos tunnel host add $domain > /dev/null 2>&1 || \
             warp-cli --accept-tos add-excluded-domain $domain > /dev/null 2>&1) || true
        done

        # Modalità Proxy (SOCKS5 su 127.0.0.1:1080)
        warp-cli --accept-tos mode proxy
        warp-cli --accept-tos proxy port 1080

        warp-cli --accept-tos connect

        # Attendi stabilizzazione connessione
        echo "⏳ Waiting for WARP to stabilize (10s)..."
        sleep 10

        # Verifica che il SOCKS5 sia in ascolto
        if nc -z 127.0.0.1 1080 2>/dev/null; then
            echo "✅ WARP SOCKS5 proxy is listening on port 1080."
        else
            echo "⚠️ WARP SOCKS5 proxy not detected yet, but proceeding..."
        fi

        warp-cli --accept-tos status
    fi
fi

# --- Avvio EasyProxy (no Xvfb, no FlareSolverr, no Byparr) ---
echo "🎬 Starting EasyProxy (Light + WARP)..."
cd /app
WORKERS_COUNT=${WORKERS:-1}
exec gunicorn \
    --bind 0.0.0.0:${PORT:-7860} \
    --workers $WORKERS_COUNT \
    --worker-class aiohttp.worker.GunicornWebWorker \
    --timeout 120 \
    --graceful-timeout 120 \
    app:app
