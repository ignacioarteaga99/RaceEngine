"""Medición del recorrido: ruta más corta posible, anchura y capacidad.

Por qué esto importa
────────────────────
Un recorrido oficial NO se mide por el eje de la calzada. Se mide por la **ruta
más corta posible** (SPR, *shortest possible route*): la línea que un corredor
puede tomar legalmente, cortando las tangentes en cada curva y pegado al bordillo.
Sobre esa línea el medidor oficial aplica además el **factor de prevención de
recorrido corto** de 1,001 — busca 42.237 m para certificar un maratón de 42.195,
de modo que ni el bamboleo de la bicicleta ni un esquive de un bache dejen el
recorrido por debajo de la distancia anunciada.

La consecuencia es incómoda y hay que decirla: medir por el eje del grafo, que es
lo que hace cualquier herramienta de rutas, **sobreestima** sistemáticamente. En
un recorrido urbano con cientos de curvas la diferencia son cientos de metros, y
un maratón que en pantalla dice 42,195 km mediría corto el día de la
certificación. Un maratón que mide corto no es un maratón: los tiempos no valen.

Cómo se calcula la SPR aquí
───────────────────────────
El problema es clásico: la polilínea más corta que atraviesa una secuencia de
«puertas». Cada vértice del recorrido puede desplazarse lateralmente hasta
`ancho/2 − holgura_bordillo`; se minimiza la longitud total sobre ese conjunto
convexo. Se resuelve por relajación (descenso proyectado, vectorizado), que para
esta geometría converge en unas decenas de pasadas.

La anchura es una ESTIMACIÓN por clase de vía. OpenStreetMap etiqueta `width` en
una minoría ínfima de las calles — en Santiago, el 0,5 % — así que fingir un dato
que no existe sería peor que estimarlo y decirlo. La estimación se declara con
confianza baja y todos los números derivados la heredan.
"""
from __future__ import annotations

import math

import numpy as np

from raceengine_geodata import ALL_CLASSES

# ── Anchura estimada de calzada por clase de vía, en metros ─────────────────
# Valores de planificación urbana típicos. NO son medidas: son el punto de
# partida cuando OSM no trae `width`, que es casi siempre.
CLASS_WIDTH_M: dict[str, float] = {
    "primary": 14.0, "primary_link": 7.0,
    "secondary": 10.0, "secondary_link": 6.5,
    "tertiary": 8.0, "tertiary_link": 6.0,
    "unclassified": 7.0, "residential": 6.5,
    "living_street": 5.0, "pedestrian": 5.0,
    # Clases de bici. Un camino agrícola o una senda no admiten un pelotón a lo
    # ancho, y ése es justo el número que la capacidad tiene que reflejar.
    "trunk": 12.0, "trunk_link": 7.0,
    "service": 4.5, "cycleway": 3.0,
    "track": 3.0, "path": 1.5, "bridleway": 2.0,
    "footway": 2.5, "steps": 1.5,
}
DEFAULT_WIDTH_M = 7.0

# El corredor no corre por el bordillo: corre a un palmo de él.
KERB_CLEARANCE_M = 0.30
# Factor de prevención de recorrido corto (World Athletics / AIMS).
SHORT_COURSE_PREVENTION_FACTOR = 1.001

# ── Supuestos de capacidad, declarados ──────────────────────────────────────
# Flujo = densidad × velocidad × anchura. Con densidad cómoda de carrera y ritmo
# de pelotón medio. Ambos se exponen para que el usuario pueda discutirlos.
PACK_DENSITY_RUNNERS_M2 = 0.8      # corredores por m² en pelotón cómodo
PACK_SPEED_M_MIN = 167.0           # ≈ 10 km/h
# Anchura efectivamente utilizable: nunca se corre pared a pared.
USABLE_WIDTH_FRACTION = 0.85
# Tiempo de referencia para dimensionar: minutos que se considera aceptable que
# tarde el pelotón entero en desfilar por el punto más estrecho. Es una elección
# de producto, no una norma; se declara para poder discutirla.
CLEAR_TARGET_MIN = 10.0


def edge_width_m(g, edges: np.ndarray) -> np.ndarray:
    """Anchura estimada de cada arista, en metros."""
    if not len(edges):
        return np.zeros(0, dtype=np.float32)
    table = np.array(
        [CLASS_WIDTH_M.get(c, DEFAULT_WIDTH_M) for c in ALL_CLASSES],
        dtype=np.float32,
    )
    cls = g.eclass[edges]
    out = np.full(len(edges), DEFAULT_WIDTH_M, dtype=np.float32)
    ok = cls >= 0
    out[ok] = table[cls[ok]]
    return out


def lateral_allowance(g, nodes: list[int], edges: list[int]) -> np.ndarray:
    """Cuánto puede apartarse del eje cada vértice, en metros.

    En un vértice se aplica la anchura MENOR de las dos aristas que lo tocan: el
    corredor no puede cortar por donde la calle se estrecha.
    """
    e = np.asarray(edges, dtype=np.int64)
    w = edge_width_m(g, e) / 2.0 - KERB_CLEARANCE_M
    w = np.maximum(w, 0.0)
    n = len(nodes)
    out = np.zeros(n, dtype=np.float64)
    if n < 2 or not len(w):
        return out
    # Un camino conexo tiene exactamente n−1 aristas. Si llega otra cosa, esto no
    # es el sitio donde hay que enterarse —quien construyó el camino lo es—, pero
    # tampoco es el sitio donde reventar: se recorta y se sigue midiendo.
    if len(w) < n - 1:
        w = np.concatenate([w, np.zeros(n - 1 - len(w))])
    elif len(w) > n - 1:
        w = w[:n - 1]
    # Vértices interiores: mínimo de las aristas entrante y saliente.
    out[1:n - 1] = np.minimum(w[:n - 2], w[1:n - 1])
    # Los extremos son la salida y la meta: están fijados, no se cortan.
    out[0] = 0.0
    out[-1] = 0.0
    return out


def _polyline_length(px: np.ndarray, py: np.ndarray) -> float:
    return float(np.hypot(np.diff(px), np.diff(py)).sum())


def shortest_possible_route(x: np.ndarray, y: np.ndarray, allow: np.ndarray,
                            iterations: int = 60) -> tuple[float, np.ndarray, np.ndarray]:
    """Polilínea más corta dentro del corredor. Devuelve (longitud, px, py).

    Cada vértice se mueve sobre un segmento perpendicular a la bisectriz local,
    de semilongitud `allow[i]`. El problema es convexo (suma de normas sobre un
    producto de segmentos), así que un descenso proyectado con paso decreciente
    converge sin sorpresas.
    """
    n = len(x)
    if n < 3:
        return _polyline_length(x, y), x.copy(), y.copy()

    # Dirección de desplazamiento: perpendicular a la cuerda entre los vecinos.
    # Es estable incluso en vértices casi rectos, donde la bisectriz es ambigua.
    dx = np.zeros(n)
    dy = np.zeros(n)
    dx[1:-1] = x[2:] - x[:-2]
    dy[1:-1] = y[2:] - y[:-2]
    norm = np.hypot(dx, dy)
    norm[norm < 1e-9] = 1.0
    ux, uy = -dy / norm, dx / norm          # perpendicular unitaria

    t = np.zeros(n)
    step = float(np.max(allow)) if len(allow) else 0.0
    if step <= 0:
        return _polyline_length(x, y), x.copy(), y.copy()

    for _ in range(iterations):
        px, py = x + t * ux, y + t * uy
        # Gradiente de |p_i − p_{i−1}| + |p_{i+1} − p_i| respecto de t_i.
        ax, ay = px[1:-1] - px[:-2], py[1:-1] - py[:-2]
        bx, by = px[1:-1] - px[2:], py[1:-1] - py[2:]
        la = np.hypot(ax, ay)
        lb = np.hypot(bx, by)
        la[la < 1e-9] = 1e-9
        lb[lb < 1e-9] = 1e-9
        gx = ax / la + bx / lb
        gy = ay / la + by / lb
        grad = gx * ux[1:-1] + gy * uy[1:-1]
        t[1:-1] = np.clip(t[1:-1] - step * grad, -allow[1:-1], allow[1:-1])
        step *= 0.93

    px, py = x + t * ux, y + t * uy
    return _polyline_length(px, py), px, py


def route_measurement(g, nodes: list[int], edges: list[int]) -> dict:
    """Longitud por el eje, longitud SPR y el ahorro entre ambas."""
    if len(nodes) < 2 or not edges:
        return {"centerline_m": 0.0, "spr_m": 0.0, "saving_m": 0.0}
    n = np.asarray(nodes, dtype=np.int64)
    x, y = g.x[n].astype(np.float64), g.y[n].astype(np.float64)
    allow = lateral_allowance(g, nodes, edges)
    centerline = float(g.elen[np.asarray(edges, dtype=np.int64)].sum())
    spr, _, _ = shortest_possible_route(x, y, allow)
    # La SPR no puede ser más larga que el eje; si el suavizado numérico lo
    # sugiere, es ruido y se ignora.
    spr = min(spr, centerline)
    return {"centerline_m": centerline, "spr_m": spr,
            "saving_m": max(0.0, centerline - spr)}


def corner_saving_estimate(turn_deg: float, allowance_m: float) -> float:
    """Metros que ahorra cortar UNA curva. Cerrado, para explicar el orden de
    magnitud sin resolver la relajación: `ahorro ≈ 2·d·sin(θ/2)`."""
    return 2.0 * allowance_m * math.sin(math.radians(turn_deg) / 2.0)


# ── Capacidad ────────────────────────────────────────────────────────────────
def capacity(g, nodes: list[int], edges: list[int],
             field_size: int | None) -> dict:
    """Cuello de botella del recorrido y tiempo de paso del pelotón.

    `flujo = densidad × velocidad × anchura útil`. Los tres supuestos están
    declarados arriba como constantes para que se puedan discutir; el número que
    sale de aquí es aritmética sobre ellos, no una tabla mágica.
    """
    e = np.asarray(edges, dtype=np.int64)
    if not len(e):
        return {"min_width_m": 0.0, "bottleneck_m": 0.0, "flow_per_min": 0.0,
                "field_clear_min": 0.0, "width_needed_m": 0.0}
    w = edge_width_m(g, e)
    i = int(np.argmin(w))
    min_w = float(w[i])
    at_m = float(g.elen[e[:i]].sum()) if i else 0.0
    flow = (PACK_DENSITY_RUNNERS_M2 * PACK_SPEED_M_MIN
            * min_w * USABLE_WIDTH_FRACTION)
    clear = (field_size / flow) if (field_size and flow > 0) else 0.0
    # Anchura que haría falta para que el pelotón entero pase por el punto más
    # estrecho en el tiempo de referencia. Es la lectura que un organizador puede
    # usar: «necesito 2,6 m y tengo 6,5» dice mucho más que «flujo 615/min».
    por_metro = PACK_DENSITY_RUNNERS_M2 * PACK_SPEED_M_MIN * USABLE_WIDTH_FRACTION
    needed = (field_size / (por_metro * CLEAR_TARGET_MIN)) if field_size else 0.0
    return {
        "min_width_m": min_w,
        "bottleneck_m": at_m,
        "flow_per_min": flow,
        "field_clear_min": clear,
        "width_needed_m": needed,
    }
