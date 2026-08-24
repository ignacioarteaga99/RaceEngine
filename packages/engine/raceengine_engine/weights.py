"""Traducción ConstraintModel → RoutingWeights.

Es el equivalente Python de un *custom model* de GraphHopper (doc 11 §6.3): las
restricciones duras se vuelven una máscara booleana y las blandas, multiplicadores
sobre la longitud de la arista.

Todo esto se calcula UNA VEZ por petición, no dentro del bucle de A*. En el MVP,
`edge_allowed()` reevaluaba las mismas condiciones geométricas millones de veces
(doc 10 §2, B2).
"""
from __future__ import annotations

import numpy as np

from raceengine_contracts import (
    CYCLING, DISCIPLINE_CLASSES, ConstraintModel, CustomZone, GeoJSONPoint,
    Surface, point_in_ring,
)
from raceengine_geodata import ALL_CLASSES, RaceGraph
from raceengine_routing import RoutingWeights

# Índices de `eclass` admisibles por disciplina, resueltos una vez al importar.
_CLASS_IDX = {c: i for i, c in enumerate(ALL_CLASSES)}
DISCIPLINE_IDS: dict[str, np.ndarray] = {
    d: np.array(sorted(_CLASS_IDX[c] for c in cs if c in _CLASS_IDX), dtype=np.int8)
    for d, cs in DISCIPLINE_CLASSES.items()
}


def discipline_edge_mask(g: RaceGraph, discipline: str) -> np.ndarray:
    """bool[E] — aristas que existen para esta disciplina.

    Es la restricción más dura del sistema, y la única que decide sobre qué mundo
    se busca. Un grafo construido con la versión anterior sólo contiene clases de
    correr; para esas aristas esta máscara no quita nada en `running` y deja muy
    poco en `road`, que es exactamente lo que debe pasar: hay que reconstruir la
    región para poder rodar por ella.
    """
    ids = DISCIPLINE_IDS.get(str(discipline), DISCIPLINE_IDS["running"])
    return np.isin(g.eclass, ids)


def node_mask_for_zone(g: RaceGraph, zone: CustomZone) -> np.ndarray:
    """bool[N] — nodos dentro de la zona."""
    if isinstance(zone.geometry, GeoJSONPoint):
        zx, zy = g.project(zone.geometry.lat, zone.geometry.lon)
        d2 = (g.x - zx) ** 2 + (g.y - zy) ** 2
        return d2 <= max(zone.buffer_m, 1.0) ** 2

    ring = zone.geometry.exterior
    if len(ring) < 3:
        return np.zeros(g.n_nodes, dtype=bool)
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    # Pre-filtro por bbox (barato) y ray casting solo sobre los candidatos.
    bbox = (
        (g.lon >= min(lons)) & (g.lon <= max(lons))
        & (g.lat >= min(lats)) & (g.lat <= max(lats))
    )
    out = np.zeros(g.n_nodes, dtype=bool)
    for i in np.nonzero(bbox)[0]:
        if point_in_ring(float(g.lon[i]), float(g.lat[i]), ring):
            out[i] = True
    if zone.buffer_m > 0 and out.any():
        # Dilatación aproximada del polígono por el buffer.
        idx = np.nonzero(out)[0]
        for i in idx:
            near = g.grid.within(float(g.x[i]), float(g.y[i]), zone.buffer_m)
            out[near] = True
    return out


# Margen sobre la distancia en línea recta al punto más lejano. Una carretera de
# montaña no va en recta: un 40 % es lo que suele sobrar para dejarla pasar.
FREE_RADIUS_MARGIN = 1.40
FREE_RADIUS_FLOOR_M = 3000.0


def effective_radius_m(g: RaceGraph, model: ConstraintModel, start_node: int) -> float:
    """Radio de búsqueda que se aplica de verdad.

    En modo objetivo es el que puso el usuario: acota el problema y es parte del
    diseño —un circuito de 10 km no debe irse a veinte kilómetros de la salida—.

    En modo LIBRE es otra cosa, y aquí estaba el fallo: el radio recortaba la red
    ANTES de llegar a la meta. Con el valor por defecto de 4 km —que además vive
    en modo avanzado y por tanto está oculto— una ruta a un punto a veinticinco
    kilómetros se quedaba a las afueras, y el resultado se presentaba como
    terminado. Un recorrido truncado que dice «listo» es peor que un error.

    Un radio no es una restricción de diseño cuando lo que se pide es ir de A a B:
    los extremos SON el problema. Así que en modo libre el radio se estira solo
    hasta cubrir la meta y todos los puntos de paso, con margen para el rodeo.
    """
    radio = float(model.geometry.search_radius_max_m)
    if not model.distance.is_free:
        return radio

    sx, sy = float(g.x[start_node]), float(g.y[start_node])
    lejos = 0.0
    puntos = []
    if model.finish is not None:
        puntos.append((model.finish.lat, model.finish.lon))
    for z in model.mandatory_zones:
        puntos.append(z.center)
    for lat, lon in puntos:
        px, py = g.project(lat, lon)
        lejos = max(lejos, float(np.hypot(px - sx, py - sy)))
    if lejos <= 0:
        return radio
    return max(radio, lejos * FREE_RADIUS_MARGIN + FREE_RADIUS_FLOOR_M)


def build_weights(g: RaceGraph, model: ConstraintModel, esrc: np.ndarray,
                  start_node: int) -> tuple[RoutingWeights, dict]:
    """Devuelve (pesos, diagnóstico)."""
    n_e = g.n_edges
    dst = g.indices

    # ── 1. Restricciones DURAS → máscara de aristas ──────────────────────────
    # La DISCIPLINA va primero. El grafo guarda el superconjunto de vías —desde
    # una nacional hasta una senda— y aquí se recorta al mundo del deporte que se
    # está diseñando. Antes este recorte estaba congelado en el momento de
    # construir la región, y por eso un grafo servía para una sola cosa.
    disciplina = getattr(model.discipline, "value", model.discipline)
    allowed = discipline_edge_mask(g, disciplina)
    es_bici = disciplina in CYCLING

    if model.streets.avoid_private:
        allowed &= ~g.eprivate
    if model.experience.avoid_tunnels:
        allowed &= ~g.etunnel

    allowed_surfaces = set(model.streets.allowed_surfaces)
    unpaved_ok = bool({Surface.GRAVEL, Surface.DIRT, Surface.COBBLE} & allowed_surfaces)
    # En gravel y MTB el firme sin asfaltar no es una concesión: es el terreno.
    # Exigir pavimento ahí dejaría la disciplina sin sentido.
    if disciplina in ("gravel", "mtb"):
        unpaved_ok = True
    if not unpaved_ok:
        allowed &= g.epaved

    # Radio de búsqueda (L4, y además acota el problema: es una palanca de NFR-1).
    sx, sy = float(g.x[start_node]), float(g.y[start_node])
    d_start = np.sqrt((g.x - sx) ** 2 + (g.y - sy) ** 2)
    node_in_radius = d_start <= effective_radius_m(g, model, start_node)
    allowed &= node_in_radius[esrc] & node_in_radius[dst]

    # Zonas prohibidas: se comprueban AMBOS extremos de la arista.
    # (El MVP solo miraba `e.v`, de modo que una arista podía atravesar la zona.)
    forbidden_nodes = np.zeros(g.n_nodes, dtype=bool)
    for z in model.forbidden_zones:
        forbidden_nodes |= node_mask_for_zone(g, z)
    if forbidden_nodes.any():
        allowed &= ~forbidden_nodes[esrc] & ~forbidden_nodes[dst]

    # Pendiente máxima como restricción dura de arista (L3).
    if model.elevation.enabled and model.elevation.max_gradient_pct is not None:
        allowed &= np.abs(g.egrad) <= float(model.elevation.max_gradient_pct)

    # ── 2. Restricciones BLANDAS → multiplicadores ───────────────────────────
    mult = np.ones(n_e, dtype=np.float32)

    if model.logistics.prioritize_wide_streets:
        # Menor coste ⇒ preferida. Las vías anchas son mejores para una carrera
        # (capacidad, cierres más limpios) pero peores para el tráfico: el
        # equilibrio real lo decide `idx.logistics`, aquí solo se sesga la búsqueda.
        wide = np.isin(g.eclass, [np.int8(i) for i in (2, 3, 4, 5, 6, 7)])
        mult[wide] *= np.float32(0.93)
    if not model.logistics.minimize_closures:
        pass
    else:
        # Cerrar una primaria cuesta más: penalización proporcional al peso de cierre.
        mult *= (1.0 + 0.06 * (g.eclosure - 1.0)).astype(np.float32)

    if not unpaved_ok:
        mult[~g.epaved] *= np.float32(1.8)

    if model.experience.prioritize_parks:
        mult[g.epark] *= np.float32(0.80)

    if es_bici:
        # Preferencias propias de ir en bici, que son casi las contrarias de las
        # de correr: la nacional es rápida pero desagradable y peligrosa, la vía
        # ciclista es lo mejor que hay, y en gravel el camino de tierra no es un
        # mal menor sino el sitio al que se ha venido.
        def _idx(*names):
            return [np.int8(_CLASS_IDX[n]) for n in names if n in _CLASS_IDX]

        mult[np.isin(g.eclass, _idx("trunk", "trunk_link"))] *= np.float32(1.55)
        mult[np.isin(g.eclass, _idx("primary", "primary_link"))] *= np.float32(1.18)
        mult[np.isin(g.eclass, _idx("cycleway"))] *= np.float32(0.78)
        if disciplina in ("gravel", "mtb"):
            mult[np.isin(g.eclass, _idx("track", "path", "bridleway"))] *= np.float32(0.82)
        else:
            # En ruta, la tierra se evita: es asfalto o no es.
            mult[~g.epaved] *= np.float32(2.5)

    if model.elevation.enabled and model.elevation.prefer_flatter:
        # Penaliza la subida más que la bajada (asimetría real de una carrera).
        # En bici la asimetría es MENOR: se sube más despacio pero se recupera
        # bajando, así que castigar la subida como en una carrera a pie llevaba a
        # rodear cualquier repecho por un desvío de kilómetros.
        k_up = 0.030 if es_bici else 0.055
        k_down = 0.004 if es_bici else 0.012
        up = np.maximum(g.egrad, 0.0)
        down = np.maximum(-g.egrad, 0.0)
        mult *= (1.0 + k_up * up + k_down * down).astype(np.float32)

    # Desnivel BUSCADO: aquí la subida deja de ser un coste y pasa a ser el
    # objetivo. Sin esto, pedir 2.500 m de desnivel y dejar activada la
    # preferencia por lo llano son dos órdenes contradictorias, y ganaba la
    # segunda porque actúa dentro de la búsqueda.
    el = model.elevation
    if el.enabled and el.target_gain_m:
        up = np.maximum(g.egrad, 0.0)
        mult *= (1.0 / (1.0 + 0.030 * up)).astype(np.float32)

    if model.intersections.penalize_crossings:
        # Coste extra por entrar en una intersección semaforizada.
        signal_dst = g.is_signal[dst]
        mult[signal_dst] *= np.float32(1.12)

    if model.geometry.smoothness > 0.5:
        # Sesga hacia vías de orden superior, que suelen ser más rectas.
        minor = np.isin(g.eclass, [np.int8(0), np.int8(1)])
        mult[minor] *= np.float32(1.0 + 0.25 * (model.geometry.smoothness - 0.5))

    # ── 3. El OBJETIVO, cuando es lo único que decide ────────────────────────
    # En modo libre no hay distancia objetivo, así que lo único que separa una
    # ruta de otra entre los mismos dos puntos es el objetivo. Y hasta ahora el
    # objetivo sólo tocaba los PESOS DE LA PUNTUACIÓN, no los de la búsqueda:
    # elegir «más llana» cambiaba el número del panel y devolvía exactamente el
    # mismo camino. Eso no es una preferencia, es una etiqueta.
    #
    # Se aplica sólo en modo libre a propósito. En modo objetivo el camino lo
    # decide sobre todo cuadrar los kilómetros, y meter aquí un sesgo fuerte
    # cambiaría el comportamiento de todos los recorridos ya diseñados.
    if model.distance.is_free:
        obj = getattr(model.objective, "value", model.objective)
        up = np.maximum(g.egrad, 0.0)
        down = np.maximum(-g.egrad, 0.0)
        if obj == "flattest":
            mult *= (1.0 + 0.14 * up + 0.05 * down).astype(np.float32)
        elif obj == "climbing":
            mult *= (1.0 / (1.0 + 0.08 * up)).astype(np.float32)
        elif obj == "fastest":
            # Lo más corto y con menos interrupciones. Se anula el resto de
            # sesgos blandos: pedir «la más rápida» y recibir un rodeo por un
            # parque es no haber escuchado.
            mult = np.ones(n_e, dtype=np.float32)
            mult[g.is_signal[dst]] *= np.float32(1.20)
        elif obj == "fewest_closures":
            # Calle menor, aunque sea más larga: es lo que abarata el corte.
            mult *= (1.0 + 0.22 * (g.eclosure - 1.0)).astype(np.float32)
        elif obj == "most_scenic":
            mult[g.epark] *= np.float32(0.62)
            grandes = np.isin(g.eclass, [np.int8(_CLASS_IDX[c])
                                         for c in ("primary", "trunk", "secondary")
                                         if c in _CLASS_IDX])
            mult[grandes] *= np.float32(1.35)

    weight = (g.elen * mult).astype(np.float32)

    ok = allowed & (g.elen > 0)
    min_ratio = float(np.min(mult[ok])) if ok.any() else 1.0
    min_ratio = max(min_ratio, 0.05)
    max_ratio = float(np.max(mult[ok])) if ok.any() else 1.0
    max_ratio = max(max_ratio, 1.0)

    diag = {
        "edges_total": int(n_e),
        "edges_allowed": int(allowed.sum()),
        "nodes_in_radius": int(node_in_radius.sum()),
        "forbidden_nodes": int(forbidden_nodes.sum()),
        "min_ratio": round(min_ratio, 4),
        "max_ratio": round(max_ratio, 4),
    }
    return RoutingWeights(allowed_edges=allowed, weight=weight,
                          min_ratio=min_ratio, max_ratio=max_ratio), diag
