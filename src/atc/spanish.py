"""Spanish ATC (improvement plan J2, first slice, `--language es`): the radio in Spanish, the logic in English.

Everything this ATC decides (clearances, readback checks, sequencing...) works on English text. Two layers make the
radio Spanish without touching that:
  - `to_english(pilot_text)`: a Spanish call ("Aeroparque Superficie, Aerolíneas uno dos uno seis, solicito rodaje")
    becomes the canonical English form the parsers know ("aeroparque ground, aerolineas 1216, request taxi");
  - `to_spanish(text)`: every transmission is said in Spanish (`SpanishSpeaker` wraps the speaker: replies, the
    controller's own calls, the chatter, the ATIS), while the history keeps the English the readbacks are checked
    against.
Phraseology after ICAO Doc 9432 (Spanish edition) and Argentine usage; STATIONS and a few phrases are marked VERIFY:
Valen, correct them where Argentine controllers say it otherwise. The LLM still answers in English: its free-form
text is passed through the same table (unknown words stay English).
"""

from __future__ import annotations

import re
import unicodedata

# position names as Argentine controllers say them. VERIFY (Valen): Ground "Superficie"? Delivery "Autorizaciones"?
STATIONS = {"Tower": "Torre", "Ground": "Superficie", "Approach": "Aproximación", "Departure": "Salidas",
            "Delivery": "Autorizaciones", "Control": "Control", "Center": "Control", "Radio": "Radio"}

_DIGITS_ES = {"zero": "cero", "one": "uno", "two": "dos", "three": "tres", "four": "cuatro", "five": "cinco",
              "six": "seis", "seven": "siete", "eight": "ocho", "niner": "nueve", "nine": "nueve"}
_HUNDREDS_ES = {"one": "cien", "two": "doscientos", "three": "trescientos", "four": "cuatrocientos",
                "five": "quinientos", "six": "seiscientos", "seven": "setecientos", "eight": "ochocientos",
                "niner": "novecientos", "nine": "novecientos"}
_SIDE_ES = {"left": "izquierda", "right": "derecha", "Left": "izquierda", "Right": "derecha"}
_TENS_ES = {"twenty": "veinte", "thirty": "treinta", "forty": "cuarenta", "fifty": "cincuenta", "sixty": "sesenta",
            "seventy": "setenta", "eighty": "ochenta", "ninety": "noventa"}
_TEENS_ES = {"ten": "diez", "eleven": "once", "twelve": "doce", "thirteen": "trece", "fourteen": "catorce",
             "fifteen": "quince", "sixteen": "dieciséis", "seventeen": "diecisiete", "eighteen": "dieciocho",
             "nineteen": "diecinueve"}

# English ATC -> Spanish, applied in this order (longest and most specific first). \1... keep what was matched.
_TO_ES: list[tuple[str, str]] = [
    # emergencies, go-arounds, the radio itself
    (r"go around, I say again, go around", "motor y al aire, repito, motor y al aire"),
    (r"follow the published missed approach procedure", "efectúe el procedimiento de aproximación frustrada publicado"),
    (r"climb straight ahead to", "ascienda en línea recta a"),
    (r"expect vectors for another approach", "espere vectores para otra aproximación"),
    (r"emergency squawk observed", "observo código de emergencia"),
    (r"say nature of emergency and intentions", "indique naturaleza de la emergencia e intenciones"),
    (r"say intentions", "indique intenciones"),
    (r"roger (mayday|pan pan)", r"recibido \1"),
    (r"emergency services will be standing by", "los servicios de emergencia estarán preparados"),
    (r"when able, report persons on board and fuel endurance",
     "cuando pueda, notifique personas a bordo y autonomía"),
    (r"emergency cancelled", "emergencia cancelada"),
    (r"low altitude warning, check your altitude immediately", "alerta de baja altitud, verifique su altitud de inmediato"),
    (r"the minimum flight altitude is", "la altitud mínima de vuelo es"),
    (r"check altitude", "verifique altitud"),
    (r"Blocked, say again", "Bloqueado, repita"),
    (r"Station calling", "Estación llamando a"),
    (r"say again your callsign", "repita su distintivo de llamada"),
    (r"say again the holding fix", "repita el punto de espera"),
    (r"say again the runway", "repita la pista"),
    (r"negative, I say again", "negativo, repito"),
    (r"I say again", "repito"),
    (r"say again", "repita"),
    (r"read you five", "lo recibo cinco"),
    (r"readback correct", "colación correcta"),
    (r"read back the clearance", "colacione la autorización"),
    (r"read back", "colacione"),
    # IFR clearance
    (r"no clearance received yet", "aún no tiene autorización"),
    (r"flight planned route", "ruta según plan de vuelo"),
    (r"\b([A-Z]{3,5}(?: (?:zero|one|two|three|four|five|six|seven|eight|niner))+(?: [a-z]+)?) departure\b",
     r"salida \1"),
    (r"\b([A-Z]{3,5}) transition\b", r"transición \1"),
    (r"climb via SID to", "ascienda vía SID a"),
    (r"climb via SID", "ascienda vía SID"),
    (r"expect (flight level [a-z ]+|[a-z ]+ feet), ten minutes after departure",
     r"espere \1 diez minutos después de la salida"),
    (r"departure frequency", "frecuencia de salida"),
    (r"When ready for push and start, contact", "Cuando esté listo para remolque y puesta en marcha, contacte"),
    (r"Report ready for push and start", "Notifique listo para remolque y puesta en marcha"),
    (r"cleared to ([A-Z][a-z]+(?: [A-Z][a-z]+)*)", r"autorizado a \1"),
    # ground
    (r"push and start approved", "remolque y puesta en marcha aprobados"),
    (r"expect push and start in", "espere remolque y puesta en marcha en"),
    (r"I'll call you", "lo llamaré"),
    (r"tail (east|west|north|south)", lambda m: "cola al " + {"east": "este", "west": "oeste", "north": "norte",
                                                                "south": "sur"}[m.group(1)]),
    (r"tail (left|right)", lambda m: "cola a la " + _SIDE_ES[m.group(1)]),
    (r"facing (east|west|north|south)", lambda m: "proa al " + {"east": "este", "west": "oeste", "north": "norte",
                                                                 "south": "sur"}[m.group(1)]),
    (r"taxi to holding point ([A-Z][a-z]+(?: [a-z]+)?) runway", r"ruede al punto de espera \1 pista"),
    (r"taxi to holding point runway", "ruede al punto de espera pista"),
    (r"taxi to stand", "ruede a la posición"),
    (r"taxi to the apron", "ruede a plataforma"),
    (r"change of routing, taxi via", "cambio de ruta, ruede vía"),
    (r"I'll call your turns", "le indicaré los virajes"),
    (r"turn (left|right) on", lambda m: f"vire a la {_SIDE_ES[m.group(1)]} en"),
    (r"continue on", "continúe por"),
    (r"continue taxi", "continúe el rodaje"),
    (r"give way to the (.+?) from the (left|right)", lambda m: f"ceda el paso al {m.group(1)} que viene por la "
                                                               f"{_SIDE_ES[m.group(2)]}"),
    (r"give way to the traffic", "ceda el paso al tránsito"),
    (r"opposite direction", "en sentido opuesto"),
    (r"hold short of runway", "mantenga fuera de la pista"),
    (r"holding short of runway", "manteniendo fuera de la pista"),
    (r"cross runway", "cruce pista"),
    (r"then contact", "luego contacte"),
    (r"backtrack runway", "retroceda por la pista"),
    (r"behind the landing (.+?) on ([a-z]+(?: [a-z]+)?) mile final", r"detrás del \1 que aterriza en final de \2 millas"),
    (r"behind the landing (.+?),", r"detrás del \1 que aterriza,"),  # (rolling out, or the readback)
    (r"line up and wait runway", "alinee y mantenga pista"),
    (r", behind\b", ", detrás"),
    (r"hold position", "mantenga posición"),
    (r"welcome to ([A-Z][a-z]+)", r"bienvenidos a \1"),
    (r"vacate via ([A-Z][a-z]+(?: [a-z]+)?) when able", r"abandone la pista por \1 cuando pueda"),
    (r"vacate the runway when able", "abandone la pista cuando pueda"),
    (r"vacate via", "abandone por"),
    (r"from intersection", "desde la intersección"),
    (r"from present position", "desde su posición"),
    (r"metres available|meters available", "metros disponibles"),
    (r"advise able to depart", "notifique si puede despegar"),
    # tower, circuit
    (r"cleared for immediate takeoff", "autorizado a despegar inmediatamente"),
    (r"cleared for takeoff", "autorizado a despegar"),
    (r"cleared to land", "autorizado a aterrizar"),
    (r"cleared touch and go", "autorizado a tocar y despegar"),
    (r"caution wake turbulence", "precaución estela turbulenta"),
    (r"number ([a-z]+) for departure", r"número \1 para la salida"),
    (r"expect departure in about", "espere despegar en aproximadamente"),
    (r"(?:join|enter) (left|right) downwind runway", lambda m: f"incorpórese a favor del viento {_SIDE_ES[m.group(1)]} "
                                                              "pista"),
    (r"report (?:left |right )?downwind", "notifique a favor del viento"),
    (r"make straight-in approach runway", "efectúe aproximación directa pista"),
    (r"climb to circuit altitude", "ascienda a altitud de circuito"),
    (r"after departure, (left|right) turn ([a-z]+)bound approved",
     lambda m: f"después del despegue, viraje por la {_SIDE_ES[m.group(1)]} rumbo {m.group(2)} aprobado"),
    (r"(left|right) turn approved", lambda m: f"viraje por la {_SIDE_ES[m.group(1)]} aprobado"),
    (r"frequency change approved", "cambio de frecuencia aprobado"),
    (r"traffic to follow", "tránsito a seguir"),
    (r"follow the (.+?) on ([a-z]+(?: [a-z]+)?) mile final", r"siga al \1 en final de \2 millas"),
    (r"on ([a-z]+(?: [a-z]+)?) mile final", r"en final de \1 millas"),
    (r"continue approach", "continúe la aproximación"),
    (r"report final", "notifique final"),
    (r"number ([a-z]+)\b", r"número \1"),
    (r"(\w+) on the runway", r"\1 en la pista"),
    # approach and en route
    (r"cleared visual approach runway", "autorizado aproximación visual pista"),
    (r"expect visual approach runway", "espere aproximación visual pista"),
    (r"report field in sight", "notifique aeródromo a la vista"),
    (r"unable visual approach", "imposible aproximación visual"),
    (r"cleared (ILS|RNAV|VOR DME|VOR|NDB DME|NDB|localizer)((?: [A-Z][a-z]+)?) approach runway",
     r"autorizado aproximación \1\2 pista"),
    (r"cleared approach runway", "autorizado aproximación pista"),
    (r"(ILS|RNAV|VOR DME|VOR|NDB DME|NDB|localizer)((?: [A-Z][a-z]+)?) approach runway", r"aproximación \1\2 pista"),
    (r"(ILS|RNAV|VOR DME|VOR|NDB DME|NDB|localizer)((?: [A-Z][a-z]+)?) approach", r"aproximación \1\2"),
    (r"expect vectors", "espere vectores para"),
    (r"\bvectors\b", "vectores para"),
    (r"report established", "notifique establecido"),
    (r"maintain (.+?) until established", r"mantenga \1 hasta establecido"),
    (r"turn (left|right) heading", lambda m: f"vire a la {_SIDE_ES[m.group(1)]} rumbo"),
    (r"fly heading", "vuele rumbo"),
    (r"reduce speed to", "reduzca velocidad a"),
    (r"radar contact", "contacto radar"),
    (r"\bidentified\b", "identificado"),
    (r"descend via ([A-Z]+ [a-z ]+?) arrival to", r"descienda vía llegada \1 a"),
    (r"climb to", "ascienda a"),
    (r"descend to", "descienda a"),
    (r"at your discretion", "a discreción"),
    (r"proceed direct", "proceda directo a"),
    (r"is behind you, continue as filed", "quedó atrás, continúe según plan"),
    (r"continue as filed", "continúe según plan"),
    (r"unable direct", "imposible directo a"),
    (r"expect descent in", "espere descenso en"),
    (r"expect approach clearance at", "espere autorización de aproximación a las"),
    (r"expect further clearance at", "espere nueva autorización a las"),
    (r"revised expected approach time", "nueva hora prevista de aproximación"),
    (r"delay due traffic", "demora por tránsito"),
    (r"delay due runway closure", "demora por pista cerrada"),
    (r"hold at ([A-Z0-9 a-z]+?) as published", r"mantenga espera sobre \1 según lo publicado"),
    (r"hold at ([A-Z0-9 a-z]+?), inbound track", r"mantenga espera sobre \1, derrota de acercamiento"),
    (r"right turns", "virajes por derecha"),
    (r"leave the hold", "abandone la espera"),
    (r"continue holding", "continúe en espera"),
    (r"own navigation approved", "navegación propia aprobada"),
    (r"resume own navigation", "reanude navegación propia"),
    (r"deviation up to", "desvío de hasta"),
    (r"left of track approved", "a la izquierda de la derrota aprobado"),
    (r"right of track approved", "a la derecha de la derrota aprobado"),
    (r"deviation approved", "desvío aprobado"),
    (r"report back on track", "notifique de regreso en la derrota"),
    (r"report clear of weather", "notifique libre de meteorología"),
    (r"(light|moderate) chop reported between", lambda m: ("turbulencia leve" if m.group(1) == "light" else
                                                            "turbulencia moderada") + " notificada entre"),
    (r"no turbulence reported at", "sin turbulencia notificada a"),
    (r"no ride reports at", "sin reportes de turbulencia a"),
    (r"side-step to runway", "cambio a la pista paralela"),
    # traffic information
    (r"traffic, ([a-z]+) o'clock, ([a-z]+) miles?", r"tránsito a las \1, \2 millas"),
    (r"same direction", "mismo sentido"),
    (r"crossing left to right", "cruzando de izquierda a derecha"),
    (r"crossing right to left", "cruzando de derecha a izquierda"),
    (r"feet above", "pies por encima"),
    (r"feet below", "pies por debajo"),
    (r"same level", "mismo nivel"),
    (r"no reported traffic", "sin tránsito notificado"),
    # ATIS
    (r"Acknowledge receipt of information ([A-Z][a-z]+) and advise aircraft type on first contact",
     r"Acuse recibo de información \1 e informe tipo de aeronave en el primer contacto"),
    (r"information ([A-Z][a-z]+) is now current", r"la información \1 es ahora la vigente"),
    (r"information ([A-Z][a-z]+) is current", r"la información \1 es la vigente"),
    (r"information ([A-Z][a-z]+) now current", r"información \1 vigente"),
    (r"confirm information", "confirme información"),
    (r"All stations", "A todas las estaciones"),
    (r"\binformation\b", "información"),
    (r"runway in use", "pista en uso"),
    (r"Low visibility procedures in operation", "Procedimientos de baja visibilidad en vigor"),
    (r"dew point", "punto de rocío"),
    (r"\bTemperature\b", "Temperatura"),
    (r"\btemperature\b", "temperatura"),
    (r"kilometers or more", "kilómetros o más"),
    (r"\bkilometers\b", "kilómetros"),
    (r"\b[Vv]isibility\b", "visibilidad"),
    (r"No significant clouds", "Sin nubes significativas"),
    (r"\b[Cc]louds\b", "nubes"),
    (r"\bfew\b", "escasas"), (r"\bscattered\b", "dispersas"), (r"\bbroken\b", "fragmentadas"),
    (r"\bovercast\b", "cubierto"),
    (r"\bcumulonimbus\b", "cumulonimbus"),
    (r"\bmist\b", "neblina"), (r"\bfog\b", "niebla"), (r"\brain\b", "lluvia"), (r"\bshowers\b", "chubascos"),
    (r"\bthunderstorm\b", "tormenta"), (r"\bdrizzle\b", "llovizna"), (r"\bhaze\b", "bruma"),
    (r"\btime\b", "hora"),
    (r"\bclosed\b", "cerrada"),
    (r"\b[Tt]axiway\b", "calle de rodaje"),
    # the pilots' own calls (our traffic, the chatter)
    (r"request push and start", "solicito remolque y puesta en marcha"),
    (r"request taxi to the stand", "solicito rodaje a la posición"),
    (r"request taxi", "solicito rodaje"),
    (r"ready for departure", "listo para salir"),
    (r"for circuits", "para circuitos"),
    (r"holding short runway", "manteniendo fuera de la pista"),
    (r"holding point runway", "punto de espera pista"),
    (r"(left|right) (downwind|base)", lambda m: ("a favor del viento " if m.group(2).lower() == "downwind" else
                                                  "básico ") + _SIDE_ES[m.group(1)]),
    (r"\bdownwind\b", "a favor del viento"),
    (r"touch and go", "tocar y despegar"),
    (r"full stop", "aterrizaje completo"),
    (r"going around", "motor y al aire"),
    (r"vacated via", "librada por"),
    (r"\bvacated\b", "librada"),
    (r"established (ILS|RNAV|VOR DME|VOR|localizer)", r"establecido \1"),
    (r"inbound for final", "en acercamiento a final"),
    (r"on final", "en final"),
    (r"\bstand\b", "posición"),
    (r"\bpassing\b", "pasando"),
    (r"\bclimbing\b", "ascendiendo"),
    # words
    (r"\bthis is\b", "aquí"),
    (r"check frequency", "verifique frecuencia"),
    (r"remain this frequency", "mantenga esta frecuencia"),
    (r"\bcontact\b", "contacte"),
    # courtesies the radio variety adds (atc/variety.py)
    (r"\bgood morning\b", "buen día"), (r"\bgood afternoon\b", "buenas tardes"),
    (r"\bgood evening\b", "buenas noches"), (r"\bhello\b", "hola"),
    (r"\bhave a good (?:flight|one)\b", "buen vuelo"), (r"\bbye bye\b|\bsee ya\b|\bta-ta\b", "chau"),
    (r"\bso long\b|\bcheers\b", "hasta luego"), (r"\bthank you\b|\bthanks\b", "gracias"),
    (r"\buh\b", "eh"), (r"\bready\b", "listo"),
    (r"\bgood day\b", "buen vuelo"),
    (r"\bwind calm\b", "viento calma"),
    (r"\b[Ww]ind\b", "viento"),
    (r"\bdegrees\b", "grados"),
    (r"\bknots\b", "nudos"),
    (r"\bflight level\b", "nivel de vuelo"),
    (r"\bfeet\b", "pies"),
    (r"\bmiles\b", "millas"),
    (r"\bmetres\b|\bmeters\b", "metros"),
    (r"\bexpect\b", "espere"),
    (r"\b[Rr]unway\b", "pista"),
    (r"\bmaintain\b", "mantenga"),
    (r"\bheading\b", "rumbo"),
    (r"\bsquawk\b", "transpondedor"),
    (r"\bavailable\b", "disponible"),
    (r"\bapproved\b", "aprobado"),
    (r"\bunable\b", "imposible"),
    (r"\bstandby\b", "aguarde"),
    (r"\broger\b", "recibido"),
    (r"\bwilco\b", "cumpliré"),
    (r"\baffirm\b", "afirmativo"),
    (r"\bnegative\b", "negativo"),
    (r"\bdecimal\b", "decimal"),
    (r"\bfinal\b", "final"),
    (r"\bground\b", "superficie"),
    (r"\btower\b", "torre"),
    (r"\bdeparture\b", "salidas"),
    (r"\bvia\b", "vía"),
    (r"\b(left|right)\b", lambda m: _SIDE_ES[m.group(1)]),
]
_TO_ES_COMPILED = [(re.compile(p) if "[A-Z]" in p else re.compile(p, re.IGNORECASE), r) for p, r in _TO_ES]


def _numbers_es(text: str) -> str:
    """'two thousand five hundred' -> 'dos mil quinientos', 'one thousand' -> 'mil', digit words one by one."""
    text = re.sub(r"\bone thousand\b", "mil", text)
    text = re.sub(r"\b(zero|one|two|three|four|five|six|seven|eight|niner|nine) thousand\b",
                  lambda m: f"{_DIGITS_ES[m.group(1)]} mil", text)
    text = re.sub(r"\b(one|two|three|four|five|six|seven|eight|niner|nine) hundred\b",
                  lambda m: _HUNDREDS_ES[m.group(1)], text)
    # type names in group form ("Airbus three twenty", "Boeing seven thirty-seven"): "tres veinte", "treinta y siete"
    text = re.sub(r"\b(twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)-(one|two|three|four|five|six|seven|eight|"
                  r"nine)\b", lambda m: f"{_TENS_ES[m.group(1)]} y {_DIGITS_ES[m.group(2)]}", text)
    text = re.sub(r"\b(twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)\b", lambda m: _TENS_ES[m.group(1)], text)
    text = re.sub(r"\b(ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen)\b",
                  lambda m: _TEENS_ES[m.group(1)], text)
    return re.sub(r"\b(zero|one|two|three|four|five|six|seven|eight|niner|nine)\b", lambda m: _DIGITS_ES[m.group(1)],
                  text)


# a pilot reads back in the first person: "ruede al punto de espera" -> "rodamos al punto de espera"
_PILOT_VERBS = [(r"\bruede\b", "rodamos"), (r"\balinee y mantenga\b", "alineamos y mantenemos"),
                (r"\bmantenga\b", "mantenemos"), (r"\bvire\b", "viramos"), (r"\bascienda\b", "ascendemos"),
                (r"\bdescienda\b", "descendemos"), (r"\bnotifique\b", "notificaremos"), (r"\bcruce\b", "cruzamos"),
                (r"\bcontacte\b", ""), (r"\breduzca\b", "reducimos"), (r"\bproceda\b", "procedemos"),
                (r"\babandone\b", "abandonamos"), (r"\bcontinúe\b", "continuamos"), (r"\bsiga\b", "seguimos")]


def to_spanish(text: str, pilot: bool = False) -> str:
    """An English transmission (code-made or the model's) said in Spanish; `pilot`: a pilot's call or readback."""
    out = text
    for word, es in STATIONS.items():  # station names first (capitalized): "Aeroparque Tower" -> "Aeroparque Torre"
        out = re.sub(rf"\b{word}\b", es, out)
    for pat, rep in _TO_ES_COMPILED:
        out = pat.sub(rep, out)
    out = _numbers_es(out)
    if pilot:
        for pat, rep in _PILOT_VERBS:
            out = re.sub(pat, rep, out, flags=re.IGNORECASE)
        out = re.sub(r"\s{2,}", " ", out).strip()
    return out[:1].upper() + out[1:] if out else out


# --- the pilot's Spanish -> the English the parsers know ---------------------------------------------------------

_DIGITS_EN = {"cero": "0", "uno": "1", "dos": "2", "tres": "3", "cuatro": "4", "cinco": "5", "seis": "6",
              "siete": "7", "ocho": "8", "nueve": "9"}
_HUNDREDS_EN = {"cien": "1", "ciento": "1", "doscientos": "2", "trescientos": "3", "cuatrocientos": "4",
                "quinientos": "5", "seiscientos": "6", "setecientos": "7", "ochocientos": "8", "novecientos": "9"}

# applied to lower-case text without accents, in this order
_TO_EN: list[tuple[str, str]] = [
    (r"\bmotor y al aire\b|\b(?:aproximacion )?frustrada\b|\bida al aire\b", "going around"),
    (r"\bautorizado a despegar\b", "cleared for takeoff"),
    (r"\bautorizado a aterrizar\b", "cleared to land"),
    (r"\bautorizado a tocar y despegar\b", "cleared touch and go"),
    (r"\bautorizado (?:a la )?aproximacion\b", "cleared approach"),
    (r"\bautorizado a\b", "cleared to"),
    (r"\blistos? para (?:el )?(?:despegue|despegar|salir|la salida)\b", "ready for departure"),
    (r"\blistos? para (?:el )?(?:rodaje|rodar)\b", "ready to taxi"),
    (r"\b(?:remolque y puesta en marcha|puesta en marcha y remolque|remolque|puesta en marcha)\b", "push and start"),
    (r"\b(?:solicito|solicitamos|solicitando|requiero|requerimos|pido|pedimos)\b", "request"),
    (r"\bautorizacion(?: de transito| ifr)?\b", "clearance"),
    (r"\b(?:ruedo|rodamos|rodando) al punto de espera\b", "taxi to holding point"),
    (r"\bal punto de espera\b", "to holding point"),
    (r"\b(?:en el|en) punto de espera\b", "on holding point"),
    (r"\bpunto de espera\b", "holding point"),
    (r"\b(?:manteniendo|mantengo|esperando) fuera de (?:la )?pista\b", "holding short of runway"),
    (r"\bcruzar (?:la )?pista\b|\bcruce (?:de )?pista\b", "cross runway"),
    (r"\balineo y mantengo\b|\balinear y mantener\b", "line up and wait"),
    (r"\bmantengo posicion\b", "hold position"),
    (r"\b(?:rodaje|rodar|ruede|rodando|ruedo|rodamos)\b", "taxi"),
    (r"\binterseccion\b", "intersection"),
    (r"\b(?:aerodromo|pista|campo|aeropuerto) a la vista\b|\btengo (?:la pista|el aerodromo|el campo)\b",
     "field in sight"),
    (r"\baproximacion visual\b", "visual approach"),
    (r"\baproximacion (ils|rnav|vor|ndb)\b", r"\1 approach"),
    (r"\bnavegacion propia\b", "own navigation"),
    (r"\b(?:en )?final corta\b", "short final"),
    (r"\ben final\b", "on final"),
    (r"\b(?:a favor del viento|viento en cola)\b", "downwind"),
    (r"\b(?:tramo )?basico\b", "base"),
    (r"\b(?:tocar y despegar|toque y despegue)\b", "touch and go"),
    (r"\b(?:parada completa|aterrizaje final|aterrizaje completo)\b", "full stop"),
    (r"\bcircuitos?\b", "circuits"),
    (r"\bcon (?:la )?informacion\b|\binformacion\b", "information"),
    (r"\b(?:prueba|comprobacion|chequeo) de radio\b", "radio check"),
    (r"\bcomo me (?:recibe|escucha)\b", "how do you read"),
    (r"\b(?:chequeo|control) de hora\b|\bsolicito hora\b|\bhora exacta\b", "time check"),
    (r"\bnivel de vuelo\b", "flight level"),
    (r"\bnivel\b", "level"),
    (r"\bpies\b", "feet"),
    (r"\b(?:ascenso|ascender|subir)\b", "climb"),
    (r"\b(?:ascendiendo|ascendemos|asciendo|subiendo|subimos)\b", "climbing"),
    (r"\bdescenso\b", "descent"),
    (r"\b(?:descender|bajar)\b", "descend"),
    (r"\b(?:descendiendo|descendemos|desciendo|bajando|bajamos)\b", "descending"),
    (r"\bdirecto a\b|\bdirecto\b", "direct"),
    (r"\b(?:salir de|abandonar) la espera\b", "leave the hold"),
    (r"\bespera\b", "hold"),
    (r"\bvectores\b", "vectors"),
    (r"\bdesvio\b", "deviation"),
    (r"\bpor (?:meteorologia|tormentas?|mal tiempo|formaciones)\b", "due weather"),
    (r"\b(?:de regreso|nuevamente|de vuelta) en (?:la )?(?:ruta|derrota)\b", "back on track"),
    (r"\blibres? de (?:meteorologia|tormentas?)\b", "clear of weather"),
    (r"\bturbulencia\b", "turbulence"),
    (r"\breportes? de turbulencia\b", "ride reports"),
    (r"\brumbo\b", "heading"),
    (r"\bvir(?:e|ando|aje)\b", "turn"),
    (r"\bizquierd[ao]\b", "left"),
    (r"\bderech[ao]\b", "right"),
    (r"\bgrados\b", "degrees"),
    (r"\bmillas\b", "miles"),
    (r"\bpasando\b", "passing"),
    (r"\bmanteniendo\b", "maintaining"),
    (r"\bcancel[ao] (?:el |la )?(mayday|pan pan|emergencia)\b", "cancel mayday"),
    (r"\bemergencia\b", "emergency"),
    (r"\b(?:regreso|regresar|retorno|retornar|volver)\b", "return"),
    (r"\baterrizaje inmediato\b|\baterrizar de inmediato\b", "immediate landing"),
    (r"\b(?:personas|almas|pasajeros) a bordo\b", "persons on board"),
    (r"\bhoras?\b", "hours"),
    (r"\bminutos?\b", "minutes"),
    (r"\b(?:de combustible|de autonomia)\b", "fuel"),
    (r"\bdesviando a\b|\bdesviamos a\b", "diverting to"),
    (r"\b(?:recibido|copiado|entendido)\b", "roger"),
    (r"\bcumplire\b", "wilco"),
    (r"\bafirmativo\b", "affirm"),
    (r"\bnegativo\b", "negative"),
    (r"\brepita\b|\brepetir\b", "say again"),
    (r"\btransito\b", "traffic"),
    (r"\bcambio de frecuencia\b", "frequency change"),
    (r"\bfrecuencia\b", "frequency"),
    (r"\b(?:posicion|puesto|puerta)\b", "stand"),
    (r"\bplataforma\b", "apron"),
    (r"\baltimetro\b", "altimeter"),
    (r"\b(?:transpondedor|codigo)\b", "squawk"),
    (r"\bbuen(?:os)? (?:dias?|tardes|noches|vuelo)\b|\bbuenas (?:tardes|noches)\b|\bhasta luego\b", "good day"),
    (r"\btorre\b", "tower"),
    (r"\bsuperficie\b", "ground"),
    (r"\b(?:autorizaciones|entrega)\b", "delivery"),
    (r"\baproximacion\b", "approach"),
    (r"\bsalidas?\b", "departure"),
    (r"\bpista\b", "runway"),
    (r"\bcon\b", "with"),
    (r"\bpara\b", "for"),
]
_TO_EN_COMPILED = [(re.compile(p), r) for p, r in _TO_EN]


_TENS_EN = {"diez": 10, "once": 11, "doce": 12, "trece": 13, "catorce": 14, "quince": 15, "dieciseis": 16,
            "diecisiete": 17, "dieciocho": 18, "diecinueve": 19, "veinte": 20, "veintiuno": 21, "veintiun": 21,
            "veintidos": 22, "veintitres": 23, "veinticuatro": 24, "veinticinco": 25, "veintiseis": 26,
            "veintisiete": 27, "veintiocho": 28, "veintinueve": 29, "treinta": 30, "cuarenta": 40, "cincuenta": 50,
            "sesenta": 60, "setenta": 70, "ochenta": 80, "noventa": 90}
_UNITS_EN = {"uno": 1, "un": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7,
             "ocho": 8, "nueve": 9}


def _counts_en(text: str) -> str:
    """Numbers said as numbers, not digit by digit (persons on board, minutes): 'ciento cincuenta y cuatro' -> 154,
    'cuarenta y cinco' -> 45, 'doce' -> 12."""
    tens = "|".join(sorted(_TENS_EN, key=len, reverse=True))
    units = "|".join(_UNITS_EN)

    def two(m) -> str:
        return str(_TENS_EN[m.group(1)] + (_UNITS_EN[m.group(2)] if m.group(2) else 0))

    text = re.sub(rf"\b({tens})(?: y ({units}))?\b", two, text)
    return re.sub(r"\b(cien|ciento|doscientos|trescientos|cuatrocientos|quinientos|seiscientos|setecientos|ochocientos|"
                  r"novecientos) (\d{1,2})\b", lambda m: str(int(_HUNDREDS_EN[m.group(1)]) * 100 + int(m.group(2))),
                  text)


def _plain(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))


def to_english(text: str) -> str:
    """A Spanish radio call -> the English the parsers understand (numbers as digits)."""
    out = _plain(text)
    for pat, rep in _TO_EN_COMPILED:
        out = pat.sub(rep, out)
    out = _counts_en(out)
    out = re.sub(r"\b(cien|ciento|doscientos|trescientos|cuatrocientos|quinientos|seiscientos|setecientos|ochocientos|"
                 r"novecientos)\b", lambda m: f"{_HUNDREDS_EN[m.group(1)]} hundred", out)
    out = re.sub(r"\b(\w+) mil\b", lambda m: f"{_DIGITS_EN.get(m.group(1), m.group(1))} thousand", out)
    out = re.sub(r"\bmil\b", "1 thousand", out)
    out = re.sub(r"\b(cero|uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve)\b", lambda m: _DIGITS_EN[m.group(1)], out)
    out = re.sub(r"\bcoma\b", "decimal", out)
    return re.sub(r"\by\b", "and", out)


class SpanishSpeaker:
    """Wraps the speaker (PrintTTS / PiperTTS): everything said goes out in Spanish."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def say(self, text: str) -> None:
        self._inner.say(to_spanish(text))

    def say_as(self, text: str, who: str = "ATC", voice=None) -> None:
        self._inner.say_as(to_spanish(text, pilot=who not in ("ATC", "ATIS")), who=who, voice=voice)


STT_PROMPT = ("Control de tránsito aéreo, radio. Pista, rodaje, punto de espera, autorizado a despegar, a favor del "
              "viento, básico, final, tocar y despegar, transpondedor, QNH, colación, mantengo fuera de la pista, "
              "nivel de vuelo, pies, rumbo, solicito, listo para salir, recibido, afirmativo, negativo.")
