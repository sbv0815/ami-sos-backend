"""
🆘 AMI SOS - Backend de Emergencias v2.0
FastAPI + PostgreSQL (asyncpg) para Render.com

3 NIVELES DE ALERTA:
  Tipo 1: Emergencia leve → Solo cuidadores registrados
  Tipo 2: Emergencia grave → Cuidadores + institucionales 1km
  Tipo 3: Emergencia crítica → Cuidadores + institucionales + RED COMUNITARIA 1km

Endpoints:
  POST /alerta                → Recibir alerta (app, manilla BLE o relay)
  POST /alerta/clasificar     → Clasificar emergencia con Claude IA
  POST /alerta/responder      → Cuidador/institucional/comunidad responde
  GET  /alerta/{id}           → Estado de una alerta
  GET  /alerta/{id}/respuestas → Quiénes respondieron
  POST /usuario/registrar     → Registrar usuario SOS
  POST /usuario/contactos     → Agregar contacto de confianza
  POST /token/registrar       → Registrar/actualizar token FCM
  POST /red/ubicacion         → Actualizar GPS del usuario (red comunitaria)
  POST /red/relay             → Relay BLE: otro dispositivo reenvía alerta
  GET  /red/cercanos          → Ver cuántos miembros de la red hay cerca
  GET  /health                → Health check

Autor: TSCAMP SAS
"""

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, date
from decimal import Decimal
import os
import json
import math
import httpx
import logging
import time
import asyncpg
import re

# ==================== APP ====================

app = FastAPI(
    title="🆘 Ami SOS API",
    description="Backend de emergencias — 3 niveles de alerta + red comunitaria",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger("amisos")

# ==================== POSTGRESQL ====================

async def get_pool():
    if not hasattr(app.state, 'pool') or app.state.pool is None:
        database_url = os.getenv('DATABASE_URL')
        if database_url:
            app.state.pool = await asyncpg.create_pool(database_url, min_size=2, max_size=10)
        else:
            app.state.pool = await asyncpg.create_pool(
                host=os.getenv('DB_HOST', 'localhost'),
                port=int(os.getenv('DB_PORT', 5432)),
                user=os.getenv('DB_USER'),
                password=os.getenv('DB_PASSWORD'),
                database=os.getenv('DB_NAME'),
                min_size=2, max_size=10,
            )
    return app.state.pool

@app.on_event("startup")
async def startup():
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        log.info("✅ Conectado a PostgreSQL")
    except Exception as e:
        log.error(f"❌ Error conectando a PostgreSQL: {e}")

@app.on_event("shutdown")
async def shutdown():
    if hasattr(app.state, 'pool') and app.state.pool:
        await app.state.pool.close()

# ==================== MODELOS ====================

class AlertaRequest(BaseModel):
    celular: str
    id_persona: Optional[int] = None
    nombre: Optional[str] = None
    nivel_emergencia: int = 2           # 1=leve, 2=grave, 3=crítica
    tipo_alerta: str = "emergencia"     # seguridad, salud, violencia, incendio, caida, otro
    nivel_alerta: str = "critica"       # leve, critica (legacy)
    mensaje: Optional[str] = None
    latitud: Optional[float] = None
    longitud: Optional[float] = None
    fuente_alerta: str = "app"          # app, manilla_ble, boton_esp32, voz, relay_ble
    receptor_destino: str = "cuidador"
    audio_base64: Optional[str] = None
    bateria_dispositivo: Optional[int] = None

class RespuestaAlerta(BaseModel):
    alerta_id: int
    celular: str
    id_persona: Optional[int] = None
    tipo_respondedor: str = "cuidador"  # cuidador, institucional, comunidad
    latitud: Optional[float] = None
    longitud: Optional[float] = None
    tiempo_estimado_min: Optional[int] = None
    accion: Optional[str] = None        # "voy_en_camino", "llame_123", "grabando_video", "vigilando"

class ClasificarRequest(BaseModel):
    texto: Optional[str] = None
    audio_base64: Optional[str] = None
    contexto_usuario: Optional[str] = None

class UsuarioRegistrar(BaseModel):
    nombre: str
    celular: str
    apellido: Optional[str] = None
    correo: Optional[str] = None
    fecha_nacimiento: Optional[str] = None
    genero: Optional[str] = "masculino"
    condiciones_salud: Optional[str] = None
    medicamentos: Optional[str] = None
    alergias: Optional[str] = None
    ciudad: Optional[str] = None
    password_hash: Optional[str] = None
    disponible_red: bool = True         # Participa en la red comunitaria

class ContactoConfianza(BaseModel):
    usuario_id: int
    nombre: str
    celular: str
    parentesco: Optional[str] = None

class TokenRegistrar(BaseModel):
    celular: str
    token: str
    id_persona: Optional[int] = None
    rol: str = "usuario"
    dispositivo_id: Optional[str] = None

class UbicacionRed(BaseModel):
    celular: str
    id_persona: Optional[int] = None
    latitud: float
    longitud: float
    disponible: bool = True             # Si está disponible para responder

class RelayBLE(BaseModel):
    mac_manilla: str                    # MAC del dispositivo BLE detectado
    celular_relay: str                  # Celular de quien detectó la señal
    latitud: float
    longitud: float
    tipo_alerta_ble: int = 3            # Tipo que transmite la manilla (1,2,3)
    rssi: Optional[int] = None          # Fuerza de señal BLE

class ReporteUsuario(BaseModel):
    celular_reporta: str                # Quien reporta
    celular_reportado: str              # A quien reporta
    motivo: str = "comportamiento"      # comportamiento, acoso, falsa_identidad, spam, otro
    descripcion: Optional[str] = None

# ==================== UTILIDADES ====================

def normalizar_celular(celular: str) -> tuple:
    celular = re.sub(r'\D', '', celular)
    sin_57 = celular[2:] if celular.startswith('57') and len(celular) > 10 else celular
    con_57 = f"57{sin_57}"
    return sin_57, con_57

def distancia_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))

def serializar(obj):
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: serializar(v) for k, v in obj.items()}
    return obj

def row_to_dict(record):
    if record is None:
        return None
    return {k: serializar(v) for k, v in dict(record).items()}

# ==================== FIREBASE ====================

async def obtener_access_token_firebase():
    import jwt as pyjwt
    client_email = os.getenv('FIREBASE_CLIENT_EMAIL')
    private_key = os.getenv('FIREBASE_PRIVATE_KEY', '').replace('\\n', '\n')
    if not client_email or not private_key:
        return None
    now = int(time.time())
    payload = {
        'iss': client_email,
        'scope': 'https://www.googleapis.com/auth/firebase.messaging',
        'aud': 'https://oauth2.googleapis.com/token',
        'iat': now, 'exp': now + 3600,
    }
    token = pyjwt.encode(payload, private_key, algorithm='RS256')
    async with httpx.AsyncClient() as client:
        resp = await client.post('https://oauth2.googleapis.com/token', data={
            'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
            'assertion': token,
        })
        return resp.json().get('access_token')

async def enviar_push(token_fcm: str, titulo: str, cuerpo: str, data: dict = None) -> dict:
    project_id = os.getenv('FIREBASE_PROJECT_ID')
    access_token = await obtener_access_token_firebase()
    if not access_token:
        return {'success': False, 'error': 'Sin access token Firebase'}
    mensaje = {
        'message': {
            'token': token_fcm,
            'notification': {'title': titulo, 'body': cuerpo},
            'data': {k: str(v) for k, v in (data or {}).items()},
            'android': {
                'priority': 'high',
                'notification': {'sound': 'default', 'channel_id': 'canal_alertas_sos', 'click_action': 'FLUTTER_NOTIFICATION_CLICK'}
            },
            'apns': {'payload': {'aps': {'sound': 'default', 'badge': 1, 'content-available': 1}}}
        }
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send",
            json=mensaje,
            headers={'Authorization': f'Bearer {access_token}', 'Content-Type': 'application/json'},
            timeout=15,
        )
        if resp.status_code == 200:
            return {'success': True}
        return {'success': False, 'error': f"HTTP {resp.status_code}: {resp.text}"}

# ==================== BUSCAR TOKEN FCM ====================

async def buscar_token(pool, cel_sin: str, cel_con: str, id_persona: int = None) -> dict:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT token FROM tokens_fcm WHERE celular IN ($1,$2) AND valido=TRUE ORDER BY fecha DESC LIMIT 1",
            cel_sin, cel_con)
        if row:
            return {'token': row['token'], 'fuente': 'tokens_fcm'}
        row = await conn.fetchrow(
            "SELECT fcm_token FROM usuarios_sos WHERE celular IN ($1,$2) AND fcm_token IS NOT NULL AND fcm_token != '' LIMIT 1",
            cel_sin, cel_con)
        if row:
            return {'token': row['fcm_token'], 'fuente': 'usuarios_sos'}
        if id_persona:
            row = await conn.fetchrow(
                "SELECT token FROM tokens_fcm WHERE id_persona=$1 AND valido=TRUE ORDER BY fecha DESC LIMIT 1",
                id_persona)
            if row:
                return {'token': row['token'], 'fuente': 'tokens_fcm_id'}
    return {'token': None, 'fuente': None}

# ==================== BUSCAR RED COMUNITARIA 1KM ====================

async def buscar_red_comunitaria(pool, lat: float, lon: float, excluir_celular: str) -> list:
    """Busca usuarios de Ami SOS disponibles en radio 1km. Excluye bloqueados."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT ur.id, ur.celular, ur.nombre, ur.latitud, ur.longitud
            FROM ubicaciones_red ur
            LEFT JOIN usuarios_sos us ON us.celular = ur.celular
            WHERE ur.disponible = TRUE
              AND ur.latitud IS NOT NULL
              AND ur.longitud IS NOT NULL
              AND ur.actualizado_at > NOW() - INTERVAL '30 minutes'
              AND ur.celular != $1
              AND (us.bloqueado IS NULL OR us.bloqueado = FALSE)
        """, excluir_celular)
    
    cercanos = []
    for r in rows:
        dist = distancia_km(lat, lon, float(r['latitud']), float(r['longitud']))
        if dist <= 1.0:
            cercanos.append({
                'id': r['id'],
                'celular': r['celular'],
                'nombre': r['nombre'] or 'Miembro red',
                'distancia_km': round(dist, 2),
                'tipo': 'comunidad',
                'id_persona': None,
            })
    
    cercanos.sort(key=lambda x: x['distancia_km'])
    return cercanos

# ==================================================================
# POST /alerta — ENDPOINT PRINCIPAL CON 3 NIVELES
# ==================================================================

@app.post("/alerta")
async def recibir_alerta(req: AlertaRequest, bg: BackgroundTasks):
    inicio = time.time()
    pool = await get_pool()
    cel_sin, cel_con = normalizar_celular(req.celular)
    
    # Determinar nivel de emergencia
    nivel = req.nivel_emergencia
    if nivel not in (1, 2, 3):
        nivel = 2
    
    etiquetas = {1: "🟡 LEVE", 2: "🟠 GRAVE", 3: "🔴 CRÍTICA"}
    log.info(f"🚨 ALERTA NIVEL {nivel} {etiquetas[nivel]}: tipo={req.tipo_alerta} fuente={req.fuente_alerta} cel={cel_con}")
    
    async with pool.acquire() as conn:
        # Nombre
        nombre = req.nombre
        if not nombre:
            row = await conn.fetchrow("SELECT nombre FROM usuarios_sos WHERE celular IN ($1,$2) LIMIT 1", cel_sin, cel_con)
            nombre = row['nombre'] if row else 'Usuario'
        
        # Mensaje según nivel
        hora = datetime.now().strftime('%H:%M')
        if req.mensaje:
            mensaje = req.mensaje
        elif nivel == 1:
            mensaje = f"🟡 {nombre} necesita ayuda — emergencia leve a las {hora}"
        elif nivel == 2:
            mensaje = f"🟠 EMERGENCIA: {nombre} necesita ayuda urgente — {req.tipo_alerta} a las {hora}"
        else:
            mensaje = f"🔴 EMERGENCIA CRÍTICA: {nombre} está en peligro — {req.tipo_alerta} a las {hora}"
        
        # Mapear tipos
        tipos_ok = ('salud','seguridad','violencia','incendio','caida','otro')
        tipo_db = req.tipo_alerta if req.tipo_alerta in tipos_ok else 'otro'
        nivel_alerta_db = 'leve' if nivel == 1 else 'critica'
        fuente_map = {'app':'manual', 'manilla_ble':'boton', 'boton_esp32':'boton', 'voz':'manual', 'relay_ble':'relay'}
        fuente_db = fuente_map.get(req.fuente_alerta, 'manual')
        
        # Guardar alerta
        alerta_id = await conn.fetchval("""
            INSERT INTO alertas_panico 
            (nombre, mensaje, fecha_hora, celular, atendida, id_persona, rol,
             tipo_alerta, latitud, longitud, nivel_alerta, receptor_destino, 
             fuente_alerta, bateria_dispositivo, nivel_emergencia)
            VALUES ($1,$2,NOW(),$3,'no',$4,'usuario',$5,$6,$7,$8,$9,$10,$11,$12)
            RETURNING id
        """, nombre, mensaje, cel_con, req.id_persona, tipo_db,
            req.latitud, req.longitud, nivel_alerta_db, req.receptor_destino,
            fuente_db, req.bateria_dispositivo, nivel)
        
        log.info(f"  💾 Alerta ID: {alerta_id}")
        
        # ===== NIVEL 1: SOLO CUIDADORES =====
        cuidadores = []
        cels_vistos = set()
        
        rows = await conn.fetch("""
            SELECT cc.celular, cc.nombre FROM contactos_confianza cc
            INNER JOIN usuarios_sos u ON u.id = cc.usuario_id
            WHERE u.celular IN ($1,$2) AND cc.disponible_emergencias=TRUE AND cc.activo=TRUE
        """, cel_sin, cel_con)
        for r in rows:
            if r['celular'] not in cels_vistos:
                cuidadores.append({'celular': r['celular'], 'nombre': r['nombre'], 'tipo': 'cuidador', 'id_persona': None})
                cels_vistos.add(r['celular'])
        
        rows = await conn.fetch(
            "SELECT celular_cuidador, id_persona_cuidador FROM cuidadores_autorizados WHERE celular_cuidado IN ($1,$2)",
            cel_sin, cel_con)
        for r in rows:
            if r['celular_cuidador'] not in cels_vistos:
                cuidadores.append({'celular': r['celular_cuidador'], 'nombre': 'Cuidador', 'tipo': 'cuidador', 'id_persona': r['id_persona_cuidador']})
                cels_vistos.add(r['celular_cuidador'])
        
        log.info(f"  👥 Cuidadores: {len(cuidadores)}")
        
        # ===== NIVEL 2+: INSTITUCIONALES 1KM =====
        institucionales = []
        if nivel >= 2 and req.latitud and req.longitud:
            rows = await conn.fetch("""
                SELECT id, nombre, entidad, celular, tipo, id_persona, latitud, longitud
                FROM cuidadores_institucionales WHERE activo=TRUE AND latitud IS NOT NULL AND longitud IS NOT NULL
            """)
            for r in rows:
                dist = distancia_km(req.latitud, req.longitud, float(r['latitud']), float(r['longitud']))
                if dist <= 1.0:
                    incluir = False
                    ti = (r['tipo'] or '').lower()
                    if req.tipo_alerta in ('emergencia','caida'):
                        incluir = True
                    elif req.tipo_alerta in ('seguridad','violencia'):
                        incluir = ti in ('policia','seguridad','')
                    elif req.tipo_alerta == 'salud':
                        incluir = ti in ('ambulancia','salud','')
                    elif req.tipo_alerta == 'incendio':
                        incluir = ti in ('bomberos','emergencia','')
                    else:
                        incluir = True
                    if incluir:
                        institucionales.append({
                            'celular': r['celular'], 'nombre': r['nombre'], 'entidad': r['entidad'],
                            'tipo': r['tipo'] or 'institucional', 'id_persona': r['id_persona'],
                            'distancia_km': round(dist, 2),
                        })
            institucionales.sort(key=lambda x: x['distancia_km'])
            log.info(f"  🏛️ Institucionales 1km: {len(institucionales)}")
        
        # ===== NIVEL 3: RED COMUNITARIA 1KM =====
        comunidad = []
        if nivel >= 3 and req.latitud and req.longitud:
            comunidad = await buscar_red_comunitaria(pool, req.latitud, req.longitud, cel_con)
            log.info(f"  🤝 Red comunitaria 1km: {len(comunidad)}")
    
    # Notificaciones en background
    bg.add_task(
        _enviar_notificaciones_v2, pool, alerta_id, nombre, mensaje,
        req.tipo_alerta, nivel, cel_con, cuidadores, institucionales, comunidad,
        req.latitud, req.longitud
    )
    
    ms = round((time.time() - inicio) * 1000)
    
    return {
        'success': True,
        'alerta_id': alerta_id,
        'nivel_emergencia': nivel,
        'mensaje': 'Alerta procesada',
        'notificados': {
            'cuidadores': len(cuidadores),
            'institucionales': len(institucionales),
            'red_comunitaria': len(comunidad),
            'total': len(cuidadores) + len(institucionales) + len(comunidad),
        },
        'institucionales_detalle': [
            {'nombre': i['nombre'], 'entidad': i.get('entidad',''), 'distancia_km': i['distancia_km']}
            for i in institucionales[:5]
        ],
        'comunidad_cercanos': len(comunidad),
        'tiempo_ms': ms,
    }

# ==================== ENVIAR NOTIFICACIONES V2 ====================

async def _enviar_notificaciones_v2(pool, alerta_id, nombre, mensaje, tipo_alerta, nivel,
                                     cel_usuario, cuidadores, institucionales, comunidad, lat, lon):
    notificados = 0
    
    data_push = {
        'alerta_id': str(alerta_id),
        'celular_usuario': cel_usuario,
        'tipo_alerta': tipo_alerta,
        'nivel_emergencia': str(nivel),
        'nombre_usuario': nombre,
        'click_action': 'FLUTTER_NOTIFICATION_CLICK',
    }
    if lat and lon:
        data_push['latitud'] = str(lat)
        data_push['longitud'] = str(lon)
        data_push['maps_url'] = f"https://maps.google.com/?q={lat},{lon}"
    
    # Construir lista unificada
    todos = []
    for c in cuidadores:
        todos.append({**c, 'rol_dest': 'cuidador'})
    for i in institucionales:
        todos.append({**i, 'rol_dest': 'institucional'})
    for m in comunidad:
        todos.append({**m, 'rol_dest': 'comunidad'})
    
    for dest in todos:
        cs, cc = normalizar_celular(dest['celular'])
        tk = await buscar_token(pool, cs, cc, dest.get('id_persona'))
        token = tk['token']
        
        # Título según rol
        if dest['rol_dest'] == 'cuidador':
            titulo = f"🚨 {'EMERGENCIA' if nivel >= 2 else 'Alerta'} de {nombre}"
        elif dest['rol_dest'] == 'institucional':
            titulo = f"🚨 Alerta {tipo_alerta.upper()} — Nivel {nivel}"
        else:
            # Red comunitaria
            dist_txt = f"{dest.get('distancia_km', '?')}km"
            titulo = f"🔴 EMERGENCIA cerca de ti ({dist_txt})"
            # Mensaje especial para la red
            mensaje_red = f"{nombre} necesita ayuda a {dist_txt}. Puedes: llamar 123, grabar video como evidencia, o acercarte si es seguro."
        
        ok = False
        if token:
            msg = mensaje_red if dest['rol_dest'] == 'comunidad' else mensaje
            result = await enviar_push(token, titulo, msg, data_push)
            ok = result['success']
            if ok:
                notificados += 1
                log.info(f"    ✅ [{dest['rol_dest']}] {dest['nombre']}")
            else:
                log.warning(f"    ❌ [{dest['rol_dest']}] {dest['nombre']}: {result.get('error','')}")
                if 'UNREGISTERED' in str(result.get('error','')) or 'INVALID' in str(result.get('error','')):
                    async with pool.acquire() as conn:
                        await conn.execute("UPDATE tokens_fcm SET valido=FALSE, motivo_invalidez='token_invalido', fecha_invalido=NOW() WHERE celular IN ($1,$2)", cs, cc)
        
        # Registrar envío
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO alertas_enviadas (alerta_id, celular_usuario, nombre_usuario,
                    celular_cuidador_institucional, nombre_cuidador_institucional,
                    token, mensaje, fecha, estado_envio, rol_destinatario, receptor_destino)
                VALUES ($1,$2,$3,$4,$5,$6,$7,NOW(),$8,$9,$10)
            """, alerta_id, cel_usuario, nombre, dest['celular'], dest['nombre'],
                token or '', mensaje, 'enviado' if ok else 'fallido',
                dest['rol_dest'], dest.get('entidad', dest['rol_dest']))
    
    log.info(f"  📊 Nivel {nivel}: {notificados}/{len(todos)} notificados")

# ==================== POST /alerta/responder (ACTUALIZADO) ====================

@app.post("/alerta/responder")
async def responder_alerta(req: RespuestaAlerta):
    pool = await get_pool()
    cs, cc = normalizar_celular(req.celular)
    
    async with pool.acquire() as conn:
        # Verificar que la alerta existe
        alerta = await conn.fetchrow("SELECT * FROM alertas_panico WHERE id=$1", req.alerta_id)
        if not alerta:
            raise HTTPException(404, "Alerta no encontrada")
        
        # Ya respondió?
        if req.id_persona:
            existe = await conn.fetchval(
                "SELECT id FROM respuestas_institucionales WHERE alerta_id=$1 AND celular IN ($2,$3)",
                req.alerta_id, cs, cc)
            if existe:
                raise HTTPException(409, "Ya respondió esta alerta")
        
        # Obtener nombre del respondedor
        nombre_resp = "Respondedor"
        entidad_resp = req.tipo_respondedor
        
        if req.tipo_respondedor == 'institucional':
            inst = await conn.fetchrow(
                "SELECT nombre, entidad FROM cuidadores_institucionales WHERE celular IN ($1,$2) LIMIT 1", cs, cc)
            if inst:
                nombre_resp = inst['nombre']
                entidad_resp = inst['entidad']
        else:
            usr = await conn.fetchrow("SELECT nombre FROM usuarios_sos WHERE celular IN ($1,$2) LIMIT 1", cs, cc)
            if usr:
                nombre_resp = usr['nombre']
            entidad_resp = 'Red comunitaria' if req.tipo_respondedor == 'comunidad' else 'Cuidador'
        
        # Registrar respuesta
        await conn.execute("""
            INSERT INTO respuestas_institucionales 
            (alerta_id, id_persona, celular, entidad, nombre, latitud, longitud, 
             tiempo_estimado_min, estado)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
        """, req.alerta_id, req.id_persona or 0, cc, entidad_resp, nombre_resp,
            req.latitud, req.longitud, req.tiempo_estimado_min,
            req.accion or 'voy_en_camino')
        
        log.info(f"✅ [{req.tipo_respondedor}] {nombre_resp} responde alerta {req.alerta_id} — acción: {req.accion or 'voy_en_camino'}")
        
        # Notificar al usuario que alguien responde
        u_sin, u_con = normalizar_celular(alerta['celular'])
        tk = await buscar_token(pool, u_sin, u_con)
        if tk['token']:
            acciones_txt = {
                'voy_en_camino': 'va en camino',
                'llame_123': 'llamó al 123',
                'grabando_video': 'está grabando evidencia',
                'vigilando': 'está vigilando la zona',
            }
            accion_txt = acciones_txt.get(req.accion, 'responde')
            t_msg = f" (~{req.tiempo_estimado_min} min)" if req.tiempo_estimado_min else ""
            await enviar_push(
                tk['token'], "🟢 Alguien responde",
                f"{nombre_resp} {accion_txt}{t_msg}",
                {'alerta_id': str(req.alerta_id), 'tipo': 'respuesta', 'respondedor': req.tipo_respondedor}
            )
    
    return {'success': True, 'nombre': nombre_resp, 'tipo': req.tipo_respondedor, 'accion': req.accion}

# ==================== RED COMUNITARIA ====================

@app.post("/red/ubicacion")
async def actualizar_ubicacion(req: UbicacionRed):
    """Actualiza GPS del usuario para la red comunitaria."""
    pool = await get_pool()
    cs, cc = normalizar_celular(req.celular)
    
    async with pool.acquire() as conn:
        # Obtener nombre
        usr = await conn.fetchrow("SELECT id, nombre FROM usuarios_sos WHERE celular IN ($1,$2) LIMIT 1", cs, cc)
        nombre = usr['nombre'] if usr else None
        
        # Upsert ubicación
        existe = await conn.fetchval("SELECT id FROM ubicaciones_red WHERE celular=$1", cc)
        if existe:
            await conn.execute("""
                UPDATE ubicaciones_red 
                SET latitud=$1, longitud=$2, disponible=$3, actualizado_at=NOW()
                WHERE celular=$4
            """, req.latitud, req.longitud, req.disponible, cc)
        else:
            await conn.execute("""
                INSERT INTO ubicaciones_red (celular, id_persona, nombre, latitud, longitud, disponible)
                VALUES ($1,$2,$3,$4,$5,$6)
            """, cc, req.id_persona, nombre, req.latitud, req.longitud, req.disponible)
    
    return {'success': True}

@app.get("/red/cercanos")
async def ver_cercanos(latitud: float, longitud: float):
    """Ver cuántos miembros de la red hay en 1km."""
    pool = await get_pool()
    
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT celular, latitud, longitud
            FROM ubicaciones_red
            WHERE disponible = TRUE
              AND actualizado_at > NOW() - INTERVAL '30 minutes'
        """)
    
    cercanos = 0
    for r in rows:
        dist = distancia_km(latitud, longitud, float(r['latitud']), float(r['longitud']))
        if dist <= 1.0:
            cercanos += 1
    
    return {'success': True, 'cercanos_1km': cercanos}

# ==================== RELAY BLE ====================

@app.post("/red/relay")
async def relay_ble(req: RelayBLE, bg: BackgroundTasks):
    """
    Otro dispositivo con Ami SOS detectó la manilla BLE de alguien 
    que perdió su celular. Reenvía la alerta.
    """
    pool = await get_pool()
    
    async with pool.acquire() as conn:
        # Buscar a quién pertenece la manilla
        disp = await conn.fetchrow(
            "SELECT usuario_id, nombre_dispositivo FROM dispositivos_ble WHERE mac_address=$1 AND activo=TRUE",
            req.mac_manilla)
        
        if not disp:
            raise HTTPException(404, "Dispositivo BLE no registrado")
        
        # Obtener datos del dueño
        usuario = await conn.fetchrow("SELECT nombre, celular FROM usuarios_sos WHERE id=$1", disp['usuario_id'])
        if not usuario:
            raise HTTPException(404, "Usuario del dispositivo no encontrado")
        
        # Verificar que no haya una alerta reciente del mismo dispositivo (evitar duplicados)
        reciente = await conn.fetchval("""
            SELECT id FROM alertas_panico 
            WHERE celular=$1 AND fuente_alerta='relay' AND fecha_hora > NOW() - INTERVAL '5 minutes'
        """, usuario['celular'])
        
        if reciente:
            return {'success': True, 'alerta_id': reciente, 'mensaje': 'Alerta ya reportada por relay'}
        
        log.info(f"📡 RELAY BLE: Manilla {req.mac_manilla} detectada por {req.celular_relay} — dueño: {usuario['nombre']}")
        
        # Crear alerta Nivel 3 automáticamente (si perdió el celular, es crítico)
        alerta_req = AlertaRequest(
            celular=usuario['celular'],
            nombre=usuario['nombre'],
            nivel_emergencia=3,
            tipo_alerta='seguridad',
            nivel_alerta='critica',
            mensaje=f"🔴 RELAY: {usuario['nombre']} puede estar en peligro. Señal BLE detectada por otro usuario a las {datetime.now().strftime('%H:%M')}",
            latitud=req.latitud,
            longitud=req.longitud,
            fuente_alerta='relay_ble',
        )
        
        return await recibir_alerta(alerta_req, bg)

# ==================== CLASIFICAR CON CLAUDE ====================

@app.post("/alerta/clasificar")
async def clasificar_emergencia(req: ClasificarRequest):
    api_key = os.getenv('ANTHROPIC_API_KEY')
    if not api_key:
        raise HTTPException(500, "API key Anthropic no configurada")
    if not req.texto:
        raise HTTPException(400, "Se requiere texto")
    
    prompt = f"""Clasifica esta emergencia reportada en Colombia.
CONTEXTO: {req.contexto_usuario or 'No disponible'}
REPORTE: "{req.texto}"

Responde SOLO en JSON:
{{"nivel_emergencia":1-3,"tipo_alerta":"seguridad|salud|violencia|incendio|caida|otro","descripcion_corta":"1 línea","acciones":["acción1","acción2"],"llamar_123":true/false,"llamar_155":true/false,"confianza":0.0-1.0}}

Niveles: 1=leve(cuidadores), 2=grave(+institucionales), 3=crítica(+red comunitaria)
Reglas: violencia→llamar_155=true+nivel 3, salud grave→llamar_123=true+nivel 2-3, robo armado/secuestro→nivel 3"""

    async with httpx.AsyncClient() as client:
        resp = await client.post("https://api.anthropic.com/v1/messages",
            json={'model':'claude-sonnet-4-20250514','max_tokens':500,'messages':[{'role':'user','content':prompt}]},
            headers={'Content-Type':'application/json','x-api-key':api_key,'anthropic-version':'2023-06-01'}, timeout=30)
        if resp.status_code != 200:
            raise HTTPException(502, f"Error Claude: {resp.status_code}")
        text = resp.json()['content'][0]['text']
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            return {'success': True, 'clasificacion': json.loads(match.group())}
        raise HTTPException(500, "Error parseando respuesta IA")

# ==================== REGISTRO ====================

@app.post("/usuario/registrar")
async def registrar_usuario(req: UsuarioRegistrar):
    pool = await get_pool()
    cs, cc = normalizar_celular(req.celular)
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT id FROM usuarios_sos WHERE celular IN ($1,$2)", cs, cc):
            raise HTTPException(409, "Usuario ya registrado")
        f_nac = None
        if req.fecha_nacimiento:
            try: f_nac = datetime.strptime(req.fecha_nacimiento, '%Y-%m-%d').date()
            except: pass
        uid = await conn.fetchval("""
            INSERT INTO usuarios_sos (nombre,apellido,celular,correo,fecha_nacimiento,genero,
                condiciones_salud,medicamentos,alergias,ciudad,password_hash,disponible_red)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) RETURNING id
        """, req.nombre, req.apellido, cc, req.correo, f_nac, req.genero,
            req.condiciones_salud, req.medicamentos, req.alergias, req.ciudad,
            req.password_hash, req.disponible_red)
    return {'success': True, 'id': uid}

@app.post("/usuario/contactos")
async def agregar_contacto(req: ContactoConfianza):
    pool = await get_pool()
    async with pool.acquire() as conn:
        cid = await conn.fetchval("INSERT INTO contactos_confianza (usuario_id,nombre,celular,parentesco) VALUES ($1,$2,$3,$4) RETURNING id",
            req.usuario_id, req.nombre, req.celular, req.parentesco)
    return {'success': True, 'id': cid}

@app.post("/token/registrar")
async def registrar_token(req: TokenRegistrar):
    pool = await get_pool()
    cs, cc = normalizar_celular(req.celular)
    async with pool.acquire() as conn:
        await conn.execute("UPDATE tokens_fcm SET valido=FALSE WHERE celular IN ($1,$2) AND valido=TRUE", cs, cc)
        await conn.execute("INSERT INTO tokens_fcm (id_persona,celular,token,valido,fecha,rol,dispositivo_id) VALUES ($1,$2,$3,TRUE,NOW(),$4,$5)",
            req.id_persona, cc, req.token, req.rol, req.dispositivo_id)
    return {'success': True}

# ==================== CONSULTAS ====================

@app.get("/alerta/{alerta_id}")
async def obtener_alerta(alerta_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM alertas_panico WHERE id=$1", alerta_id)
        if not row: raise HTTPException(404, "Alerta no encontrada")
        stats = await conn.fetchrow("SELECT COUNT(*) as total, COUNT(*) FILTER (WHERE estado_envio='enviado') as enviados FROM alertas_enviadas WHERE alerta_id=$1", alerta_id)
        resps = await conn.fetch("SELECT nombre,entidad,celular,fecha_respuesta,tiempo_estimado_min,estado FROM respuestas_institucionales WHERE alerta_id=$1 ORDER BY fecha_respuesta", alerta_id)
    return {
        'success': True,
        'alerta': row_to_dict(row),
        'notificaciones': {'total': stats['total'], 'enviadas': stats['enviados']},
        'respuestas': [row_to_dict(r) for r in resps],
    }

@app.get("/alerta/{alerta_id}/respuestas")
async def obtener_respuestas(alerta_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT nombre,entidad,celular,fecha_respuesta,tiempo_estimado_min,latitud,longitud,estado FROM respuestas_institucionales WHERE alerta_id=$1 ORDER BY fecha_respuesta", alerta_id)
    return {'success': True, 'respuestas': [row_to_dict(r) for r in rows], 'total': len(rows)}

# ==================== REPORTAR USUARIO ====================

@app.post("/usuario/reportar")
async def reportar_usuario(req: ReporteUsuario):
    """
    Reportar un usuario. Si acumula 3+ reportes, se bloquea automáticamente.
    """
    pool = await get_pool()
    cs_reporta, cc_reporta = normalizar_celular(req.celular_reporta)
    cs_reportado, cc_reportado = normalizar_celular(req.celular_reportado)
    
    async with pool.acquire() as conn:
        # Verificar que no se reporte a sí mismo
        if cc_reporta == cc_reportado:
            raise HTTPException(400, "Cannot report yourself")
        
        # Verificar que no haya reportado antes
        existe = await conn.fetchval(
            "SELECT id FROM reportes_usuario WHERE celular_reportado=$1 AND celular_reporta=$2",
            cc_reportado, cc_reporta)
        if existe:
            raise HTTPException(409, "Already reported this user")
        
        # Registrar reporte
        await conn.execute("""
            INSERT INTO reportes_usuario (celular_reportado, celular_reporta, motivo, descripcion)
            VALUES ($1, $2, $3, $4)
        """, cc_reportado, cc_reporta, req.motivo, req.descripcion)
        
        # Contar reportes totales
        total_reportes = await conn.fetchval(
            "SELECT COUNT(*) FROM reportes_usuario WHERE celular_reportado=$1",
            cc_reportado)
        
        log.info(f"⚠️ Reporte: {cc_reporta} → {cc_reportado} ({req.motivo}). Total: {total_reportes}")
        
        # Auto-bloqueo si 3+ reportes
        bloqueado = False
        if total_reportes >= 3:
            await conn.execute("""
                UPDATE usuarios_sos SET bloqueado=TRUE, motivo_bloqueo=$1, fecha_bloqueo=NOW()
                WHERE celular IN ($2, $3)
            """, f"auto_block_{total_reportes}_reports", cs_reportado, cc_reportado)
            
            # También desactivar de la red
            await conn.execute(
                "UPDATE ubicaciones_red SET disponible=FALSE WHERE celular=$1", cc_reportado)
            
            bloqueado = True
            log.warning(f"🚫 AUTO-BLOQUEO: {cc_reportado} con {total_reportes} reportes")
    
    return {
        'success': True,
        'total_reportes': total_reportes,
        'usuario_bloqueado': bloqueado,
        'mensaje': 'User blocked automatically' if bloqueado else 'Report registered',
    }

@app.get("/usuario/{celular}/reportes")
async def ver_reportes(celular: str):
    """Ver cuántos reportes tiene un usuario (solo conteo, no detalle)."""
    pool = await get_pool()
    cs, cc = normalizar_celular(celular)
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM reportes_usuario WHERE celular_reportado IN ($1,$2)", cs, cc)
        bloqueado = await conn.fetchval(
            "SELECT bloqueado FROM usuarios_sos WHERE celular IN ($1,$2)", cs, cc)
    return {'success': True, 'total_reportes': total, 'bloqueado': bloqueado or False}

# ==================== HEALTH ====================

@app.get("/health")
async def health():
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"status": "ok", "db": "postgresql", "version": "3.0.0", "timestamp": datetime.now().isoformat()}
    except:
        return {"status": "degraded", "db": "disconnected"}

@app.get("/")
async def root():
    return {"app": "🆘 Ami SOS", "version": "3.0.0", "features": [
        "3 alert levels", "community network", "BLE relay",
        "auto-block after 3 reports", "multi-country support"
    ], "docs": "/docs"}