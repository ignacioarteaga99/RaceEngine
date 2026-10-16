#!/usr/bin/env python3
"""Lanzador único y multiplataforma.

    python serve.py

Resuelve las rutas de los paquetes por sí mismo, así que no depende de PYTHONPATH
(que usa ':' en macOS/Linux y ';' en Windows, una fuente típica de errores).
Funciona igual en macOS, Linux y Windows.

Opciones:
    python serve.py --port 8080          otro puerto
    python serve.py --region las_condes  precalienta esa región
    python serve.py --reload             recarga automática al editar código
    python serve.py --no-open            no abrir el navegador
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

def _force_utf8_console() -> None:
    """La consola de Windows usa cp1252 por defecto y revienta con «▸», «·» o «»».

    Reconfigurar la salida a UTF-8 con reemplazo evita un UnicodeEncodeError que
    tumbaría el arranque en cmd.exe. En macOS/Linux no cambia nada.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


_force_utf8_console()

ROOT = Path(__file__).resolve().parent
PACKAGES = [
    "packages/contracts", "packages/routing", "packages/geodata",
    "packages/engine", "apps/api",
]


def bootstrap_path() -> None:
    """Inserta los paquetes del monorepo en sys.path."""
    for rel in PACKAGES:
        p = str(ROOT / rel)
        if p not in sys.path:
            sys.path.insert(0, p)


def check_python() -> None:
    if sys.version_info < (3, 12):
        print("─" * 62)
        print(f"  ERROR: se requiere Python 3.12 o superior.")
        print(f"  Detectado: {sys.version.split()[0]}")
        print()
        print("  macOS:    brew install python@3.12")
        print("  Windows:  https://www.python.org/downloads/  (marca 'Add to PATH')")
        print("  Linux:    sudo apt install python3.12 python3.12-venv")
        print("─" * 62)
        sys.exit(1)


def check_deps() -> None:
    missing = []
    for mod, pkg in (("numpy", "numpy"), ("fastapi", "fastapi"),
                     ("uvicorn", "uvicorn[standard]"), ("pydantic", "pydantic")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if missing:
        print("─" * 62)
        print("  Faltan dependencias. Instálalas con:")
        print()
        print(f"    {Path(sys.executable).name} -m pip install -r requirements.txt")
        print()
        print(f"  Faltan: {', '.join(missing)}")
        print("─" * 62)
        sys.exit(1)


LEAFLET = {
    "leaflet.js": "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js",
    "leaflet.css": "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css",
}


def ensure_leaflet() -> None:
    """Descarga Leaflet a web/vendor/ si falta. Silencioso si no hay red.

    Se hace aquí, en Python, para no depender de curl (Unix) ni de PowerShell
    (Windows). Si falla, la interfaz cae al CDN automáticamente.
    """
    import urllib.request

    vendor = ROOT / "web" / "vendor"
    vendor.mkdir(parents=True, exist_ok=True)
    if (vendor / "leaflet.js").exists():
        return
    print("▸ descargando Leaflet…")
    for name, url in LEAFLET.items():
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                (vendor / name).write_bytes(r.read())
        except Exception:  # noqa: BLE001
            print("  (sin red: la interfaz usará el CDN al abrirse)")
            return
    images = vendor / "images"
    images.mkdir(exist_ok=True)
    for img in ("marker-icon.png", "marker-icon-2x.png", "marker-shadow.png",
                "layers.png", "layers-2x.png"):
        try:
            with urllib.request.urlopen(
                    f"https://unpkg.com/leaflet@1.9.4/dist/images/{img}", timeout=20) as r:
                (images / img).write_bytes(r.read())
        except Exception:  # noqa: BLE001
            pass


def ensure_region(region: str) -> None:
    from raceengine_geodata.regions import REGIONS, graph_path

    if graph_path(region).exists():
        return
    spec = REGIONS.get(region)
    if spec is None:
        print(f"  Región desconocida: {region}. Disponibles: {', '.join(sorted(REGIONS))}")
        sys.exit(1)
    if not spec.synthetic:
        print(f"  La región «{spec.label}» no está construida. Ejecuta primero:")
        print(f"    {Path(sys.executable).name} -m raceengine_geodata.cli build {region}")
        sys.exit(1)
    print(f"▸ construyendo la región «{spec.label}» (sintética, sin red)…")
    from raceengine_geodata import build_region
    build_region(region)


def ensure_mobile() -> None:
    """Regenera web/mobile.html si index.html es más nuevo (o si no existe)."""
    src, dst = ROOT / "web" / "index.html", ROOT / "web" / "mobile.html"
    if not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
        import build_mobile
        build_mobile.build()


def lan_ip() -> str:
    """IP de este equipo en la red local (sin enviar nada: UDP no conecta)."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "IP-DE-TU-PC"
    finally:
        s.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="RaceEngine V2")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--region", default="demo", help="regiones a precalentar (coma)")
    ap.add_argument("--reload", action="store_true")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    check_python()
    bootstrap_path()
    check_deps()

    ensure_leaflet()
    ensure_mobile()
    os.environ["RACEENGINE_WARM"] = args.region
    for r in args.region.split(","):
        ensure_region(r.strip())

    url = f"http://localhost:{args.port}"
    print()
    print("  ╭──────────────────────────────────────────────╮")
    print(f"  │  RaceEngine V2  →  {url:<25} │")
    print("  ╰──────────────────────────────────────────────╯")
    if args.host == "0.0.0.0":
        print(f"  iPhone (misma WiFi):  http://{lan_ip()}:{args.port}/m")
    else:
        print("  ¿Desde el iPhone?  python serve.py --host 0.0.0.0  → abre /m")
    print()

    if not args.no_open:
        threading.Thread(
            target=lambda: (time.sleep(2.0), webbrowser.open(url)),
            daemon=True,
        ).start()

    import uvicorn
    uvicorn.run("raceengine_api.main:app", host=args.host, port=args.port,
                reload=args.reload, log_level="info")


if __name__ == "__main__":
    main()
