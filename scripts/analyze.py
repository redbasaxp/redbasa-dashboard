#!/usr/bin/env python3
"""
Red Basa · Análisis nocturno
Lee la planilla consolidada UNA vez, pre-calcula todo,
y escribe una hoja de resultados plana que el tablero lee directamente.

Estructura de salida (una fila por combinación centro × período × financiador):
  centro | periodo | financiador | nps | n_nps | csat_rrhh | csat_confort | csat_adic | csat_global
  | estrellas | n_estrellas | nps_prev | estrellas_prev | csat_prev
  | dist_nps (JSON) | sparkline (JSON) | resumen_ia | problemas (JSON) | tags
  | fecha_analisis
"""

import os, json, csv, io, re, datetime, urllib.request, urllib.parse, time

# ── CONFIG ────────────────────────────────────────────────────────────────
CONSOLIDATED_SHEET_ID = "1mhUnoBaKmomr2HM3Ojr_-2Anf0deefnS4TWm0P_WLbc"
RESULTS_SHEET_ID      = os.environ["RESULTS_SHEET_ID"]
ANTHROPIC_API_KEY     = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_SA             = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT"])
GOOGLE_RATINGS_SHEET  = os.environ.get("GOOGLE_RATINGS_SHEET_ID", "")

# Token cache
_token_cache = {"token": None, "expires": 0}

# ── PLACE IDs GOOGLE MAPS ─────────────────────────────────────────────────
PLACE_IDS = {
    "POLICLÍNICO REGIONAL AVELLANEDA":      "ChIJc0KmDq7MvJURkWByxyeoVyw",
    "SANATORIO AUGUSTO VANDOR":             "ChIJzbPfUUhyu5URZLl0-uH13d0",
    "CLÍNICA SAGRADO CORAZÓN":              "ChIJm1F2Co6hvJURW2gJxMQMOCI",
    "SANATORIO SAN MARTÍN":                 "ChIJlbjjdna3vJUR2iDMN2kc30Q",
    "CLÍNICA SANTA CLARA VARELA":           "ChIJ0TsF13opo5UR9kNjwwAvUjA",
    "CLÍNICA SANTA CLARA QUILMES":          "ChIJP9HQBQwyo5UR7L7if6QWCqg",
    "CLÍNICA SANTA CLARA MORÓN":            "ChIJh-IYCwDHvJURurPVSQhVKPY",
    "CLÍNICA SANTA CLARA TALAR":            "ChIJm8FZRV2jvJURcRCDW058AZo",
    "CLÍNICA SANTA CLARA ZÁRATE":           "ChIJ61rCoV4Lu5URBuFY7d9Tb0U",
    "SANATORIO GENERAL SARMIENTO":          "ChIJrcA81Gy9vJURt2zOrHt9vjM",
    "SANATORIO LOBOS":                      "ChIJNaVyaVIHvZURUx5ARZhxeXw",
    "CENTRO GALLEGO DE BUENOS AIRES":       "ChIJ4Q2ZVObKvJURfRdBRH9oDds",
    "POLICLÍNICO CENTRAL UOM":              "ChIJwdnBjPbKvJURMeoB0IsY5Go",
    "SANATORIO SAN JOSÉ":                   "ChIJuQaCbITKvJURjCUz0wOjBak",
    "CLÍNICA SANTA ROSA":                   "ChIJ99XsoDEJfpYREkiEXvh2crg",
    "SOCIEDAD ESPAÑOLA":                    "ChIJIVW75CIJfpYR4Ca-xVpF6nk",
    "CLÍNICA SANTA CLARA MENDOZA":          "ChIJ34WiSWUJfpYRxxaTK0Lc2eY",
    "CENTRO MÉDICO SANTA CLARA DORREGO MALL":"ChIJFWK77mIJfpYRLgavaHRKN4I",
    "SANATORIO JULIÁN MORENO":              "ChIJtwTLdgAttpUR6VRcjlAeOz0",
    "CLÍNICA SANTA CLARA SAN JUAN":         "ChIJxxxVZCdAgZYRKXU0VzA5M80",
}

def match_place_id(centro_name):
    """Match a centro name from the sheet to a Place ID (fuzzy)."""
    cu = centro_name.upper().strip()
    # Direct match
    if cu in PLACE_IDS:
        return PLACE_IDS[cu]
    # Partial match
    for key, pid in PLACE_IDS.items():
        if key in cu or cu in key:
            return pid
    return None

# Columnas 0-based
C_DATE  = 0;  C_CENTRO = 2
C_ADM   = 3;  C_MED    = 4;  C_ENF  = 5;  C_SEG  = 6;  C_LIMP  = 7
C_LCAL  = 8;  C_INST   = 9;  C_MENU = 10
C_STAR  = 11; C_NPS    = 12
C_ESP   = 15; C_SOL    = 16; C_CMT  = 17; C_PREP = 22
C_NOMBRE = 18  # S: Nombre del paciente — se usa para filtrar filas de prueba

PREMIUM_NAMES = ['Swiss Medical','OSDE','Omint','Medicus','Sanidad','Accord Salud','Galeno','Jerárquico']
PREMIUM_KEYS  = [p.lower() for p in PREMIUM_NAMES] + ['swis medical','jerarquico']
MIN_N = 5

# ── UTILS ─────────────────────────────────────────────────────────────────
def get_token():
    """Get (or reuse) a Google OAuth2 token via service account JWT."""
    now = int(time.time())
    if _token_cache["token"] and now < _token_cache["expires"] - 60:
        return _token_cache["token"]

    import base64
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.backends import default_backend

    sa = GOOGLE_SA
    hdr = base64.urlsafe_b64encode(json.dumps({"alg":"RS256","typ":"JWT"}).encode()).rstrip(b'=').decode()
    pay = base64.urlsafe_b64encode(json.dumps({
        "iss": sa["client_email"],
        "scope": "https://www.googleapis.com/auth/spreadsheets",
        "aud": "https://oauth2.googleapis.com/token",
        "iat": now, "exp": now + 3600
    }).encode()).rstrip(b'=').decode()
    pk = serialization.load_pem_private_key(sa["private_key"].encode(), password=None, backend=default_backend())
    sig = base64.urlsafe_b64encode(
        pk.sign(f"{hdr}.{pay}".encode(), padding.PKCS1v15(), hashes.SHA256())
    ).rstrip(b'=').decode()
    jwt = f"{hdr}.{pay}.{sig}"

    data = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": jwt
    }).encode()
    with urllib.request.urlopen(urllib.request.Request("https://oauth2.googleapis.com/token", data=data)) as r:
        resp = json.loads(r.read())
    _token_cache["token"]   = resp["access_token"]
    _token_cache["expires"] = now + resp.get("expires_in", 3600)
    return _token_cache["token"]

def fetch_sheet_values(sheet_id):
    """Read all values from sheet using Sheets API v4 (authenticated)."""
    token = get_token()
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/A1:Z100000"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req) as r:
        data = json.loads(r.read())
    rows = data.get("values", [])
    # Pad rows to same length
    max_len = max((len(r) for r in rows), default=0)
    return [r + [''] * (max_len - len(r)) for r in rows]

def fetch_csv(sheet_id, gid="0"):
    """Kept for compatibility — uses authenticated Sheets API instead of CSV export."""
    return fetch_sheet_values(sheet_id)

def parse_date(s):
    if not s: return None
    m = re.match(r'(\d{1,2})/(\d{1,2})/(\d{4})', str(s).strip())
    if m: return datetime.date(int(m[3]), int(m[2]), int(m[1]))
    return None

def today(): return datetime.date.today()

def cutoffs():
    t = today()
    def safe_month(y, m, d):
        import calendar
        last = calendar.monthrange(y, m)[1]
        return datetime.date(y, m, min(d, last))
    wk  = t - datetime.timedelta(days=7)
    mo  = safe_month(t.year if t.month>1 else t.year-1, t.month-1 if t.month>1 else 12, t.day)
    yr  = safe_month(t.year-1, t.month, t.day)
    pwk = t - datetime.timedelta(days=14)
    pmo = safe_month(t.year if t.month>2 else t.year-1, (t.month-2) if t.month>2 else (12+t.month-2), t.day)
    pyr = safe_month(t.year-2, t.month, t.day)
    return {
        'week':  (wk,  t),
        'month': (mo,  t),
        'year':  (yr,  t),
        'prev_week':  (pwk, wk),
        'prev_month': (pmo, mo),
        'prev_year':  (pyr, yr),
    }

def is_premium(p):
    if not p: return False
    pl = p.lower().strip()
    return any(k in pl for k in PREMIUM_KEYS)

def norm_prepaga(p):
    """Return canonical premium name or None."""
    if not p: return None
    pl = p.lower().strip()
    for name in PREMIUM_NAMES:
        if name.lower() in pl: return name
    if 'swis' in pl: return 'Swiss Medical'
    if 'jerarquico' in pl or 'jerárquico' in pl: return 'Jerárquico'
    return None

TEXT_MAP = {'muy bueno':5,'muy malo':1,'bueno':4,'malo':2,'regular':3}
def t2n(v):
    if not v: return None
    return TEXT_MAP.get(str(v).lower().strip())

def to_num(v):
    try: return float(str(v).replace(',','.'))
    except: return None

def invalid(v):
    if not v: return True
    s = str(v).lower().strip()
    return not s or any(x in s for x in ['no tengo','no aplica','no me corresponde','no opinion'])

def safe(r, i):
    return r[i] if i < len(r) else ''

# ── METRIC CALCS ──────────────────────────────────────────────────────────
def calc_nps(rows):
    vals = [v for r in rows for v in [to_num(safe(r,C_NPS))] if v is not None and 1<=v<=10]
    if len(vals) < MIN_N: return None, len(vals), None, None
    p = sum(1 for v in vals if v>=9) / len(vals)
    d = sum(1 for v in vals if v<=6) / len(vals)
    return round((p-d)*100), len(vals), round(p*100,1), round(d*100,1)

def calc_csat_col(rows, cols, use_text):
    vals = []
    for r in rows:
        for c in cols:
            v = safe(r,c)
            if invalid(v): continue
            n = t2n(v) if use_text else to_num(v)
            if n is not None and 1<=n<=5: vals.append(n)
    if len(vals) < MIN_N: return None
    return round(sum(vals)/len(vals), 2)

def calc_csat(rows):
    rrhh = calc_csat_col(rows, [C_ADM,C_MED,C_ENF,C_SEG,C_LIMP], True)
    conf = calc_csat_col(rows, [C_LCAL,C_INST,C_MENU], False)
    adic = calc_csat_col(rows, [C_ESP,C_SOL], True)
    parts = [v for v in [rrhh,conf,adic] if v is not None]
    glob = round(sum(parts)/len(parts),2) if parts else None
    return rrhh, conf, adic, glob

def calc_stars(rows):
    vals = [v for r in rows for v in [to_num(safe(r,C_STAR))] if v is not None and 1<=v<=5]
    if len(vals) < MIN_N: return None, len(vals)
    return round(sum(vals)/len(vals),2), len(vals)

def calc_dist(rows):
    """Distribution of NPS scores 1-10."""
    counts = {i:0 for i in range(1,11)}
    for r in rows:
        v = to_num(safe(r,C_NPS))
        if v is not None and 1<=v<=10:
            counts[int(v)] += 1
    return counts

def calc_sparkline(rows):
    """Monthly NPS/stars for last 18 months. Returns list of {m, nps, stars}."""
    t = today()
    result = []
    for i in range(17,-1,-1):
        yr  = t.year  + (t.month - 1 - i) // 12 * (1 if (t.month-1-i)>=0 else -1)
        mo  = ((t.month - 1 - i) % 12) + 1
        yr  = t.year + ((t.month - 1 - i) // 12)
        if t.month - 1 - i < 0:
            yr = t.year - (-(t.month - 1 - i) + 11) // 12
            mo = 12 - (-(t.month - 1 - i) - 1) % 12
        label = f"{yr}-{mo:02d}"
        mrows = [r for r in rows if parse_date(safe(r,C_DATE)) and
                 parse_date(safe(r,C_DATE)).year==yr and parse_date(safe(r,C_DATE)).month==mo]
        nps_v, n_nps, *_ = calc_nps(mrows)
        st_v, n_st       = calc_stars(mrows)
        _, _, _, csat_v  = calc_csat(mrows)
        result.append({"m": label, "nps": nps_v, "stars": st_v, "csat": csat_v, "n": len(mrows)})
    return result

# ── FILTER BY DATE ────────────────────────────────────────────────────────
def period_rows(rows, start, end):
    return [r for r in rows if parse_date(safe(r,C_DATE)) and start <= parse_date(safe(r,C_DATE)) <= end]

# ── FINANCIADOR GROUPS ────────────────────────────────────────────────────
def financiador_rows(rows, fin):
    if fin == 'TODAS':      return rows
    if fin == 'PREMIUM':    return [r for r in rows if is_premium(safe(r,C_PREP))]
    if fin == 'NO_PREMIUM': return [r for r in rows if safe(r,C_PREP) and not is_premium(safe(r,C_PREP))]
    if fin == 'SIN_DATO':   return [r for r in rows if not safe(r,C_PREP)]
    # Individual premium name
    return [r for r in rows if norm_prepaga(safe(r,C_PREP)) == fin]

FINANCIADORES = ['TODAS','PREMIUM','NO_PREMIUM','SIN_DATO'] + PREMIUM_NAMES

# ── GOOGLE AUTH ───────────────────────────────────────────────────────────
# get_token() is defined above near fetch_sheet_values

def write_sheet(values):
    token = get_token()
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{RESULTS_SHEET_ID}/values/{urllib.parse.quote('A1')}?valueInputOption=RAW"
    body = json.dumps({"values": values}).encode()
    req = urllib.request.Request(url, data=body, method='PUT')
    req.add_header('Authorization', f'Bearer {token}')
    req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

def clear_sheet():
    token = get_token()
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{RESULTS_SHEET_ID}/values/{urllib.parse.quote('A1:Z2000')}:clear"
    req = urllib.request.Request(url, data=b'{}', method='POST')
    req.add_header('Authorization', f'Bearer {token}')
    req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req) as r: pass

# ── COMENTARIOS NEGATIVOS ─────────────────────────────────────────────────
NEGATIVE_SHEET_NAME = "Comentarios Negativos"
NEGATIVE_COMMENTS_HEADER = [
    "id", "fecha_encuesta", "centro", "financiador", "estrellas",
    "nombre_paciente", "comentario", "fecha_deteccion", "notificado_dm", "fecha_notificacion"
]
NPS_NEGATIVO_UMBRAL = 4   # NPS ≤ 4
FECHA_INICIO_ALERTAS = datetime.date(2026, 5, 1)

def ensure_negative_sheet(token):
    """Create the 'Comentarios Negativos' sheet if it doesn't exist."""
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{RESULTS_SHEET_ID}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req) as r:
        meta = json.loads(r.read())
    sheets = [s['properties']['title'] for s in meta.get('sheets', [])]
    if NEGATIVE_SHEET_NAME not in sheets:
        body = json.dumps({"requests": [{"addSheet": {"properties": {"title": NEGATIVE_SHEET_NAME}}}]}).encode()
        req2 = urllib.request.Request(
            f"https://sheets.googleapis.com/v4/spreadsheets/{RESULTS_SHEET_ID}:batchUpdate",
            data=body, method='POST')
        req2.add_header("Authorization", f"Bearer {token}")
        req2.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req2) as r:
            json.loads(r.read())
        print(f"   ✓ Hoja '{NEGATIVE_SHEET_NAME}' creada")
        # Write header
        write_negative_rows([NEGATIVE_COMMENTS_HEADER], token, overwrite_range="'Comentarios Negativos'!A1:I1")
    else:
        print(f"   ✓ Hoja '{NEGATIVE_SHEET_NAME}' ya existe")

def read_existing_negative_comments(token):
    """Read existing negative comments. Returns list of dicts and set of existing keys."""
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{RESULTS_SHEET_ID}/values/{urllib.parse.quote(NEGATIVE_SHEET_NAME + '!A1:I10000')}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read())
        rows = data.get("values", [])
        if len(rows) < 2:
            return [], set()
        header = rows[0]
        records = []
        keys = set()
        for row in rows[1:]:
            padded = row + [''] * (len(header) - len(row))
            rec = dict(zip(header, padded))
            records.append(rec)
            # Key: fecha_encuesta + centro + primeros 80 chars del comentario
            k = f"{rec.get('fecha_encuesta','')}|{rec.get('centro','')}|{rec.get('comentario','')[:80]}"
            keys.add(k)
        return records, keys
    except Exception as e:
        print(f"   ⚠ Error leyendo comentarios negativos: {e}")
        return [], set()

def write_negative_rows(values, token, overwrite_range=None):
    """Append rows to the Comentarios Negativos sheet."""
    if overwrite_range:
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{RESULTS_SHEET_ID}/values/{urllib.parse.quote(overwrite_range)}?valueInputOption=RAW"
        method = 'PUT'
    else:
        range_name = urllib.parse.quote(f"{NEGATIVE_SHEET_NAME}!A1")
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{RESULTS_SHEET_ID}/values/{range_name}:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS"
        method = 'POST'
    body = json.dumps({"values": values}).encode()
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

NEGATIVE_KEYWORDS = [
    'mal', 'malo', 'mala', 'pésimo', 'pésima', 'terrible', 'horrible',
    'demora', 'espera', 'tardaron', 'tarde', 'lento', 'lenta',
    'maltrato', 'maltrataron', 'grosero', 'grosera', 'descortés',
    'sucio', 'sucia', 'mugre', 'inmundo', 'desastre',
    'no atienden', 'no atendieron', 'ignoraron', 'abandonaron',
    'error', 'equivocaron', 'equivocación', 'negligencia', 'negligente',
    'queja', 'reclamo', 'indignante', 'vergüenza', 'inaceptable',
    'no funciona', 'roto', 'falta', 'faltan', 'no hay',
]

def is_negative_comment(row):
    """Return True if row has NPS ≤ 4 OR clearly negative comment text."""
    nps = to_num(safe(row, C_NPS))
    if nps is not None and nps <= NPS_NEGATIVO_UMBRAL:
        return True
    cmt = safe(row, C_CMT).lower().strip()
    if not cmt or len(cmt) < 10:
        return False
    return any(kw in cmt for kw in NEGATIVE_KEYWORDS)

def process_negative_comments(all_rows, token):
    """Detect new negative comments since FECHA_INICIO_ALERTAS and append to sheet."""
    print(f"\n5. Procesando comentarios negativos (desde {FECHA_INICIO_ALERTAS})...")
    ensure_negative_sheet(token)
    _, existing_keys = read_existing_negative_comments(token)

    new_rows = []
    today_str = today().isoformat()
    counter_start = len(existing_keys) + 1

    for row in all_rows:
        fecha = parse_date(safe(row, C_DATE))
        if not fecha or fecha < FECHA_INICIO_ALERTAS:
            continue
        if not is_negative_comment(row):
            continue
        cmt = safe(row, C_CMT).strip()
        if not cmt:
            continue
        centro      = safe(row, C_CENTRO).strip()
        nombre      = safe(row, C_NOMBRE).strip()
        fin         = norm_prepaga(safe(row, C_PREP)) or ('PREMIUM' if is_premium(safe(row, C_PREP)) else ('NO PREMIUM' if safe(row, C_PREP) else 'SIN DATO'))
        estrellas_val = to_num(safe(row, C_STAR)) or ''
        key         = f"{fecha.isoformat()}|{centro}|{cmt[:80]}"
        if key in existing_keys:
            continue
        existing_keys.add(key)
        row_id = f"NC-{counter_start:04d}"
        counter_start += 1
        new_rows.append([
            row_id,
            fecha.isoformat(),
            centro,
            fin,
            estrellas_val,
            nombre,
            cmt,
            today_str,
            '',
            '',
        ])

    if new_rows:
        write_negative_rows(new_rows, token)
        print(f"   ✓ {len(new_rows)} comentarios nuevos agregados")
    else:
        print(f"   ✓ Sin comentarios nuevos")

# ── COBERTURA EP ──────────────────────────────────────────────────────────
COBERTURA_SHEET_NAME = "Cobertura EP"
COBERTURA_HEADER = [
    "centro", "ep_desde",
    "altas_total", "encuestas_total", "pct_cobertura_total",
    "altas_semana", "encuestas_semana", "pct_cobertura_semana",
    "altas_mes",   "encuestas_mes",   "pct_cobertura_mes",
    "altas_anio",  "encuestas_anio",  "pct_cobertura_anio",
    "fecha_actualizacion"
]

# Planillas de trabajo EP — sheet_id + nombre de hoja donde están los pacientes
EP_PLANILLAS = {
    "SANATORIO SAN JOSÉ": {
        "ep_desde": "2026-02",
        "fuentes": [
            {"sheet_id": "1cUW3oHoNPhpKpppkGPlab7EcG0rrlrvFxKJvOkr4g5Y", "hoja": "pacientes"},
        ]
    },
    "CENTRO GALLEGO DE BUENOS AIRES": {
        "ep_desde": "2025-10",
        "fuentes": [
            {"sheet_id": "1eawLlhFCNc0-jw3I8ljkmvwpTt-G66ZRE3aqq0Ofxvw", "hoja": "pacientes"},
            {"sheet_id": "1ac-_Ifkwzs5bUsFw1JxPeIiSPtETPZIBP5JpFmkPHO0",  "hoja": "altas"},
        ]
    },
    "SANTA CLARA FLORENCIO VARELA": {
        "ep_desde": "2026-05",
        "fuentes": [
            {"sheet_id": "1X9IlYWOqsEvD7OB4uRkGuEjmbZRAyuKX5G-WIhXumII", "hoja": "pacientes"},
        ]
    },
}

def parse_fecha_flexible(s):
    """Intenta parsear fechas en formato DD/MM/YYYY, YYYY-MM-DD, D/M/YYYY."""
    s = s.strip()
    if not s:
        return None
    # Tomar solo la parte de fecha (ignorar hora)
    s = s.split(' ')[0].split('\t')[0]
    for fmt in ('%d/%m/%Y', '%Y-%m-%d', '%d-%m-%Y', '%m/%d/%Y'):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except:
            pass
    return None

def count_altas_from_sheet(sheet_id, hoja, token, desde=None, hasta=None):
    """
    Lee la hoja de trabajo EP y cuenta filas con Fecha Alta no vacía,
    excluyendo Duplicados. Filtra por rango de fechas si se indica.
    """
    range_name = urllib.parse.quote(f"'{hoja}'!A1:Z3000")
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{range_name}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read())
    except Exception as e:
        print(f"     ⚠ Error leyendo {sheet_id}/{hoja}: {e}")
        return 0

    rows = data.get("values", [])
    if len(rows) < 2:
        return 0

    # Encontrar el primer header con columnas relevantes
    header_idx, header = None, []
    for i, row in enumerate(rows):
        rl = [str(c).strip().lower() for c in row]
        if any('fecha alta' in c or c == 'alta' for c in rl) and \
           any('encuesta' in c or 'nombre' in c or 'paciente' in c for c in rl):
            header_idx = i
            header = rl
            break

    if header_idx is None:
        return 0

    # Índices clave
    col_enc   = next((i for i, h in enumerate(header) if 'encuesta' in h), None)
    col_alta  = next((i for i, h in enumerate(header)
                      if 'fecha alta' in h or h == 'alta'), None)

    if col_alta is None:
        return 0

    count = 0
    for row in rows[header_idx + 1:]:
        if not row or not any(str(c).strip() for c in row[:4]):
            continue
        # Parar si parece otro bloque de header o resumen
        primera = str(row[0]).strip().lower()
        if primera in ('', 'nombre del paciente', 'rango fechas', 'totales', 'semana actual'):
            continue

        # Excluir Duplicados
        enc = str(row[col_enc]).strip().lower() if col_enc is not None and col_enc < len(row) else ''
        if enc == 'duplicado':
            continue

        # Verificar que tenga fecha de alta
        alta_str = str(row[col_alta]).strip() if col_alta < len(row) else ''
        if not alta_str or alta_str in ('-', ''):
            continue

        fecha_alta = parse_fecha_flexible(alta_str)
        if fecha_alta is None:
            continue

        # Filtrar por rango si aplica
        if desde and fecha_alta < desde:
            continue
        if hasta and fecha_alta > hasta:
            continue

        count += 1

    return count

def ensure_cobertura_sheet(token):
    """Crea la hoja Cobertura EP si no existe."""
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{RESULTS_SHEET_ID}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req) as r:
        meta = json.loads(r.read())
    sheets = [s['properties']['title'] for s in meta.get('sheets', [])]
    if COBERTURA_SHEET_NAME not in sheets:
        body = json.dumps({"requests": [{"addSheet": {"properties": {"title": COBERTURA_SHEET_NAME}}}]}).encode()
        req2 = urllib.request.Request(
            f"https://sheets.googleapis.com/v4/spreadsheets/{RESULTS_SHEET_ID}:batchUpdate",
            data=body, method='POST')
        req2.add_header("Authorization", f"Bearer {token}")
        req2.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req2):
            pass
        print(f"   ✓ Hoja '{COBERTURA_SHEET_NAME}' creada")

def write_cobertura_rows(rows, token):
    """Sobreescribe la hoja Cobertura EP."""
    sheet_quoted = urllib.parse.quote(COBERTURA_SHEET_NAME)
    range_name   = urllib.parse.quote(f"'{COBERTURA_SHEET_NAME}'!A1")
    # Clear entire sheet
    url_clear = f"https://sheets.googleapis.com/v4/spreadsheets/{RESULTS_SHEET_ID}/values/{sheet_quoted}:clear"
    req = urllib.request.Request(url_clear, data=b'{}', method='POST')
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req):
        pass
    # Write
    url_write = f"https://sheets.googleapis.com/v4/spreadsheets/{RESULTS_SHEET_ID}/values/{range_name}?valueInputOption=RAW"
    body = json.dumps({"values": rows}).encode()
    req2 = urllib.request.Request(url_write, data=body, method='PUT')
    req2.add_header("Authorization", f"Bearer {token}")
    req2.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req2):
        pass

def get_all_altas_from_sheet(sheet_id, hoja, token):
    """
    Lee la hoja EP y devuelve lista de dicts:
      { 'fecha': date, 'respondida': bool }
    - fecha: Fecha Alta parseada
    - respondida: True si Calificación Clinica tiene valor numérico 1-5
    Excluye Duplicados y filas sin Fecha Alta.
    """
    range_name = urllib.parse.quote(f"'{hoja}'!A1:Z3000")
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{range_name}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read())
    except Exception as e:
        print(f"     ⚠ Error leyendo {sheet_id}/{hoja}: {e}")
        return []

    rows = data.get("values", [])
    if len(rows) < 2:
        return []

    # Buscar el primer header que tenga TANTO Fecha Alta COMO Calificación Clinica
    header_idx, header = None, []
    for i, row in enumerate(rows):
        rl = [str(c).strip().lower() for c in row]
        has_alta  = any('fecha alta' in c or c == 'alta' for c in rl)
        has_calif = any('calificaci' in c for c in rl)
        has_paciente = any('encuesta' in c or 'nombre' in c or 'paciente' in c for c in rl)
        if has_alta and has_calif and has_paciente:
            header_idx = i
            header = rl
            break

    if header_idx is None:
        print(f"     ⚠ No se encontró header válido en {sheet_id}/{hoja}")
        return []

    print(f"     Header en fila {header_idx}: enc={col_enc}, alta={col_alta}, calif={col_calif}")
    print(f"     Primeras cols: {header[:8]}")

    col_enc  = next((i for i, h in enumerate(header) if 'encuesta' in h and 'tipo' not in h and 'envio' not in h), None)
    col_alta = next((i for i, h in enumerate(header) if 'fecha alta' in h or h == 'alta'), None)
    col_calif = next((i for i, h in enumerate(header) if 'calificaci' in h), None)

    if col_alta is None:
        return []

    resultados = []
    for row in rows[header_idx + 1:]:
        if not row:
            continue
        primera = str(row[0]).strip().lower()

        # Parar si encontramos la fila de resumen ("Totales", "Semana Actual", etc.)
        if primera in ('totales', 'semana actual'):
            break
        # Parar si la fila parece un segundo bloque de header
        # (tiene "nombre del paciente" en col 0 pero NO tiene Fecha Alta en la posición esperada)
        if primera == 'nombre del paciente':
            # Verificar si este header tiene calificación — si no, es otro bloque, parar
            row_lower = [str(c).strip().lower() for c in row]
            if not any('calificaci' in c for c in row_lower):
                break
        # Ignorar filas completamente vacías
        if not any(str(c).strip() for c in row[:4]):
            continue
        # Ignorar si la primera celda empieza con texto que indica resumen
        if any(primera.startswith(x) for x in ('rango', 'semana', 'total', 'scsj')):
            break

        # Excluir Duplicados
        enc = str(row[col_enc]).strip().lower() if col_enc is not None and col_enc < len(row) else ''
        if enc == 'duplicado':
            continue

        # Requiere Fecha Alta
        alta_str = str(row[col_alta]).strip() if col_alta < len(row) else ''
        if not alta_str or alta_str in ('-', ''):
            continue
        fecha = parse_fecha_flexible(alta_str)
        if fecha is None:
            continue

        # Respondida = Calificación Clinica tiene valor numérico
        respondida = False
        if col_calif is not None and col_calif < len(row):
            cal = str(row[col_calif]).strip()
            if cal and cal not in ('', '-'):
                try:
                    v = float(cal)
                    respondida = 1 <= v <= 5
                except:
                    pass

        resultados.append({'fecha': fecha, 'respondida': respondida})

    return resultados

def process_cobertura_ep(token):
    """
    Lee planillas EP, usa Calificación Clinica para determinar respondidas,
    Fecha Alta para el período. Todo desde la misma fuente.
    """
    print(f"\n6. Procesando cobertura EP...")
    ensure_cobertura_sheet(token)

    hoy = today()

    rows_out = [COBERTURA_HEADER]
    for centro, cfg in EP_PLANILLAS.items():

        # Recolectar todas las altas de todas las fuentes
        todas = []
        for fuente in cfg["fuentes"]:
            filas = get_all_altas_from_sheet(fuente["sheet_id"], fuente["hoja"], token)
            todas.extend(filas)
            print(f"     {centro} / {fuente['hoja']}: {len(filas)} altas leídas")

        if not todas:
            print(f"   {centro}: sin datos")
            rows_out.append([centro, cfg["ep_desde"], 0,0,0, 0,0,0, 0,0,0, 0,0,0, hoy.isoformat()])
            continue

        fecha_min = min(f['fecha'] for f in todas)

        def stats_rango(desde, hasta):
            subset = [f for f in todas if desde <= f['fecha'] <= hasta]
            altas = len(subset)
            enc   = sum(1 for f in subset if f['respondida'])
            pct   = round(min(enc / altas * 100, 100), 1) if altas > 0 else 0
            return altas, enc, pct

        desde_sem  = max(fecha_min, hoy - datetime.timedelta(days=7))
        desde_mes  = max(fecha_min, hoy - datetime.timedelta(days=30))
        desde_anio = max(fecha_min, hoy - datetime.timedelta(days=365))

        alt_tot,  enc_tot,  pct_tot  = stats_rango(fecha_min, hoy)
        alt_sem,  enc_sem,  pct_sem  = stats_rango(desde_sem,  hoy)
        alt_mes,  enc_mes,  pct_mes  = stats_rango(desde_mes,  hoy)
        alt_anio, enc_anio, pct_anio = stats_rango(desde_anio, hoy)

        row = [
            centro,
            fecha_min.isoformat(),
            alt_tot,  enc_tot,  pct_tot,
            alt_sem,  enc_sem,  pct_sem,
            alt_mes,  enc_mes,  pct_mes,
            alt_anio, enc_anio, pct_anio,
            hoy.isoformat(),
        ]
        rows_out.append(row)
        print(f"   {centro}: inicio={fecha_min} | tot={alt_tot}/{enc_tot} ({pct_tot}%) | mes={alt_mes}/{enc_mes} ({pct_mes}%) | sem={alt_sem}/{enc_sem} ({pct_sem}%)")

    write_cobertura_rows(rows_out, token)
    print(f"   ✓ Cobertura EP escrita")

# ── CLAUDE AI ─────────────────────────────────────────────────────────────
def analyze_with_ai(centro, neg_comments, nps_val, csat_val, stars_val):
    if not neg_comments:
        return "Sin comentarios negativos en el período.", "[]", ""
    prompt = f"""Sos analista de calidad de atención médica para {centro}.

Métricas del último mes: NPS={nps_val}, CSAT={csat_val}, Estrellas={stars_val}

Comentarios negativos (NPS≤6 o estrellas≤2):
{chr(10).join(f'- {c[:120]}' for c in neg_comments[:20])}

Respondé SOLO con JSON válido (sin markdown, sin texto extra, máximo 800 caracteres total):
{{"resumen":"máximo 2 oraciones cortas sobre los problemas principales","problemas":[{{"tema":"...","frecuencia":"alta|media|baja","ejemplo":"max 60 chars"}}],"tags":["tag1","tag2","tag3"]}}"""

    data = json.dumps({"model":"claude-haiku-4-5-20251001","max_tokens":600,
                       "messages":[{"role":"user","content":prompt}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=data)
    req.add_header('x-api-key', ANTHROPIC_API_KEY)
    req.add_header('anthropic-version', '2023-06-01')
    req.add_header('content-type', 'application/json')
    try:
        with urllib.request.urlopen(req) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='ignore')
        print(f"     ⚠ API error {e.code}: {body[:300]}")
        return "Error en análisis IA.", '[]', ''
    except Exception as e:
        print(f"     ⚠ API error: {e}")
        return "Error en análisis IA.", '[]', ''
    text = resp['content'][0]['text'].strip()
    # Limpiar markdown fences si el modelo los incluye
    text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'```\s*$', '', text, flags=re.MULTILINE)
    text = text.strip()
    # Extraer solo el objeto JSON si hay texto extra
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        text = match.group(0)
    try:
        p = json.loads(text)
        resumen   = p.get('resumen','')
        problemas = json.dumps(p.get('problemas',[]), ensure_ascii=False)
        tags      = ','.join(p.get('tags',[]))
        return resumen, problemas, tags
    except Exception as ex:
        print(f"     DEBUG parse error: {ex} | text: {text[:200]}")
        return text[:300], '[]', ''

# ── GOOGLE MAPS SCRAPING ─────────────────────────────────────────────────
def scrape_google_rating(place_id):
    """
    Fetch rating and review count from Google Maps for a given Place ID.
    Uses the public /maps/place URL — no API key required.
    Returns (rating, review_count) or (None, None) on failure.
    """
    url = f"https://www.google.com/maps/place/?q=place_id:{place_id}"
    req = urllib.request.Request(url)
    req.add_header('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
    req.add_header('Accept-Language', 'es-AR,es;q=0.9')
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode('utf-8', errors='ignore')

        # Rating: appears as "4.2" near "estrellas" or in structured data
        rating = None
        review_count = None

        # Try structured data first (most reliable)
        m = re.search(r'"aggregateRating".*?"ratingValue":\s*"?([\d.]+)"?', html)
        if m:
            rating = float(m.group(1))

        # Fallback: look for pattern like >4.2< near reviews
        if not rating:
            m = re.search(r'(\d\.\d)\s*\([\d,\.]+\s*rese', html)
            if m:
                rating = float(m.group(1))

        # Fallback 2: window.APP_INITIALIZATION_STATE data
        if not rating:
            m = re.search(r'\[null,(\d\.\d),\d+\]', html)
            if m:
                rating = float(m.group(1))

        # Review count
        m = re.search(r'([\d,\.]+)\s*rese[ñn]as?', html)
        if m:
            review_count = int(re.sub(r'[,\.]', '', m.group(1)))

        if not review_count:
            m = re.search(r'\[null,(\d\.\d),(\d+)\]', html)
            if m:
                review_count = int(m.group(2))

        return rating, review_count
    except Exception as e:
        print(f"     ⚠ Error scraping {place_id}: {e}")
        return None, None

def fetch_google_ratings(centros):
    """Scrape Google Maps ratings for all centros. Returns dict centro→{rating, reviews}."""
    print("\n4. Scraping Google Maps ratings...")
    results = {}
    for centro in centros:
        pid = match_place_id(centro)
        if not pid:
            print(f"   ⚠ Sin Place ID para: {centro}")
            continue
        rating, reviews = scrape_google_rating(pid)
        results[centro] = {'rating': rating, 'reviews': reviews, 'place_id': pid}
        status = f"★ {rating} ({reviews} reseñas)" if rating else "sin datos"
        print(f"   {centro[:40]:<40} {status}")
        time.sleep(1.5)  # rate limit
    return results

def write_ratings_sheet(token, ratings_by_centro):
    """Append today's ratings to the Google Ratings sheet."""
    if not GOOGLE_RATINGS_SHEET:
        print("   ⚠ GOOGLE_RATINGS_SHEET_ID no configurado, saltando escritura de ratings")
        return
    t = today().isoformat()
    new_rows = []
    for centro, d in ratings_by_centro.items():
        if d['rating'] is not None:
            new_rows.append([centro, t, d['rating'], d['reviews'] or 0])
    if not new_rows:
        print("   ⚠ Sin ratings para escribir")
        return
    # Append (don't clear — we want historical data)
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{GOOGLE_RATINGS_SHEET}/values/{urllib.parse.quote('Hoja 1!A:D')}:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS"
    body = json.dumps({"values": new_rows}).encode()
    req = urllib.request.Request(url, data=body, method='POST')
    req.add_header('Authorization', f'Bearer {token}')
    req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req) as r:
        json.loads(r.read())
    print(f"   ✓ {len(new_rows)} ratings escritos")

def read_ratings_history():
    """Read full ratings history from Google Ratings sheet. Returns dict centro→list of {fecha,rating,reviews}."""
    if not GOOGLE_RATINGS_SHEET:
        return {}
    try:
        token = get_token()
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{GOOGLE_RATINGS_SHEET}/values/A1:D10000"
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read())
        rows = data.get("values", [])
        history = {}
        for row in rows[1:]:  # skip header
            if len(row) < 3: continue
            centro, fecha, rating = row[0], row[1], row[2]
            reviews = int(row[3]) if len(row) > 3 else 0
            try: rating = float(rating)
            except: continue
            if centro not in history:
                history[centro] = []
            history[centro].append({'fecha': fecha, 'rating': rating, 'reviews': reviews})
        return history
    except Exception as e:
        print(f"   ⚠ Error leyendo ratings history: {e}")
        return {}

# ── MAIN ──────────────────────────────────────────────────────────────────
def main():
    print("=== Red Basa · Análisis nocturno ===")
    print(f"Fecha: {today()}")

    print("\n1. Descargando planilla consolidada...")
    raw = fetch_sheet_values(CONSOLIDATED_SHEET_ID)
    all_rows = [r for r in raw[1:] if r and len(r)>C_CENTRO and safe(r,C_DATE) and safe(r,C_CENTRO)
                and 'prueba' not in safe(r,C_NOMBRE).lower()]
    print(f"   {len(all_rows)} filas cargadas")

    cuts = cutoffs()
    centros = sorted(set(safe(r,C_CENTRO).strip() for r in all_rows))
    print(f"   Centros: {centros}")

    # ── GOOGLE RATINGS (se leen de la hoja manual, sin scraping) ─────────
    print("\n4. Leyendo Google Ratings desde hoja manual...")
    ratings_history = read_ratings_history()

    HEADER = [
        "centro","periodo","financiador",
        "nps","n_nps","pct_promotores","pct_detractores",
        "csat_rrhh","csat_confort","csat_adic","csat_global",
        "csat_adm","csat_med","csat_enf","csat_seg","csat_limp",
        "csat_limp_cal","csat_inst","csat_menu",
        "csat_espera","csat_solucion",
        "estrellas","n_estrellas",
        "nps_prev","csat_prev","estrellas_prev",
        "dist_nps","sparkline",
        "google_rating","google_reviews","google_sparkline",
        "resumen_ia","problemas_ia","tags_ia",
        "fecha_analisis"
    ]
    rows_out = [HEADER]
    PERIODOS = ['week','month','year']

    print("\n2. Pre-calculando métricas...")
    for centro in centros:
        print(f"   → {centro}")
        crows = [r for r in all_rows if safe(r,C_CENTRO).strip()==centro]
        sparkline = calc_sparkline(crows)

        # Google rating actual e historial
        hist = sorted(ratings_history.get(centro, []), key=lambda x: x['fecha'])
        google_rating    = hist[-1]['rating']  if hist else None
        google_reviews   = hist[-1]['reviews'] if hist else None
        google_sparkline = [{'m': h['fecha'], 'r': h['rating']} for h in hist[-18:]]

        # AI analysis
        month_rows = period_rows(crows, *cuts['month'])
        neg_cmts = [safe(r,C_CMT) for r in month_rows
                    if not invalid(safe(r,C_CMT)) and
                    ((to_num(safe(r,C_NPS)) or 99) <= 6 or (to_num(safe(r,C_STAR)) or 99) <= 2)]
        nps_m, n_m, *_ = calc_nps(month_rows)
        _, _, _, csat_m = calc_csat(month_rows)
        st_m, _         = calc_stars(month_rows)
        resumen, problemas, tags = analyze_with_ai(centro, neg_cmts, nps_m, csat_m, st_m)

        for periodo in PERIODOS:
            start, end           = cuts[periodo]
            prev_start, prev_end = cuts[f'prev_{periodo}']
            p_rows  = period_rows(crows, start, end)
            pp_rows = period_rows(crows, prev_start, prev_end)
            dist    = calc_dist(p_rows)

            for fin in FINANCIADORES:
                f_rows  = financiador_rows(p_rows, fin)
                fp_rows = financiador_rows(pp_rows, fin)

                nps_v, n_nps, pct_p, pct_d = calc_nps(f_rows)
                rrhh, conf, adic, glob      = calc_csat(f_rows)
                st_v, n_st                  = calc_stars(f_rows)
                c_adm  = calc_csat_col(f_rows, [C_ADM],  True)
                c_med  = calc_csat_col(f_rows, [C_MED],  True)
                c_enf  = calc_csat_col(f_rows, [C_ENF],  True)
                c_seg  = calc_csat_col(f_rows, [C_SEG],  True)
                c_limp = calc_csat_col(f_rows, [C_LIMP], True)
                c_lcal = calc_csat_col(f_rows, [C_LCAL], False)
                c_inst = calc_csat_col(f_rows, [C_INST], False)
                c_menu = calc_csat_col(f_rows, [C_MENU], False)
                c_esp  = calc_csat_col(f_rows, [C_ESP],  True)
                c_sol  = calc_csat_col(f_rows, [C_SOL],  True)
                nps_prev, *_ = calc_nps(fp_rows)
                _, _, _, cp  = calc_csat(fp_rows)
                st_prev, _   = calc_stars(fp_rows)

                def v(x): return x if x is not None else ''

                rows_out.append([
                    centro, periodo, fin,
                    v(nps_v), n_nps, v(pct_p), v(pct_d),
                    v(rrhh), v(conf), v(adic), v(glob),
                    v(c_adm), v(c_med), v(c_enf), v(c_seg), v(c_limp),
                    v(c_lcal), v(c_inst), v(c_menu),
                    v(c_esp), v(c_sol),
                    v(st_v), n_st,
                    v(nps_prev), v(cp), v(st_prev),
                    json.dumps(dist, ensure_ascii=False)             if fin=='TODAS' else '',
                    json.dumps(sparkline, ensure_ascii=False)        if fin=='TODAS' and periodo=='month' else '',
                    v(google_rating)                                 if fin=='TODAS' and periodo=='month' else '',
                    v(google_reviews)                                if fin=='TODAS' and periodo=='month' else '',
                    json.dumps(google_sparkline, ensure_ascii=False) if fin=='TODAS' and periodo=='month' else '',
                    resumen   if fin=='TODAS' and periodo=='month' else '',
                    problemas if fin=='TODAS' and periodo=='month' else '',
                    tags      if fin=='TODAS' and periodo=='month' else '',
                    today().isoformat(),
                ])

        print(f"     {len(PERIODOS)*len(FINANCIADORES)} filas generadas")

    print(f"\n3. Escribiendo {len(rows_out)-1} filas en Google Sheets...")
    clear_sheet()
    write_sheet(rows_out)
    print("   ✓ Listo")
    print(f"\nTotal: {len(centros)} centros × {len(PERIODOS)} períodos × {len(FINANCIADORES)} financiadores = {len(rows_out)-1} filas")

    # ── Comentarios negativos
    token = get_token()
    process_negative_comments(all_rows, token)

    # ── Cobertura EP — todo se calcula desde las planillas EP directamente
    process_cobertura_ep(token)

if __name__ == '__main__':
    main()
