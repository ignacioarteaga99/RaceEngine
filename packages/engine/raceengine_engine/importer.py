"""Importar un recorrido existente y puntuarlo con el mismo motor.

Para qué sirve
─────────────
El score de una ruta generada es un número abstracto hasta que se compara con
algo que el organizador ya conoce. Importar el recorrido del año pasado convierte
la conversación en «el tuyo saca 61, éste 74, y aquí está por qué»: la misma
métrica, el mismo grafo, los mismos supuestos.

Cómo se engancha — y por qué la primera versión salía escalonada
────────────────────────────────────────────────────────────────
Un GPX es una nube de puntos con ruido de GPS; el motor razona sobre aristas de
grafo. Entre medias hace falta un emparejamiento. La primera versión hacía lo
ingenuo y fallaba de dos formas visibles:

  · **Adelgazaba a 120 m y enganchaba cada punto al nodo más cercano.** Con esa
    separación el tramo entre dos anclas admitía rodeos de manzana entera (la
    cota era `max(250 m, recta × 3)`), así que en cada esquina el enganche podía
    irse por la calle de al lado y volver. Eso es el aspecto «escalonado».
  · **Enganchaba con los pesos de la carrera.** Si el usuario tenía una zona
    prohibida, una pendiente máxima o un radio de búsqueda configurados, las
    calles por las que de verdad pasaba la traza estaban VETADAS para el
    emparejador, que no tenía más remedio que inventar un camino por otro sitio.
    Es exactamente el síntoma de «pasa por otros lugares».

Lo que hace ahora
─────────────────
1. **Adelgaza a ~30 m**, no a 120. A esa escala una esquina no se pierde.
2. **Empareja con pesos permisivos**: toda calle de carrera vale, coste = metros.
   Una traza importada tiene derecho a violar las restricciones — de hecho, saber
   que las viola es justo la información que se busca. Las violaciones se
   detectan DESPUÉS, al puntuar con el modelo real.
3. **Búsqueda por haz (beam search) sobre nodos candidatos.** En vez de decidir
   cada punto por su cuenta —que es lo que hace saltar entre calles paralelas—,
   mantiene varias hipótesis vivas y se queda con la secuencia cuyo coste total
   (distancia de enganche + desvío del tramo respecto a la recta) es mínimo. Una
   hipótesis que se meta por la calle paralela paga el rodeo en el tramo
   siguiente y muere sola.
4. **Los huecos se cortan, no se rellenan.** Si un trozo de la traza no tiene red
   utilizable debajo —un parque, un túnel, otra ciudad—, la cadena se corta ahí y
   se retoma después, y el hueco se declara. Antes se saltaba en silencio, que es
   como el emparejamiento acababa uniendo dos puntos lejanos por una calle que la
   traza original no pisaba.

Se devuelve también la traza ORIGINAL, para poder dibujarla debajo del enganche.
Sin ella el usuario no tiene forma de juzgar si el emparejamiento es fiel.
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET

import numpy as np

from raceengine_contracts import ConstraintModel
from raceengine_routing import RoutingWeights

from .context import GraphContext
from .draw import draw_state
from .snap import usable_node_mask

# Separación a la que se adelgaza la traza antes de enganchar. A 30 m una esquina
# de calle conserva su vértice; a 120 m se perdía y el tramo la cortaba en recto.
THIN_SPACING_M = 30.0
# Un punto que engancha más lejos que esto no es un punto de esta red.
MAX_SNAP_M = 120.0
# Candidatos por observación y anchura del haz. Cuatro y tres bastan: el caso
# difícil es una avenida con calzadas separadas o una ciclovía paralela, y ahí
# los candidatos relevantes son dos o tres.
MAX_CANDIDATES = 4
BEAM_WIDTH = 3
# Cota del tramo entre dos anclas consecutivas. Con 30 m de separación, un tramo
# admisible mide 30–120 m; permitir 250 m era permitir dar la vuelta a la manzana.
LEG_SLACK = 2.2
LEG_FLOOR_M = 90.0
# Conversión de metros de enganche y de rodeo a coste comparable. El rodeo pesa
# más que el enganche a propósito: el ruido de GPS mueve un punto veinte metros,
# pero no hace que la ruta dé un rodeo de cien.
SNAP_COST = 1.0
DETOUR_COST = 2.5
# Coste de cambiar de rumbo respecto a la dirección que llevaba la traza, en
# metros equivalentes. Es lo que mata el zigzag entre las dos calzadas de una
# misma avenida: cruzar de una a otra y volver son dos giros de 90°, y el
# emparejador antes sólo veía que el enganche mejoraba unos metros.
HEADING_COST = 60.0
# Un hueco se PUENTEA si el camino por calle no se pasa de esto respecto a la
# recta; si se pasa, no hay forma honesta de unirlo y se corta.
# Cota del puente sobre un hueco. Generosa a propósito: un hueco real es un
# túnel, un paso por dentro de un parque, un tramo de pasarela o un agujero de
# datos, y todos ellos se rodean por calle con un desvío que puede ser largo.
# Con 3× y 400 m de suelo, un maratón con un solo hueco perdía TODO lo que venía
# después —el trozo corto se descartaba entero— y aparecía como media traza.
BRIDGE_SLACK = 6.0
BRIDGE_FLOOR_M = 2500.0
# Segundo intento sin cota, validando después: A* con cota corta se rinde antes
# de encontrar un desvío que existe. Si lo que sale se pasa de esto, no era un
# desvío: era otro sitio.
BRIDGE_LAST_RESORT = 9.0


def parse_gpx(text: str) -> list[tuple[float, float]]:
    """Puntos (lat, lon) de un GPX. Acepta trkpt, rtept y wpt."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"GPX ilegible: {exc}") from exc
    pts: list[tuple[float, float]] = []
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag not in ("trkpt", "rtept", "wpt"):
            continue
        try:
            pts.append((float(el.attrib["lat"]), float(el.attrib["lon"])))
        except (KeyError, ValueError):
            continue
    return pts


def parse_geojson(text: str) -> list[tuple[float, float]]:
    """Puntos (lat, lon) de la primera LineString de un GeoJSON."""
    import json

    data = json.loads(text)

    def coords_of(geom: dict) -> list:
        t = (geom or {}).get("type")
        if t == "LineString":
            return geom.get("coordinates", [])
        if t == "MultiLineString":
            parts = geom.get("coordinates", [])
            return [p for part in parts for p in part]
        return []

    geoms: list[dict] = []
    if data.get("type") == "FeatureCollection":
        geoms = [f.get("geometry", {}) for f in data.get("features", [])]
    elif data.get("type") == "Feature":
        geoms = [data.get("geometry", {})]
    else:
        geoms = [data]
    for g in geoms:
        c = coords_of(g)
        if c:
            return [(float(p[1]), float(p[0])) for p in c if len(p) >= 2]
    return []


def parse_track(text: str) -> list[tuple[float, float]]:
    """Detecta el formato por el contenido, no por la extensión."""
    head = text.lstrip()[:400].lower()
    if head.startswith("<") or "<gpx" in head:
        return parse_gpx(text)
    if head.startswith("{"):
        return parse_geojson(text)
    # Último recurso: pares «lat,lon» por línea.
    pts = []
    for line in text.splitlines():
        m = re.match(r"\s*(-?\d+\.?\d*)\s*[,;\t ]\s*(-?\d+\.?\d*)", line)
        if m:
            pts.append((float(m.group(1)), float(m.group(2))))
    if not pts:
        raise ValueError("Formato no reconocido: se esperaba GPX o GeoJSON")
    return pts


def _thin(g, pts: list[tuple[float, float]],
          spacing_m: float = THIN_SPACING_M) -> list[tuple[float, float]]:
    if len(pts) < 3:
        return list(pts)
    out = [pts[0]]
    lx, ly = g.project(*pts[0])
    for p in pts[1:-1]:
        x, y = g.project(*p)
        if math.hypot(x - lx, y - ly) >= spacing_m:
            out.append(p)
            lx, ly = x, y
    out.append(pts[-1])
    return out


def match_weights(ctx: GraphContext) -> RoutingWeights:
    """Pesos permisivos: toda calle de carrera vale, el coste son los metros.

    Emparejar con los pesos de la carrera era el error de fondo. Una traza
    importada puede —y suele— pasar por donde el usuario ha prohibido pasar; ahí
    está la gracia de importarla. Si el emparejador respeta esas prohibiciones,
    devuelve un recorrido que no es el que se importó, y todo lo que se mida
    después es sobre una ficción.
    """
    g = ctx.graph
    return RoutingWeights(
        allowed_edges=(g.eclass >= 0),
        weight=g.elen.astype(np.float32),
        min_ratio=1.0, max_ratio=1.0,
    )


def _candidates(g, usable: np.ndarray, lat: float, lon: float) -> list[tuple[int, float]]:
    """Nodos utilizables cerca del punto, con su distancia de enganche."""
    px, py = g.project(lat, lon)
    idx = g.grid.within(px, py, MAX_SNAP_M)
    if idx is None or len(idx) == 0:
        return []
    idx = np.asarray(idx)
    idx = idx[usable[idx]]
    if not len(idx):
        return []
    d = np.hypot(g.x[idx] - px, g.y[idx] - py)
    order = np.argsort(d)[:MAX_CANDIDATES]
    return [(int(idx[i]), float(d[i])) for i in order]


def _bearing(g, a: int, b: int) -> float | None:
    dx = float(g.x[b] - g.x[a])
    dy = float(g.y[b] - g.y[a])
    if math.hypot(dx, dy) < 1e-6:
        return None
    return math.atan2(dy, dx)


def _beam_match(ctx: GraphContext, usable: np.ndarray, w: RoutingWeights,
                thin: list[tuple[float, float]]) -> tuple[list[tuple[list, list]], int]:
    """Empareja la traza y devuelve (tramos, puntos sin candidato).

    Devuelve TRAMOS —cada uno con sus nodos y sus aristas— en vez de un único
    camino. Es la corrección de un fallo real: al cerrar un hueco, la versión
    anterior pegaba el trozo siguiente detrás del anterior sin arista que los
    uniera, y el resultado violaba la invariante `aristas = nodos − 1`. Nadie lo
    notaba hasta que la medición de la ruta más corta posible intentaba emparejar
    dos arrays de distinta longitud y el servidor devolvía un 500.
    """
    g = ctx.graph
    tramos: list[tuple[list, list]] = []
    beam: dict[int, tuple[float, list[int], list[int]]] = {}
    sin_cand = 0
    prev_pt: tuple[float, float] | None = None

    def cerrar() -> None:
        if not beam:
            return
        _, bn, be = min(beam.values(), key=lambda v: v[0])
        if len(bn) >= 2 and be:
            tramos.append((bn, be))

    for lat, lon in thin:
        cands = _candidates(g, usable, lat, lon)
        if not cands:
            sin_cand += 1
            continue

        if not beam:
            beam = {n: (SNAP_COST * d, [n], []) for n, d in cands}
            prev_pt = (lat, lon)
            continue

        # Rumbo y distancia entre las DOS observaciones: es lo que de verdad
        # recorrió quien grabó la traza, y contra eso se juzga cada hipótesis.
        recta_obs = 0.0
        rumbo_obs = None
        if prev_pt is not None:
            ax, ay = g.project(*prev_pt)
            bx, by = g.project(lat, lon)
            recta_obs = math.hypot(bx - ax, by - ay)
            if recta_obs > 5.0:
                rumbo_obs = math.atan2(by - ay, bx - ax)

        nxt: dict[int, tuple[float, list[int], list[int]]] = {}
        for node, dsnap in cands:
            mejor: tuple[float, list[int], list[int]] | None = None
            for prev, (coste, pn, pe) in beam.items():
                if prev == node:
                    cand = (coste + SNAP_COST * dsnap, pn, pe)
                else:
                    recta = math.hypot(float(g.x[node] - g.x[prev]),
                                       float(g.y[node] - g.y[prev]))
                    cap = max(LEG_FLOOR_M, recta * LEG_SLACK)
                    leg = ctx.provider.shortest_path(prev, node, w, max_distance_m=cap)
                    if leg is None or not leg.ok:
                        continue
                    ref = max(recta_obs, recta, 1.0)
                    desvio = max(0.0, leg.distance_m - ref)
                    # Penalización por rumbo: si el tramo enganchado apunta a otro
                    # sitio que la traza, es que se ha cruzado a la calzada de al
                    # lado. Sin esto, en una avenida de doble calzada el enganche
                    # cruzaba y volvía en cada manzana, y el mapa salía escalonado.
                    giro = 0.0
                    rumbo_leg = _bearing(g, prev, node)
                    if rumbo_obs is not None and rumbo_leg is not None:
                        giro = HEADING_COST * (1.0 - math.cos(rumbo_leg - rumbo_obs))
                    cand = (coste + SNAP_COST * dsnap + DETOUR_COST * desvio + giro,
                            pn + [int(x) for x in leg.nodes[1:]],
                            pe + [int(x) for x in leg.edges])
                if mejor is None or cand[0] < mejor[0]:
                    mejor = cand
            if mejor is not None:
                nxt[node] = mejor

        if not nxt:
            # Ninguna hipótesis alcanza este punto: hueco. Se cierra el tramo y se
            # reanuda limpio. El puente, si procede, se decide después.
            cerrar()
            beam = {n: (SNAP_COST * d, [n], []) for n, d in cands}
            prev_pt = (lat, lon)
            continue

        best = sorted(nxt.items(), key=lambda kv: kv[1][0])[:BEAM_WIDTH]
        base = best[0][1][0]
        beam = {n: (c - base, bn, be) for n, (c, bn, be) in best}
        prev_pt = (lat, lon)

    cerrar()
    return tramos, sin_cand


# Longitud máxima de un ramal que se considera artefacto. Por encima de esto, la
# ida y vuelta es DEL RECORRIDO, no del emparejador.
MAX_SPUR_M = 80.0


def _drop_spurs(nodes: list[int], edges: list[int], g=None,
                max_spur_m: float = MAX_SPUR_M) -> tuple[list[int], list[int]]:
    """Quita los ramales de ida y vuelta CORTOS (A → B → A).

    El emparejamiento los produce cuando un punto ruidoso engancha a una calle
    perpendicular y el siguiente vuelve. Son invisibles en las cifras —suman unos
    metros— pero son exactamente los dientes que se ven sobre el mapa.

    El límite de longitud no es una optimización: es la corrección de un fallo.
    Sin él, la limpieza es recursiva y desmonta un ramal entero: A→B→C→B→A colapsa
    primero a A→B→A y después a A. Un recorrido con un tramo de ida y vuelta de
    verdad —que es una figura habitual, se usa para cuadrar distancia— perdía ese
    tramo completo y aparecía un 12 % más corto de lo que era.
    """
    if len(nodes) < 3:
        return nodes, edges
    n, e = list(nodes), list(edges)
    largo = (lambda i: float(g.elen[e[i]]) if g is not None else 0.0)
    cambiado = True
    while cambiado and len(n) >= 3:
        cambiado = False
        i = 1
        while i < len(n) - 1 and i <= len(e) - 1:
            if n[i - 1] == n[i + 1] and (largo(i - 1) + largo(i)) <= max_spur_m:
                del n[i]                 # el pico
                del n[i]                 # y el regreso
                del e[i - 1:i + 1]       # sus dos aristas
                cambiado = True
                i = max(1, i - 1)
            else:
                i += 1
    return n, e


def _stitch(ctx: GraphContext, w: RoutingWeights,
            tramos: list[tuple[list, list]]
            ) -> tuple[list[int], list[int], int, int, float]:
    """Une los tramos en UN camino conexo.

    Devuelve (nodos, aristas, puentes, cortes, metros descartados).

    Un hueco se puentea si existe camino por calle y no se pasa de tres veces la
    distancia en línea recta: un túnel, un tramo de parque o un agujero de datos
    caben ahí. Si no cabe, no se inventa: se corta y se conserva la cadena más
    larga, y el resto se declara descartado.
    """
    if not tramos:
        return [], [], 0, 0, 0.0

    cadenas: list[tuple[list, list]] = [tramos[0]]
    puentes = 0
    for nodos, aristas in tramos[1:]:
        an, ae = cadenas[-1]
        a, b = an[-1], nodos[0]
        g = ctx.graph
        recta = math.hypot(float(g.x[b] - g.x[a]), float(g.y[b] - g.y[a]))
        cap = max(BRIDGE_FLOOR_M, recta * BRIDGE_SLACK)
        leg = ctx.provider.shortest_path(a, b, w, max_distance_m=cap) if a != b else None
        if a != b and (leg is None or not leg.ok):
            # Segundo intento con la cota muy suelta. Perder la mitad de un
            # recorrido por no insistir sale mucho más caro que un desvío largo,
            # y el resultado se valida igual antes de aceptarlo.
            amplio = ctx.provider.shortest_path(
                a, b, w, max_distance_m=max(cap, recta * BRIDGE_LAST_RESORT + 5000.0))
            if amplio is not None and amplio.ok \
                    and amplio.distance_m <= max(recta * BRIDGE_LAST_RESORT, 4000.0):
                leg = amplio
        if a == b:
            cadenas[-1] = (an + nodos[1:], ae + aristas)
        elif leg is not None and leg.ok:
            cadenas[-1] = (an + [int(x) for x in leg.nodes[1:]] + nodos[1:],
                           ae + [int(x) for x in leg.edges] + aristas)
            puentes += 1
        else:
            cadenas.append((nodos, aristas))

    cortes = len(cadenas) - 1
    # Con varias cadenas irreconciliables se conserva la más larga: un recorrido
    # es un camino, no una colección de trozos sueltos. Pero hay que decir CUÁNTO
    # se ha tirado: perder doce kilómetros en silencio y enseñar el resto como si
    # fuera la traza entera es la peor forma de fallar.
    g = ctx.graph
    largo = [float(g.elen[np.asarray(c[1], dtype=np.int64)].sum()) if c[1] else 0.0
             for c in cadenas]
    i = int(np.argmax(largo))
    descartado = float(sum(largo) - largo[i])
    return cadenas[i][0], cadenas[i][1], puentes, cortes, descartado
def match_track(ctx: GraphContext, model: ConstraintModel,
                pts: list[tuple[float, float]]) -> dict:
    """Engancha una traza a la red y devuelve el estado puntuado."""
    g = ctx.graph
    if len(pts) < 2:
        return {"ok": False, "error": "La traza no tiene puntos suficientes."}

    w = match_weights(ctx)
    usable = usable_node_mask(g, w.allowed_edges, ctx.esrc)
    if not usable.any():
        return {"ok": False, "error": "La región no tiene red utilizable."}

    thin = _thin(g, pts)
    tramos, sin_cand = _beam_match(ctx, usable, w, thin)
    nodes, edges, puentes, cortes, descartado = _stitch(ctx, w, tramos)
    nodes, edges = _drop_spurs(nodes, edges, g)

    # INVARIANTE. Un recorrido es un camino: cada par de nodos consecutivos tiene
    # su arista. Romperla no daba un resultado raro, daba un 500 doscientas líneas
    # más abajo, al intentar medir la ruta más corta posible sobre dos arrays de
    # distinta longitud. Se comprueba aquí, que es donde se puede explicar.
    if edges and len(edges) != len(nodes) - 1:
        return {"ok": False,
                "error": "El emparejado produjo un camino con saltos "
                         f"({len(nodes)} nodos, {len(edges)} tramos). "
                         "Es un fallo del motor; por favor, repórtalo."}

    if len(nodes) < 3 or not edges:
        if sin_cand >= max(1, len(thin) - 1):
            return {"ok": False,
                    "error": "Ningún punto de la traza cae sobre esta región. "
                             "¿Es la ciudad correcta?"}
        return {"ok": False,
                "error": "No se pudo enganchar la traza a ninguna calle utilizable."}

    total = len(thin)
    enganchados = total - sin_cand
    # La salida de un recorrido importado es donde empieza SU traza, no donde el
    # usuario haya pinchado la suya. Sin esto la importación falla en cuanto se
    # hace antes de fijar una salida, que es justo cuando más sentido tiene.
    model = _rebase_start(model, g, nodes)
    state = draw_state(ctx, model, nodes, edges, closed=(nodes[0] == nodes[-1]))
    state["ok"] = True
    state["imported"] = {
        "points_in": len(pts),
        "points_used": total,
        "anchors": len(nodes),
        "unmatched_points": sin_cand,
        "gaps": cortes,
        "bridged": puentes,
        "dropped_m": round(descartado, 1),
        "segments": len(tramos),
        "matched_pct": round(100.0 * enganchados / max(total, 1), 1),
        "deviation_m": _deviation(g, pts, nodes),
        "raw_length_m": round(_raw_length(g, pts), 1),
    }
    # La traza tal cual venía, para dibujarla debajo del enganche. Se adelgaza a
    # 15 m: más resolución que el emparejado, pero sin mandar 40.000 puntos.
    raw = _thin(g, pts, 15.0)
    state["raw_coordinates"] = [[round(lo, 6), round(la, 6)] for la, lo in raw]
    return state


def _rebase_start(model: ConstraintModel, g, nodes: list[int]) -> ConstraintModel:
    """Devuelve el modelo con la salida —y la meta si es punto a punto— en la traza."""
    from raceengine_contracts import GeoPoint, RouteShape

    a, b = nodes[0], nodes[-1]
    upd: dict = {"start": GeoPoint(lat=float(g.lat[a]), lon=float(g.lon[a]))}
    if model.routetype.shape == RouteShape.OPEN:
        upd["finish"] = GeoPoint(lat=float(g.lat[b]), lon=float(g.lon[b]))
    return model.model_copy(update=upd)


def _raw_length(g, pts: list[tuple[float, float]]) -> float:
    if len(pts) < 2:
        return 0.0
    xs, ys = zip(*(g.project(la, lo) for la, lo in pts))
    dx = np.diff(np.asarray(xs))
    dy = np.diff(np.asarray(ys))
    return float(np.hypot(dx, dy).sum())


def _deviation(g, pts: list[tuple[float, float]], nodes: list[int]) -> float:
    """Distancia media entre la traza original y el recorrido enganchado."""
    if not pts or len(nodes) < 2:
        return 0.0
    n = np.asarray(nodes, dtype=np.int64)
    rx, ry = g.x[n], g.y[n]
    step = max(1, len(pts) // 300)
    tot, cnt = 0.0, 0
    for lat, lon in pts[::step]:
        px, py = g.project(lat, lon)
        d2 = (rx - px) ** 2 + (ry - py) ** 2
        tot += math.sqrt(float(d2.min()))
        cnt += 1
    return round(tot / max(cnt, 1), 1)
