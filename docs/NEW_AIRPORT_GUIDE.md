# Cómo agregar un aeropuerto nuevo a la base de datos

Todo lo aprendido volando SABE, SARC, SAAR, KSFO y KLAX, para no repetir errores. Un aeropuerto es un archivo
`airports/<ICAO>.yaml`; el resto (mapa de rodaje, pistas, frecuencias, aproximaciones) sale de las bases de datos.

## 1. Cómo se crea
- **Automático:** spawneás en el aeropuerto (o sintonizás una frecuencia suya) con `--sim` y `world.discover()` escribe el
  YAML una sola vez, desde el db del sim (Little Navmap) o, si no hay, desde OurAirports. Queda `needs_review: true`.
- **A mano:** `atc-gen KXXX` (OurAirports CSV -> YAML). Nunca pisa un archivo existente salvo `--force`.
- **Nunca regenerar encima de un YAML editado a mano.** `gen.py` los saltea; no uses `--force` con los tuyos.
- Después de revisarlo: poné `needs_review: false`.

## 2. Campos que se llenan solos (verificar, no escribir)
- `lat`, `lon`, `elevation_ft`, `country`, `towered`, `runways` (ident, rumbo, largo, superficie), `frequencies`.
- **Cabeceras de pista:** se reemplazan con las del scenery del sim (`navdb.enrich`). Los umbrales de KSFO faltaban en
  OurAirports y salían mal. Si una pista "no existe" para el código (sin `lat/lon`), el db del sim no la tiene.
- `trans_alt_ft` y `approaches` (ILS/RNAV/VOR por pista) salen del navdata. `trans_alt_ft` decide FL vs pies
  (Argentina 3000, EE.UU. 18000, UK 6000).
- `mag_var_deg` del navdata si hay. Si falta, los vientos se dicen en grados verdaderos (como los da el sim).

## 3. Campos a mano (los que cambian cómo suena y se comporta el ATC)
| Campo | Para qué | Notas |
|---|---|---|
| `spoken_name` | nombre hablado: "Aeroparque Ground", "cleared to Rosario" | Sin esto dice el nombre largo del db. |
| `mag_var_deg` | vientos y rumbos en magnético | Este positivo: "VAR 10° W" -> `-10`. Del AD chart. |
| `taxi_routes` | `'13': via Alfa, Bravo` | Sin esto Ground nombra calles solo si el mapa de rodaje existe; si no, "sin nombrar calles". Nunca inventa calles. |
| `pattern_alt_agl_ft`, `pattern_direction` (por pista) | circuito VFR | En no-EE.UU. el generador pone 1000 ft izquierda como PLACEHOLDER: verificar contra AIP. |
| `runway_configs` | pistas distintas para despegue y aterrizaje | KSFO: aterriza 28L/28R, despega 1L/1R. Una entrada por configuración de viento. Vacío = una sola pista para todo (mejor viento de frente). |
| `initial_alt_ft` | altitud inicial sin SID | EE.UU. default 5000 ("maintain five thousand"). |
| `traffic_per_hour` | movimientos/h de nuestro tráfico propio | Si no, sale del horario de FS Traffic. Campos VFR chicos: muy bajo, solo GA. Internacionales chicos: ~1 cada 10 min. JFK/SAEZ: mucho. |
| `crossings_by` | quién autoriza cruces de pista | `ground`, `tower` o vacío/`auto` (Ground con buena visibilidad; Tower si visibilidad < 5 km o techo < 1500 ft). |
| `notes` | procedimientos locales en texto | Entra al contexto del LLM. |

## 4. Mapa de rodaje (lo que más falla)
- Fuente: Little Navmap db (`navdb.taxi_network`), si no OSM (`tools/fetch_osm_taxi.py` -> `airports/osm/<ICAO>.json`).
- Sin mapa no hay rutas de taxi con nombres, ni cruces de pista, ni nuestro tráfico propio (necesita stands y paths).
- Verificar en el sim: **nombres de calles** (las del OSM a veces están mal o faltan), **puntos de espera** (`holds`:
  uno por pista y extremo; si falta, se usa el nodo de calle más cercano a la pista), **stands/parkings** (`navdb.parkings()`).
- **Stands cerca de la pista:** en SABE los gates 27-29 caen dentro de la caja del punto de espera de la 13. Por eso el
  traspaso Ground -> Tower exige taxi autorizado y no estar en un stand (`flow._at_a_stand`, radio 45 m). Revisar si en el
  aeropuerto nuevo hay gates a menos de ~0,3 NM del extremo de pista.
- **Pistas sin calle hasta el extremo** (SARC 02/20: las calles entran a mitad de pista): hace falta backtrack
  ("backtrack runway two zero, line up and wait"). Se detecta solo (`flow.needs_backtrack`: punto de espera a > 0,08 NM
  del umbral). Verificar que el giro de 180° entra en pista angosta (SARC: 45 m). Alternativa: salida desde intersección.
- **Aeropuertos chicos con plataforma apretada:** los stands se eligen con separación por envergadura; si "no free stand"
  aparece seguido, bajar `traffic_per_hour`.

## 5. Frecuencias y estaciones
- Tipos: `CLD` (clearance/delivery), `GND`, `TWR`, `APP`, `DEP`, `ATIS`, `CTAF`, `RMP`. La frecuencia sintonizada en COM1
  decide quién contesta (`facility.py`); ATIS y CTAF son silenciosas.
- **Con ATIS** (SABE): el piloto debe decir la letra; si no, "confirm information X". **Sin ATIS** (SARC): el controlador
  da el clima en el primer contacto, estilo VATSIM (`atis.CONTROLLER_WEATHER`).
- **Espacio aéreo/Center:** sale solo de `data/centers.json` (todo el mundo, por sector y altitud: `python
  tools/build_centers.py`). Los archivos de `airspace/` y la FIR del navdata quedan como respaldo. Si un Center está
  mal o falta, se corrige en `data/centers_extra.yaml` (manda sobre todo) y se vuelve a compilar.
- **Cruzar fuentes de frecuencias** (lo que aprendimos con SGAS/SBGR/ENGM): OurAirports a veces está viejo, el sim
  (MSFS navdata) suele estar al día, VATSIM (VATGlasses, wiki de la división) y el AIP (AISWEB en Brasil, la wiki de
  VATSIM Scandinavia para Noruega/Islandia) desempatan. La principal va primero en el YAML (es la que se usa para
  "contact ..."); las secundarias después, con `spoken` (así contestan si alguien las sintoniza).
- **Nombres hablados por país, en inglés:** Brasil "Guarulhos Clearance / Ground / Tower", aproximación "Sao Paulo
  Control" (Controle), Centro "Curitiba Center"; Argentina "Ezeiza Control" (ACC), "Baires Control" (TMA); Noruega
  "Gardermoen Tower", "Oslo Approach / Director", "Polaris Control"; Islandia "Reykjavik Control", "Iceland Radio".
- **8,33 kHz:** en Europa guardar el nombre de canal que muestra el COM (118.305, no 118.300).
- Frecuencias 8,33 kHz se leen igual (118.105). Verificar contra la carta, el db a veces trae frecuencias viejas.

## 6. Fraseología según el país
- `country: US` activa FAA (7110.65): "United four thirty-six", "climb and maintain", "ground point eight", "hold short of
  runway X". Cualquier otro país usa ICAO ("QNH", "line up and wait", "cleared for takeoff").
- Altitudes: pies hasta `trans_alt_ft`, niveles de vuelo arriba. Sin `trans_alt_ft`, FL desde 10000.
- Voces: el acento sale del país del aeropuerto (controlador) y del país de la aerolínea/matrícula (piloto). Pool
  español (Argentina) tiene solo 4 voces; ver `voices.py` para variantes de tono.

## 7. Tráfico propio (`--own-traffic`)
- Necesita: mapa de rodaje, stands, y modelos FSLTL o FS Traffic instalados (`ATC_COMMUNITY_DIR`). Sin modelo para la
  aerolínea/tipo no spawnea ("no model for ...").
- **Apagar la inyección de FS Traffic en el sim**: sus aviones (etiquetas tipo "5231/B738/BONDI") no obedecen a este
  ATC, pueden entrar a pista sin autorización y chocar. El paquete queda instalado solo para modelos y horarios.
- Nuestros aviones tienen tasa: mínima 6/h, máxima 20/h, hub x2. Al arrancar hay arranque en caliente (uno en final,
  otro rodando).
- Ground: rutas más cortas, cola máxima de 3 en salida, reruteo ("change of routing") si hay alguien trabado, y como
  último recurso se ignoran entre sí.
- Piso, taxi < 20 kt, aceleraciones suaves. Luces: ver `tools/probe_lights.py` y `ATC_OWN_LIGHTS`.

## 8. Checklist para un aeropuerto nuevo
1. Spawnear ahí (o `atc-gen`) y abrir el YAML; revisar nombre, `spoken_name`, `country`, pistas, frecuencias.
2. Poner `mag_var_deg` y `trans_alt_ft` si faltan; `pattern_*` por pista; `runway_configs` si hay pistas separadas.
3. Cargar `taxi_routes` si querés calles nombradas desde la carta (AIP).
4. Verificar mapa de rodaje: nombres, holds, gates cerca de pista, extremos sin calle (backtrack).
5. Decidir `traffic_per_hour` y `crossings_by`.
6. Correr `python -m atc --airport XXXX` (fake sim) y `python -m atc.scenarios`; luego volar un departure y un arrival.
7. Probar en el sim: Delivery -> Ground -> Tower en el gate, ATIS (o clima por controlador), backtrack/intersección,
   cruces de pista, traffic propio.
8. `needs_review: false` cuando esté revisado.

## 9. Errores que ya pasaron (no repetir)
- Pistas con cabeceras sin `lat/lon` -> head-on y secuenciación fallan. Revisar que cada pista tenga extremos del sim.
- Gate dentro de la caja del holding point -> "contact Tower" sin hablar (arreglado, pero revisá gates nuevos).
- Backtrack bloqueado por un arribo lejano -> el piloto espera de más; hoy Tower ofrece salida desde intersección.
- Calles inventadas por el LLM -> los nombres de calles solo salen de `taxi_routes` o del mapa, nunca del modelo.
- Voces repetidas -> ver `voices.py` (variantes de tono) y `voices/blacklist.txt` (descartar voces feas).
- El LLM no debe decidir pista, frecuencia, clima ni tráfico: eso lo decide el código y el LLM solo lo dice.
