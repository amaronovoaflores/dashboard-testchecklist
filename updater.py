#!/usr/bin/env python3
"""
updater.py - Dashboard multi-cliente (dash.net.pe)
Lee config.json del repo y, para cada vehiculo, descarga datos de Hunter GPS
y mantenimientos (Drive). Genera datos_hunter_{PLACA}.json y datos_mantos_{PLACA}.json.
Corre automaticamente cada noche via GitHub Actions.

GPS: un solo usuario/clave por CLIENTE (secrets HUNTER_USER / HUNTER_PASS del repo),
aplica a todas las placas listadas en config.json -> vehiculos.
Mantenimientos: un file_id_mantos de Drive por VEHICULO (columna en config.json).
"""

import json, urllib.request, urllib.error, ssl, math, sys, os, io, shutil, re
from datetime import datetime, timedelta

HUNTER_LOGIN   = 'http://pxapi.24hm.net/apiGeo/login'
HUNTER_REPORTE = 'http://pxapi.24hm.net/apiGeo/reporteHistoricoPBi'
CONFIG_FILE    = 'config.json'

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

# -- HELPERS --------------------------------------------------------------
def http_request(method, url, payload=None, headers={}):
    data = json.dumps(payload).encode('utf-8') if payload else None
    urls = [url.replace('http://', 'https://'), url] if url.startswith('http://') else [url]
    last_error = None
    for target_url in urls:
        req = urllib.request.Request(target_url, data=data, headers={
            'Content-Type': 'application/json', **headers
        }, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 307, 308):
                new_url = e.headers.get('Location', '')
                print(f"  Redirect {e.code} -> {new_url}")
                if new_url:
                    req2 = urllib.request.Request(new_url, data=data, headers={
                        'Content-Type': 'application/json', **headers
                    }, method=method)
                    try:
                        with urllib.request.urlopen(req2, timeout=30, context=ctx) as r2:
                            return r2.read()
                    except Exception as e2:
                        last_error = e2; continue
            last_error = e; continue
        except Exception as e:
            last_error = e; continue
    raise last_error or Exception(f"No se pudo conectar a {url}")

def post_json(url, payload, headers={}):
    return json.loads(http_request('POST', url, payload, headers).decode('utf-8'))

def get_json(url, payload, headers={}):
    return json.loads(http_request('GET', url, payload, headers).decode('utf-8'))

def haversine(lat1, lon1, lat2, lon2):
    R = 6371
    dLat = math.radians(lat2 - lat1); dLon = math.radians(lon2 - lon1)
    a = math.sin(dLat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dLon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

# -- HUNTER GPS -------------------------------------------------------------
def login(usuario, contrasena):
    print(f"  Login Hunter usuario={usuario}...")
    resp = post_json(HUNTER_LOGIN, {"usuario": usuario, "contrasena": contrasena})
    print(f"  Login status: {resp.get('status')} auth: {resp.get('auth')}")
    token = resp.get('token', '')
    if not token:
        raise Exception(f"Login fallido: {resp}")
    return token

def descargar_dia(token, usuario, placa, fecha_str):
    try:
        resp = get_json(
            HUNTER_REPORTE,
            {"usuario": usuario, "placa": placa, "fecha": fecha_str},
            {"x-access-token": token}
        )
        regs = resp.get('registros', [])
        if regs:
            print(f"    OK {fecha_str} -> {len(regs)} registros")
            return regs
        status = resp.get('status', '')
        msg = resp.get('message', resp.get('msg', str(resp)[:80]))
        print(f"    [{fecha_str}]: status='{status}' msg='{msg}'")
    except Exception as e:
        print(f"    [{fecha_str}] ERROR: {e}")
    return []

def procesar_dia(fecha_str, registros):
    if not registros: return None
    campos = list(registros[0].keys())
    tiene_km = 'kilometraje' in campos
    km_dia = odo_ini = odo_fin = 0
    fuente_km = 'haversine'
    lats_todas = []; lons_todas = []
    for reg in registros:
        try:
            lat = float(reg.get('latitud', 0) or 0); lon = float(reg.get('longitud', 0) or 0)
            if lat and lon: lats_todas.append(lat); lons_todas.append(lon)
        except: pass
    if tiene_km:
        odos = [float(r['kilometraje']) for r in registros if r.get('kilometraje') and float(r.get('kilometraje', 0)) > 0]
        if odos:
            odo_ini = min(odos); odo_fin = max(odos)
            km_dia = round(odo_fin - odo_ini, 1)
            fuente_km = 'hunter_kilometraje'
    else:
        for i in range(1, len(lats_todas)):
            km_dia += haversine(lats_todas[i - 1], lons_todas[i - 1], lats_todas[i], lons_todas[i])
        km_dia = round(km_dia, 1)
    # Ultima posicion GPS conocida ese dia (el ultimo registro con lat/lon validos)
    ultima_lat = lats_todas[-1] if lats_todas else None
    ultima_lon = lons_todas[-1] if lons_todas else None
    return {"fecha": fecha_str, "km": km_dia, "odo_ini": odo_ini, "odo_fin": odo_fin,
            "registros": len(registros), "campos": campos, "fuente_km": fuente_km,
            "tiene_km": tiene_km, "tiene_odo": tiene_km,
            "ultima_lat": ultima_lat, "ultima_lon": ultima_lon}

def actualizar_hunter(usuario, contrasena, token, placa, output_file):
    print(f"\n=== HUNTER GPS: {placa} -> {output_file} ===")
    try:
        with open(output_file, 'r', encoding='utf-8') as f: datos = json.load(f)
    except:
        datos = {"placa": placa, "ultima_actualizacion": "", "dias": {}, "campos_disponibles": [], "fuente_km": ""}

    hoy = datetime.now()
    fechas = [(hoy - timedelta(days=i)).strftime('%Y-%m-%d') for i in range(35, -1, -1)]
    print(f"  Descargando {len(fechas)} dias ({fechas[0]} -> {fechas[-1]})...")

    campos_detectados = []; fuente_detectada = ''; nuevos = 0
    for fecha_str in fechas:
        try:
            regs = descargar_dia(token, usuario, placa, fecha_str)
            if regs:
                resumen = procesar_dia(fecha_str, regs)
                if resumen:
                    datos['dias'][fecha_str] = resumen
                    nuevos += 1
                    if not campos_detectados:
                        campos_detectados = resumen['campos']
                        fuente_detectada = resumen['fuente_km']
                    print(f"  {fecha_str}: {resumen['km']} kms ({resumen['registros']} regs)")
        except Exception as e:
            print(f"  {fecha_str}: ERROR {e}")

    datos.update({'ultima_actualizacion': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                   'placa': placa, 'campos_disponibles': campos_detectados,
                   'fuente_km': fuente_detectada, 'total_dias': len(datos['dias'])})
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)
    print(f"  OK {output_file}: {nuevos} dias nuevos - total {len(datos['dias'])} dias")
    return datos

# -- GPS: DISPATCHER MULTI-PROVEEDOR ------------------------------------------
# config.json -> gps.empresa decide que conector usar. Hoy solo Hunter esta
# implementado; el panel admin ya permite elegir Comsatel/Tracklink/Otro pero
# todavia no tienen conector propio - se agregan aca sin tocar el resto.
def preparar_sesion_gps(empresa, usuario, contrasena):
    empresa = (empresa or 'hunter').strip().lower()
    if empresa == 'hunter':
        try:
            return {'empresa': 'hunter', 'token': login(usuario, contrasena)}
        except Exception as e:
            print(f"ERROR login Hunter GPS: {e}")
            return None
    print(f"Conector GPS '{empresa}' no implementado todavia - se omite GPS para todo el cliente")
    return None

# -- POSICION ACTUAL + GEOCODING INVERSO (para el mapa) -----------------------
NOMINATIM_URL = 'https://nominatim.openstreetmap.org/reverse'
NOMINATIM_USER_AGENT = 'FlotasDash/1.0 (dash.net.pe)'

def geocodificar_inverso(lat, lon):
    """Devuelve (departamento, provincia) via Nominatim. Respeta 1 req/seg."""
    try:
        url = f"{NOMINATIM_URL}?format=json&lat={lat}&lon={lon}&zoom=10&addressdetails=1"
        req = urllib.request.Request(url, headers={'User-Agent': NOMINATIM_USER_AGENT})
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        addr = data.get('address', {})
        departamento = addr.get('state', '')
        provincia = addr.get('county') or addr.get('state_district') or addr.get('city') or ''
        return departamento, provincia
    except Exception as e:
        print(f"    Geocoding inverso fallo: {e}")
        return '', ''

def generar_posicion(datos_hunter, placa, output_file):
    """Toma el dia mas reciente con lat/lon valida de datos_hunter y escribe
    datos_posicion_{PLACA}.json con la ubicacion + departamento/provincia."""
    dias = datos_hunter.get('dias', {})
    fechas_con_posicion = [f for f in sorted(dias.keys()) if dias[f].get('ultima_lat') and dias[f].get('ultima_lon')]
    if not fechas_con_posicion:
        print(f"  Sin coordenadas GPS validas para {placa} - no se genera posicion")
        return
    ultima_fecha = fechas_con_posicion[-1]
    dia = dias[ultima_fecha]
    lat, lon = dia['ultima_lat'], dia['ultima_lon']

    # Reutiliza el departamento/provincia si ya se geocodifico este mismo punto antes
    departamento = provincia = ''
    try:
        with open(output_file, 'r', encoding='utf-8') as f:
            anterior = json.load(f)
        if anterior.get('lat') == lat and anterior.get('lon') == lon:
            departamento, provincia = anterior.get('departamento', ''), anterior.get('provincia', '')
    except Exception:
        pass
    if not departamento:
        import time; time.sleep(1)  # Nominatim: maximo 1 req/seg
        departamento, provincia = geocodificar_inverso(lat, lon)

    resultado = {
        'placa': placa, 'lat': lat, 'lon': lon,
        'fecha_posicion': ultima_fecha,
        'departamento': departamento, 'provincia': provincia,
        'ultima_actualizacion': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(resultado, f, ensure_ascii=False, indent=2)
    print(f"  OK {output_file}: {lat},{lon} ({departamento}, {provincia}) al {ultima_fecha}")

# -- MANTENIMIENTOS DESDE DRIVE ---------------------------------------------
def actualizar_mantos(gkey, file_id_mantos, output_file):
    print(f"\n=== MANTENIMIENTOS: {file_id_mantos} -> {output_file} ===")
    url = f'https://www.googleapis.com/drive/v3/files/{file_id_mantos}?alt=media&key={gkey}'
    try:
        req = urllib.request.Request(url, headers={'Cache-Control': 'no-cache'})
        with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
            data = r.read()
        print(f"  Descargado: {len(data)} bytes")

        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))

        mantos = []; llantas = []; tope_manto = 50000; tope_llanta = 60000; seccion = ''

        for row in rows:
            if not row or not any(c is not None for c in row): continue
            col0 = str(row[0]).lower().strip() if row[0] else ''
            col1 = str(row[1]).lower().strip() if row[1] else ''

            if 'mantenimiento' in col0 or 'mantenimiento' in col1: seccion = 'manto'; continue
            if 'llanta' in col0 or 'llanta' in col1: seccion = 'llanta'; continue
            if 'fecha' in col0: continue

            fecha = str(row[0]).strip() if row[0] else ''

            def to_float(v):
                if v is None: return 0.0
                try: return float(str(v).replace(',', '').strip())
                except: return 0.0

            col1v = to_float(row[1]); col2v = to_float(row[2])
            col3v = to_float(row[3]); col4v = to_float(row[4])
            detalle = str(row[5]).strip() if row[5] else ''

            if seccion == 'manto':
                if not fecha and col1v > 0: tope_manto = int(col1v); continue
                if not fecha: continue
                if col2v > 0 or col3v > 0 or col4v > 0:
                    mantos.append({'f': fecha, 'n': str(row[1]).strip() if row[1] else '', 'o': col2v, 'c': col3v, 'a': col4v})
            elif seccion == 'llanta':
                if not fecha and col1v > 0: tope_llanta = int(col1v); continue
                if not fecha: continue
                if col2v > 0 or col3v > 0:
                    llantas.append({'f': fecha, 'm': detalle or 'Llantas', 'o': col2v, 'c': col3v})

        resultado = {'mantos': mantos, 'llantas': llantas,
                     'tope_manto': tope_manto, 'tope_llanta': tope_llanta,
                     'ultima_actualizacion': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(resultado, f, ensure_ascii=False, indent=2)

        print(f"  OK {output_file}: {len(mantos)} mantos, {len(llantas)} llantas")
        print(f"  Tope manto: {tope_manto} | Tope llanta: {tope_llanta}")

    except Exception as e:
        print(f"  Error: {e}")
        import traceback; traceback.print_exc()

# -- SOAT / REVISION TECNICA (API JSON.pe, misma cuenta que TOMBO) ------------
# Se consulta como maximo 1 vez por semana por placa (throttle abajo) para
# cuidar el pool de creditos compartido con TOMBO (100/mes en el plan gratuito).
JSONPE_BASE_URL = 'https://api.json.pe/api'
JSONPE_THROTTLE_DIAS = 7

def normalizar_placa(p):
    return re.sub(r'[^A-Z0-9]', '', (p or '').upper())

def fecha_a_iso(ddmmyyyy):
    m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{4})$', (ddmmyyyy or '').strip())
    if not m: return ''
    d, mo, y = m.groups()
    return f"{y}-{mo.zfill(2)}-{d.zfill(2)}"

def jsonpe_consultar(token, endpoint, payload):
    req = urllib.request.Request(
        f"{JSONPE_BASE_URL}/{endpoint}",
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {token}',
                 'User-Agent': 'FlotasDash/1.0'},
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            cuerpo = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode('utf-8')).get('message')
        except Exception:
            msg = None
        if msg: return False, None, msg
        if e.code == 401: return False, None, 'Token de JSON.pe invalido o vencido'
        if e.code == 429: return False, None, 'Creditos de JSON.pe agotados o limite de velocidad'
        return False, None, f'Error HTTP {e.code} de JSON.pe'
    except Exception as e:
        return False, None, f'No se pudo contactar a JSON.pe: {e}'
    if not cuerpo.get('success', True) and not cuerpo.get('data'):
        return False, None, cuerpo.get('message', 'Respuesta sin datos de JSON.pe')
    return True, cuerpo.get('data'), ''

def consultar_soat(token, placa):
    ok, data, error = jsonpe_consultar(token, 'soat', {'placa': placa})
    if not ok:
        return {'ok': False, 'error': error}
    fecha_fin = data.get('fecha_fin', '')
    return {
        'ok': True,
        'aseguradora': data.get('nombre_compania'),
        'estado': data.get('estado'),
        'vigente_desde': data.get('fecha_inicio'),
        'vigente_hasta': fecha_fin,
        'numero_poliza': data.get('numero_poliza'),
        'fecha_vencimiento': fecha_a_iso(fecha_fin),
    }

def consultar_revision_tecnica(token, placa):
    ok, data, error = jsonpe_consultar(token, 'revision-tecnica', {'placa': placa})
    if not ok:
        return {'ok': False, 'error': error}
    registros = data if isinstance(data, list) else [data]
    if not registros:
        return {'ok': False, 'error': 'La API no devolvio inspecciones'}
    ultimo = next((r for r in registros if r.get('orden') == 'ULTIMO'), registros[0])
    vigente_hasta = ultimo.get('vigente_hasta', '')
    return {
        'ok': True,
        'estado': ultimo.get('estado'),
        'resultado_inspeccion': ultimo.get('resultado_inspeccion'),
        'vigente_desde': ultimo.get('vigente_desde'),
        'vigente_hasta': vigente_hasta,
        'certificado': ultimo.get('numero_certificado'),
        'planta': ultimo.get('empresa_certificadora'),
        'fecha_vencimiento': fecha_a_iso(vigente_hasta),
    }

ANIOS_EXONERACION_PARTICULAR = 3  # Reglamento MTC: autos particulares, primeros 3 anios exonerados

def revision_exonerada(placa, anio_vehiculo):
    """Si el vehiculo es reciente, ni siquiera consulta la API: ahorra un credito."""
    try:
        anio = int(anio_vehiculo)
    except (TypeError, ValueError):
        return None
    primer_anio = anio + ANIOS_EXONERACION_PARTICULAR
    if datetime.now().year >= primer_anio:
        return None  # ya le corresponde revision, seguir con la consulta normal
    return {
        'ok': True,
        'exonerado': True,
        'anio_vehiculo': anio,
        'primera_revision_estimada': primer_anio,
        'mensaje': (f'Vehiculo {anio}: exonerado de revision tecnica hasta {primer_anio} '
                    f'(reglamento MTC, primeros {ANIOS_EXONERACION_PARTICULAR} anios)'),
    }

def actualizar_soat_revision(token, placa, anio_vehiculo, output_file):
    placa = normalizar_placa(placa)
    # Throttle: no volver a gastar creditos si ya se consulto hace menos de
    # JSONPE_THROTTLE_DIAS dias (SOAT/revision tecnica no cambian a diario).
    try:
        with open(output_file, 'r', encoding='utf-8') as f:
            existente = json.load(f)
        ultima = existente.get('ultima_actualizacion', '')
        if ultima:
            edad = datetime.now() - datetime.strptime(ultima, '%Y-%m-%d %H:%M:%S')
            if edad.days < JSONPE_THROTTLE_DIAS:
                print(f"\n=== SOAT / REVISION TECNICA: {placa} ===")
                print(f"  Actualizado hace {edad.days} dia(s) - se omite (throttle {JSONPE_THROTTLE_DIAS} dias)")
                return
    except Exception:
        pass

    print(f"\n=== SOAT / REVISION TECNICA: {placa} ===")
    soat = consultar_soat(token, placa)

    exonerada = revision_exonerada(placa, anio_vehiculo)
    if exonerada:
        revision = exonerada
        print(f"  Revision tecnica: EXONERADA hasta {exonerada['primera_revision_estimada']} (no se consulto la API)")
    else:
        revision = consultar_revision_tecnica(token, placa)
        print(f"  Revision tecnica: {'OK' if revision.get('ok') else 'ERROR - ' + str(revision.get('error'))}")

    resultado = {
        'placa': placa,
        'soat': soat,
        'revision_tecnica': revision,
        'ultima_actualizacion': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(resultado, f, ensure_ascii=False, indent=2)
    print(f"  SOAT: {'OK' if soat.get('ok') else 'ERROR - ' + str(soat.get('error'))}")

# -- MAIN ---------------------------------------------------------------------
if __name__ == '__main__':
    usuario    = os.environ.get('HUNTER_USER', '')
    contrasena = os.environ.get('HUNTER_PASS', '')
    gkey       = os.environ.get('GAPI_KEY', '')

    if not os.path.exists(CONFIG_FILE):
        print(f"ERROR: no se encontro {CONFIG_FILE} en el repo"); sys.exit(1)
    with open(CONFIG_FILE, encoding='utf-8') as f:
        cfg = json.load(f)

    jsonpe_token = os.environ.get('JSONPE_TOKEN', '')

    vehiculos = [v for v in cfg.get('vehiculos', []) if v.get('placa', '').strip()]
    if not vehiculos:
        print("ERROR: config.json no tiene vehiculos con placa"); sys.exit(1)

    print(f"Cliente: {cfg.get('empresa', {}).get('nombre', '?')} - {len(vehiculos)} vehiculo(s)")

    gps_empresa = (cfg.get('gps', {}) or {}).get('empresa', 'hunter')
    sesion_gps = None
    if usuario and contrasena:
        sesion_gps = preparar_sesion_gps(gps_empresa, usuario, contrasena)
    else:
        print("Faltan HUNTER_USER / HUNTER_PASS (secrets del repo) - saltando GPS")

    if not jsonpe_token:
        print("Sin JSONPE_TOKEN (secret del repo) - saltando SOAT/revision tecnica")

    for v in vehiculos:
        placa = v['placa'].strip()
        slug = placa.replace('-', '')
        print(f"\n{'=' * 60}\nVEHICULO: {placa}\n{'=' * 60}")

        if sesion_gps and sesion_gps['empresa'] == 'hunter':
            datos_gps = actualizar_hunter(usuario, contrasena, sesion_gps['token'], placa, f'datos_hunter_{slug}.json')
            if datos_gps:
                generar_posicion(datos_gps, placa, f'datos_posicion_{slug}.json')

        file_id_mantos = (v.get('file_id_mantos') or '').strip()
        if file_id_mantos and gkey:
            actualizar_mantos(gkey, file_id_mantos, f'datos_mantos_{slug}.json')
        elif file_id_mantos and not gkey:
            print("  Sin GAPI_KEY (secret del repo) - saltando mantenimientos")

        if jsonpe_token:
            actualizar_soat_revision(jsonpe_token, placa, v.get('anio_vehiculo'), f'datos_soat_{slug}.json')

    # Compatibilidad: si el cliente tiene un solo vehiculo, generar tambien
    # los nombres genericos datos_hunter.json / datos_mantos.json
    if len(vehiculos) == 1:
        slug = vehiculos[0]['placa'].strip().replace('-', '')
        for prefix in ('datos_hunter', 'datos_mantos', 'datos_posicion'):
            src = f'{prefix}_{slug}.json'
            if os.path.exists(src):
                shutil.copyfile(src, f'{prefix}.json')
                print(f"  Copia generica: {prefix}.json")

    print("\nListo.")
