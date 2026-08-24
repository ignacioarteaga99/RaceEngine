#!/usr/bin/env bash
# Descarga Leaflet a web/vendor/ para que la interfaz funcione sin internet.
# (Los teselados del mapa sí requieren red; sin ella verás la ruta sobre fondo liso.)
set -euo pipefail
cd "$(dirname "$0")/web/vendor"
BASE="https://unpkg.com/leaflet@1.9.4/dist"
echo "▸ descargando Leaflet 1.9.4…"
curl -fsSL "$BASE/leaflet.js"  -o leaflet.js
curl -fsSL "$BASE/leaflet.css" -o leaflet.css
mkdir -p images
for f in marker-icon.png marker-icon-2x.png marker-shadow.png layers.png layers-2x.png; do
  curl -fsSL "$BASE/images/$f" -o "images/$f" || true
done
echo "✓ Leaflet vendorizado en web/vendor/"
