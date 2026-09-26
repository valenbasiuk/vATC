# Contexto del proyecto: ATC por IA propio (basado en OpenSquawk)

## Objetivo

Armar una alternativa a SayIntentions.AI (ATC por IA para flight simulator) sin
pagar la suscripción de USD 16/mes. **Uso 100% personal**, no está pensado para
vender ni compartir con terceros ni para uso organizacional.

Se descartó armar todo desde cero: existe un proyecto open source llamado
**OpenSquawk** que ya resuelve gran parte del problema (telemetría del sim,
STT/TTS, motor de decisión, UI). El plan es partir de ahí y sumarle un fallback
generativo con un LLM (Claude vía API directa u OpenRouter) para el dinamismo
que SayIntentions tiene y que BeyondATC y otros clones no logran.

## Sobre el Claude Max ($200/mes) que ya tiene el usuario

- Da uso ampliado de Claude vía claude.ai / Claude Code / Cowork (interactivo).
- **No sirve como backend/API para que la app llame al modelo en tiempo real.**
  Para eso hace falta una API key de Anthropic (o de OpenRouter) facturada por
  token, aparte de la suscripción.
- Sí conviene usar el Max/Claude Code para **programar el proyecto** (mucho
  ahorro ahí). Para que la app *funcione en vivo* mientras se vuela, se necesita
  la API separada — el costo estimado por vuelo con un modelo chico/rápido es
  de centavos, bastante menos que los $16/mes de SayIntentions.

## OpenSquawk: qué es y qué no es

- Se posiciona como *"Train ATC before you fly VATSIM"*: es más una herramienta
  de entrenamiento de fraseología ICAO/SERA (modo "Classroom") que un
  reemplazo maduro de ATC en vivo. El modo "Live ATC" está en **alpha**.
- Ya existe un servicio hosteado gratuito en `app.opensquawk.de` con una app
  "Bridge" instalable (Win/Mac/Linux) que conecta directo a MSFS 2020/2024 vía
  SimConnect. Recomendación: probarlo tal cual antes de construir nada.
- Arquitectura interna: **no** es un LLM libre respondiendo todo. Usa flows
  determinísticos en YAML (árboles de estado con fraseología ya escrita) y solo
  cuando el regex de la transcripción falla, consulta a un LLM como *router*
  para elegir entre las ramas candidatas ya existentes (no genera texto nuevo).

### Los 3 repos

| Repo | Lenguaje | Rol | Licencia |
|---|---|---|---|
| `OpenSquawk/OpenSquawk` | TypeScript (Nuxt 4 + H3) | Frontend web, cuentas, TTS/LLM (vía config OpenAI-compatible), decision trees | **OpenSquawk Community Source License 1.0** (restrictiva, ver abajo) |
| `OpenSquawk/OpenSquawk-API` | Python (FastAPI) | Backend de decisión: carga flows YAML, maneja sesiones, LLM router de fallback | AGPL-3.0 |
| `OpenSquawk/OpenSquawk-Bridge` | Python (PySide6) | App de escritorio: SimConnect (MSFS 2020/2024), telemetría, push-to-talk, STT/TTS local (Piper + faster-whisper) | AGPL-3.0 |

**Nota de licencia importante:** el repo principal (frontend) tiene una
licencia propia "Community Source License 1.0", NO open source real: permite
uso y modificación **solo personal** (self-host únicamente para uno mismo),
prohíbe explícitamente ofrecerlo como servicio a otros (SaaS, aunque sea
gratis, aunque sea para un club/comunidad). El README del backend dice
erróneamente "aplica la misma licencia AGPL a la app" — eso está desactualizado,
manda el archivo LICENSE real del repo frontend. Para uso 100% personal (que es
el caso) esto no es un problema.

## Puntos de integración técnica ya verificados

- El **frontend Nuxt** requiere una OpenAI API Key para TTS y llamadas de LLM,
  y permite conectar a **cualquier servicio compatible con OpenAI** seteando
  `OPENAI_BASE_URL` en su `.env`. Este es el punto de enganche para meter Claude
  sin tocar código.
- **OpenRouter** (`https://openrouter.ai/api/v1`) es compatible con la API de
  OpenAI y da acceso a modelos Claude con IDs como `anthropic/claude-haiku-4.5`
  o `anthropic/claude-sonnet-4.5`.
- **Anthropic** lanzó en marzo de 2026 su propio endpoint compatible con OpenAI
  (pensado para evaluación/testeo, no explícitamente para producción) — otra
  alternativa de drop-in.
- El backend (`OpenSquawk-API`) tiene ya las env vars para el LLM router:
  `LLM_ROUTER_ENABLED`, `FRONTEND_BASE_URL` (debe apuntar al Nuxt server con
  `/api/decision/route`), `SERVICE_SECRET` (debe coincidir en ambos lados),
  `LLM_ROUTER_TIMEOUT_MS`. Sin `SERVICE_SECRET` seteado en ambos, el router
  queda inactivo y el motor rutea 100% determinístico (`bad_next`).
- **Datos de aeropuerto en vivo**: la API de SimConnect (`AddToFacilityDefinition`
  + `RequestFacilityData`) permite pedir, por ICAO, pistas, frecuencias y
  parkings/jetways tal cual están simulados (mejor que una base de datos
  estática, porque refleja addons/escenarios instalados).
- El **Bridge** ya transmite telemetría: fase de vuelo, frecuencia activa,
  squawk, posición, velocidad, altitud. Falta sumarle ahí los datos de
  aeropuerto de SimConnect.

## Plan de acción (7 fases)

**Fase 0 — Bajar y validar el baseline.** Clonar los 3 repos, levantar MongoDB
local, correr el stack completo tal cual está contra MSFS. Confirmar que el
loop Bridge → API → Nuxt → TTS funciona antes de tocar nada.

**Fase 1 — Apuntar el LLM a Claude/OpenRouter.** En el `.env` del repo Nuxt,
cambiar `OPENAI_BASE_URL` a OpenRouter o al endpoint de Anthropic, elegir un
modelo rápido (`anthropic/claude-haiku-4.5`). Activar `LLM_ROUTER_ENABLED` y
setear el mismo `SERVICE_SECRET` en ambos lados. Solo valida que la integración
de credenciales funciona, sin funcionalidad nueva todavía.

**Fase 2 — Meter datos de aeropuerto en vivo al Bridge.** En `msfs_source.py`,
sumar `AddToFacilityDefinition` + `RequestFacilityData` por el ICAO activo/más
cercano, pidiendo pistas, frecuencias y parkings. Agregar esos campos al
payload de telemetría que el Bridge ya manda.

**Fase 3 — Agregar una rama "generativa" real en el backend.** Hoy el router
solo elige entre ramas YAML ya escritas. Sumar un paso más: si nada matchea,
llamar al LLM para que **genere** texto libre (no que elija), usando el
contexto de aeropuerto + telemetría de la Fase 2. Devolver la respuesta en el
mismo formato que las ramas YAML para que el TTS downstream no note la
diferencia.

**Fase 4 — Diseñar el prompt de grounding.** System prompt con: rol
(torre/aproximación/ground de tal ICAO), datos de pista/frecuencia inyectados,
fase de vuelo actual, últimas transmisiones como contexto, few-shot examples
sacados de los propios YAML (para mantener tono/formato), instrucción explícita
de no inventar datos no provistos. Probar primero con transcripciones
guardadas, sin voz.

**Fase 5 — Ajustar latencia y modelo.** Medir el loop STT→LLM→TTS completo.
Modelo chico/rápido (Haiku, o Gemini Flash vía OpenRouter) para tráfico normal;
reservar un modelo más grande solo para casos raros que toleren más demora
(emergencias, reruteos).

**Fase 6 — Validar el readback igual que las ramas fijas.** Pedirle al LLM que
devuelva, además del texto hablado, campos estructurados (altitud/rumbo/pista
asignada) para chequear el readback del piloto contra esos datos en vez de
texto libre contra texto libre.

**Fase 7 — Iterar cobertura con vuelos reales.** Loguear cuándo se dispara el
fallback generativo. Si una situación se repite mucho, conviene escribir un
flow YAML nuevo para ese caso (más rápido y gratis); el generativo queda para
lo verdaderamente impredecible.

## Riesgos / cosas a tener en cuenta

- La Fase 3 es la más delicada: generar texto libre es una categoría de
  problema distinta a elegir entre opciones fijas (control de alucinaciones,
  formato consistente).
- El fallback generativo va a tener una pausa perceptible (STT + LLM + TTS)
  comparado con la respuesta casi instantánea de un flow YAML — esperable, es
  el mismo trade-off que tiene SayIntentions en casos complejos.
- Los datos de SimConnect reflejan exactamente lo que está instalado en el sim
  (incluye addons de terceros), lo cual es una ventaja sobre una base de datos
  estática desactualizada.