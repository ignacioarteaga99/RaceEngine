"""Lo que cambia cuando el recorrido es en bicicleta.

Por qué hace falta un módulo aparte
───────────────────────────────────
Un corredor y un ciclista no discrepan en los detalles: discrepan en casi todo lo
que el motor mide.

  · **La velocidad.** Un corredor va a ritmo casi constante y una cuesta le cuesta
    tiempo de forma suave. Un ciclista pasa de 40 km/h bajando a 8 subiendo: el
    tiempo de una ruta de montaña no se parece en nada a sus kilómetros. Estimarlo
    con un ritmo fijo por kilómetro —que es lo que hacía el motor— da errores del
    100 % en cuanto hay un puerto.
  · **La cuesta.** Para una carrera a pie el desnivel es un coste que se minimiza.
    Para una marcha cicloturista suele ser el PRODUCTO: se va a buscar el puerto.
  · **El firme.** En ruta, un kilómetro de tierra arruina la salida. En gravel es
    la razón de existir del recorrido.

Lo que se mide aquí y no en `metrics/compute.py` es lo que sólo tiene sentido
sobre dos ruedas. El resto —distancia, cruces, cierres, SPR— sigue siendo común.

Sobre el modelo de velocidad
────────────────────────────
Es un balance de potencia real, no una curva inventada: potencia del ciclista
contra gravedad, rodadura y aire. Sale de ahí que subir sea desproporcionadamente
lento y que bajar tenga un techo. Los parámetros —potencia, masa, CdA, Crr— están
declarados como constantes justo debajo, porque cualquier ciclista tiene opinión
sobre ellos y debe poder cambiarlos.
"""
from __future__ import annotations

import numpy as np

# ── Modelo de velocidad: balance de potencia ────────────────────────────────
#
# El primer intento fue una recta —«resta un 11 % de velocidad por cada punto de
# pendiente»— y no aguantaba: daba 5 km/h en una rampa del 8 %, que es andar. La
# velocidad de un ciclista no es lineal con la pendiente porque los términos que
# la frenan no lo son: la gravedad crece con la pendiente pero el aire crece con
# el CUBO de la velocidad, y por eso subir es lento y bajar no es infinitamente
# rápido.
#
# Se resuelve el balance real, que es una cúbica en v:
#
#     P·η = v · ( m·g·(pendiente + Crr) + ½·ρ·CdA·v² )
#
# Newton sobre esa cúbica converge en cuatro o cinco pasadas y se vectoriza sobre
# todas las aristas a la vez. En las bajadas el término lineal se vuelve negativo
# y la raíz que sale es justamente la velocidad terminal — el modelo da gratis lo
# que una recta tenía que inventarse con un tope.
#
# TODOS estos números son discutibles y por eso están aquí y no enterrados: son un
# ciclista aficionado de fondo, no un profesional.
RIDER_POWER_W: dict[str, float] = {"road": 180.0, "gravel": 180.0, "mtb": 165.0}
DRIVETRAIN_EFF = 0.96
TOTAL_MASS_KG = 82.0          # ciclista + bici + bidones
GRAVITY = 9.81
AIR_DENSITY = 1.20
CDA_M2: dict[str, float] = {"road": 0.40, "gravel": 0.42, "mtb": 0.45}
CRR: dict[str, float] = {"road": 0.006, "gravel": 0.012, "mtb": 0.020}

# Factor de terreno: lo que la física no ve. Un camino de grava obliga a frenar
# antes, a esquivar y a no fiarse; un sendero de montaña, mucho más. No es
# resistencia a la rodadura —eso ya está en Crr—, es técnica y prudencia.
TERRAIN_FACTOR: dict[str, float] = {"road": 1.00, "gravel": 0.82, "mtb": 0.60}

# Tope de bajada. Por encima de esto nadie sigue: se frena en las curvas.
MAX_DESCENT_KMH = 65.0
# Suelo: por encima de cierta rampa se acaba andando, pero nunca a cero.
MIN_CLIMB_KMH = 4.5

# ── Qué cuenta como «puerto» ────────────────────────────────────────────────
# Un repecho de doscientos metros no es un puerto por mucho que pique. El umbral
# es de producto, no de física, y por eso está declarado.
CLIMB_MIN_LEN_M = 500.0
CLIMB_MIN_GAIN_M = 30.0
# Una bajada corta no parte un puerto: el perfil de una carretera de montaña
# ondula, y trocear el Tourmalet en once subidas no describe nada.
CLIMB_BREAK_M = 250.0


def speed_kmh(discipline: str, grad_pct: np.ndarray) -> np.ndarray:
    """Velocidad por arista, en km/h, resolviendo el balance de potencia.

    `P·η = v·(m·g·(s + Crr) + ½·ρ·CdA·v²)` por Newton, vectorizado. En bajada el
    término lineal se vuelve negativo y la raíz es la velocidad terminal.
    """
    d = str(discipline) if str(discipline) in RIDER_POWER_W else "road"
    s = np.asarray(grad_pct, dtype=np.float64) / 100.0
    p = RIDER_POWER_W[d] * DRIVETRAIN_EFF
    a = 0.5 * AIR_DENSITY * CDA_M2[d]                 # coeficiente del término v³
    b = TOTAL_MASS_KG * GRAVITY * (s + CRR[d])        # gravedad + rodadura

    # Bisección, no Newton. `f(v) = a·v³ + b·v − P` vale −P en v = 0 y crece sin
    # límite, y para v > 0 sólo cambia de signo una vez (el máximo local de la
    # cúbica cae en v negativo siempre que a > 0). Así que hay exactamente una
    # raíz positiva y la bisección la encuentra siempre.
    #
    # Newton NO servía: bajando fuerte, el término lineal se vuelve muy negativo,
    # la derivada en el punto de arranque es negativa y el primer paso se va a
    # velocidad negativa. El resultado era un descenso del 10 % a 4,5 km/h, que
    # es el suelo de la función — un ciclista bajando a paso de peatón.
    lo = np.zeros(s.shape)
    hi = np.full(s.shape, 35.0)                       # m/s, muy por encima de todo
    for _ in range(40):
        mid = 0.5 * (lo + hi)
        f = a * mid ** 3 + b * mid - p
        lo = np.where(f < 0, mid, lo)
        hi = np.where(f < 0, hi, mid)
    v = 0.5 * (lo + hi)

    # El factor de terreno se aplica en proporción a la velocidad. Subiendo al
    # 8 % la técnica no es lo que frena —ya se va despacio y la rodadura extra ya
    # está en Crr—; bajando por un sendero, sí. Aplicarlo plano dejaba al MTB
    # subiendo a 4,5 km/h, que es empujar la bici.
    kmh = v * 3.6
    mezcla = np.clip(kmh / 25.0, 0.0, 1.0)
    kmh *= 1.0 - (1.0 - TERRAIN_FACTOR[d]) * mezcla
    return np.clip(kmh, MIN_CLIMB_KMH, MAX_DESCENT_KMH)


def ride_time_min(g_graph, edges: np.ndarray, discipline: str) -> float:
    """Tiempo de rodaje, en minutos, integrando arista a arista.

    Integrar por arista y no aplicar un ritmo medio al total es la diferencia
    entre un número útil y uno inventado: dos rutas de 100 km con el mismo
    desnivel pueden diferir una hora según cómo esté repartido.
    """
    if not len(edges):
        return 0.0
    v = speed_kmh(discipline, g_graph.egrad[edges])
    km = g_graph.elen[edges].astype(np.float64) / 1000.0
    return float((km / v).sum() * 60.0)


def unpaved_share_pct(g_graph, edges: np.ndarray) -> float:
    """Porcentaje de la ruta sobre firme no asfaltado."""
    if not len(edges):
        return 0.0
    ln = g_graph.elen[edges].astype(np.float64)
    total = ln.sum()
    if total <= 0:
        return 0.0
    return float(100.0 * ln[~g_graph.epaved[edges]].sum() / total)


def climbs(g_graph, edges: np.ndarray) -> list[dict]:
    """Puertos del recorrido: tramos de subida sostenida.

    Un ciclista no lee «1.850 m de desnivel»: lee «tres puertos, el más duro de
    6,8 km al 7,2 %». El agregado no dice si la ruta es dura o sólo larga.

    Un bache de bajada no parte un puerto —una carretera de montaña ondula—, así
    que se permite hasta `CLIMB_BREAK_M` de respiro antes de darlo por terminado.
    """
    if not len(edges):
        return []
    ln = g_graph.elen[edges].astype(np.float64)
    grad = g_graph.egrad[edges].astype(np.float64)
    rise = ln * grad / 100.0

    out: list[dict] = []
    inicio = None
    largo = gain = 0.0
    respiro = 0.0
    desde = 0.0          # metros recorridos desde el arranque de la ruta
    acumulado = 0.0

    def cerrar(fin_m: float) -> None:
        nonlocal inicio, largo, gain, respiro
        if inicio is not None and largo >= CLIMB_MIN_LEN_M and gain >= CLIMB_MIN_GAIN_M:
            out.append({
                "start_m": round(inicio, 1),
                "length_m": round(largo, 1),
                "gain_m": round(gain, 1),
                "avg_pct": round(100.0 * gain / max(largo, 1.0), 2),
            })
        inicio = None
        largo = gain = respiro = 0.0

    for i in range(len(ln)):
        subiendo = grad[i] > 1.0
        if subiendo:
            if inicio is None:
                inicio = acumulado
                largo = gain = respiro = 0.0
            largo += ln[i] + respiro
            gain += max(rise[i], 0.0)
            respiro = 0.0
        elif inicio is not None:
            respiro += ln[i]
            if respiro > CLIMB_BREAK_M:
                cerrar(acumulado)
        acumulado += ln[i]
    cerrar(acumulado)
    return out


def climb_summary(lista: list[dict]) -> dict:
    """Las tres cifras que resumen la dureza: cuántos, el más largo, el más duro."""
    if not lista:
        return {"climbs": 0, "longest_climb_m": 0.0, "hardest_climb_pct": 0.0,
                "hardest_climb_m": 0.0}
    mas_largo = max(lista, key=lambda c: c["length_m"])
    # «El más duro» no es el de mayor pendiente media: un repecho corto al 12 % no
    # es un puerto. Se pondera por el desnivel, que es lo que de verdad se sufre.
    mas_duro = max(lista, key=lambda c: c["gain_m"])
    return {
        "climbs": len(lista),
        "longest_climb_m": float(mas_largo["length_m"]),
        "hardest_climb_pct": float(mas_duro["avg_pct"]),
        "hardest_climb_m": float(mas_duro["length_m"]),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Monte a pie: trail y senderismo
# ══════════════════════════════════════════════════════════════════════════════
# Aquí NO sirve ni el ritmo fijo del asfalto ni el balance de potencia de la bici.
# Andar y correr por monte tienen una curva propia y muy conocida: la **función de
# Tobler**, ajustada sobre datos reales de marcha en terreno irregular.
#
#     v = 6 · exp(−3,5 · |pendiente + 0,05|)   km/h
#
# El `+0,05` no es un error de signo: el máximo NO está en el llano sino en una
# bajada suave del 5 %, que es exactamente la experiencia de cualquiera que haya
# andado por monte. Un modelo simétrico en torno al cero se equivoca justo donde
# más se camina.
#
# Para correr por monte se escala la misma curva: la forma de la penalización por
# pendiente es la misma, lo que cambia es el nivel. El factor sale de comparar el
# 6 km/h de Tobler con el ritmo de un corredor de montaña en llano.
TOBLER_BASE_KMH = 6.0
TOBLER_DECAY = 3.5
TOBLER_PEAK_SLOPE = 0.05           # el óptimo está bajando un 5 %, no en llano
FOOT_FACTOR: dict[str, float] = {"hiking": 1.00, "trail": 1.65}
# Techo: por muy cuesta abajo que sea, nadie desciende un sendero a 25 km/h.
MAX_FOOT_KMH: dict[str, float] = {"hiking": 7.5, "trail": 16.0}
MIN_FOOT_KMH = 1.2


def foot_speed_kmh(discipline: str, grad_pct: np.ndarray) -> np.ndarray:
    """Velocidad andando o corriendo por monte, en km/h (función de Tobler)."""
    d = str(discipline) if str(discipline) in FOOT_FACTOR else "hiking"
    s = np.asarray(grad_pct, dtype=np.float64) / 100.0
    v = TOBLER_BASE_KMH * np.exp(-TOBLER_DECAY * np.abs(s + TOBLER_PEAK_SLOPE))
    return np.clip(v * FOOT_FACTOR[d], MIN_FOOT_KMH, MAX_FOOT_KMH[d])


def foot_time_min(g_graph, edges: np.ndarray, discipline: str) -> float:
    """Tiempo por monte, integrando arista a arista."""
    if not len(edges):
        return 0.0
    v = foot_speed_kmh(discipline, g_graph.egrad[edges])
    km = g_graph.elen[edges].astype(np.float64) / 1000.0
    return float((km / v).sum() * 60.0)
