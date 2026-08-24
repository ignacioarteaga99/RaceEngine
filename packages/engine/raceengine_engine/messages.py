"""Catálogo de mensajes del motor.

Los textos que ve el usuario (explicaciones, banderas operativas, motivos de
infactibilidad) los produce el MOTOR, no la interfaz. Si vivieran solo en el
cliente habría que duplicar la lógica de qué decir y cuándo; y si vivieran solo
en español, cambiar de idioma en la UI dejaría medio panel sin traducir — que es
exactamente el bug que había.

La petición lleva un campo `lang`; el motor emite en ese idioma. Añadir uno nuevo
es añadir una clave aquí.
"""
from __future__ import annotations

MESSAGES: dict[str, dict[str, str]] = {
    # ── Explicación de la ruta ───────────────────────────────────────────────
    "expl_distance": {
        "es": "Distancia {d:.2f} km (objetivo {t:.2f} km, desvío {e:+.2f}%).",
        "en": "Distance {d:.2f} km (target {t:.2f} km, deviation {e:+.2f}%).",
    },
    "expl_indices": {
        "es": "Índices — velocidad {s:.2f} · logística {l:.2f} · paisaje {c:.2f} · dificultad {f:.2f}.",
        "en": "Indices — speed {s:.2f} · logistics {l:.2f} · scenery {c:.2f} · difficulty {f:.2f}.",
    },
    "expl_crossings": {
        "es": "{x} intersecciones, de las cuales {l} semaforizadas; {t} giros ({s} cerrados).",
        "en": "{x} intersections, {l} of them signalised; {t} turns ({s} sharp).",
    },
    "expl_elevation": {
        "es": "Desnivel +{g:,.0f} m / −{o:,.0f} m, pendiente máxima {p:.1f}%.",
        "en": "Elevation +{g:,.0f} m / −{o:,.0f} m, max gradient {p:.1f}%.",
    },
    "expl_no_elevation": {
        "es": "Sin datos de elevación para esta región: dificultad no evaluada.",
        "en": "No elevation data for this region: difficulty not assessed.",
    },
    "expl_closure": {
        "es": "Impacto de cierre {c:,.1f} (km ponderados por clase de vía) y {e} intersecciones encerradas por el circuito.",
        "en": "Closure impact {c:,.1f} (km weighted by road class) and {e} intersections enclosed by the loop.",
    },
    "expl_selfx": {
        "es": "{n} auto-cruce(s) a nivel: es un coste operativo, no un impedimento.",
        "en": "{n} at-grade self-crossing(s): an operational cost, not a blocker.",
    },
    "expl_score": {
        "es": "Score global {s:.1f}/100; la mayor contribución viene de «{k}» ({v:.1f} puntos).",
        "en": "Global score {s:.1f}/100; the largest contribution comes from “{k}” ({v:.1f} points).",
    },
    "expl_scenery_proxy": {
        "es": "El índice de paisaje es aproximado (proxy de parques): tómalo como orientativo, no como una medición.",
        "en": "The scenery index is approximate (park proxy): treat it as indicative, not measured.",
    },
    "expl_hard": {
        "es": "Restricciones duras incumplidas: {list}",
        "en": "Hard constraints violated: {list}",
    },

    # ── Banderas operativas ──────────────────────────────────────────────────
    "ops_at_grade": {
        "es": "{n} auto-cruce(s) a nivel: requieren personal de control o separación horaria de pelotones.",
        "en": "{n} at-grade self-crossing(s): require marshals or time-separated waves.",
    },
    "ops_signals": {
        "es": "{n} intersecciones semaforizadas a neutralizar durante el evento.",
        "en": "{n} signalised intersections to neutralise during the event.",
    },
    "ops_hospital": {
        "es": "Distancia media a hospital {d:.1f} km: revisar cobertura de ambulancias.",
        "en": "Average distance to hospital {d:.1f} km: review ambulance coverage.",
    },
    "ops_steep": {
        "es": "Pendiente máxima {p:.1f}%: tramo exigente.",
        "en": "Max gradient {p:.1f}%: demanding section.",
    },

    # ── Nombres de índices (para la explicación del score) ───────────────────
    "term_fit": {"es": "ajuste de distancia", "en": "distance fit"},
    "term_speed": {"es": "velocidad", "en": "speed"},
    "term_logistics": {"es": "logística", "en": "logistics"},
    "term_scenery": {"es": "paisaje", "en": "scenery"},
    "term_difficulty": {"es": "dificultad", "en": "difficulty"},

    # ── Preflight ────────────────────────────────────────────────────────────
    "pf_tolerance_tight": {
        "es": "Tolerancia de {p:.2f}% muy estrecha: en un grafo real la exactitud submétrica suele ser inalcanzable.",
        "en": "A {p:.2f}% tolerance is very tight: sub-metre accuracy is rarely reachable on a real graph.",
    },
    "pf_radius_small": {
        "es": "El radio de {r:,.0f} m no permite un circuito de {d:,.0f} m (mínimo geométrico ≈ {m:,.0f} m).",
        "en": "A {r:,.0f} m radius cannot fit a {d:,.0f} m loop (geometric minimum ≈ {m:,.0f} m).",
    },
    "pf_free_needs_points": {
        "es": "Sin distancia objetivo, un circuito necesita al menos un punto de "
              "paso obligatorio: si no, «salir y volver al mismo sitio» no dice "
              "por dónde. Marca un punto de paso o cambia a punto a punto y pon "
              "una meta.",
        "en": "Without a target distance, a loop needs at least one mandatory "
              "waypoint: otherwise “leave and come back” says nothing about where "
              "to go. Add a waypoint, or switch to point-to-point and set a finish.",
    },
    "pf_zones_maybe": {
        "es": "La estimación dice que visitar todas las zonas pide unos {t:,.1f} km "
              "y tu máximo es {m:,.1f} km. Es una ESTIMACIÓN sobre la línea recta, "
              "así que se deja intentar: puede que el motor encuentre un enlace más "
              "corto por calle. Si no lo consigue, lo dirá con la ruta delante.",
        "en": "The estimate says visiting every zone needs about {t:,.1f} km and your "
              "maximum is {m:,.1f} km. It is an ESTIMATE over straight lines, so the "
              "attempt is allowed: the engine may find a shorter link on streets. If "
              "it cannot, it will say so with the route in front of you.",
    },
    "pf_no_route_constraints": {
        "es": "Todos los puntos marcados están sobre red alcanzable ({n:,} nodos "
              "desde la salida), así que el mapa no es el problema: lo que corta el "
              "paso es una restricción tuya. Revisa la pendiente máxima, el firme "
              "permitido, las zonas prohibidas y —si estás en bici de ruta— que el "
              "único enlace no sea un camino sin asfaltar.",
        "en": "Every marked point sits on reachable network ({n:,} nodes from the "
              "start), so the map is not the problem: one of your own constraints "
              "cuts the way through. Check maximum gradient, allowed surface, "
              "forbidden zones and —on a road bike— whether the only link is an "
              "unpaved track.",
    },
    "pf_no_route_why": {
        "es": "No hay camino por calle entre los puntos marcados. Desde la salida "
              "se alcanzan {n:,} de {t:,} nodos de la red ({p:.0f} %), y quedan "
              "fuera de ese trozo: {list}. Suele ser una de tres cosas: el mapa se "
              "descargó incompleto o a trozos —míralo en el bloque Región—, la "
              "franja descargada no llega hasta allí, o una restricción tuya "
              "(superficie, pendiente máxima, zona prohibida) corta el único paso.",
        "en": "There is no road route between the marked points. From the start, "
              "{n:,} of {t:,} network nodes are reachable ({p:.0f} %), and these fall "
              "outside that piece: {list}. It is usually one of three things: the map "
              "downloaded incomplete or in pieces —check the Region block—, the "
              "downloaded strip does not reach that far, or one of your constraints "
              "(surface, max gradient, forbidden zone) cuts the only way through.",
    },
    "pf_no_route": {
        "es": "No hay camino por calle entre los puntos marcados con estas "
              "restricciones. Prueba a ampliar el radio de búsqueda o a relajar "
              "la superficie o la pendiente máxima.",
        "en": "There is no road route between the marked points under these "
              "constraints. Try widening the search radius or relaxing surface "
              "or maximum gradient.",
    },
    "pf_no_start": {
        "es": "No hay ninguna calle utilizable cerca del punto de partida.",
        "en": "There is no usable street near the start point.",
    },
    "pf_start_snapped": {
        "es": "El punto de partida se ajustó a la calle utilizable más cercana, a {d:,.0f} m.",
        "en": "The start point was snapped to the nearest usable street, {d:,.0f} m away.",
    },
    "pf_start_moved": {
        "es": "La calle bajo el punto de partida no conecta con red suficiente bajo "
              "estas restricciones. La salida se movió {d:,.0f} m hasta la calle "
              "utilizable más cercana que sí conecta.",
        "en": "The street under the start point does not connect to enough network "
              "under these constraints. The start was moved {d:,.0f} m to the "
              "nearest usable street that does.",
    },
    "pf_graph_empty": {
        "es": "Las restricciones duras dejan solo {a} aristas utilizables de {t:,}. El grafo admisible está vacío.",
        "en": "Hard constraints leave only {a} usable edges out of {t:,}. The admissible graph is empty.",
    },
    "pf_disconnected": {
        "es": "Desde el inicio solo se alcanzan {n} nodos dentro de {b:,.0f} m de recorrido. La red está desconectada o el radio es demasiado pequeño.",
        "en": "Only {n} nodes are reachable within {b:,.0f} m of travel from the start. The network is disconnected or the radius is too small.",
    },
    "pf_start_isolated": {
        "es": "Desde la salida sólo se alcanzan {n:,} de {t:,} nodos de la red, y por "
              "eso quedan sin cubrir zonas que están cerca ({list}). El problema no "
              "son las zonas: la salida está sobre un trozo de calle mal conectado, "
              "o el radio de búsqueda recorta la red antes de llegar. Mueve la salida "
              "a una avenida o amplía el radio.",
        "en": "Only {n:,} of {t:,} network nodes are reachable from the start, which is "
              "why nearby zones ({list}) come out uncovered. The zones are not the "
              "problem: the start sits on a poorly connected piece of street, or the "
              "search radius cuts the network short. Move the start to a main road or "
              "widen the radius.",
    },
    "pf_zone_no_streets": {
        "es": "«{z}» no contiene ninguna calle utilizable.",
        "en": "“{z}” contains no usable street.",
    },
    "pf_zone_in_forbidden": {
        "es": "«{z}» está completamente dentro de una zona prohibida.",
        "en": "“{z}” lies entirely inside a forbidden zone.",
    },
    "pf_zone_unreachable": {
        "es": "«{z}» queda fuera de lo alcanzable desde el inicio con la mitad de la distancia objetivo (está a ~{d:,.0f} m en línea recta).",
        "en": "“{z}” is outside what is reachable from the start within half the target distance (~{d:,.0f} m as the crow flies).",
    },
    "pf_zones_too_far": {
        "es": "Pasar por las {n} zonas obligatorias exige al menos ~{t:,.1f} km por calle ({s:,.1f} km en línea recta × {f:.2f} de rodeo urbano), por encima del máximo de {m:,.1f} km que permite tu tolerancia.",
        "en": "Visiting the {n} mandatory zones requires at least ~{t:,.1f} km on streets ({s:,.1f} km straight-line × {f:.2f} urban detour factor), above the {m:,.1f} km maximum your tolerance allows.",
    },
    "pf_zones_tight": {
        "es": "Las zonas obligatorias consumen ~{p:.0f}% de la distancia objetivo: quedará poco margen para dar forma al circuito.",
        "en": "Mandatory zones consume ~{p:.0f}% of the target distance: little room left to shape the loop.",
    },
    "pf_caps_crossings": {
        "es": "El tope de {c} cruces es irreal para {k:.1f} km (lo típico son ~{e}). Ninguna ruta lo cumplirá; déjalo vacío o súbelo.",
        "en": "A cap of {c} crossings is unrealistic for {k:.1f} km (typically ~{e}). No route will satisfy it; leave it empty or raise it.",
    },
    "pf_caps_lights": {
        "es": "El tope de {l} semáforos es muy bajo para {k:.1f} km. Considera dejarlo vacío y usar el índice de velocidad para comparar.",
        "en": "A cap of {l} traffic lights is very low for {k:.1f} km. Consider leaving it empty and comparing with the speed index instead.",
    },
    "pf_no_elevation": {
        "es": "Esta región no tiene datos de elevación: las restricciones de desnivel y pendiente no se aplicarán. Reconstruye la región con elevación.",
        "en": "This region has no elevation data: gain and gradient constraints will not apply. Rebuild the region with elevation.",
    },
    "pf_no_loop": {
        "es": "No se pudo cerrar ningún circuito con estas restricciones. Prueba a ampliar el radio de búsqueda o la tolerancia.",
        "en": "No loop could be closed under these constraints. Try widening the search radius or the tolerance.",
    },

    # ── Veredicto final ──────────────────────────────────────────────────────
    "verdict_distance": {
        "es": "La mejor ruta encontrada mide {d:,.2f} km frente a un objetivo de {t:,.2f} km ({p:+.1f}%). ",
        "en": "The best route found is {d:,.2f} km against a {t:,.2f} km target ({p:+.1f}%). ",
    },
    "verdict_distance_zones": {
        "es": "Las zonas obligatorias fuerzan un recorrido mayor: sube la distancia objetivo o retira alguna zona.",
        "en": "The mandatory zones force a longer course: raise the target distance or remove a zone.",
    },
    "verdict_distance_plain": {
        "es": "Amplía el radio de búsqueda o sube la tolerancia.",
        "en": "Widen the search radius or raise the tolerance.",
    },
    "verdict_uncovered": {
        "es": "No se pudo pasar por: {list}. Las zonas obligatorias no son opcionales, así que este resultado no es válido.",
        "en": "Could not pass through: {list}. Mandatory zones are not optional, so this result is not valid.",
    },

    # ── Política de paso ─────────────────────────────────────────────────────
    "touch_missing": {"es": "{z}: no se pasa por la zona", "en": "{z}: the zone is not visited"},
    "touch_repeat": {
        "es": "{z}: {v} pasadas separadas (máx {m})",
        "en": "{z}: {v} separate passes (max {m})",
    },
    "touch_dwell": {
        "es": "{z}: permanencia {d:,.0f} m (máx {m:,.0f} m)",
        "en": "{z}: dwell {d:,.0f} m (max {m:,.0f} m)",
    },

    # ── Modo creativo (trazado manual) ───────────────────────────────────────
    "draw_no_start": {
        "es": "Fija primero la salida en el mapa.",
        "en": "Set the start point on the map first.",
    },
    "draw_no_point": {
        "es": "No se recibió ningún punto.",
        "en": "No point was received.",
    },
    "draw_no_street": {
        "es": "Ahí no hay calle utilizable (la más cercana está a {d:,.0f} m). "
              "Pincha sobre una vía.",
        "en": "No usable street there (the nearest one is {d:,.0f} m away). "
              "Click on a road.",
    },
    "draw_outside_region": {
        "es": "Ese punto queda fuera de la región descargada. Pincha dentro del "
              "área de la ciudad o descarga una región más amplia.",
        "en": "That point lies outside the downloaded region. Click inside the "
              "city area or download a wider region.",
    },
    "draw_same_node": {
        "es": "Ese punto ya es el extremo actual del trazado.",
        "en": "That point is already the current end of the drawing.",
    },
    "draw_already_closed": {
        "es": "El trazado ya está en la meta.",
        "en": "The drawing is already at the finish.",
    },
    "draw_unreachable": {
        "es": "No hay ningún camino admisible hasta ahí dentro de los {r:,.2f} km "
              "que quedan. Puede haber una zona prohibida, una pendiente por encima "
              "del máximo o el límite del radio de búsqueda de por medio.",
        "en": "There is no admissible path to that point within the {r:,.2f} km left. "
              "A forbidden zone, a gradient above the maximum or the search-radius "
              "limit may be in the way.",
    },
    "draw_budget_spent": {
        "es": "Ya llevas {d:,.2f} km y el máximo es {m:,.2f} km. Deshaz un tramo "
              "o sube la distancia objetivo.",
        "en": "You are at {d:,.2f} km and the maximum is {m:,.2f} km. Undo a leg "
              "or raise the target distance.",
    },
    "draw_no_return": {
        "es": "Desde ahí no se puede volver a la meta dentro de los {r:,.2f} km "
              "que quedarían.",
        "en": "From there you cannot get back to the finish within the {r:,.2f} km "
              "that would be left.",
    },
    "draw_no_room": {
        "es": "No cabe: {d:,.2f} km trazados + {b:,.2f} km de vuelta = {t:,.2f} km, "
              "y el máximo es {x:,.2f} km. Elige un punto más cerca.",
        "en": "It does not fit: {d:,.2f} km drawn + {b:,.2f} km back = {t:,.2f} km, "
              "and the maximum is {x:,.2f} km. Pick a closer point.",
    },
    "draw_gain": {
        "es": "El desnivel acumulado sería {g:,.0f} m y el máximo es {m:,.0f} m.",
        "en": "Cumulative elevation gain would be {g:,.0f} m and the maximum is {m:,.0f} m.",
    },
    "draw_close_short": {
        "es": "Cerrando aquí quedarían {d:,.2f} km, por debajo de la banda "
              "{n:,.2f}–{x:,.2f} km. Sigue trazando.",
        "en": "Closing here gives {d:,.2f} km, below the {n:,.2f}–{x:,.2f} km band. "
              "Keep drawing.",
    },
    "draw_close_long": {
        "es": "Cerrando aquí quedarían {d:,.2f} km, por encima de la banda "
              "{n:,.2f}–{x:,.2f} km. Deshaz algún tramo.",
        "en": "Closing here gives {d:,.2f} km, above the {n:,.2f}–{x:,.2f} km band. "
              "Undo a leg.",
    },
}


def msg(key: str, lang: str = "es", **kw) -> str:
    entry = MESSAGES.get(key)
    if not entry:
        return key
    template = entry.get(lang) or entry.get("es") or key
    try:
        return template.format(**kw)
    except (KeyError, IndexError, ValueError):
        return template
