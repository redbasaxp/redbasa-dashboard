#!/usr/bin/env python3
"""
Red Basa · Test diagnóstico Todoist San José
Correr manualmente o via GitHub Actions para verificar acceso y datos disponibles.
Requiere: TODOIST_SSJ env variable
"""
import os, json, urllib.request, urllib.parse, datetime

TOKEN = os.environ.get('TODOIST_SSJ', '')
if not TOKEN:
    print("ERROR: TODOIST_SSJ no configurado")
    exit(1)

def api_get(url):
    req = urllib.request.Request(url)
    req.add_header('Authorization', f'Bearer {TOKEN}')
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

def api_post(url, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method='POST')
    req.add_header('Authorization', f'Bearer {TOKEN}')
    req.add_header('Content-Type', 'application/x-www-form-urlencoded')
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

today     = datetime.date.today()
week_ago  = today - datetime.timedelta(days=7)
month_ago = today - datetime.timedelta(days=30)
year_ago  = today - datetime.timedelta(days=365)

print("=" * 60)
print("RED BASA · Diagnóstico Todoist San José")
print(f"Fecha: {today}")
print("=" * 60)

# 1. Proyectos activos
print("\n1. PROYECTOS ACTIVOS (SSJ):")
try:
    projects = api_get('https://api.todoist.com/rest/v2/projects')
    ssj = [p for p in projects if p['name'].startswith('SSJ')]
    print(f"   Total proyectos activos SSJ: {len(ssj)}")
    for p in ssj[:5]:
        print(f"   - {p['name'][:70]}")
    if len(ssj) > 5:
        print(f"   ... y {len(ssj)-5} más")
except Exception as e:
    print(f"   ERROR: {e}")

# 2. Todos los proyectos incluyendo archivados
print("\n2. PROYECTOS ARCHIVADOS (SSJ):")
try:
    sync = api_post('https://api.todoist.com/sync/v9/sync',
                    {'sync_token': '*', 'resource_types': '["projects"]'})
    all_projects = sync.get('projects', [])
    ssj_all      = [p for p in all_projects if p['name'].startswith('SSJ')]
    ssj_archived = [p for p in ssj_all if p.get('is_archived', False)]
    ssj_active   = [p for p in ssj_all if not p.get('is_archived', False)]
    print(f"   Activos: {len(ssj_active)} · Archivados: {len(ssj_archived)} · Total: {len(ssj_all)}")
except Exception as e:
    print(f"   ERROR: {e}")

# 3. Tareas completadas por período
print("\n3. TAREAS COMPLETADAS:")
TAREAS = ['Bienvenida al paciente', 'Visita Diaria', 'Realización de la encuesta']
PERIODOS = [
    ('Última semana',  week_ago.isoformat(),  today.isoformat()),
    ('Último mes',     month_ago.isoformat(), today.isoformat()),
    ('Último año',     year_ago.isoformat(),  today.isoformat()),
]

for label, since, until in PERIODOS:
    print(f"\n   [{label}] {since} → {until}")
    try:
        url = (f"https://api.todoist.com/sync/v9/items/completed/get_all"
               f"?since={since}T00:00:00Z&until={until}T23:59:59Z&limit=200")
        data = api_get(url)
        items = data.get('items', [])
        total = len(items)
        print(f"   Total tareas completadas: {total}")

        # Contar por tipo
        conteos = {t: 0 for t in TAREAS}
        otros   = 0
        for item in items:
            nombre = item.get('content', '').strip()
            if nombre in conteos:
                conteos[nombre] += 1
            # Contar proyectos únicos (pacientes)
        pacientes = len(set(item.get('project_id') for item in items))
        print(f"   Pacientes únicos involucrados: {pacientes}")
        for tarea, count in conteos.items():
            print(f"   - {tarea}: {count}")

    except Exception as e:
        print(f"   ERROR: {e}")

# 4. Muestra de datos reales
print("\n4. ÚLTIMAS 5 TAREAS COMPLETADAS:")
try:
    url = (f"https://api.todoist.com/sync/v9/items/completed/get_all"
           f"?since={month_ago.isoformat()}T00:00:00Z"
           f"&until={today.isoformat()}T23:59:59Z&limit=5")
    data  = api_get(url)
    items = data.get('items', [])
    for item in items:
        print(f"   {item.get('completed_at','')[:10]} | {item.get('content','')} | "
              f"proyecto:{item.get('project_id','')[:8]}")
except Exception as e:
    print(f"   ERROR: {e}")

print("\n" + "=" * 60)
print("Diagnóstico completado")
