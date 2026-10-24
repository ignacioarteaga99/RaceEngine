# RaceEngine V2

Motor de diseño de recorridos deportivos óptimos + interfaz de mapa.
Implementación de la arquitectura Engine V2 definida en `11_Engine_V2_Design.md`.

---

## Arranque en 2 minutos

| Sistema | Comando |
|---|---|
| **macOS / Linux** | `./run.sh` |
| **Windows** | doble clic en `run.bat` (o `.\run.bat` desde la terminal) |
| **Cualquiera** (con deps ya instaladas) | `python serve.py` |

El navegador se abre solo en **http://localhost:8000**.

Eso es todo. El script crea el entorno virtual, instala las dependencias, genera
una ciudad sintética y levanta la app. **No necesita conexión a internet** (salvo
para bajar los paquetes de Python la primera vez) ni GraphHopper ni base de datos.

> **Requisito duro: Python 3.12 o superior.**
> En macOS: `brew install python@3.12` y luego `PYTHON=python3.12 ./run.sh`

### Alternativa manual

```bash
python3.12 -m venv .venv
source .venv/bin/activate          # Windows:  .venv\Scripts\activate
pip install -r requirements.txt
python serve.py                     # resuelve sys.path por sí mismo
```

`serve.py` inserta los paquetes del monorepo en `sys.path`, así que **no depende
de `PYTHONPATH`** — que usa `:` en Unix y `;` en Windows y es una fuente clásica
de errores. Opciones: `--port`, `--host`, `--region`, `--reload`, `--no-open`.

---

## Desde el iPhone (versión móvil)

La interfaz móvil vive en **`/m`** y se genera desde la de escritorio con
`python build_mobile.py` (`serve.py` la regenera solo si `index.html` cambió).

- **En casa:** `python serve.py --host 0.0.0.0` y abre en Safari la URL que
  imprime (`http://IP-DE-TU-PC:8000/m`). Compartir → *Añadir a pantalla de inicio*.
- **En internet, gratis (Render + Neon + GitHub):**
  1. [neon.tech](https://neon.tech) → crea un proyecto y copia el *connection
     string* (`postgresql://…`). Ahí se guardan las carreras.
  2. [render.com](https://render.com) → *New → Blueprint* → este repo. Lee
     `render.yaml` y pide `RACEENGINE_PASSWORD` (contraseña para entrar; el
     usuario da igual) y `DATABASE_URL` (lo de Neon).
  3. Abre `https://<tu-servicio>.onrender.com/m` en el iPhone.

  El plan gratis se duerme tras 15 min sin uso (~1 min en despertar) y su disco
  se borra, pero no se pierde nada: las carreras viven en Neon y las regiones en
  la Release `graphs` de GitHub, de donde se descargan en segundos.

### Regiones en un servidor pequeño

Construir una región real necesita mucha más RAM que usarla (medido):

| Región | Nodos | .pkl (gzip) | RAM construir | RAM cargada |
|---|---|---|---|---|
| Santiago Centro (9 km) | 269 mil | 27,6 MB (11,8) | 639 MB | ~80 MB |
| Santiago Metro (20 km) | 793 mil | 80,0 MB (33,6) | 1,6 GB | ~180 MB |

Por eso, con `RACEENGINE_LOCAL_BUILD=0` el servidor no construye: las regiones
se construyen en GitHub Actions → **Construir región** → *Run workflow* (id del
catálogo, o id + nombre + lat/lon/radio para una ciudad nueva). El workflow
publica `<id>.pkl.gz` y `manifest.json` en la Release `graphs`, y el resumen del
run muestra la RAM pico de generar un 10K sobre ella. Con
`RACEENGINE_GRAPHS_URL` apuntando a esa Release, el servidor las ofrece como
construidas y las descarga al primer uso. La elevación sale de OpenTopoData,
cuya API pública admite ~1.000 consultas al día: Santiago Metro gasta ~900.

RAM pico generando un 10K (medida en el workflow): Santiago Centro 169 MB,
Santiago Metro 395 MB. Con `RACEENGINE_MAX_REGIONS=1` el servidor mantiene una
sola región real en memoria y suelta la anterior al cargar otra.

---

## Cómo se usa

1. **Elige la región** (arriba a la izquierda). Arranca con *Demo*, una ciudad
   sintética con retícula, avenidas, un parque y un cerro. Cuando quieras trabajar
   sobre Santiago real, construye esa región (§ *Regiones reales*).

2. **Fija la salida**: con el modo `📍 Salida` activo, haz clic en el mapa. El
   marcador se puede arrastrar.

3. **Marca zonas**: cambia a `✅ Obligatorio` o `⛔ Prohibido` y haz clic. Cada
   clic crea una zona circular con el radio del deslizador de la derecha.
   - **Obligatorio** = restricción DURA: la ruta debe pasar por dentro.
   - **Prohibido** = restricción DURA: la ruta no puede entrar. Se comprueban
     ambos extremos de cada arista, así que tampoco puede atravesarla.
   - *No hay zonas "preferentes"*: se eliminaron por decisión de producto.

4. **Ajusta las capas** en el panel izquierdo: distancia, tolerancia, desnivel
   máximo, pendiente máxima, radio de búsqueda, suavidad, topes de cruces y
   semáforos, superficie, túneles, parques.

5. **Pulsa Generar**. Verás geometría en el mapa en décimas de segundo y una ruta
   ya en tolerancia poco después; el resto del presupuesto se dedica a refinarla.
   Puedes **detener** en cualquier momento y te quedas con lo mejor encontrado.

El panel derecho muestra el score global, los cuatro índices, el perfil de
elevación, el catálogo de métricas con sus banderas de confianza, las incidencias
operativas, la explicación en texto y los exports GPX/GeoJSON.

### Editar zonas directamente sobre el mapa

Las zonas no son estáticas: se arrastra el **centro** para moverlas, se arrastra
el **tirador del borde** para cambiar el radio y se hace **doble clic** para
renombrarlas en el sitio. El arrastre no crea zonas nuevas por accidente: el clic
que cierra un arrastre queda suprimido.

### Lecturas al pasar el cursor

Tres lecturas contextuales, deliberadamente escuetas:

| Dónde | Qué muestra |
|---|---|
| Sobre el mapa | Distancia en línea recta a la salida/meta + coordenadas |
| Sobre la ruta | Kilómetro de carrera y pendiente instantánea (ventana ±40 m) |
| Sobre el perfil | Kilómetro, altitud y pendiente, con cursor sincronizado |

### Carreras y galería

Cada carrera es un **proyecto independiente** en su propia pestaña: sus zonas,
sus ajustes y su ruta. La ruta **persiste al cambiar de pestaña** — se guarda de
forma incremental y con escritura atómica, ligada al proyecto que estaba activo
en el momento de programar el guardado, no al que esté activo al escribirse.
Puedes crear (nombre + descripción), renombrar, duplicar, guardar, cerrar y
eliminar carreras.

El botón **Galería** abre todas las carreras guardadas como tarjetas con una
miniatura SVG de su ruta (proyección de aspecto conservado), desde donde se
revisan y se vuelven a abrir para editar.

### Idioma y unidades

Los dos selectores viven juntos arriba a la derecha del panel izquierdo:
`ES / EN` y `km / mi`. Actúan al instante y **nunca requieren regenerar**: el
motor emite su narrativa en ambos idiomas (`explanation_i18n`, `message_i18n`) y
la interfaz sólo elige cuál mostrar. Las unidades son conversión de presentación:
internamente todo son metros, siempre.

### Modo creativo — trazar la ruta a mano

`✏️ Trazar a mano` cambia el mapa a modo de dibujo. Cada clic **no traza una
recta**: se engancha a la calle más cercana y calcula el tramo de coste mínimo
bajo exactamente los mismos pesos que usa el generador automático. El panel
derecho se rellena tramo a tramo — altimetría, cruces, semáforos, giros,
logística, score — como si la ruta hubiera salido del motor.

Las tres reglas que **bloquean** el trazado (la petición explícita: si rompe lo
configurado, no se puede seguir):

| Regla | Qué comprueba |
|---|---|
| Admisibilidad | Si no hay camino admisible hasta el punto, se rechaza. Cubre gratis las zonas prohibidas, la pendiente máxima, superficies y radio |
| Distancia | El tramo tiene que caber **contando el regreso**: `trazado + vuelta ≤ máximo`. Si no, se rechaza antes de dibujarlo |
| Desnivel | Si el desnivel positivo acumulado superaría el máximo, se rechaza |

Cerrar el recorrido exige además caer **dentro de la banda de tolerancia**, no
sólo por debajo del máximo: 7 km no es un 10K. Cada rechazo dice exactamente por
qué y con qué números (`4,01 km trazados + 1,29 km de vuelta = 5,30 km, y el
máximo es 5,10 km`).

`↶ Deshacer tramo` retrocede clic a clic; `🗑 Descartar` vacía el trazado. El
dibujo se guarda con la carrera —como índices de nodo, no como geometría— así que
sobrevive al cambio de pestaña igual que una ruta generada. Se exporta a GPX y
GeoJSON como cualquier otra.

Los controles del modo creativo **flotan sobre el mapa** en vez de ocupar filas
del layout. Si fueran filas, activar el modo encogería el mapa justo en el momento
en que más superficie hace falta: cuando estás dibujando.

Flotar dentro del mapa tiene tres trampas, y las tres están resueltas. La primera:
sus eventos burbujean hasta Leaflet, así que pulsar *Cerrar recorrido* disparaba
además un clic en el mapa y el trazado seguía hacia el botón. Se cortan en origen.
La segunda: los avisos vivían abajo y se pisaban con la lectura del cursor, que
también vive abajo a la izquierda; ahora barra y avisos van **apilados arriba**.
La tercera: los avisos del panel usan fondos con un 9 % de alfa, que se leen
perfectamente sobre el panel oscuro y **desaparecen sobre un mapa base claro**
como Voyager o el satélite; sobre el mapa el fondo es opaco y sólo el borde
conserva el color de severidad. Contraste medido: 15,3:1.

> El score del trazado manual se calcula en **escala absoluta**. La normalización
> aprobada (doc 06 §4) es relativa al conjunto de candidatos, y con un único
> candidato colapsa: todas las métricas normalizarían a 1.0 y el score saldría
> cerca de 100 sin significar nada. Se compara con un par de referencias sintéticas
> escaladas a la distancia objetivo. No es directamente comparable con el score de
> una ruta generada; la interfaz lo indica al pasar el cursor por el número.

### Dos peticiones distintas: diseñar un recorrido o trazar una ruta

Son cosas diferentes y el motor ahora las distingue.

**Diseñar un recorrido** es la tarea de un organizador: «dame 42,195 km que
cumplan esto». La distancia es la promesa central y por tanto una restricción
dura. Es para lo que nació el motor.

**Trazar una ruta** es otra cosa: «llévame de San Vicente a Rapel pasando por el
embalse, y hazlo lo más llano posible». Aquí **no hay distancia objetivo**. La
distancia es *consecuencia* de la ruta, no una restricción sobre ella, y
perseguirla no restringe nada — obliga al motor a inventar rodeos para rellenar
kilómetros que nadie pidió.

En `L1 · Distancia` hay tres modos:

| Modo | Qué manda | Para qué |
|---|---|---|
| Distancia exacta | los kilómetros | competición: un 10K son 10 km |
| Horquilla + desnivel | el desnivel | marcha: «2.500 m y que me dé el día» |
| **Sin distancia** | el objetivo | ruta lógica entre los puntos marcados |

En modo *sin distancia* el pipeline cambia entero: no hay generación de circuitos,
ni ajuste de distancia, ni banda de tolerancia. Hay **encadenado de caminos
mínimos** bajo los pesos que dicte el objetivo, y el preflight deja de evaluar las
reglas de distancia — que no es que se cumplan, es que no aplican. Antes daban
bloqueos absurdos del tipo «el radio no permite el circuito de 10 km» sobre una
petición que nunca habló de 10 km.

#### Los puntos de paso se ordenan solos

El orden en que se pinchan tres sitios no es el orden en que conviene recorrerlos.
Se resuelve el orden óptimo, que es un viajante de comercio con extremos fijos:
hasta 8 puntos, **programación dinámica exacta** (Held-Karp); más allá, vecino más
próximo y 2-opt.

La matriz de distancias se calcula con árboles de caminos mínimos, no con
distancias en línea recta. Cuesta más y es lo correcto justo donde se usa: en
montaña, dos puntos a dos kilómetros en línea recta pueden estar a treinta por
carretera, y un orden decidido sobre la recta manda a la ruta a cruzar un valle
que no existe.

#### El objetivo cambia la RUTA, no sólo el número

Éste era un defecto de fondo: el objetivo sólo tocaba los pesos de la
**puntuación**. Elegir «más llana» cambiaba la cifra del panel y devolvía
exactamente el mismo camino. Eso no es una preferencia, es una etiqueta.

En modo sin distancia el objetivo se traduce a pesos de arista:

| Objetivo | Qué hace en la búsqueda |
|---|---|
| Más rápida | Longitud pura + penalización por semáforo. Se anulan los demás sesgos: pedir «la más rápida» y recibir un rodeo por un parque es no haber escuchado |
| Más llana | Penaliza la subida ×0,14 por punto de pendiente |
| Buscar montaña | La subida abarata la arista en vez de encarecerla |
| Menos cierres | Penaliza según el peso de cierre: calle menor aunque sea más larga |
| Más paisaje | Parques ×0,62; primarias y nacionales ×1,35 |

Medido sobre la región de referencia, seis objetivos dan **seis rutas distintas**,
y las cifras se mueven en la dirección prometida: *más rápida* sale con cero
semáforos, *menos cierres* con el menor impacto de cierre, *buscar montaña* con
más desnivel que *más llana*.

Se aplica **sólo en modo sin distancia**, a propósito: en modo objetivo el camino
lo decide sobre todo cuadrar los kilómetros, y meter ahí un sesgo fuerte cambiaría
el comportamiento de todos los recorridos ya diseñados.

### Salida y meta como en un navegador

Pinchar el mapa exige saber dónde cae el sitio **antes** de buscarlo, y para
invertir un trayecto había que borrar los dos puntos y volver a ponerlos en el
otro orden. Dos casillas y un intercambio es como se hace esto desde hace veinte
años y no hay razón para reinventarlo.

Sobre el mapa hay ahora **Salida / Meta** con autocompletado (mismo geocodificador
que el buscador de lugares, y acepta coordenadas pegadas), una ✕ por casilla y un
**⇅** que intercambia las puntas.

Tres decisiones que hacen que se comporte como uno espera:

- **Poner una meta cambia el tipo de recorrido a punto a punto.** Exigir además
  que el usuario lo diga en L2 es hacerle decir dos veces lo mismo, y hasta que lo
  dijera la meta no se aplicaba.
- **El nombre buscado no se sustituye por coordenadas.** Si se buscó «San Vicente
  de Tagua Tagua», ver luego un par de números en su sitio es perder información.
  Colocar el punto a mano sí borra el nombre: ya no describe ese punto.
- **Intercambiar descarta la ruta generada.** Invertir las puntas es un cambio de
  problema, no de presentación, y la ruta que había describe el trayecto contrario.

### El radio no recorta una ruta de A a B

El radio de búsqueda de L4 es una restricción de **diseño**: un circuito de 10 km
no debe irse a veinte kilómetros de la salida. Pero cuando lo que se pide es ir de
A a B, **los extremos son el problema**, no algo que acotar.

Con el valor por defecto de 4 km —que además vive en modo avanzado y por tanto
está oculto— una ruta a un punto a veinticinco kilómetros se quedaba a las afueras
y el panel la presentaba como **terminada**. Un recorrido truncado que dice «listo»
es peor que un error, porque no se parece a uno.

En modo sin distancia el radio se estira solo hasta cubrir la meta y todos los
puntos de paso, con un 40 % de margen para el rodeo. En modo objetivo sigue
mordiendo exactamente igual que antes.

### El preflight no puede bloquear con una cuenta mal hecha

VR-5 estima cuánto mide el recorrido que visita todas las zonas obligatorias.
Estimaba mal de dos formas, y **bloquear es lo único que el usuario no puede
rebatir**:

1. **Sumaba en el orden en que se pincharon los puntos.** Catorce zonas trazadas
   sobre un maratón real daban 58,6 km de línea recta para un recorrido de
   41,7 km. Ahora se reordenan con la misma rutina que usa el modo sin distancia.
2. **Aplicaba un rodeo fijo del 20 %.** Con dos puntos lejanos ese margen es
   correcto; con catorce, la poligonal **ya es** la ruta y el margen sobra. Ahora
   el factor se estrecha con la densidad: 1,20 con dos zonas, ~1,02 con catorce.

Y el bloqueo pasa a exigir un exceso claro (12 %). Dentro de ese margen se avisa y
se deja intentar: es una estimación sobre líneas rectas, y si de verdad no cabe,
el motor lo dirá con la ruta delante.

### Un hueco no puede costar media traza

Al importar, un tramo sin red debajo se puentea por calle. Si el puente no cabía,
se conservaba **sólo la cadena más larga** y el resto se perdía en silencio: un
maratón con un solo hueco aparecía como media traza. La cota del puente era de
3× la línea recta con 400 m de suelo — demasiado corta para un túnel o un rodeo
por el único puente sobre un río.

Ahora la cota es 6× con 2,5 km de suelo, hay un segundo intento sin cota validado
después (hasta 9× la recta), y lo que aun así se descarta **se declara en metros**.
Perder doce kilómetros en silencio y enseñar el resto como si fuera la traza
entera es la peor forma de fallar.

### «No hay ruta» tiene que decir por qué

Era cierto y no servía: no distinguía entre media descarga que falta, una meta en
una isla del grafo, y una restricción propia que corta el único paso. Ahora el
mensaje trae cuánta red alcanza la salida, qué puntos quedan fuera de ese trozo, y
las tres causas probables en orden. Si todos los puntos son alcanzables, el
diagnóstico cambia: el mapa no es el problema, lo es una restricción.

Además, la cota del tramo entre puntos tenía un segundo intento pendiente: existe
para que A\* no se pasee por medio grafo cuando algo es inalcanzable, pero cuando
**sí** es alcanzable y el camino da un rodeo largo —bordear un embalse, cruzar por
el único puente— se lo comía y devolvía «no hay ruta» sobre algo que existe. Ahora
reintenta sin cota antes de rendirse.

### Cuando el mapa llega en trozos

Una descarga de OpenStreetMap puede llegar incompleta: Overpass corta consultas
grandes, un radio recorta a mitad de una avenida, una autopista parte un barrio en
dos. El resultado **no parece un error: parece un mapa**. El motor traza un
recorrido perfectamente coherente dentro del trozo donde cayó la salida, con
métricas coherentes, y no dice nada de los demás.

Ahora la conectividad se mide al construir la región y al unir varias, y se enseña
en el panel: cuántos trozos hay y qué fracción cubre el mayor. Por debajo del 99 %
es un aviso amarillo; por debajo del 90 %, rojo, con la explicación de que el
recorrido puede aparecer cortado y de que marcar la comuna vecina cose la costura.

Se calcula por barrido de frentes vectorizado sobre CSR: décimas de segundo sobre
300.000 aristas.

### Medición certificable: la SPR y el factor 1,001

Un recorrido oficial **no se mide por el eje de la calzada**. Se mide por la
**ruta más corta posible** (SPR): la línea que el corredor puede tomar legalmente,
cortando las tangentes en cada curva y a un palmo del bordillo. Sobre esa línea el
medidor oficial aplica el **factor de prevención de recorrido corto de 1,001** —
busca 42.237 m para certificar un maratón de 42.195, de modo que ni el bamboleo de
la bicicleta ni un esquive dejen el recorrido por debajo de lo anunciado.

La consecuencia es incómoda y hay que decirla: medir por el eje del grafo, que es
lo que hace cualquier herramienta de rutas, **sobreestima sistemáticamente**. Un
maratón que en pantalla dice 42,195 km mediría corto el día de la certificación, y
un maratón corto no es un maratón: los tiempos no valen para nada.

El selector de L1 tiene dos modos:

| Modo | Qué mide | Para qué |
|---|---|---|
| Eje de la calzada | Longitud de las aristas del grafo | Entrenamiento, planificación general |
| Certificable | SPR, y objetivo × 1,001 | Recorridos que van a certificarse |

En modo certificable el motor apunta la búsqueda a la SPR. Medido sobre la región
de referencia con objetivo de 10 km y ±1 %:

| Modo | Eje | SPR | ¿Certificaría? |
|---|---|---|---|
| Eje | 9.988 m | 9.823 m | No — 187 m corto |
| Certificable | 10.114 m | 9.971 m | Sí — dentro de ±1 % de 10.010 m |

#### La banda de tolerancia es asimétrica en este modo

Y no por capricho. **La certificación no es simétrica**: un recorrido que mide de
más sigue siendo homologable —los corredores hacen unos metros extra y no pasa
nada—, pero uno que mide de menos no lo es, aunque le falte un metro.

Aplicar aquí el ±2 % habitual tenía una consecuencia visible: el motor apuntaba al
objetivo exacto, la mitad de los resultados caían unos metros por debajo, y el
panel llegaba a decir a la vez **«✓ en tolerancia»** y **«✗ no certificaría»**. Esa
contradicción es peor que cualquiera de los dos números por separado, porque
enseña al usuario a no fiarse de ninguno.

Ahora, en modo certificable:

- la banda admisible es `[objetivo×1,001 , objetivo×1,001 + 2·tolerancia]`;
- el motor apunta al **centro** de esa banda, no a su borde inferior;
- el panel da un veredicto explícito de si certificaría y por cuántos metros.

Medido sobre 8 km con ±2 %: antes salía 7.985 m de SPR contra 8.008 m necesarios
—23 m corto, homologación denegada—; ahora sale 8.182 m, con 174 m de sobra.

El algoritmo es el clásico de la polilínea más corta que atraviesa una secuencia
de puertas: cada vértice puede desplazarse lateralmente hasta `ancho/2 − 0,30 m`,
y se minimiza la longitud sobre ese conjunto convexo por descenso proyectado. Se
valida contra la forma cerrada `ahorro ≈ 2·d·sin(θ/2)` en una esquina de 90°.

> **La anchura es una estimación.** OpenStreetMap etiqueta `width` en una minoría
> ínfima de las calles — en Santiago, el 0,5 % — así que se estima por clase de vía
> y se marca con confianza baja. Fingir un dato que no existe sería peor. El
> recorrido llega al medidor oficial con un error de decenas de metros en vez de
> cientos, que es la diferencia que importa.

### Vueltas

`L2 · Vueltas` es un concepto de primera clase, no una casilla de reutilización.
Nadie diseña «42 km reutilizando tramos»: diseña **tres vueltas de 14**. El motor
genera UNA vuelta con la distancia dividida y la repite, lo que además reduce
enormemente el espacio de búsqueda.

Y refleja la asimetría que hace que los organizadores prefieran las vueltas: los
cruces se multiplican —se pasa N veces por cada uno, y eso es personal real— pero
**los cierres no**, porque la calle se corta una vez. Medido en una media maratón:

| Vueltas | Distancia | Calle cerrada | Cruces |
|---|---|---|---|
| 1 | 20,95 km | 20,95 km | 166 |
| 2 | 21,09 km | 10,40 km | 167 |
| 3 | 21,14 km | 6,92 km | 169 |

Dos tercios menos de calle cerrada es la partida más cara del presupuesto de una
carrera urbana. Repetir la vuelta no cuenta como reutilización de tramos: es la
definición del formato.

### Capacidad: ¿cabe la gente?

`L7 · Inscritos previstos` activa el cálculo de cuello de botella. No hay tabla
mágica: es aritmética sobre tres supuestos declarados en `measure.py`.

```
flujo (corredores/min) = densidad × velocidad × anchura útil
                       = 0,8 corr/m² × 167 m/min × (ancho × 0,85)
```

Cuatro cifras sueltas —«ancho 6,5 · cuello km 4,2 · flujo 615/min · 4,1 min»— no
le dicen a nadie si su carrera cabe. El panel da la lectura al revés, que es como
se decide de verdad:

> Para **3.000 inscritos** harían falta **2,6 m** de ancho útil si quieres que el
> pelotón entero pase en 10 minutos. El punto más estrecho del recorrido mide
> **6,5 m** y está a 4,2 km de la salida; por ahí el pelotón tarda 1,8 min.

Los 10 minutos son una elección de producto (`CLEAR_TARGET_MIN` en `measure.py`),
no una norma, y por eso están escritos en el propio texto. El veredicto sale de la
holgura: ancho disponible ≥ 2× el necesario es «da de sobra», ≥ 1× es «justo», por
debajo es un embudo.

> **El ancho está ESTIMADO por tipo de calle.** OSM casi nunca trae `width`
> medido — el 0,5 % de las vías en Santiago. Es la mejor estimación disponible y
> se marca con confianza baja, pero para dimensionar una salida hay que medir el
> ancho real sobre el terreno.

### Importar un recorrido existente

El score de una ruta generada es un número abstracto hasta que se compara con algo
que el organizador ya conoce. `📂 Cargar GPX o GeoJSON` sube el recorrido del año
pasado, lo engancha a la red y lo puntúa **con el mismo motor, el mismo grafo y los
mismos supuestos**. La conversación pasa a ser «el tuyo saca 61, éste 74, y aquí
está por qué».

#### Los dos fallos que se veían en el mapa, y por qué ocurrían

La primera versión del emparejador se veía **escalonada** y **pasaba por sitios
por los que la traza no pasa**. Eran dos causas distintas, ambas de raíz:

1. **Adelgazaba la traza a 120 m y decidía cada punto por su cuenta.** A esa
   separación la cota del tramo entre dos anclas era `max(250 m, recta × 3)`, que
   da para rodear una manzana entera. En cada esquina el enganche podía irse por
   la calle de al lado y volver: ese ida y vuelta es el aspecto escalonado.
2. **Emparejaba con los pesos de la carrera.** Si había una zona prohibida, una
   pendiente máxima o un radio de búsqueda configurados, las calles por las que
   de verdad pasaba la traza estaban **vetadas** para el emparejador, que no tenía
   más remedio que inventar un camino por otro sitio. Ese es el segundo síntoma, y
   es el error conceptual de fondo: una traza importada tiene derecho a violar las
   restricciones del usuario — saber que las viola es justo lo que se busca.

Lo que hace ahora:

- **adelgaza a ~30 m**, con lo que una esquina conserva su vértice;
- **empareja con pesos permisivos** (toda calle de carrera vale, coste = metros) y
  detecta las violaciones DESPUÉS, al puntuar con el modelo real;
- usa **búsqueda por haz** sobre nodos candidatos en vez de decidir punto a punto:
  una hipótesis que se meta por la calzada paralela paga el rodeo en el tramo
  siguiente y muere sola;
- **corta en los huecos** en lugar de coser: si un trozo de traza no tiene red
  utilizable debajo, se declara el hueco en vez de unir dos puntos por una calle
  que nadie recorrió;
- **penaliza el cambio de rumbo** respecto a la dirección que lleva la traza, y
  **elimina los ramales de ida y vuelta** (A → B → A). Los dos atacan el mismo
  artefacto: una avenida grande viene en OSM como **dos calzadas paralelas**
  separadas veinte metros, unidas sólo en los cruces. Con ruido de GPS el nodo más
  cercano alterna entre una y otra, y el enganche cruzaba y volvía en cada manzana
  — un diente de sierra fino a lo largo de una avenida recta. Cruzar de calzada son
  dos giros de 90° respecto al rumbo de la traza, y ahora eso cuesta.

Medido con la ruta generada exportada y vuelta a importar con **ruido de GPS de
9 m** encima: 6.992 m contra 6.992 m, 100 % de la traza enganchada, 11,9 m de
desvío medio. Con calzadas paralelas separadas 28 m y ruido de 15 m —el caso que
rompía la versión anterior— cero nodos enganchados a la calzada equivocada.

#### Comparar de verdad: una sola vara de medir

Importar ya no **sustituye** lo generado: convive con ello. Antes, subir el
recorrido del año pasado hacía desaparecer exactamente aquello con lo que se
quería comparar.

Con los dos en pantalla se aplica la regla de doc 06 §4.2 —lo que se enseña junto
se puntúa junto— mediante `POST /api/routes/rescore`, que renormaliza **todos** los
recorridos en un solo conjunto. Sin eso, la generada traía la escala de sus rivales
del generador y la importada la suya propia, y comparar «61 contra 74» no comparaba
nada.

La interfaz añade: ver ambas / solo generada / solo importada, la **traza original
del fichero** dibujada por debajo del enganche (para juzgar si es fiel), una tabla
lado a lado, y un botón para adoptar la distancia del recorrido importado como
objetivo cuando cae fuera de la banda configurada.

Y se puede importar **antes de generar nada**: la sección vive fuera del bloque de
resultado, y la salida del recorrido evaluado es la de su propia traza.

### Preflight

Cada cambio dispara una verificación de factibilidad en menos de 100 ms. Si la
configuración es imposible, la app lo dice **antes** de generar, explica qué
restricción bloquea y ofrece un botón que aplica la corrección sugerida.

### Estilo de mapa

Selector **sobre el propio mapa**, abajo a la derecha: **Oscuro** (CARTO Dark
Matter, el que va con la interfaz y el que viene por defecto), **Voyager**,
**Claro**, **OSM estándar**, **Satélite** (Esri World Imagery) y **Sin mapa base**.
La atribución de cada proveedor se muestra y es obligatoria. El satélite es útil
para comprobar el terreno real de un tramo antes de proponerlo.

Vivía en la cabecera del panel izquierdo, a medio metro de lo que cambiaba. Un
control que modifica el mapa se entiende solo cuando está encima del mapa.

### Modo básico y modo avanzado

Las once capas desplegadas a la vez abruman, y el 90 % de las carreras se define
con distancia, forma, desnivel, inscritos y objetivo. El conmutador de la cabecera
decide cuánto se enseña:

- **Básico** — lo imprescindible. El resto sigue aplicándose con sus valores por
  defecto.
- **Avanzado** — todas las capas: medición certificable, reutilización de tramos,
  pendiente máxima, radio y suavidad, topes de cruces y semáforos, superficies y
  reproducibilidad.

Con una salvedad que importa: **ocultar un control no lo desactiva**. Si en modo
básico hay ajustes avanzados fuera de su valor por defecto, el panel lo dice y
cuántos son. Un panel que esconde una restricción activa es un panel que miente.

### Modos de clic y el modo Mover

Los botones sobre el mapa deciden qué hace un clic. El primero, **🖐 Mover**, no
hace nada: sólo desplaza el mapa, con el cursor de mano. Es el que evita colocar
zonas sin querer, y al que se vuelve con **Escape** desde cualquier otro modo.
Fijar la salida devuelve automáticamente a Mover, porque no se fija dos veces.
Los modos de zona sí se quedan activos: normalmente se marcan varias seguidas.

### Reproducibilidad: por qué el mismo problema da siempre la misma ruta

Con presupuesto por reloj, dos ejecuciones idénticas hacen distinto número de
iteraciones —la máquina no siempre tarda lo mismo— y devuelven rutas distintas.
Técnicamente explicable; en la práctica, desconcertante: no cambias nada y sale
otra cosa.

Por eso el **modo reproducible viene activado**. El presupuesto se convierte en
una cantidad fija de trabajo —un número de iteraciones— y ése es el criterio de
parada. El reloj queda como red de seguridad; si salta, el resultado se marca
explícitamente como **no reproducible** en el panel.

La conversión de segundos a iteraciones **se calibra por región**. Al cargar una
ciudad se mide el coste real de una consulta de ruta sobre ese grafo
(`context.calibrate`) y de ahí sale el coste por iteración:

```
ms_iter = 17 · astar_ms · km^0,4
```

Esto importa mucho más de lo que parece. La primera versión usaba una constante
fija medida sobre la región de referencia (4.761 nodos). Aplicada a Santiago
Centro (119.337 nodos), pedía el mismo trabajo, tardaba el triple del presupuesto,
saltaba la red de seguridad y **el resultado dejaba de ser reproducible**: pedías
20 segundos, tardaba un minuto y dos generaciones idénticas daban rutas distintas.
Escalar por el coste real de una consulta en ese grafo es lo que lo arregla.

La medida se **redondea a una escala geométrica** de peldaños de 1,3×: una medida
de tiempo tiene un 10–20 % de ruido y, sin redondear, ese ruido cambiaría el
número de iteraciones entre dos arranques del servidor.

El precio asumido: en una máquina lenta la misma petición tarda más, en vez de
devolver un resultado peor **y distinto**. Nos parece el intercambio correcto.
Se puede desactivar desde la casilla del panel de presupuesto.

### Una sola vara de medir

La normalización del score es **relativa al conjunto de candidatos** (doc 06 §4.2).
De ahí se sigue una regla que el motor respeta al pie de la letra: todo lo que se
enseña junto se puntúa junto, una sola vez, al final. Puntuar la recomendada
contra un conjunto y las alternativas contra otro produce el absurdo de una
«alternativa» con más puntos que la recomendación.

Por la misma razón, la recomendación se elige entre **todo** lo encontrado y no
sólo entre el frente de Pareto: el archivo rechaza rutas que se parecen más de un
70 % a otra ya guardada, y una de esas rechazadas puede ser la mejor ruta de la
ejecución.

Consecuencia para los tests: comparar índices normalizados entre dos ejecuciones
distintas no significa nada. Las comparaciones se hacen sobre métricas crudas.

### Anclaje de la salida

El punto donde pinchas y el nodo del grafo que el motor usa como salida no son lo
mismo. Antes se cogía el nodo más cercano **sin mirar las restricciones**, y eso
producía un fallo desconcertante: pinchabas sobre una avenida perfectamente
utilizable y el preflight respondía «no hay ninguna calle utilizable cerca del
punto de partida». El nodo más próximo pertenecía a una escalera, a un pasaje
privado o a un tramo por encima de la pendiente máxima, y desde ahí el grafo
admisible se veía vacío.

`packages/engine/raceengine_engine/snap.py` ancla la salida al nodo utilizable
más cercano —el que tiene al menos una arista admisible— y **sondea varios
candidatos, quedándose con el que alcanza más red**, no con el primero que supera
un mínimo. Ese matiz importa: en una ciudad densa el nodo más próximo al clic
puede pertenecer a un fragmento peatonal de doscientos nodos, que supera cualquier
mínimo modesto. Con la versión anterior el motor se quedaba ahí y a partir de ese
momento cualquier zona obligatoria —aunque estuviera a 900 m— salía «inalcanzable».
El diagnóstico acusaba a las zonas cuando el problema era el anclaje.

El sondeo es barato porque su presupuesto está acotado a 2,5 km: sólo tiene que
distinguir un fragmento aislado de la malla principal. El árbol de caminos mínimos
completo se calcula una sola vez, ya sobre el ganador. Si la salida se mueve, se
avisa con la distancia exacta en vez de bloquear.

Y cuando una zona cercana sale realmente inalcanzable, el preflight ya no la culpa
a ella: emite un único bloqueo de **conectividad de la salida** que dice cuántos
nodos se alcanzan de cuántos y qué hacer al respecto.

---

### Cuando algo falla, dónde se arregla

Dos huecos que hacían que un panel correcto fuera inútil:

- **Un tope incumplido no decía dónde se cambia.** «6 giros en U superan el máximo
  de 4» obliga a recorrer once capas buscándolo, y si vive en modo avanzado ni
  siquiera está a la vista. Ahora el panel lo dice —capa por capa— y ofrece un
  botón para desplegar los ajustes avanzados. Se deduce de los **valores**, no del
  texto del aviso: comparar cadenas traducidas para saber qué capa tocar se rompe
  en cuanto se añade un idioma.
- **Fuera de tolerancia y «no reproducible» son el mismo rojo y no son lo mismo.**
  El segundo significa que la búsqueda se cortó por el tope de seguridad antes de
  cuadrar la distancia: se acabó el tiempo, no las soluciones. Ahora se dice con
  esas palabras y hay un botón para doblar el presupuesto y reintentar.

## Ciclismo: ruta, gravel y MTB

### La disciplina decide qué calles existen

No es una preferencia estética, es la restricción más dura del sistema: determina
el grafo sobre el que se busca. Una carretera nacional es la columna vertebral de
una marcha cicloturista y un sitio al que jamás se manda a un corredor; un camino
agrícola es el pan del gravel y una trampa en un 10K urbano.

| Disciplina | Qué vías |
|---|---|
| 🏃 A pie | Residenciales, avenidas, peatonal — el prefiltro histórico, sin tocar |
| 🚴 Ruta | Lo anterior + nacionales y enlaces + vía ciclista. Asfalto obligatorio |
| 🚵 Gravel | + caminos, pistas y sendas anchas. El firme sin asfaltar deja de ser una concesión |
| ⛰️ MTB | + cañadas y sendas estrechas |

**El grafo guarda ahora el superconjunto** y la disciplina recorta en cada
petición. Antes ese recorte estaba congelado al construir la región: un grafo de
Santiago descargado para una carrera a pie no contenía ni una carretera nacional
ni un camino, así que no había forma de usarlo en bici sin reconstruirlo.

> **Compatibilidad.** `eclass` guarda el índice en la tabla de clases, y esa tabla
> es un formato en disco. Las clases nuevas se añaden **al final**: un grafo
> construido con la versión anterior tiene índices 0–9 y siguen significando
> exactamente lo mismo. Lo que sí hace falta es **reconstruir** las regiones que
> se vayan a usar en bici — las viejas no traen las vías que la bici necesita, y
> el motor lo notará como una red pobre, no como un error.

Las preferencias blandas también se dan la vuelta: la nacional se penaliza (es
rápida y es la peor de rodar), la vía ciclista se premia, y en gravel el camino de
tierra pasa a ser preferido. Y la asimetría de la cuesta se relaja: castigar la
subida como en una carrera a pie llevaba a rodear cualquier repecho por un desvío
de kilómetros.

### Velocidad: un balance de potencia, no un ritmo fijo

El primer intento fue una recta —«resta un 11 % de velocidad por cada punto de
pendiente»— y no aguantaba el contraste: daba **5 km/h en una rampa del 8 %**, que
es andar. La velocidad de un ciclista no es lineal con la pendiente porque los
términos que la frenan no lo son: la gravedad crece con la pendiente, pero el aire
crece con el **cubo** de la velocidad.

Se resuelve el balance real, que es una cúbica en `v`:

```
P·η = v · ( m·g·(pendiente + Crr) + ½·ρ·CdA·v² )
```

Por bisección, no por Newton: bajando fuerte el término lineal se vuelve muy
negativo, la derivada en el arranque es negativa y el primer paso de Newton se iba
a velocidad negativa — un descenso del 10 % salía a 4,5 km/h.

| Pendiente | −10 % | −6 % | −3 % | 0 % | +4 % | +6 % | +8 % | +12 % |
|---|---|---|---|---|---|---|---|---|
| Ruta | 65,0 | 54,4 | 42,8 | 29,6 | 15,1 | 11,2 | 8,8 | 6,1 |
| Gravel | 52,7 | 41,9 | 32,3 | 21,8 | 12,3 | 9,6 | 7,8 | 5,4 |
| MTB | 35,7 | 27,7 | 20,6 | 14,2 | 9,2 | 7,5 | 6,2 | 4,7 |

Los parámetros —180 W, 82 kg, CdA 0,40, Crr 0,006— están declarados como
constantes en `cycling.py`, porque cualquier ciclista tiene opinión sobre ellos y
debe poder cambiarlos. El tiempo se integra **arista a arista**: dos rutas de
100 km con el mismo desnivel pueden diferir una hora según cómo esté repartido, y
un ritmo medio no distingue eso.

### Puertos, no «metros de desnivel»

Un ciclista no lee «1.850 m de desnivel»: lee «tres puertos, el más duro de 6,8 km
al 7,2 %». El agregado no distingue una ruta dura de una sólo larga. El panel
detecta las subidas sostenidas (≥ 500 m y ≥ 30 m de desnivel, con hasta 250 m de
respiro antes de dar una por terminada, porque una carretera de montaña ondula) y
reporta cuántas hay, la más larga y la más dura. También el porcentaje de firme
sin asfaltar, que en gravel es la métrica que decide.

### Distancia exacta o desnivel objetivo

Las dos formas de plantear un recorrido, y no es un matiz de interfaz:

- **Distancia exacta** — un 10K son 10 km y un maratón 42,195. Se anuncia y manda.
- **Horquilla + desnivel** — en una marcha nadie promete 137 km exactos: promete
  un recorrido y una cifra de desnivel. La distancia pasa a ser un rango y el
  criterio es `elevation.target_gain_m`.

Con un desnivel buscado puesto, el «ajuste» del score deja de ser sólo la
distancia y pasa a ser las **dos promesas a la vez**, a partes iguales. Y la
preferencia por lo llano se desactiva sola: pedir 2.500 m de desnivel y a la vez
evitar las cuestas son dos órdenes contrarias, y ganaba la segunda porque actúa
dentro de la búsqueda. Hay además un objetivo nuevo, **Buscar montaña**, que baja
el peso de la dificultad a casi nada: castigar la dureza cuando la dureza es el
producto sería contradecirse.

Presets de bici: Criterium, CRI 40, Marcha 120, Marcha 180, Gravel 80, MTB 50. Los
de competición traen distancia exacta; los de marcha, horquilla y desnivel.

### Corredores: una franja a lo largo de un trayecto

Una ruta de 150 km **no cabe en un círculo alrededor de un punto**. El círculo que
la contuviera tendría 75 km de radio, y descargaría sobre todo sitios por los que
la ruta no pasa.

Se marca el trayecto sobre el mapa —salida, un par de puntos de paso, meta— y se
descarga sólo la franja a su alrededor, con **recorte poligonal en el propio
Overpass** (`poly:`), así que lo que viaja por la red ya es sólo la franja. Medido
sobre Santiago → Valparaíso con 8 km a cada lado:

| | Área | |
|---|---|---|
| Franja del corredor | 1.359 km² | |
| Círculo que lo contendría | 10.730 km² | **87 % menos que descargar** |

El panel enseña esa comparación **antes** de descargar: una espera de veinte
minutos no debería ser una sorpresa. Hay topes declarados —600 km de trayecto,
12.000 km² de franja— porque por encima Overpass corta por su cuenta.

**El muestreo de elevación también tuvo que cambiar.** Con paso fijo de 120 m
sobre la caja envolvente, un corredor de 200 km da dos millones de puntos de
rejilla: veinte mil peticiones y varias horas. Ahora el paso se afloja en peldaños
deterministas hasta caber en el tope, y **sólo se piden las celdas que tienen red
debajo**. Una región circular desperdicia un 21 % de su caja envolvente; un
corredor a lo largo de una carretera desperdicia el 95 %.

## Regiones reales (31 ciudades)

### Varias comunas a la vez

**Una comuna no es una unidad deportiva.** Un recorrido de Santiago cruza Las
Condes, Providencia y Ñuñoa sin enterarse. Con una sola región cargada, el motor
no evitaba las otras por criterio: es que esas calles **no existían** para él, y
el resultado salía perfectamente coherente sobre un tercio del mapa sin que nada
lo advirtiera. Es el peor tipo de error, porque no se parece a un error.

El selector de región es una lista de casillas: marca todas las comunas por las
que pueda pasar el recorrido y se **unen en un solo grafo de trabajo**. Los nodos
se identifican por su id de OpenStreetMap, así que la unión es exacta en las
costuras — un cruce que aparece en dos descargas es UN nodo, no dos, y por eso el
recorrido puede pasar de una comuna a la otra por la calle que las une.

El identificador de región pasa a ser compuesto (`las_condes+nunoa+providencia`) y
viaja como uno solo por el modelo, el guardado de carreras y la caché de contextos.
Las cifras que muestra el panel son las del grafo **ya unido**, no la suma de las
partes: en las costuras hay nodos compartidos y prometer una precisión inventada
sería peor que esperar los dos segundos que cuesta unirlos.

Además, **en cuanto hay datos reales la ciudad sintética deja de estar marcada**.
Descargar tres comunas y seguir midiendo sobre `demo` era demasiado fácil.

La región `demo` es sintética y sirve para probar la herramienta al instante.
Para trabajar con datos reales de OpenStreetMap, lo más cómodo es hacerlo **desde
la propia app**: elige la ciudad en el selector y pulsa **Construir región**. Verás
una barra de progreso real (porcentaje, fase actual y tiempo estimado), no un
spinner indefinido. También desde la línea de órdenes:

**macOS / Linux**

```bash
./.venv/bin/python build.py --list                                   # ver estado
./.venv/bin/python build.py santiago_centro --elevation opentopodata
./.venv/bin/python build.py las_condes      --elevation opentopodata
```

**Windows**

```powershell
.\build.bat --list
.\build.bat santiago_centro --elevation opentopodata
```

> En Windows usa siempre los `.bat`: no dependen de PowerShell y esquivan la
> política de ejecución de scripts, que viene bloqueada de fábrica.

Se pueden **descargar varias de una vez**: la lista es de casillas, marcas las
comunas que necesites y el botón pasa a decir «Descargar N regiones». Las
construye **una tras otra desde el servidor**, no desde el navegador — bajar seis
comunas son veinte minutos y no tiene sentido obligar a dejar la pestaña abierta.
La barra indica cuál va y cuántas quedan.

Que la cola tenga un solo trabajador es deliberado: Overpass limita por IP y
lanzar seis descargas en paralelo es la forma más rápida de que las rechace todas.

| Endpoint | Qué hace |
|---|---|
| `POST /api/regions/build-batch` | Encola varias regiones. Idempotente: ignora las que ya están en curso o en cola |
| `GET /api/regions/build-queue` | Cuál se está construyendo, cuáles quedan, cuáles terminaron y cuáles fallaron |

El selector las agrupa por continente / país:

| Grupo | Regiones |
|---|---|
| Demo | `demo` (sintética) |
| Santiago | `santiago_centro`, `las_condes`, `providencia`, `nunoa`, `vitacura`, `santiago_metro` |
| Chile | `valparaiso`, `vina_del_mar`, `concepcion` |
| LATAM | `buenos_aires`, `montevideo`, `lima`, `bogota`, `mexico_city`, `sao_paulo`, `rio` |
| Europa | `london`, `paris`, `madrid`, `barcelona`, `berlin`, `amsterdam`, `rome`, `lisbon` |
| Norteamérica | `new_york`, `chicago`, `boston`, `san_francisco` |
| Asia-Pacífico | `tokyo`, `sydney` |

Sí, funciona en Londres o en Tokyo igual que en Las Condes: el motor no sabe nada
de Chile, sólo consume un grafo.

### Cualquier otra ciudad — el buscador

El catálogo de arriba es un **atajo, no un límite**. Bajo el selector de región,
`🔎 Buscar y descargar otra ciudad` abre un buscador contra OpenStreetMap
(Nominatim): escribe el nombre, elige el resultado correcto —el mapa vuela ahí
para que compruebes que acertó antes de descargar—, ajusta el radio y pulsa
descargar. Se construye con el mismo pipeline y la misma barra de progreso que una
región curada, y aparece en el selector bajo **Mis ciudades**.

El radio sugerido sale del *bounding box* que devuelve Nominatim, acotado a
2,5–25 km: por debajo no hay red ni para un 5K y por encima la descarga de
Overpass se vuelve impracticable. Las ciudades propias se guardan en
`data/custom_regions.json` con escritura atómica, así que sobreviven a un
reinicio —si no, reiniciar el servidor haría desaparecer del selector una ciudad
que ya está en disco— y se pueden quitar desde la propia ficha de la región.

| Endpoint | Qué hace |
|---|---|
| `GET /api/geocode?q=…` | Busca lugares. Devuelve `[]` con `error` si no hay red, en vez de romper |
| `POST /api/regions/custom` | Registra la región y arranca la descarga. Idempotente por `id` |
| `DELETE /api/regions/custom/{id}` | Quita la región y borra su grafo. Las curadas no se tocan |

> Nominatim es un servicio público con política de uso estricta: se respeta el
> User-Agent identificado y ~1 petición/segundo. Para producción hay que
> auto-hospedarlo o contratar un geocodificador, igual que con los teselados
> (doc 07 §2.4).

Añadir una ciudad al catálogo **curado** sigue siendo un cambio de datos, no de
código (NFR-2.3): una entrada en `REGIONS` dentro de
`packages/geodata/raceengine_geodata/regions.py` con centro y radio.

### Sobre la elevación

`--elevation opentopodata` usa una API pública gratuita, limitada a ~1 req/s.
Muestrear nodo a nodo tardaba más de 30 minutos por comuna. Ahora se muestrea una
**malla de 120 m** y se interpola bilinealmente a cada nodo: unos 4 minutos, **una
sola vez** (queda cacheado), con error muy por debajo del ruido del propio DEM.

Todos los DEM globales gratuitos son **modelos de superficie** (DSM): incluyen
copas de árboles y edificios, lo que produce escalones falsos. Se corrigen con
suavizado sobre el grafo (2 pasadas, mezcla 60/40) y recorte de pendientes a 18 %,
y el número de aristas implausibles se reporta en la construcción. Es un
compromiso de arranque, no la arquitectura objetivo.

Lo correcto en producción es un DEM local — Copernicus GLO-30, según `07_Data_Strategy` §3.1:

```bash
pip install rasterio
# Descarga tu tile desde https://dataspace.copernicus.eu (registro gratuito)
./.venv/bin/python build.py santiago_centro \
    --elevation local_dem --dem data/dem/copernicus_santiago.tif
```

**Punto importante:** la elevación se hornea en las aristas del grafo al
construir la región. Eso significa que `max_gain_m`, `max_gradient_pct` y
`prefer_flatter` **influyen en la búsqueda**, no solo en el informe final. En el
MVP anterior `Edge.grad` valía `0.0` durante toda la generación y la elevación no
afectaba a ninguna decisión (hallazgo H1 de `10_Engine_V2_Architecture_Review.md`).
Sin datos de elevación la app lo dice explícitamente en vez de fingir.

---

## Arquitectura

```
raceengine/
├── serve.py                  lanzador multiplataforma (resuelve sys.path solo)
├── build.py                  construye regiones (idem)
├── run.sh · run.bat          arranque en un comando (macOS-Linux · Windows)
├── build.bat                 idem para construir regiones en Windows
├── docker-compose.yml        opcional: graphhopper · postgis · redis
├── apps/
│   └── api/                  FastAPI: routers, SSE, gestor de jobs
├── packages/
│   ├── contracts/            Pydantic v2 — ÚNICA fuente de verdad
│   │   ├── constraints.py      ConstraintModel (frozen) + las 10 capas
│   │   ├── metrics.py          catálogo de métricas + confianza
│   │   └── results.py          RouteCandidate · Incumbent · GenerationResult
│   ├── routing/              sustrato reemplazable
│   │   ├── base.py             Protocol RoutingProvider
│   │   ├── local/              A* + Dijkstra sobre CSR en numpy
│   │   └── graphhopper/        adaptador HTTP (opcional)
│   ├── geodata/              pipeline OFFLINE
│   │   ├── graph.py            RaceGraph en CSR + índice espacial
│   │   ├── osm.py              Overpass → grafo
│   │   ├── synthetic.py        ciudad sintética determinista
│   │   ├── elevation.py        proveedores DEM
│   │   └── regions.py          registro de regiones
│   └── engine/               ◄── EL NÚCLEO
│       ├── preflight.py        VR-1..VR-7, < 100 ms
│       ├── weights.py          ConstraintModel → pesos de arista
│       ├── generate/           coverage-first: área → anclas → K circuitos
│       ├── repair/             Mandatory Repair · Distance Adjuster
│       ├── metrics/            catálogo de doc 06 §3
│       ├── scoring/            normalización · 4 índices · score global
│       ├── improve/            anytime · operadores · archivo de Pareto
│       └── engine.py           orquestador del pipeline
├── web/index.html            interfaz completa en un archivo (Leaflet)
├── infra/graphhopper/        config + custom models
└── tests/                    32 tests, sin red
```

### El pipeline

```
ConstraintModel (frozen)
   │
   ├─ 0 · PREFLIGHT          VR-1..VR-7 · factibilidad real     < 100 ms
   ├─ 1 · LOOP GENERATOR     coverage-first: área → anillo → K circuitos
   ├─ 2 · MANDATORY REPAIR   snap · desvío · reanclaje
   ├─ 3 · DISTANCE ADJUSTER  bulge · chord · reescalado (transaccional)
   ├─ 4 · METRICS + SCORING  20 métricas · 4 índices · score 0–100
   ├──── EMITE LA RUTA RECOMENDADA ────
   └─ 5 · ANYTIME IMPROVE    6 operadores · bandit · Pareto ≤ 70 % solape
```

### Las zonas obligatorias son SEMILLA, no parche

Éste es el cambio de diseño más importante del motor, y salió de un fallo real:
un maratón en Santiago con cinco zonas obligatorias devolvía tres o cuatro sin
cubrir, y —el síntoma que lo delató— **con más presupuesto salía peor**.

Había dos causas, ambas estructurales:

1. **Las zonas no participaban en la generación.** Se producían anillos por
   muestreo de la isócrona, ignorando las zonas, y después Mandatory Repair las
   insertaba una a una. Con una zona funciona. Con cinco repartidas por una ciudad
   de 120.000 nodos, cada inserción deforma el anillo y el presupuesto se agota
   antes de terminar. Ahora `mandatory_seed_anchors` elige un ancla por zona —una
   intersección alcanzable cerca de su centro— y **todos los anillos nacen con
   ellas dentro**. La cobertura es gratis desde el primer circuito.

2. **El bucle de mejora las borraba.** Los seis operadores movían o eliminaban un
   ancla cualquiera, incluidas las de zona obligatoria, y como el criterio de
   aceptación mira primero el error de distancia, un candidato que arreglaba la
   longitud a costa de perder una zona entraba igual. De ahí que más iteraciones
   dieran peor cobertura. Ahora las anclas obligatorias van en un conjunto
   `protected` que ningún operador puede tocar; `swap_anchor_pair` sigue libre
   porque sólo reordena.

Medido sobre la región de referencia, maratón de 42,195 km con cinco zonas:

| Presupuesto | Cobertura | Distancia | En tolerancia | Score |
|---|---|---|---|---|
| 10 s | 5/5 | 42,133 km | Sí | 45,1 |
| 25 s | 5/5 | 42,242 km | Sí | 55,2 |
| 50 s | 5/5 | 42,195 km | Sí | 69,0 |

Más presupuesto vuelve a significar mejor resultado, que es lo mínimo que se le
puede pedir a un algoritmo *anytime*.

### Generación *coverage-first*

El MVP anterior era *shape-first*: elegía una figura geométrica (`box`, `poly7`),
la rotaba y escalaba, y forzaba los puntos obligatorios dentro como vértices. Eso
deformaba el circuito y provocaba 2.880 semillas de fuerza bruta.

Aquí se parte del **área que el circuito debe encerrar**, se muestrean anclas
sobre la red **realmente alcanzable por calle** (no por radio en línea recta), se
rutea el anillo, y los obligatorios se insertan después como una *reparación
local*. Esa inversión de orden es todo el sentido de "coverage-first".

---

## API

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/api/regions` | regiones + cobertura de atributos por región |
| `GET` | `/api/presets` | los seis presets de `04_Layers` §5 |
| `POST` | `/api/preflight` | factibilidad sin generar (< 100 ms) |
| `POST` | `/api/generations` | crea la generación → `202` |
| `GET` | `/api/generations/{id}` | estado + incumbente actual |
| `GET` | `/api/generations/{id}/stream` | **SSE**: `stage` · `recommended` · `done` |
| `POST` | `/api/generations/{id}/cancel` | cancelación cooperativa |
| `GET` | `/api/routes/{id}.gpx` | export GPX |
| `GET` | `/api/routes/{id}.geojson` | export GeoJSON |
| `POST` | `/api/routes/import` | engancha un GPX/GeoJSON a la red y lo puntúa |
| `POST` | `/api/routes/rescore` | renormaliza varios recorridos en UNA escala |
| `POST` | `/api/regions/compose` | une varias comunas en un grafo de trabajo |

Documentación interactiva en **http://localhost:8000/docs**.

Ejemplo:

```bash
curl -X POST localhost:8000/api/preflight -H 'Content-Type: application/json' -d '{
  "region": "demo",
  "start": {"lat": -33.4372, "lon": -70.6506},
  "distance": {"target_m": 10000, "tolerance_pct": 2.0},
  "geometry": {"search_radius_max_m": 4000},
  "elevation": {"enabled": true, "max_gain_m": 150},
  "zones": [{"id":"z1","name":"Estadio","behavior":"mandatory",
             "geometry":{"type":"Point","coordinates":[-70.6300,-33.4250]},
             "buffer_m":300}],
  "objective": "fewest_closures"
}'
```

---

## Trazabilidad con los documentos aprobados

| Documento | Qué implementa este código |
|---|---|
| `04_Layers` | `ConstraintModel` con las 10 capas; VR-1..VR-7 en `preflight.py`; los 6 presets |
| `06_Metrics` | catálogo de métricas, normalización, 4 índices, score global 0–100, tabla objetivo→pesos, Pareto con solape ≤ 70 % |
| `07_Data` | pre-filtro de grafo de carrera, cobertura por atributo con umbrales 80/40, factores de confianza 1,0/0,6/0,3, atribución ODbL |
| `08_NFR` | anytime + streaming (NFR-1.3), cancelación < 500 ms (NFR-1.4), determinismo (NFR-6.1) |
| `09_System_Architecture` | AD-1 anytime, AD-2 semilla desde el hash de la petición, AD-3 GraphHopper detrás de `RoutingProvider`, FastAPI |
| `10_Architecture_Review` | corrige H1 (elevación invisible), H2 (no determinismo), H3 (score descartado), H4 (coverage-first), B1–B9 |

### Estado por capa

| Capa | Estado |
|---|---|
| L1 Distancia | ✅ objetivo + tolerancia como restricción dura |
| L2 Tipo | 🟡 solo `circuit` (abierto / ida-vuelta / multi-vuelta: fase F6) |
| L3 Elevación | ✅ desnivel y pendiente influyen en la búsqueda |
| L4 Geometría | ✅ radio, suavidad, giros en U |
| L5 Intersecciones | ✅ conteo + topes duros opcionales |
| L6 Calles | ✅ superficie, privadas, pre-filtro por clase de vía |
| L7 Logística | 🟡 impacto de cierre, encerramiento, hospitales (capa de operaciones completa: fase F7) |
| L8 Experiencia | ✅ parques, túneles |
| L9 Seguridad | 🟡 cercanía a hospitales, iluminación |
| L10 Zonas | ✅ obligatorias y prohibidas (círculo o polígono GeoJSON) |

---

## Dos desviaciones documentadas

**1. La fórmula de `fit` de `06_Metrics` §4.1.** El documento define
`n = max(0, 1 − |v − target| / tolerance_band)`. Esa función vale 0 para *todo*
candidato fuera de la banda, así que su gradiente desaparece justo cuando el
optimizador más lo necesita. Lo verifiqué: sin corregirlo, el motor degradaba una
ruta de 8,68 km a 7,59 km sobre un objetivo de 9 km, porque acercarse al objetivo
no mejoraba el score. La implementación conserva la semántica (1.0 en el objetivo
exacto, monótona decreciente) pero añade una cola continua fuera de la banda.
Está documentado en `scoring/score.py::distance_fit` y cubierto por un test.

**2. Determinismo bajo presupuesto de reloj.** La geometría es idéntica entre
ejecuciones, pero el score puede variar en centésimas porque el conjunto de
candidatos depende de cuántas iteraciones caben en el presupuesto, y la
normalización de `06` §4.2 es relativa a ese conjunto. Para reproducibilidad
estricta (`NFR-6.1`, golden master) usa `deterministic_iterations`, que acota el
bucle por iteraciones en vez de por tiempo. Los tests lo verifican bit a bit.

---

## Activar GraphHopper (opcional)

El motor está diseñado para que el sustrato de routing sea reemplazable
(`RoutingProvider`). El proveedor por defecto es el local; para usar GraphHopper:

```bash
mkdir -p data/pbf
curl -o data/pbf/chile.osm.pbf https://download.geofabrik.de/south-america/chile-latest.osm.pbf
osmium extract -b -70.85,-33.65,-70.45,-33.30 data/pbf/chile.osm.pbf -o data/pbf/santiago.osm.pbf

docker compose --profile graphhopper up graphhopper
export RACEENGINE_ROUTING=graphhopper
export RACEENGINE_GH_URL=http://localhost:8989
```

**Aviso conocido:** la Matrix API de GraphHopper es de licencia comercial y no
está en el bundle Apache-2.0. El motor está diseñado para no depender de ella
(el 2-opt sobre ≤ 12 anclas se resuelve con `/spt` o rutas por lotes), pero
conviene verificarlo contra la versión concreta que despliegues. Es el riesgo
GH-R1 de `11_Engine_V2_Design.md` §6.4.

---

## Tests

```bash
make test
# o:  python -m pytest tests/ -q
```

32 tests, sin dependencia de red (usan la región sintética determinista). Cubren:
contratos e inmutabilidad, reglas de validación, determinismo bit a bit,
convergencia de distancia, cobertura de zonas obligatorias, no-violación de zonas
prohibidas, que la elevación influye realmente en la búsqueda, monotonía del
incumbente, cancelación, límite de solape entre alternativas y la capa HTTP
completa incluidos los exports.

---

## Rendimiento observado (región demo, 4.761 nodos)

| Escenario | 1.ª ruta | Total | Distancia | Error | En tolerancia |
|---|---|---|---|---|---|
| 5K limpio | 81 ms | 1,9 s | 4,998 km | −2 m | ✅ |
| 9K + zona obligatoria + prohibida | 53 ms | 6,4 s | 9,004 km | +4 m | ✅ |
| 21K media maratón | 172 ms | 6,3 s | 21,099 km | +2 m | ✅ |
| 10K con tope de desnivel 60 m | 61 ms | 5,6 s | 10,002 km | +2 m | ✅ |

Sobre un grafo real de Santiago los tiempos serán mayores (más nodos), pero el
orden de magnitud del gap contra el MVP anterior —que declaraba un techo de 240 s—
se mantiene. Mide con tu propia región antes de sacar conclusiones.

---

## Limitaciones conocidas

- **Solo circuitos** (`start = finish`). Abierto, ida-vuelta y multi-vuelta están
  diseñados pero no implementados (fase F6 del plan de migración).
- **Zonas circulares en la UI.** El backend acepta polígonos GeoJSON completos;
  la interfaz solo permite dibujar círculos por ahora.
- **La capa de operaciones** (`FR-8`: estimación de personal, acceso de
  ambulancias, avituallamientos) emite banderas pero no el plan completo.
- **Sin persistencia.** Las generaciones viven en memoria del proceso. PostGIS
  está en el compose para cuando llegue la fase F4.
- **Sin autenticación ni multi-tenancy.** Es una herramienta de diseño local.
- **Teselas de mapa** desde `tile.openstreetmap.org`: aceptable en desarrollo,
  prohibido en producción (`07_Data_Strategy` §2.4). Hay que auto-hospedarlas.

---

## Atribución

Datos viales © OpenStreetMap contributors, bajo ODbL.
Elevación: SRTM vía OpenTopoData, o Copernicus DEM GLO-30 © ESA si usas un DEM local.
Toda ruta exportada lleva la atribución incrustada.

## Licencia

Copyright © 2026 Ignacio Arteaga. Todos los derechos reservados: el código es
público sólo para leerlo; no se permite copiarlo ni usarlo sin permiso. Ver
[`LICENSE`](LICENSE).
