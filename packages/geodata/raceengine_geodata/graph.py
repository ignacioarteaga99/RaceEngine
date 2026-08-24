"""RaceGraph — grafo vial en formato CSR sobre arrays de numpy.

Decisiones que corrigen los cuellos de botella B1/B2/B4 de la auditoría (doc 10 §2):

  · CSR (indptr/indices) en vez de dict-de-listas-de-dataclasses.
  · Coordenadas PROYECTADAS a metros locales una sola vez. Ningún haversine en el
    bucle interno de A*.
  · Máscaras de nodo/arista admisibles PRE-CALCULADAS por petición. `edge_allowed`
    del MVP hacía 2+ haversines por relajación de arista; aquí es un acceso a bool.
  · Índice espacial de rejilla con iteración de perímetro correcta (el MVP iteraba
    el cuadrado completo por anillo → O(R³)).
  · Elevación HORNEADA en las aristas al construir el grafo, de modo que influye en
    la búsqueda y no solo en el informe final (corrige H1).
"""
from __future__ import annotations

import math
import pickle
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# Clases de vía utilizables por una carrera de calle (doc 07 §6.3.1).
# ~40% de las vías de OSM en Las Condes; el resto son service/footway/steps/path.
#
# ⚠ EL ORDEN DE ESTA TUPLA ES UN FORMATO EN DISCO. `eclass` guarda el ÍNDICE en
# esta tupla dentro del pickle de cada región. Insertar o reordenar aquí
# reinterpretaría en silencio los grafos ya construidos: una residencial pasaría
# a ser un sendero. Las clases nuevas se AÑADEN AL FINAL, nunca en medio.
RACE_CLASSES: tuple[str, ...] = (
    "residential", "living_street", "tertiary", "secondary", "primary",
    "tertiary_link", "secondary_link", "primary_link", "unclassified", "pedestrian",
)

# Clases que no sirven para correr pero sí para rodar, o al revés. Se añaden
# DESPUÉS de las diez originales, así que un grafo construido con la versión
# anterior sigue leyéndose exactamente igual: sus índices son 0–9 y significan lo
# mismo. Un grafo nuevo trae además éstas, y es la DISCIPLINA la que decide en
# cada petición cuáles se pueden usar.
#
# `motorway` no está ni estará: no se corre ni se pedalea por una autopista.
EXTRA_CLASSES: tuple[str, ...] = (
    "trunk", "trunk_link",       # carretera nacional: ruta sí, correr casi nunca
    "service",                   # accesos y conexiones, útiles en gravel
    "track",                     # camino agrícola — el pan del gravel
    "path", "bridleway",         # sendas — MTB
    "cycleway",                  # vía ciclista segregada
    "footway", "steps",          # peatonal estricta; `steps` nunca para bici
)
ALL_CLASSES: tuple[str, ...] = RACE_CLASSES + EXTRA_CLASSES
CLASS_ID = {c: i for i, c in enumerate(ALL_CLASSES)}

# Peso relativo de cierre por clase de vía: cerrar una primaria cuesta más que una
# residencial. Proxy de `logistics.minimize_vehicular_impact` (doc 04 L7).
CLOSURE_WEIGHT = {
    "primary": 3.0, "primary_link": 3.0,
    "secondary": 2.2, "secondary_link": 2.2,
    "tertiary": 1.6, "tertiary_link": 1.6,
    "unclassified": 1.1, "residential": 1.0,
    "living_street": 0.8, "pedestrian": 0.5,
    # Clases de bici. Una nacional es lo más caro de cortar que hay; un camino
    # agrícola o una senda no se «cortan» en ningún sentido útil.
    "trunk": 3.5, "trunk_link": 3.5,
    "service": 0.6, "cycleway": 0.4,
    "track": 0.3, "path": 0.2, "bridleway": 0.2,
    "footway": 0.3, "steps": 0.3,
}

SURFACE_PAVED = {"paved", "asphalt", "concrete", "concrete:plates", "paving_stones"}
SURFACE_UNPAVED = {"gravel", "dirt", "ground", "unpaved", "sand", "compacted", "fine_gravel"}


@dataclass
class RaceGraph:
    """Grafo no dirigido en CSR. Todas las longitudes en metros."""

    # Nodos
    node_ids: np.ndarray          # int64[N]  id OSM original
    lat: np.ndarray               # float64[N]
    lon: np.ndarray               # float64[N]
    x: np.ndarray                 # float64[N]  metros locales este
    y: np.ndarray                 # float64[N]  metros locales norte
    elev: np.ndarray              # float32[N]
    is_signal: np.ndarray         # bool[N]
    is_hospital: np.ndarray       # bool[N]

    # Aristas (CSR, ambos sentidos)
    indptr: np.ndarray            # int32[N+1]
    indices: np.ndarray           # int32[E]   nodo destino
    elen: np.ndarray              # float32[E] longitud
    eclass: np.ndarray            # int8[E]    índice en ALL_CLASSES, -1 = no apta
    egrad: np.ndarray             # float32[E] pendiente % (con signo)
    epaved: np.ndarray            # bool[E]
    elit: np.ndarray              # bool[E]
    epark: np.ndarray             # bool[E]
    eclosure: np.ndarray          # float32[E] peso de cierre
    eprivate: np.ndarray          # bool[E]
    etunnel: np.ndarray           # bool[E]

    # Metadatos
    version: str = ""
    origin_lat: float = 0.0
    origin_lon: float = 0.0
    coverage: dict = field(default_factory=dict)   # cobertura por atributo (doc 07 §4.1)

    # Derivados perezosos
    _degree: np.ndarray | None = None
    _grid: "GridIndex | None" = None
    _reverse: np.ndarray | None = None

    # ── Propiedades ──────────────────────────────────────────────────────────
    @property
    def n_nodes(self) -> int:
        return len(self.lat)

    @property
    def n_edges(self) -> int:
        return len(self.indices)

    @property
    def degree(self) -> np.ndarray:
        if self._degree is None:
            self._degree = (self.indptr[1:] - self.indptr[:-1]).astype(np.int32)
        return self._degree

    @property
    def is_intersection(self) -> np.ndarray:
        return self.degree >= 3

    # ── Proyección ───────────────────────────────────────────────────────────
    def project(self, lat: float, lon: float) -> tuple[float, float]:
        """WGS84 → metros locales (equirrectangular en torno al origen)."""
        k = math.cos(math.radians(self.origin_lat))
        return (
            math.radians(lon - self.origin_lon) * 6371008.8 * k,
            math.radians(lat - self.origin_lat) * 6371008.8,
        )

    def unproject(self, x: float, y: float) -> tuple[float, float]:
        k = math.cos(math.radians(self.origin_lat))
        return (
            self.origin_lat + math.degrees(y / 6371008.8),
            self.origin_lon + math.degrees(x / (6371008.8 * k)),
        )

    # ── Índice espacial ──────────────────────────────────────────────────────
    @property
    def grid(self) -> "GridIndex":
        if self._grid is None:
            self._grid = GridIndex(self.x, self.y, cell=150.0)
        return self._grid

    def nearest_node(self, lat: float, lon: float, mask: np.ndarray | None = None) -> int:
        x, y = self.project(lat, lon)
        return self.grid.nearest(x, y, mask)

    # ── Arista inversa ───────────────────────────────────────────────────────
    @property
    def reverse(self) -> np.ndarray:
        """int64[E] — índice de la arista contraria, o el propio si no la hay.

        Vive AQUÍ, colgada del grafo, y no en un diccionario del módulo que la
        usa. Estaba cacheada en dos sitios distintos con `id(grafo)` de clave, y
        eso es un error latente de los feos: Python reutiliza direcciones de
        memoria en cuanto el recolector libera un objeto, así que un grafo nuevo
        podía caer en el `id` de uno anterior ya liberado y heredar su tabla. Con
        suerte reventaba con un IndexError —que es como se descubrió—; sin suerte,
        devolvía kilómetros de cierre calculados sobre las aristas de otro mapa.

        Además se calcula vectorizado: la versión anterior recorría las aristas en
        Python con un diccionario, ~1 s por cada 300.000 aristas.
        """
        if self._reverse is None:
            src = np.repeat(np.arange(self.n_nodes, dtype=np.int64),
                            np.diff(self.indptr).astype(np.int64))
            dst = self.indices.astype(np.int64)
            n = np.int64(self.n_nodes)
            ida = src * n + dst
            vuelta = dst * n + src
            # Buscar cada (dst, src) entre los (src, dst) ordenados.
            orden = np.argsort(ida, kind="stable")
            pos = np.searchsorted(ida[orden], vuelta)
            pos = np.clip(pos, 0, len(orden) - 1)
            cand = orden[pos]
            ok = ida[cand] == vuelta
            rev = np.arange(self.n_edges, dtype=np.int64)
            rev[ok] = cand[ok]
            self._reverse = rev
        return self._reverse

    # ── Persistencia ─────────────────────────────────────────────────────────
    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
        with path.open("wb") as f:
            pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)

    @classmethod
    def load(cls, path: Path) -> "RaceGraph":
        with path.open("rb") as f:
            payload = pickle.load(f)
        return cls(**payload)


class GridIndex:
    """Rejilla uniforme sobre coordenadas proyectadas.

    Itera SOLO el perímetro de cada anillo (el MVP recorría el cuadrado completo y
    descartaba el interior con un `continue`, lo que daba complejidad cúbica).
    """

    def __init__(self, x: np.ndarray, y: np.ndarray, cell: float = 150.0):
        self.x, self.y, self.cell = x, y, cell
        self.ix = np.floor(x / cell).astype(np.int32)
        self.iy = np.floor(y / cell).astype(np.int32)
        self.buckets: dict[tuple[int, int], list[int]] = {}
        for n, (a, b) in enumerate(zip(self.ix.tolist(), self.iy.tolist())):
            self.buckets.setdefault((a, b), []).append(n)

    def _ring_cells(self, ci: int, cj: int, r: int):
        if r == 0:
            yield (ci, cj)
            return
        for d in range(-r, r + 1):
            yield (ci + d, cj - r)
            yield (ci + d, cj + r)
        for d in range(-r + 1, r):
            yield (ci - r, cj + d)
            yield (ci + r, cj + d)

    def nearest(self, x: float, y: float, mask: np.ndarray | None = None,
                max_rings: int = 80) -> int:
        ci, cj = int(math.floor(x / self.cell)), int(math.floor(y / self.cell))
        best, bestd = -1, float("inf")
        for r in range(max_rings):
            found_any = False
            for cell in self._ring_cells(ci, cj, r):
                for n in self.buckets.get(cell, ()):
                    if mask is not None and not mask[n]:
                        continue
                    d = (self.x[n] - x) ** 2 + (self.y[n] - y) ** 2
                    found_any = True
                    if d < bestd:
                        best, bestd = n, d
            # Un anillo de margen garantiza que no hay nada más cerca en diagonal.
            if best >= 0 and math.sqrt(bestd) <= r * self.cell:
                return best
            if best >= 0 and found_any and r > 0:
                continue
        return best

    def within(self, x: float, y: float, radius: float) -> np.ndarray:
        """Índices de nodos dentro del radio (búsqueda vectorizada por bbox)."""
        m = (np.abs(self.x - x) <= radius) & (np.abs(self.y - y) <= radius)
        idx = np.nonzero(m)[0]
        if len(idx) == 0:
            return idx
        d2 = (self.x[idx] - x) ** 2 + (self.y[idx] - y) ** 2
        return idx[d2 <= radius * radius]


class GraphBuilder:
    """Acumula nodos y aristas, y emite un RaceGraph en CSR."""

    def __init__(self, origin_lat: float, origin_lon: float):
        self.origin_lat, self.origin_lon = origin_lat, origin_lon
        self.nodes: dict[int, tuple[float, float]] = {}
        self.signals: set[int] = set()
        self.hospitals: set[int] = set()
        self.parks: set[int] = set()
        self.edges: list[tuple] = []          # (u, v, len, class, paved, lit, park, private, tunnel)
        self._seen: set[tuple[int, int]] = set()

    def add_node(self, nid: int, lat: float, lon: float) -> None:
        self.nodes[nid] = (lat, lon)

    def add_edge(self, u: int, v: int, length: float, highway: str, *,
                 paved: bool = True, lit: bool = False, park: bool = False,
                 private: bool = False, tunnel: bool = False) -> None:
        if (u, v) in self._seen or u == v or length <= 0:
            return
        self._seen.add((u, v))
        self._seen.add((v, u))
        self.edges.append((u, v, length, highway, paved, lit, park, private, tunnel))

    def build(self, version: str = "") -> RaceGraph:
        used: set[int] = set()
        for e in self.edges:
            used.add(e[0])
            used.add(e[1])
        order = sorted(used)
        idx_of = {nid: i for i, nid in enumerate(order)}
        n = len(order)

        lat = np.array([self.nodes[i][0] for i in order], dtype=np.float64)
        lon = np.array([self.nodes[i][1] for i in order], dtype=np.float64)
        k = math.cos(math.radians(self.origin_lat))
        x = np.radians(lon - self.origin_lon) * 6371008.8 * k
        y = np.radians(lat - self.origin_lat) * 6371008.8

        # Duplicar cada arista en ambos sentidos (carrera con calles cerradas).
        src: list[int] = []
        dst: list[int] = []
        attrs: list[tuple] = []
        for u, v, ln, hw, paved, lit, park, priv, tun in self.edges:
            iu, iv = idx_of[u], idx_of[v]
            cls = CLASS_ID.get(hw, -1)
            clo = CLOSURE_WEIGHT.get(hw, 1.0)
            for a, b in ((iu, iv), (iv, iu)):
                src.append(a)
                dst.append(b)
                attrs.append((ln, cls, paved, lit, park, clo, priv, tun))

        src_a = np.array(src, dtype=np.int32)
        order_e = np.argsort(src_a, kind="stable")
        src_a = src_a[order_e]
        dst_a = np.array(dst, dtype=np.int32)[order_e]
        att = [attrs[i] for i in order_e.tolist()]

        indptr = np.zeros(n + 1, dtype=np.int32)
        np.add.at(indptr, src_a + 1, 1)
        np.cumsum(indptr, out=indptr)

        g = RaceGraph(
            node_ids=np.array(order, dtype=np.int64),
            lat=lat, lon=lon, x=x, y=y,
            elev=np.zeros(n, dtype=np.float32),
            is_signal=np.array([i in self.signals for i in order], dtype=bool),
            is_hospital=np.array([i in self.hospitals for i in order], dtype=bool),
            indptr=indptr, indices=dst_a,
            elen=np.array([a[0] for a in att], dtype=np.float32),
            eclass=np.array([a[1] for a in att], dtype=np.int8),
            egrad=np.zeros(len(att), dtype=np.float32),
            epaved=np.array([a[2] for a in att], dtype=bool),
            elit=np.array([a[3] for a in att], dtype=bool),
            epark=np.array([a[4] for a in att], dtype=bool),
            eclosure=np.array([a[5] for a in att], dtype=np.float32),
            eprivate=np.array([a[6] for a in att], dtype=bool),
            etunnel=np.array([a[7] for a in att], dtype=bool),
            version=version,
            origin_lat=self.origin_lat, origin_lon=self.origin_lon,
        )
        return g


def merge_graphs(graphs: list[RaceGraph], version: str = "") -> RaceGraph:
    """Une varios grafos de región en uno solo.

    Por qué hace falta
    ──────────────────
    Una comuna no es una unidad deportiva. Un recorrido de Santiago cruza Las
    Condes, Providencia y Ñuñoa sin enterarse, y con una sola región cargada el
    motor no puede usar las calles de las otras dos: no es que las evite por
    criterio, es que no existen para él. El usuario ve un recorrido plausible y
    unas métricas coherentes, y no tiene forma de saber que se han calculado
    sobre un tercio del mapa que le importa.

    Cómo
    ────
    Los nodos se identifican por su id de OSM, así que la unión es exacta en las
    costuras: un cruce que aparece en dos descargas es UN nodo, no dos. Las
    aristas se deduplican por el par de ids. Todo se reproyecta al origen del
    primer grafo, porque las coordenadas en metros son locales a su origen y
    mezclarlas sin reproyectar desplazaría media ciudad.

    La elevación ya viene horneada en los nodos; se conserva y se recalculan las
    pendientes, sin volver a suavizar (ya se suavizó al construir cada región).
    """
    graphs = [g for g in graphs if g is not None and g.n_nodes]
    if not graphs:
        raise ValueError("No hay grafos que unir.")
    if len(graphs) == 1:
        return graphs[0]

    b = GraphBuilder(graphs[0].origin_lat, graphs[0].origin_lon)
    elev: dict[int, float] = {}
    vistas: set[tuple[int, int]] = set()

    for g in graphs:
        ids = g.node_ids
        src = np.repeat(np.arange(g.n_nodes, dtype=np.int64),
                        np.diff(g.indptr).astype(np.int64))
        dst = g.indices.astype(np.int64)
        # Cada arista no dirigida aparece dos veces en CSR: nos quedamos con una.
        keep = np.nonzero(ids[src] < ids[dst])[0]

        for n in np.nonzero(g.is_signal)[0].tolist():
            b.signals.add(int(ids[n]))
        for n in np.nonzero(g.is_hospital)[0].tolist():
            b.hospitals.add(int(ids[n]))

        lat, lon, ev = g.lat, g.lon, g.elev
        s_l, d_l = src[keep].tolist(), dst[keep].tolist()
        ln = g.elen[keep].tolist()
        cl = g.eclass[keep].tolist()
        pv = g.epaved[keep].tolist()
        lt = g.elit[keep].tolist()
        pk = g.epark[keep].tolist()
        pr = g.eprivate[keep].tolist()
        tu = g.etunnel[keep].tolist()

        for k in range(len(s_l)):
            u, v = s_l[k], d_l[k]
            iu, iv = int(ids[u]), int(ids[v])
            par = (iu, iv)
            if par in vistas:
                continue
            vistas.add(par)
            b.add_node(iu, float(lat[u]), float(lon[u]))
            b.add_node(iv, float(lat[v]), float(lon[v]))
            # Una altitud de 0 es «sin medir», no «a nivel del mar»: no debe
            # ganarle a la medida real que traiga la otra región.
            for nid, node in ((iu, u), (iv, v)):
                e = float(ev[node])
                if e and abs(e) > abs(elev.get(nid, 0.0)):
                    elev[nid] = e
            c = int(cl[k])
            hw = ALL_CLASSES[c] if 0 <= c < len(ALL_CLASSES) else "residential"
            b.add_edge(iu, iv, float(ln[k]), hw, paved=bool(pv[k]), lit=bool(lt[k]),
                       park=bool(pk[k]), private=bool(pr[k]), tunnel=bool(tu[k]))

    merged = b.build(version=version or "+".join(g.version for g in graphs))
    ev = np.array([elev.get(int(n), 0.0) for n in merged.node_ids.tolist()],
                  dtype=np.float64)
    # smooth=False: cada región ya venía suavizada; un segundo pase aplanaría
    # relieve real en vez de quitar artefactos.
    bake_elevation(merged, ev, smooth=False)

    # Cobertura: se suma lo cubierto y el total, y se reevalúan los umbrales.
    attrs: dict[str, list[int]] = {}
    for g in graphs:
        for k, info in (g.coverage or {}).items():
            if not isinstance(info, dict) or "covered" not in info:
                continue
            acc = attrs.setdefault(k, [0, 0])
            acc[0] += int(info.get("covered", 0))
            acc[1] += int(info.get("total", 0))
    merged.coverage = compute_coverage(merged, {k: (a, t) for k, (a, t) in attrs.items()})
    # Unir dos comunas que no se tocan produce un grafo en dos trozos, y eso hay
    # que decirlo: el motor no podrá trazar nada que vaya de una a la otra.
    con = connectivity(merged)
    merged.coverage["connectivity"] = {
        "covered": con["largest"], "total": merged.n_nodes,
        "pct": con["largest_pct"], "level": con["level"],
        "components": con["components"],
    }
    return merged


# Todos los DEM globales gratuitos son modelos de SUPERFICIE (DSM): miden la copa
# de los árboles y los tejados, no el suelo (doc 07 §3.2). En ciudad eso produce
# pendientes fantasma de 20–40% donde la calle es plana pero pasa junto a un
# edificio, bajo un puente o por un túnel. Sin filtrar, esos artefactos:
#   · disparan `grad_max_pct` a valores imposibles (25,7% en Santiago Centro),
#   · hacen que `max_gradient_pct` prohíba calles perfectamente llanas.
MAX_PLAUSIBLE_GRADE_PCT = 18.0
MIN_EDGE_FOR_GRADE_M = 25.0


def smooth_node_elevation(g: RaceGraph, elevations: np.ndarray,
                          passes: int = 2) -> np.ndarray:
    """Suavizado sobre el grafo: cada nodo se promedia con sus vecinos.

    Es el equivalente de "muestrear a lo largo del eje de la calle y suavizar"
    que doc 07 §3.2 exige como mitigación del artefacto DSM.
    """
    e = elevations.astype(np.float64)
    deg = np.diff(g.indptr).astype(np.float64)
    deg_safe = np.maximum(deg, 1.0)
    src = np.repeat(np.arange(g.n_nodes, dtype=np.int32), np.diff(g.indptr).astype(np.int32))
    for _ in range(passes):
        acc = np.zeros(g.n_nodes, dtype=np.float64)
        np.add.at(acc, src, e[g.indices])
        neigh_mean = acc / deg_safe
        # 60% nodo + 40% vecinos: quita el pico sin aplanar el relieve real.
        e = np.where(deg > 0, 0.60 * e + 0.40 * neigh_mean, e)
    return e


def bake_elevation(g: RaceGraph, elevations: np.ndarray, smooth: bool = True) -> None:
    """Escribe la elevación en los nodos y deriva la pendiente de cada arista.

    Esto es lo que hace que L3 influya en la BÚSQUEDA y no solo en el informe.
    En el MVP `Edge.grad` era 0.0 durante toda la generación (doc 10 §3.1).
    """
    e = smooth_node_elevation(g, elevations) if smooth else elevations.astype(np.float64)
    g.elev = e.astype(np.float32)

    src = np.repeat(np.arange(g.n_nodes, dtype=np.int32),
                    np.diff(g.indptr).astype(np.int32))
    rise = g.elev[g.indices] - g.elev[src]
    ln = np.maximum(g.elen, 1e-6)
    grad = np.where(g.elen >= MIN_EDGE_FOR_GRADE_M, 100.0 * rise / ln, 0.0)

    implausible = int((np.abs(grad) > MAX_PLAUSIBLE_GRADE_PCT).sum())
    grad = np.clip(grad, -MAX_PLAUSIBLE_GRADE_PCT, MAX_PLAUSIBLE_GRADE_PCT)
    g.egrad = grad.astype(np.float32)

    if implausible:
        g.coverage.setdefault("elevation", {})["implausible_edges"] = implausible
        print(f"      {implausible:,} aristas con pendiente implausible "
              f"(>{MAX_PLAUSIBLE_GRADE_PCT:.0f}%) acotadas — artefactos del DSM")


def connectivity(g: RaceGraph) -> dict:
    """Cuántos trozos sueltos tiene el grafo y qué fracción cubre el mayor.

    Una descarga de OSM puede llegar incompleta —Overpass corta consultas grandes,
    un radio recorta a mitad de una avenida, una autopista parte un barrio en dos—
    y el resultado NO parece un error: parece un mapa. El motor genera un recorrido
    perfectamente coherente sobre el trozo en el que cayó la salida y no dice nada
    de los otros.

    Esto es exactamente lo que se ve como «el mapa se corta en una parte», y por
    eso se mide al construir la región y se enseña.

    Componentes conexas por barrido de frentes sobre CSR, vectorizado: sobre
    300.000 aristas es cuestión de décimas.
    """
    n = g.n_nodes
    if n == 0:
        return {"components": 0, "largest": 0, "largest_pct": 0.0, "level": "deferred"}

    comp = np.full(n, -1, dtype=np.int32)
    indptr, indices = g.indptr, g.indices
    total = 0
    for semilla in range(n):
        if comp[semilla] >= 0:
            continue
        comp[semilla] = total
        frente = np.array([semilla], dtype=np.int64)
        while len(frente):
            ini = indptr[frente].astype(np.int64)
            fin = indptr[frente + 1].astype(np.int64)
            cuantos = fin - ini
            if not cuantos.sum():
                break
            # Aplanar los rangos [ini, fin) de todos los nodos del frente.
            idx = np.repeat(ini, cuantos) + (
                np.arange(cuantos.sum()) - np.repeat(np.cumsum(cuantos) - cuantos, cuantos))
            vecinos = np.unique(indices[idx].astype(np.int64))
            nuevos = vecinos[comp[vecinos] < 0]
            if not len(nuevos):
                break
            comp[nuevos] = total
            frente = nuevos
        total += 1

    tamanos = np.bincount(comp, minlength=total)
    mayor = int(tamanos.max()) if total else 0
    pct = 100.0 * mayor / n
    # Umbrales de producto: por debajo del 90 % hay red importante desconectada.
    nivel = "reliable" if pct >= 99.0 else ("partial" if pct >= 90.0 else "deferred")
    return {"components": int(total), "largest": mayor,
            "largest_pct": round(pct, 1), "level": nivel}


def compute_coverage(g: RaceGraph, raw_counts: dict[str, tuple[int, int]]) -> dict:
    """Cobertura por atributo con los umbrales 80/40 de doc 07 §4.1."""
    out = {}
    for attr, (covered, total) in raw_counts.items():
        pct = 100.0 * covered / total if total else 0.0
        level = "reliable" if pct >= 80 else ("partial" if pct >= 40 else "deferred")
        out[attr] = {"covered": covered, "total": total, "pct": round(pct, 1), "level": level}
    return out
