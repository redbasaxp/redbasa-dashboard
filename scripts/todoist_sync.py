#!/usr/bin/env python3
"""
Red Basa · Sincronización Todoist
Corre como paso separado en GitHub Actions.
Lee tareas completadas (Bienvenida, Visita Diaria) por centro
y escribe los conteos en la hoja "Acciones Todoist" del libro de resultados.
"""
import os, json, urllib.request, urllib.parse, datetime, time

# ── CONFIG ────────────────────────────────────────────────────────────────
RESULTS_SHEET_ID  = os.environ.get('RESULTS_SHEET_ID', '')
GOOGLE_SA_JSON    = os.environ.get('GOOGLE_SERVICE_ACCOUNT', '')
ACCIONES_SHEET    = 'Acciones Todoist'
ACCIONES_HEADER   = ['centro', 'periodo', 'bienvenidas', 'visitas', 'fecha_analisis']

CENTROS_TODOIST = {
    'SANATORIO SAN JOSÉ': {
        'token_env': 'TODOIST_SSJ',
        'prefix':    'SSJ',
    },
    # Agregar cuando estén listos:
    # 'CENTRO GALLEGO DE BUENOS AIRES': { 'token_env': 'TODOIST_CGBA', 'prefix': 'CGBA' },
    # 'SANATORIO GRAL. SARMIENTO':      { 'token_env': 'TODOIST_SGS',  'prefix': 'SGS'  },
    # 'SANTA CLARA FLORENCIO VARELA':   { 'token_env': 'TODOIST_SCFV', 'prefix': 'SCFV' },
}

TAREAS = ['Bienvenida al paciente', 'Visita Diaria']

# ── FECHAS ────────────────────────────────────────────────────────────────
def get_periods():
    today     = datetime.date.today()
    week_ago  = today - datetime.timedelta(days=7)
    month_ago = today - datetime.timedelta(days=30)

    # Año: 12 llamadas mensuales encadenadas (API limita ventana máxima)
    year_months = []
    d = today
    for _ in range(12):
        first = d.replace(day=1)
        year_months.append((first, d))
        d = first - datetime.timedelta(days=1)

    return {
        'week':  (week_ago,  today),
        'month': (month_ago, today),
        'year':  year_months,  # lista de (desde, hasta) por mes
    }

# ── TODOIST API v1 ────────────────────────────────────────────────────────
def todoist_get(url, token):
    req = urllib.request.Request(url)
    req.add_header('Authorization', f'Bearer {token}')
    req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='ignore')
        print(f"     ⚠ Todoist HTTP {e.code}: {body[:200]}")
        return None
    except Exception as e:
        print(f"     ⚠ Todoist error: {e}")
        return None

def get_completed_tasks(token, since, until):
    """Trae tareas completadas en un rango de fechas (máx ~6 semanas)."""
    url = (f"https://api.todoist.com/api/v1/tasks/completed/by_completion_date"
           f"?since={since}T00:00:00Z&until={until}T23:59:59Z&limit=200")
    data = todoist_get(url, token)
    if data is None:
        return []
    return data.get('items', data.get('results', []))

def count_tasks(items, tarea):
    """Cuenta tareas completadas por nombre exacto."""
    return sum(1 for i in items
               if i.get('content', i.get('task_content', '')).strip() == tarea)

def fetch_year_counts(token):
    """Trae conteos anuales haciendo 12 llamadas mensuales."""
    counts = {t: 0 for t in TAREAS}
    year_months = get_periods()['year']
    for since, until in year_months:
        items = get_completed_tasks(token, since.isoformat(), until.isoformat())
        for tarea in TAREAS:
            counts[tarea] += count_tasks(items, tarea)
        time.sleep(0.3)  # respetar rate limit
    return counts

# ── GOOGLE SHEETS ─────────────────────────────────────────────────────────
def get_token():
    """Obtiene token OAuth2 usando la cuenta de servicio."""
    import json as _json, base64, time as _time
    try:
        import cryptography
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError:
        import subprocess, sys
        subprocess.check_call([sys.executable, '-m', 'pip', 'install',
                               'cryptography', '-q'])
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

    sa = _json.loads(GOOGLE_SA_JSON)
    now = int(_time.time())
    header  = base64.urlsafe_b64encode(
        _json.dumps({"alg":"RS256","typ":"JWT"}).encode()).rstrip(b'=')
    payload = base64.urlsafe_b64encode(_json.dumps({
        "iss":   sa['client_email'],
        "scope": "https://www.googleapis.com/auth/spreadsheets",
        "aud":   "https://oauth2.googleapis.com/token",
        "iat":   now, "exp": now + 3600
    }).encode()).rstrip(b'=')
    msg = header + b'.' + payload
    key = serialization.load_pem_private_key(
        sa['private_key'].encode(), password=None)
    sig = base64.urlsafe_b64encode(
        key.sign(msg, padding.PKCS1v15(), hashes.SHA256())).rstrip(b'=')
    jwt = (msg + b'.' + sig).decode()

    body = urllib.parse.urlencode({
        'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
        'assertion':  jwt
    }).encode()
    req = urllib.request.Request(
        'https://oauth2.googleapis.com/token', data=body, method='POST')
    req.add_header('Content-Type', 'application/x-www-form-urlencoded')
    with urllib.request.urlopen(req) as r:
        return _json.loads(r.read())['access_token']

def sheets_get(token, range_):
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{RESULTS_SHEET_ID}"
           f"/values/{urllib.parse.quote(range_)}")
    req = urllib.request.Request(url)
    req.add_header('Authorization', f'Bearer {token}')
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

def sheets_put(token, range_, values):
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{RESULTS_SHEET_ID}"
           f"/values/{urllib.parse.quote(range_)}?valueInputOption=RAW")
    body = json.dumps({'values': values}).encode()
    req = urllib.request.Request(url, data=body, method='PUT')
    req.add_header('Authorization', f'Bearer {token}')
    req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

def sheets_append(token, range_, values):
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{RESULTS_SHEET_ID}"
           f"/values/{urllib.parse.quote(range_)}:append"
           f"?valueInputOption=RAW&insertDataOption=INSERT_ROWS")
    body = json.dumps({'values': values}).encode()
    req = urllib.request.Request(url, data=body, method='POST')
    req.add_header('Authorization', f'Bearer {token}')
    req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

def ensure_sheet(gtoken):
    """Crea la hoja 'Acciones Todoist' si no existe."""
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{RESULTS_SHEET_ID}"
    req = urllib.request.Request(url)
    req.add_header('Authorization', f'Bearer {gtoken}')
    with urllib.request.urlopen(req) as r:
        meta = json.loads(r.read())
    sheets = [s['properties']['title'] for s in meta.get('sheets', [])]
    if ACCIONES_SHEET not in sheets:
        body = json.dumps({"requests": [{"addSheet": {
            "properties": {"title": ACCIONES_SHEET}}}]}).encode()
        req2 = urllib.request.Request(
            f"https://sheets.googleapis.com/v4/spreadsheets/{RESULTS_SHEET_ID}:batchUpdate",
            data=body, method='POST')
        req2.add_header('Authorization', f'Bearer {gtoken}')
        req2.add_header('Content-Type', 'application/json')
        with urllib.request.urlopen(req2):
            pass
        print(f"   ✓ Hoja '{ACCIONES_SHEET}' creada")
        sheets_put(gtoken, f"'{ACCIONES_SHEET}'!A1:E1", [ACCIONES_HEADER])
    else:
        print(f"   ✓ Hoja '{ACCIONES_SHEET}' ya existe")

def read_existing(gtoken):
    """Lee filas existentes. Devuelve lista de dicts y set de keys (centro|periodo)."""
    try:
        data = sheets_get(gtoken, f"'{ACCIONES_SHEET}'!A1:E10000")
        rows = data.get('values', [])
        if len(rows) < 2:
            return [], set()
        header  = rows[0]
        records = []
        keys    = set()
        for i, row in enumerate(rows[1:], start=2):
            padded = row + [''] * (len(header) - len(row))
            rec    = dict(zip(header, padded))
            rec['_row'] = i
            records.append(rec)
            keys.add(f"{rec.get('centro','')}|{rec.get('periodo','')}")
        return records, keys
    except Exception as e:
        print(f"   ⚠ Error leyendo hoja: {e}")
        return [], set()

def upsert_row(gtoken, records, centro, periodo, bienvenidas, visitas, fecha):
    """Actualiza fila existente o agrega nueva."""
    key = f"{centro}|{periodo}"
    existing = next((r for r in records if
                     r.get('centro') == centro and r.get('periodo') == periodo), None)
    values = [[centro, periodo, bienvenidas, visitas, fecha]]
    if existing:
        row_num = existing['_row']
        sheets_put(gtoken, f"'{ACCIONES_SHEET}'!A{row_num}:E{row_num}", values)
        print(f"   ↺ Actualizado: {centro} | {periodo} | bienvenidas={bienvenidas} visitas={visitas}")
    else:
        sheets_append(gtoken, f"'{ACCIONES_SHEET}'!A1", values)
        print(f"   + Nuevo: {centro} | {periodo} | bienvenidas={bienvenidas} visitas={visitas}")

# ── MAIN ──────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("RED BASA · Sincronización Todoist")
    print(f"Fecha: {datetime.date.today()}")
    print("=" * 60)

    if not RESULTS_SHEET_ID:
        print("ERROR: RESULTS_SHEET_ID no configurado")
        return
    if not GOOGLE_SA_JSON:
        print("ERROR: GOOGLE_SERVICE_ACCOUNT no configurado")
        return

    # Token Google Sheets
    print("\n1. Autenticando con Google Sheets...")
    gtoken = get_token()
    print("   ✓ Token obtenido")

    # Asegurar hoja
    print("\n2. Verificando hoja 'Acciones Todoist'...")
    ensure_sheet(gtoken)

    # Leer existentes
    records, existing_keys = read_existing(gtoken)

    # Períodos
    periods = get_periods()
    today   = datetime.date.today().isoformat()

    # Procesar cada centro
    print(f"\n3. Procesando {len(CENTROS_TODOIST)} centro(s)...")
    for centro, cfg in CENTROS_TODOIST.items():
        token_env = cfg['token_env']
        token     = os.environ.get(token_env, '')
        if not token:
            print(f"\n   ⚠ {centro}: token {token_env} no encontrado, saltando")
            continue

        print(f"\n   [{centro}]")

        # Semana
        since_w, until_w = periods['week']
        items_w = get_completed_tasks(token, since_w.isoformat(), until_w.isoformat())
        bienvenidas_w = count_tasks(items_w, 'Bienvenida al paciente')
        visitas_w     = count_tasks(items_w, 'Visita Diaria')
        upsert_row(gtoken, records, centro, 'week', bienvenidas_w, visitas_w, today)

        # Mes
        since_m, until_m = periods['month']
        items_m = get_completed_tasks(token, since_m.isoformat(), until_m.isoformat())
        bienvenidas_m = count_tasks(items_m, 'Bienvenida al paciente')
        visitas_m     = count_tasks(items_m, 'Visita Diaria')
        upsert_row(gtoken, records, centro, 'month', bienvenidas_m, visitas_m, today)

        # Año (12 llamadas mensuales)
        print(f"   Calculando año (12 meses)...")
        counts_y      = fetch_year_counts(token)
        bienvenidas_y = counts_y['Bienvenida al paciente']
        visitas_y     = counts_y['Visita Diaria']
        upsert_row(gtoken, records, centro, 'year', bienvenidas_y, visitas_y, today)

    print("\n" + "=" * 60)
    print("✓ Sincronización Todoist completada")

if __name__ == '__main__':
    main()
