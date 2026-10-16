#!/usr/bin/env python3
"""Genera web/mobile.html a partir de web/index.html.

    python build_mobile.py

La interfaz de escritorio sigue siendo la única fuente de verdad: este script
NO la toca. Copia index.html e inyecta una capa móvil (CSS + un poco de JS):

  · Los tres paneles (Configuración · Mapa · Resultado) pasan a ser pantallas
    completas que se alternan con una barra de pestañas inferior, estilo iOS.
  · Controles táctiles de 40 px mínimo e inputs a 16 px (Safari hace zoom al
    enfocar cualquier input de menos de 16 px).
  · Respeta el notch / home indicator (safe-area) y se puede añadir a la
    pantalla de inicio del iPhone como app a pantalla completa.
  · API configurable (?api=https://mi-servidor) para servir el HTML desde otro
    sitio que el backend; por defecto usa el mismo origen.

Cada vez que cambies index.html, vuelve a ejecutar este script.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "web" / "index.html"
DST = ROOT / "web" / "mobile.html"

HEAD = """<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
<meta name="apple-mobile-web-app-capable" content="yes" />
<meta name="mobile-web-app-capable" content="yes" />
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
<meta name="apple-mobile-web-app-title" content="RaceEngine" />
<meta name="theme-color" content="#0e1116" />
<script>
/* ── API base (móvil) ────────────────────────────────────────────────────
   Por defecto, mismo origen. Con ?api=https://host:puerto se recuerda y todas
   las llamadas a /api/… van a ese servidor (el backend ya tiene CORS abierto). */
(function () {
  var q = new URLSearchParams(location.search).get('api');
  try { if (q !== null) localStorage.setItem('re_api', q); } catch (e) {}
  var base = '';
  try { base = (q !== null ? q : localStorage.getItem('re_api')) || ''; } catch (e) {}
  base = base.replace(/\\/+$/, '');
  window.__apiURL = function (u) {
    return (base && typeof u === 'string' && u.charAt(0) === '/' && u.indexOf('/api/') === 0)
      ? base + u : u;
  };
  if (!base) return;
  var f = window.fetch.bind(window);
  window.fetch = function (u, o) { return f(window.__apiURL(u), o); };
  var ES = window.EventSource;
  window.EventSource = function (u, o) { return new ES(window.__apiURL(u), o); };
  window.EventSource.prototype = ES.prototype;
  var wo = window.open.bind(window);
  window.open = function (u, t, f2) { return wo(window.__apiURL(u), t, f2); };
})();
</script>
"""

CSS = """
  /* ═══ CAPA MÓVIL (generada por build_mobile.py) ═══════════════════════ */
  :root{--navh:58px;--sb:env(safe-area-inset-bottom,0px);--st:env(safe-area-inset-top,0px)}
  html,body{height:100%;overscroll-behavior:none;-webkit-tap-highlight-color:transparent}
  body{-webkit-text-size-adjust:100%}
  #app{display:block;height:100dvh;position:relative}
  /* !important: el panel central trae display/position en línea. */
  #app>div{position:absolute!important;left:0;right:0;top:0;
    bottom:calc(var(--navh) + var(--sb));display:none!important;padding-top:var(--st)}
  body.m-cfg #app>div:nth-child(1){display:block!important}
  body.m-map #app>div:nth-child(2){display:flex!important}
  body.m-res #app>div:nth-child(3){display:block!important}
  .panel,.panel.right{border:none;-webkit-overflow-scrolling:touch}
  #map{height:auto;min-height:0}

  /* Táctil: objetivos de 40 px e inputs a 16 px (evita el zoom de Safari). */
  button{min-height:40px;padding:9px 13px;font-size:14px}
  .chip{padding:8px 12px;font-size:13px}
  input[type=number],select,input[type=text],textarea,
  .routebar .endrow input,.mapfind input{font-size:16px;padding:9px 10px}
  input[type=range]{height:30px}
  label{font-size:13px}
  .hint{font-size:12px}
  section{padding:14px 16px}
  .zone{padding:10px;font-size:14px}
  .zone .rm{font-size:22px;padding:0 4px}
  .alt{padding:11px;font-size:13px}
  .kv{font-size:13.5px;padding:6px 0}

  /* Barra de modos del mapa: una sola fila con scroll horizontal. */
  #app>div:nth-child(2)>div:nth-child(4){flex-wrap:nowrap!important;overflow-x:auto;
    padding:8px 10px!important;scrollbar-width:none}
  #app>div:nth-child(2)>div:nth-child(4)::-webkit-scrollbar{display:none}
  #app>div:nth-child(2)>div:nth-child(4)>*{flex:0 0 auto}
  #app>div:nth-child(2)>div:nth-child(4)>span:first-child{display:none}
  #tabs{min-height:44px}
  .tab{padding:8px 11px;font-size:13px}

  /* Controles flotantes sobre el mapa. */
  .mapstack{left:56px;right:8px;top:8px}
  .routebar{max-width:none}
  .mapbar #drawRead{min-width:0}
  .mapfind{left:8px;right:8px;width:auto;bottom:66px}
  .mapctl{right:8px;bottom:8px}
  .leaflet-touch .leaflet-bar a{width:38px;height:38px;line-height:38px}
  #maphud{display:none}

  /* Modales y galería a pantalla casi completa. */
  .modal .box{width:calc(100vw - 24px);max-height:85dvh;overflow:auto}
  .gal{padding:calc(var(--st) + 10px) 10px calc(var(--sb) + 10px)}
  .gal .inner{max-height:100%}
  .gal .grid{grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;padding:0 12px 12px}
  .card:hover{transform:none}
  .cmp{font-size:12px;display:block;overflow-x:auto}

  /* Barra de pestañas inferior. */
  #mnav{position:fixed;left:0;right:0;bottom:0;z-index:1500;display:flex;
    height:calc(var(--navh) + var(--sb));padding-bottom:var(--sb);
    background:rgba(17,21,28,.97);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);
    border-top:1px solid var(--line)}
  #mnav button{flex:1;min-height:0;border:none;background:none;border-radius:0;
    display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;
    color:var(--tx3);font-size:10.5px;font-weight:600;letter-spacing:.2px;padding:6px 0;
    position:relative}
  #mnav button svg{width:24px;height:24px;stroke:currentColor;fill:none;stroke-width:1.8;
    stroke-linecap:round;stroke-linejoin:round}
  #mnav button.on{color:var(--accent)}
  #mnav button .dot{position:absolute;top:7px;left:calc(50% + 9px);width:8px;height:8px;
    border-radius:50%;background:var(--ok);display:none}
  #mnav button.new .dot{display:block}
  #mGen{position:fixed;right:14px;bottom:calc(var(--navh) + var(--sb) + 14px);z-index:1400;
    border-radius:26px;padding:12px 20px;font-size:15px;box-shadow:0 8px 24px rgba(0,0,0,.5);
    display:none}
  body.m-cfg #mGen{display:block}
"""

NAV = """
<nav id="mnav">
  <button data-m="cfg"><svg viewBox="0 0 24 24"><path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/></svg><span data-ml="cfg"></span></button>
  <button data-m="map"><svg viewBox="0 0 24 24"><path d="M9 4 3 6.5v13.5l6-2.5 6 2.5 6-2.5V4l-6 2.5z"/><path d="M9 4v13.5M15 6.5V20"/></svg><span data-ml="map"></span></button>
  <button data-m="res"><svg viewBox="0 0 24 24"><path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/></svg><span data-ml="res"></span><i class="dot"></i></button>
</nav>
<button id="mGen" class="primary"></button>
<script>
/* ── Navegación móvil ───────────────────────────────────────────────────── */
(function () {
  var L10N = {
    es: { cfg: 'Ajustes', map: 'Mapa', res: 'Resultado', gen: '▶ Generar' },
    en: { cfg: 'Settings', map: 'Map', res: 'Result', gen: '▶ Generate' },
  };
  function lang() { try { return localStorage.getItem('re_lang') === 'en' ? 'en' : 'es'; } catch (e) { return 'es'; } }
  function labels() {
    var d = L10N[lang()];
    document.querySelectorAll('[data-ml]').forEach(function (el) { el.textContent = d[el.dataset.ml]; });
    document.getElementById('mGen').textContent = d.gen;
  }
  var cur = 'map';
  function show(m) {
    cur = m;
    document.body.classList.remove('m-cfg', 'm-map', 'm-res');
    document.body.classList.add('m-' + m);
    document.querySelectorAll('#mnav button').forEach(function (b) {
      b.classList.toggle('on', b.dataset.m === m);
      if (b.dataset.m === m) b.classList.remove('new');
    });
    try { sessionStorage.setItem('re_mtab', m); } catch (e) {}
    // Leaflet mide el contenedor al crearse; si estaba oculto hay que avisarle.
    if (m === 'map' && typeof map !== 'undefined' && map && map.invalidateSize) setTimeout(function () { map.invalidateSize(); }, 30);
  }
  window.__mShow = show;
  document.querySelectorAll('#mnav button').forEach(function (b) {
    b.addEventListener('click', function () { show(b.dataset.m); });
  });
  // «Generar» desde Ajustes: lanza y lleva al mapa para ver la ruta crecer.
  document.getElementById('mGen').addEventListener('click', function () {
    var g = document.getElementById('btnGen');
    if (g && !g.disabled) { show('map'); g.click(); }
  });
  var bg = document.getElementById('btnGen');
  if (bg) bg.addEventListener('click', function () { if (cur !== 'map') show('map'); });
  // Punto verde en «Resultado» cuando aparece un resultado nuevo.
  var res = document.getElementById('result');
  if (res) new MutationObserver(function () {
    if (res.style.display !== 'none' && cur !== 'res')
      document.querySelector('#mnav [data-m=res]').classList.add('new');
  }).observe(res, { attributes: true, attributeFilter: ['style'] });
  // Traducción de la barra si cambia el idioma.
  document.addEventListener('click', function () { setTimeout(labels, 0); }, true);
  window.addEventListener('resize', function () {
    if (cur === 'map' && typeof map !== 'undefined' && map && map.invalidateSize) map.invalidateSize();
  });
  labels();
  var start = 'map';
  try { start = sessionStorage.getItem('re_mtab') || 'map'; } catch (e) {}
  show(start);
})();
</script>
"""


def build() -> None:
    html = SRC.read_text(encoding="utf-8")

    def sub(old: str, new: str) -> None:
        nonlocal html
        if html.count(old) != 1:
            sys.exit(f"build_mobile: no encuentro exactamente una vez: {old[:60]!r}\n"
                     "¿Cambió la estructura de index.html? Ajusta build_mobile.py.")
        html = html.replace(old, new)

    sub('<meta name="viewport" content="width=device-width, initial-scale=1" />\n', HEAD)
    sub("<title>RaceEngine — Diseño de recorridos</title>",
        "<title>RaceEngine</title>")
    sub("</style>\n</head>", CSS + "</style>\n</head>")
    # La descarga GPX navega con window.location: también tiene que respetar la API base.
    sub("window.location = `/api/routes/${rt.route_id}.gpx`;",
        "window.location = __apiURL(`/api/routes/${rt.route_id}.gpx`);")
    sub("</body>", NAV + "</body>")
    DST.write_text(html, encoding="utf-8")
    print(f"✔ {DST.relative_to(ROOT)}  ({len(html) // 1024} KB)")


if __name__ == "__main__":
    build()
