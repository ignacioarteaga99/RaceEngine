# Cómo correrlo — guía rápida

## 1. Requisito

**Python 3.12 o superior.** Es obligatorio, no una recomendación.

```bash
# macOS
brew install python@3.12
python3.12 --version     # debe decir 3.12.x o superior
```

## 2. Arrancar

### macOS / Linux

```bash
unzip raceengine-v2.zip
cd raceengine
./run.sh
```

### Windows

Descomprime el zip, entra en la carpeta `raceengine` y **haz doble clic en
`run.bat`**. Desde CMD o PowerShell:

```powershell
.\run.bat
```

`run.bat` **no usa PowerShell**, así que la política de ejecución de scripts no
le afecta. Es la vía recomendada en Windows.

> **Si intentas `.\run.ps1` y te dice "is not digitally signed"**, es la política
> de ejecución de PowerShell bloqueando scripts sin firmar. Windows viene así de
> fábrica. Usa `.\run.bat` y listo. Si aun así prefieres el `.ps1`:
> `powershell -ExecutionPolicy Bypass -File .\run.ps1`

### Cualquier sistema (si ya tienes las dependencias)

```bash
python serve.py
```

---

El navegador se abre solo en **http://localhost:8000**.

El lanzador hace todo: crea el entorno virtual, instala las 5 dependencias,
descarga Leaflet, genera una ciudad sintética de prueba y levanta el servidor.
Tarda entre uno y dos minutos la primera vez; después, segundos.

Opciones útiles:

```bash
./run.sh --port 8080              # otro puerto
./run.sh --region las_condes      # precalentar otra región
./run.sh --reload                 # recarga al editar código
./run.sh --no-open                # no abrir el navegador
```

## 3. Probar la herramienta

1. Haz clic en el mapa → fija la salida.
2. Pulsa **✅ Obligatorio** y haz clic en dos o tres puntos → zonas por las que
   la ruta debe pasar.
3. Pulsa **⛔ Prohibido** y haz clic en otro punto → zona vetada.
4. Ajusta a la izquierda: distancia, tolerancia, desnivel máximo, pendiente
   máxima, radio, suavidad, topes de cruces y semáforos.
5. **Generar ruta**. Verás la geometría casi al instante y cómo se refina sola.

Si algo es imposible, el panel derecho te lo dice **antes** de generar y ofrece un
botón que aplica la corrección sugerida.

### Modos de clic

Los botones sobre el mapa deciden qué hace un clic. **🖐 Mover** no coloca nada:
sólo desplaza el mapa. Vuelves a él con **Escape**, y fijar la salida te devuelve
solo. El **estilo de mapa** (oscuro, claro, Voyager, OSM, satélite) se cambia con
el selector que hay **sobre el propio mapa**, abajo a la derecha.

### Elige la disciplina

Lo primero del panel izquierdo, y no es un detalle: decide qué calles existen para
el motor.

| | Para qué |
|---|---|
| 🏃 A pie | Carrera urbana: calles y avenidas |
| 🥾 Trail | Correr por monte: sendas, caminos y pistas |
| 🧭 Senderismo | Andar por monte, escaleras incluidas |
| 🚴 Ruta | Bici de asfalto, nacionales incluidas |
| 🚵 Gravel | Asfalto + caminos y pistas de tierra |
| ⛰️ MTB | Además cañadas y sendas |

El ritmo o la velocidad se calculan distinto en cada una: por potencia en bici,
por la curva de Tobler a pie por monte.

Cambiar de disciplina cambia los presets, la velocidad con la que se estima el
tiempo y las métricas del panel (aparecen los puertos y el porcentaje sin
asfaltar).

> Si ya tenías comunas descargadas **para correr**, reconstrúyelas antes de usarlas
> en bici: no traen las carreteras ni los caminos que la bici necesita.

### Ir a un sitio del mapa

Abajo a la derecha, sobre el mapa: **Ir a…**. Escribe un lugar (*San Vicente de
Tagua Tagua*, *Cerro Provincia*, *Embalse Rapel*) o pega unas **coordenadas**
(`-34.4361, -71.0742`). No descarga nada: sólo te lleva.

### Una ruta larga, de punta a punta

Por ejemplo, **de San Vicente de Tagua Tagua a Rapel en bici**:

1. Elige la disciplina — **Ruta** o **Gravel**.
2. **Ir a…** *San Vicente de Tagua Tagua*.
3. Pulsa **📈 Trazar trayecto** en la barra del mapa y haz clic en la salida, uno
   o dos puntos por donde quieras pasar, y en Rapel. Sobre el mapa aparece una
   barra con los puntos, el largo, y **↶ Deshacer** y **Borrar**.
4. Ajusta el **ancho de la franja** (por defecto 6 km a cada lado). El panel te
   dice lo que abarca y cuánto te ahorras frente a descargar el círculo que lo
   contendría — suele ser un 85 % menos.
5. Ponle nombre y **⬇ Descargar esta franja**. Tarda unos minutos.

Al terminar, **la carrera queda montada**: la región seleccionada, el recorrido
puesto como punto a punto, la salida y la meta en los extremos y una horquilla de
distancia con holgura. Ajusta lo que quieras y pulsa **Generar ruta**.

Para MTB en el cerro es exactamente igual: disciplina **MTB**, trayecto por donde
suba la ruta, y a generar. El trayecto no aparece en carrera urbana a pie, porque
ahí no hace falta.

### Salida y meta por nombre, e intercambiarlas

Sobre el mapa, arriba, hay dos casillas: **Salida** y **Meta**. Escribe el nombre
de un lugar —o pega unas coordenadas— y se coloca el punto; también puedes seguir
pinchando el mapa, y entonces la casilla muestra las coordenadas.

El botón **⇅** entre las dos intercambia las puntas. Como invertir el trayecto
cambia el problema, la ruta que hubiera generada se descarta: hay que volver a
generar.

Poner una meta convierte el recorrido en punto a punto por sí solo. No hace falta
decirlo dos veces.

### Sólo quiero la ruta de A a B

En **L1 · Distancia**, pestaña **Sin distancia**. Pon la salida, la meta y los
puntos de paso obligatorios que quieras, elige el objetivo (más llana, más rápida,
menos cierres, buscar montaña…) y genera.

No te pedirá kilómetros ni te dirá que estás fuera de tolerancia: la distancia
sale de la ruta, no al revés. Los puntos de paso se recorren en el orden más
corto, no en el que los marcaste, y te da tres variantes para elegir.

Vale igual para correr, para bici y para monte.

### Kilómetros exactos o desnivel

En **L1 · Distancia** hay dos pestañas:

- **Distancia exacta** — para competición. Un 10K son 10 km.
- **Horquilla + desnivel** — para una marcha. Pones un rango de kilómetros y, en
  **L3**, el *desnivel buscado*. El motor va a **buscar** esa cifra en vez de
  evitar las cuestas; la preferencia por lo llano se apaga sola, porque serían dos
  órdenes contrarias.

En el desplegable de objetivo tienes además **Buscar montaña**.

### El perímetro de la zona cargada

En **Región** hay una casilla, *Ver el perímetro de la zona cargada*. Dibuja hasta
dónde llega lo descargado —un círculo para una ciudad, la franja real para una
ruta larga—. Verlo explica de un vistazo por qué un recorrido se corta o por qué
un punto queda inalcanzable.

Apagarlo **no deselecciona nada**: el perímetro es información, no la selección.
La preferencia se recuerda.

Cuando terminas de descargar una franja, los puntos que marcaste para definirla se
borran solos y pasa a verse el perímetro de lo que de verdad se descargó.

### Bici y monte arrancan sin distancia

Al elegir Ruta, Gravel, MTB, Trail o Hiking, L1 se pone en **Sin distancia**: fuera
del asfalto urbano la pregunta casi nunca son los kilómetros exactos. Si cambias
la pestaña a mano, se respeta y deja de moverse sola.

### Si el recorrido aparece cortado

Mira el bloque **Región**. Si el mapa se descargó a trozos, ahí lo dice: cuántos y
qué parte cubre el mayor. El motor sólo puede trazar dentro del trozo donde caiga
la salida — lo demás no existe para él.

Se arregla volviendo a descargar la región, o marcando también la comuna vecina
para coser la costura.

### Si un aviso no dice dónde se cambia

Ahora sí: bajo el aviso aparece la capa concreta y, si está en modo avanzado, un
botón para desplegarlo.

Y si ves **«no reproducible»** junto a «fuera de tolerancia», no es que no exista
solución: es que se acabó el tiempo. Hay un botón para doblarlo y reintentar.

### Vaciar y eliminar

**🧹 Vaciar** borra salida, meta, zonas, ruta y trazado, y deja la región y la
disciplina como estaban. **Eliminar** borra la carrera guardada; si estás en una
sin guardar te ofrece vaciar, en vez de no hacer nada.

### Básico o avanzado

Arriba a la derecha, junto al idioma. Se arranca siempre en **Básico**, que deja a la vista lo imprescindible
—distancia, forma, vueltas, desnivel, inscritos y objetivo— y el resto se aplica
con sus valores por defecto. **Avanzado** despliega todo lo demás.

Si en básico tienes algún ajuste avanzado cambiado, el panel te lo dice: sigue
aplicándose aunque no se vea.

### Varias comunas a la vez

El selector de región es una lista de casillas. **Marca todas las comunas por las
que pueda pasar el recorrido**: se unen en un solo mapa de trabajo. Lo que no
marques, para el motor no existe — y eso no da error, da un recorrido plausible
medido sobre medio mapa.

En cuanto descargas datos reales, la ciudad de prueba se desmarca sola.

### Zonas: mover, redimensionar y nombrar

- **Arrastra el centro** de una zona para desplazarla.
- **Arrastra el punto del borde** (el tirador) para cambiar el radio.
- **Doble clic** sobre la zona para renombrarla en el sitio.

### Lecturas al pasar el cursor

- **Sobre el mapa**: distancia en línea recta a la salida/meta y coordenadas.
- **Sobre cualquier ruta visible** (recomendada o alternativa): kilómetro de
  carrera, altitud y pendiente — la misma lectura que da el perfil.
- **Sobre el perfil de elevación**: kilómetro, altitud y pendiente, con cursor
  sincronizado sobre la curva y sobre el mapa.

### Modo creativo: trazar a mano

Pulsa **✏️ Trazar a mano**. Cada clic engancha a la calle más cercana y traza el
tramo siguiendo la red vial; el panel derecho se va rellenando con la altimetría,
los cruces, los semáforos y el score conforme dibujas.

El motor **no te deja** romper lo que configuraste a la izquierda: rechaza el
tramo si no cabe contando la vuelta a la meta, si el desnivel acumulado se pasa
del máximo o si no hay camino admisible hasta ahí (zona prohibida, pendiente
excesiva, radio de búsqueda). Cada rechazo te dice por qué y con qué números.

**⭕ Cerrar recorrido** exige caer dentro de la banda de tolerancia. **↶ Deshacer
tramo** retrocede clic a clic. El trazado se guarda con la carrera.

### Si el recorrido va a certificarse

En **L1 · Distancia** (modo avanzado) hay un selector de medición.

Un medidor oficial **no mide por el centro de la calle**: mide la línea más corta
que un corredor puede tomar legalmente, cortando las curvas por dentro a un palmo
del bordillo. Y sobre esa línea busca un 0,1 % de más, para que el recorrido nunca
quede corto. Un recorrido que en pantalla marca 42,195 km medirá **corto** el día
de la certificación.

Déjalo en *Eje de la calzada* para entrenar o planificar. Ponlo en **Certificable**
si va a pasar por un medidor: el motor apunta a esa línea, y el panel te dice con
todas las letras si certificaría y por cuántos metros.

En ese modo la tolerancia deja de ser simétrica, y es a propósito: pasarse unos
metros no invalida nada, quedarse corto sí, aunque falte un metro.

### Vueltas

**L2 · Vueltas** hace lo que un organizador espera: tres vueltas de 14 km en vez
de un circuito de 42. Cierras un tercio de la calle y pasas tres veces por cada
cruce, que es exactamente el intercambio que abarata una carrera urbana.

### ¿Cabe la gente?

Pon los **inscritos previstos** en la sección *Capacidad* (hay ejemplos de un clic:
500, 2.000, 5.000, 15.000). El panel te lo dice en la dirección en la que se
decide: *«para 3.000 inscritos harían falta 2,6 m de ancho útil si quieres que el
pelotón pase en 10 minutos; el punto más estrecho mide 6,5 m y está a 4,2 km de la
salida»*.

Ojo: el ancho de calle está **estimado** por tipo de vía, porque OSM casi nunca lo
trae medido. Para dimensionar una salida, mide sobre el terreno.

### Comparar con otro recorrido

En el panel derecho, **📂 Cargar GPX o GeoJSON**. Sirve para cualquier traza: el
GPX de otra carrera, una ruta que ya tienes, la grabación de una salida. Puedes
hacerlo **antes de generar nada**. Se engancha a las calles y se puntúa con el
mismo motor que las rutas generadas.

Lo importante: el recorrido importado **se queda en pantalla** junto a lo que
generes después, y ambos se puntúan en la misma escala. Tienes *ver ambas*, *solo
generada*, *solo importada*, y una casilla para dibujar por debajo la traza
original del fichero, que es como se comprueba si el enganche es fiel.

Si la traza no cabe en la banda de distancia que tienes configurada, aparece un
botón para adoptar su distancia como objetivo.

El panel te dice qué porcentaje de la traza se pudo enganchar y cuánto se desvía.
Si hay tramos sin calle debajo, se corta ahí y te avisa, en vez de inventar un
atajo.

### ¿Por qué sale siempre la misma ruta?

Porque debe. La casilla **Resultado reproducible** (activada por defecto, en el
panel de presupuesto) hace que el motor gaste una cantidad fija de trabajo en vez
de segundos de reloj: la misma configuración devuelve siempre el mismo recorrido.
Si alguna vez ves el distintivo «no reproducible», es que el tope de seguridad
cortó la búsqueda antes de tiempo.

### Carreras (proyectos) y galería

Cada carrera es una pestaña independiente con sus propias zonas, ajustes y ruta.
La ruta generada **se conserva al cambiar de pestaña**; sólo se recalcula si
cambias restricciones y vuelves a generar. Puedes crear, renombrar, duplicar,
guardar, cerrar y eliminar carreras. El botón **Galería** abre todas las carreras
guardadas con una miniatura de su ruta, para revisarlas y volver a abrirlas.

También puedes alternar **español / English** y **km / millas** desde la barra
superior en cualquier momento, sin regenerar nada.

## 4. Usar datos reales (31 ciudades)

La región `demo` es sintética (arranca al instante, sin red). Para datos reales:

**macOS / Linux**

```bash
./.venv/bin/python build.py --list
./.venv/bin/python build.py santiago_centro --elevation opentopodata
```

**Windows**

```powershell
.\build.bat --list
.\build.bat santiago_centro --elevation opentopodata
```

Tarda varios minutos (descarga OSM + muestrea la elevación en malla de 120 m con
interpolación), **una sola vez**. Después la región aparece en el selector.

Lo más cómodo es hacerlo desde la propia app: bajo el selector de región hay una
lista con casillas. Marca **todas las comunas que necesites** y pulsa el botón, que
pasará a decir *Descargar N regiones*. Se construyen una tras otra desde el
servidor —puedes cerrar la pestaña— y la barra indica cuál va y cuántas quedan.

Hay **31 ciudades** agrupadas en el selector:

| Grupo | Ciudades |
|---|---|
| Santiago | Santiago Centro, Las Condes, Providencia, Ñuñoa, Vitacura, área metropolitana |
| Chile | Valparaíso, Viña del Mar, Concepción |
| LATAM | Buenos Aires, Montevideo, Lima, Bogotá, Ciudad de México, São Paulo, Rio de Janeiro |
| Europa | London, Paris, Madrid, Barcelona, Berlin, Amsterdam, Roma, Lisboa |
| Norteamérica | New York, Chicago, Boston, San Francisco |
| Asia-Pacífico | Tokyo, Sydney |

### ¿Y una ciudad que no está en la lista?

Bajo el selector de región, abre **🔎 Buscar y descargar otra ciudad**. Escribe el
nombre (Viña del Mar, Kyoto, Bogotá…), elige el resultado —el mapa vuela ahí para
que compruebes que es el sitio correcto—, ajusta el radio de descarga y pulsa
**⬇ Descargar esta ciudad**. Verás la misma barra de progreso que con las de la
lista, y después aparece en el selector bajo **Mis ciudades**.

Necesita conexión (busca en OpenStreetMap). Para quitar una ciudad propia, se
selecciona en el desplegable y se pulsa *Quitar esta ciudad*.

## 5. Tests

**macOS / Linux**

```bash
make test
```

**Windows**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ -q
```

## Problemas frecuentes

| Síntoma | Causa y solución |
|---|---|
| `SyntaxError` al arrancar | Python < 3.12. Usa `PYTHON=python3.12 ./run.sh` |
| El mapa sale en blanco | Sin internet: no hay teselas. La ruta se dibuja igual sobre fondo liso |
| "No se pudo cargar la librería de mapas" | Ejecuta `./vendor.sh` con conexión, una vez |
| El puerto 8000 está ocupado | `./run.sh --port 8080` |
| Windows: `run.ps1 is not digitally signed` | Usa `.\run.bat` — no depende de PowerShell |
| Windows: acentos raros en la consola | Cosmético. `serve.py` fuerza UTF-8; si persiste, `chcp 65001` |
| Windows: `python` no se reconoce | Reinstala Python marcando **"Add python.exe to PATH"** |
| `ModuleNotFoundError: raceengine_*` | Arranca con `python serve.py`, que resuelve las rutas solo |
| Overpass da error al construir una región | Reintenta: rota entre tres endpoints, a veces están saturados |
| Una región dice "no construida" | Ejecuta `python -m raceengine_geodata.cli build <region>` |

Todo lo demás está en `README.md`.
