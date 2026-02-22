"""
🆘 AMI SOS - Backend de Emergencias v1.0
FastAPI + PostgreSQL (asyncpg) para Render.com

Endpoints:
  POST /alerta                → Recibir alerta (app o manilla BLE)
  POST /alerta/clasificar     → Clasificar emergencia con Claude IA
  POST /alerta/responder      → Institucional confirma que va en camino
  GET  /alerta/{id}           → Estado de una alerta
  GET  /alerta/{id}/respuestas → Quiénes respondieron
  POST /usuario/registrar     → Registrar usuario SOS
  POST /usuario/contactos     → Agregar contacto de confianza
  POST /token/registrar       → Registrar/actualizar token FCM
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
    description="Backend de emergencias para mujeres y adultos mayores",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger("amisos")

# ==================== BASE DE DATOS POSTGRESQL ====================

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
    tipo_alerta: str = "emergencia"
    nivel_alerta: str = "critica"
    mensaje: Optional[str] = None
    latitud: Optional[float] = None
    longitud: Optional[float] = None
    fuente_alerta: str = "app"
    receptor_destino: str = "cuidador"
    audio_base64: Optional[str] = None
    bateria_dispositivo: Optional[int] = None

class RespuestaInstitucional(BaseModel):
    alerta_id: int
    celular: str
    id_persona: int
    latitud: Optional[float] = None
    longitud: Optional[float] = None
    tiempo_estimado_min: Optional[int] = None

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

# ==================== POST /alerta ====================

@app.post("/alerta")
async def recibir_alerta(req: AlertaRequest, bg: BackgroundTasks):
    inicio = time.time()
    pool = await get_pool()
    cel_sin, cel_con = normalizar_celular(req.celular)
    
    log.info(f"🚨 ALERTA: tipo={req.tipo_alerta} fuente={req.fuente_alerta} cel={cel_con}")
    
    async with pool.acquire() as conn:
        nombre = req.nombre
        if not nombre:
            row = await conn.fetchrow("SELECT nombre FROM usuarios_sos WHERE celular IN ($1,$2) LIMIT 1", cel_sin, cel_con)
            nombre = row['nombre'] if row else 'Usuario'
        
        hora = datetime.now().strftime('%H:%M')
        mensaje = req.mensaje or f"🚨 ALERTA: {nombre} ha activado alerta de {req.tipo_alerta} a las {hora}"
        
        tipos_ok = ('salud','seguridad','violencia','incendio','caida','otro')
        tipo_db = req.tipo_alerta if req.tipo_alerta in tipos_ok else 'otro'
        nivel_db = req.nivel_alerta if req.nivel_alerta in ('leve','critica') else 'critica'
        fuente_map = {'app':'manual', 'manilla_ble':'boton', 'boton_esp32':'boton', 'voz':'manual'}
        fuente_db = fuente_map.get(req.fuente_alerta, 'manual')
        
        alerta_id = await conn.fetchval("""
            INSERT INTO alertas_panico 
            (nombre, mensaje, fecha_hora, celular, atendida, id_persona, rol,
             tipo_alerta, latitud, longitud, nivel_alerta, receptor_destino, fuente_alerta, bateria_dispositivo)
            VALUES ($1,$2,NOW(),$3,'no',$4,'usuario',$5,$6,$7,$8,$9,$10,$11)
            RETURNING id
        """, nombre, mensaje, cel_con, req.id_persona, tipo_db,
            req.latitud, req.longitud, nivel_db, req.receptor_destino, fuente_db, req.bateria_dispositivo)
        
        log.info(f"  💾 Alerta ID: {alerta_id}")
        
        # PRIMERA LÍNEA
        cuidadores = []
        cels_vistos = set()
        
        rows = await conn.fetch("""
            SELECT cc.celular, cc.nombre FROM contactos_confianza cc
            INNER JOIN usuarios_sos u ON u.id = cc.usuario_id
            WHERE u.celular IN ($1,$2) AND cc.disponible_emergencias=TRUE AND cc.activo=TRUE
        """, cel_sin, cel_con)
        for r in rows:
            if r['celular'] not in cels_vistos:
                cuidadores.append({'celular': r['celular'], 'nombre': r['nombre'], 'tipo': 'primera_linea', 'id_persona': None})
                cels_vistos.add(r['celular'])
        
        rows = await conn.fetch(
            "SELECT celular_cuidador, id_persona_cuidador FROM cuidadores_autorizados WHERE celular_cuidado IN ($1,$2)",
            cel_sin, cel_con)
        for r in rows:
            if r['celular_cuidador'] not in cels_vistos:
                cuidadores.append({'celular': r['celular_cuidador'], 'nombre': 'Cuidador', 'tipo': 'primera_linea', 'id_persona': r['id_persona_cuidador']})
                cels_vistos.add(r['celular_cuidador'])
        
        log.info(f"  👥 Primera línea: {len(cuidadores)}")
        
        # INSTITUCIONALES 1KM
        institucionales = []
        if req.latitud and req.longitud:
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
                            'tipo': r['tipo'] or 'general', 'id_persona': r['id_persona'],
                            'distancia_km': round(dist, 2),
                        })
            institucionales.sort(key=lambda x: x['distancia_km'])
            log.info(f"  🏛️ Institucionales 1km: {len(institucionales)}")
    
    bg.add_task(_enviar_notificaciones, pool, alerta_id, nombre, mensaje, req.tipo_alerta, cel_con, cuidadores, institucionales, req.latitud, req.longitud)
    
    ms = round((time.time() - inicio) * 1000)
    return {
        'success': True, 'alerta_id': alerta_id, 'mensaje': 'Alerta procesada',
        'cuidadores_primera_linea': len(cuidadores),
        'cuidadores_institucionales': len(institucionales),
        'institucionales_detalle': [{'nombre': i['nombre'], 'entidad': i['entidad'], 'distancia_km': i['distancia_km']} for i in institucionales[:5]],
        'tiempo_ms': ms,
    }

async def _enviar_notificaciones(pool, alerta_id, nombre, mensaje, tipo_alerta, cel_usuario, cuidadores, institucionales, lat, lon):
    notificados = 0
    data_push = {'alerta_id': str(alerta_id), 'celular_usuario': cel_usuario, 'tipo_alerta': tipo_alerta, 'nombre_usuario': nombre, 'click_action': 'FLUTTER_NOTIFICATION_CLICK'}
    if lat and lon:
        data_push['latitud'] = str(lat)
        data_push['longitud'] = str(lon)
        data_push['maps_url'] = f"https://maps.google.com/?q={lat},{lon}"
    
    todos = [{**c, 'es_inst': False} for c in cuidadores] + [{**i, 'es_inst': True} for i in institucionales]
    
    for dest in todos:
        cs, cc = normalizar_celular(dest['celular'])
        tk = await buscar_token(pool, cs, cc, dest.get('id_persona'))
        token = tk['token']
        titulo = "🚨 EMERGENCIA" if not dest['es_inst'] else f"🚨 Alerta {tipo_alerta.upper()}"
        ok = False
        
        if token:
            result = await enviar_push(token, titulo, mensaje, data_push)
            ok = result['success']
            if ok:
                notificados += 1
                log.info(f"    ✅ {dest['nombre']}")
            else:
                log.warning(f"    ❌ {dest['nombre']}: {result.get('error','')}")
                if 'UNREGISTERED' in str(result.get('error','')) or 'INVALID' in str(result.get('error','')):
                    async with pool.acquire() as conn:
                        await conn.execute("UPDATE tokens_fcm SET valido=FALSE, motivo_invalidez='token_invalido', fecha_invalido=NOW() WHERE celular IN ($1,$2)", cs, cc)
        
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO alertas_enviadas (alerta_id, celular_usuario, nombre_usuario, celular_cuidador_institucional, nombre_cuidador_institucional, token, mensaje, fecha, estado_envio, rol_destinatario, receptor_destino)
                VALUES ($1,$2,$3,$4,$5,$6,$7,NOW(),$8,$9,$10)
            """, alerta_id, cel_usuario, nombre, dest['celular'], dest['nombre'], token or '', mensaje,
                'enviado' if ok else 'fallido', 'institucional' if dest['es_inst'] else 'cuidador', dest.get('entidad','cuidador'))
    
    log.info(f"  📊 {notificados}/{len(todos)} notificados")

# ==================== POST /alerta/responder ====================

@app.post("/alerta/responder")
async def responder_alerta(req: RespuestaInstitucional):
    pool = await get_pool()
    cs, cc = normalizar_celular(req.celular)
    async with pool.acquire() as conn:
        cuidador = await conn.fetchrow("SELECT nombre, entidad FROM cuidadores_institucionales WHERE id_persona=$1 AND celular IN ($2,$3)", req.id_persona, cs, cc)
        if not cuidador:
            raise HTTPException(404, "Cuidador institucional no encontrado")
        if await conn.fetchval("SELECT id FROM respuestas_institucionales WHERE alerta_id=$1 AND id_persona=$2", req.alerta_id, req.id_persona):
            raise HTTPException(409, "Ya respondió esta alerta")
        await conn.execute("""
            INSERT INTO respuestas_institucionales (alerta_id, id_persona, celular, entidad, nombre, latitud, longitud, tiempo_estimado_min)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
        """, req.alerta_id, req.id_persona, cc, cuidador['entidad'], cuidador['nombre'], req.latitud, req.longitud, req.tiempo_estimado_min)
        
        log.info(f"✅ {cuidador['nombre']} ({cuidador['entidad']}) responde alerta {req.alerta_id}")
        
        alerta = await conn.fetchrow("SELECT celular FROM alertas_panico WHERE id=$1", req.alerta_id)
        if alerta:
            us, uc = normalizar_celular(alerta['celular'])
            tk = await buscar_token(pool, us, uc)
            if tk['token']:
                t_msg = f" (~{req.tiempo_estimado_min} min)" if req.tiempo_estimado_min else ""
                await enviar_push(tk['token'], "🟢 Ayuda en camino", f"{cuidador['nombre']} de {cuidador['entidad']} va en camino{t_msg}", {'alerta_id': str(req.alerta_id), 'tipo': 'respuesta_institucional'})
    
    return {'success': True, 'nombre': cuidador['nombre'], 'entidad': cuidador['entidad']}

# ==================== POST /alerta/clasificar ====================

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
{{"tipo_alerta":"seguridad|salud|violencia|incendio|caida|otro","nivel_alerta":"leve|critica","descripcion_corta":"1 línea","acciones":["acción1","acción2"],"llamar_123":true/false,"llamar_155":true/false,"confianza":0.0-1.0}}

Reglas: violencia→llamar_155=true, salud grave→llamar_123=true, seguridad activa→llamar_123=true"""

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
            INSERT INTO usuarios_sos (nombre,apellido,celular,correo,fecha_nacimiento,genero,condiciones_salud,medicamentos,alergias,ciudad,password_hash)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING id
        """, req.nombre, req.apellido, cc, req.correo, f_nac, req.genero, req.condiciones_salud, req.medicamentos, req.alergias, req.ciudad, req.password_hash)
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
        resps = await conn.fetch("SELECT nombre,entidad,fecha_respuesta,tiempo_estimado_min FROM respuestas_institucionales WHERE alerta_id=$1", alerta_id)
    return {'success': True, 'alerta': row_to_dict(row), 'notificaciones': {'total': stats['total'], 'enviadas': stats['enviados']}, 'respuestas_institucionales': [row_to_dict(r) for r in resps]}

@app.get("/alerta/{alerta_id}/respuestas")
async def obtener_respuestas(alerta_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT nombre,entidad,celular,fecha_respuesta,tiempo_estimado_min,latitud,longitud FROM respuestas_institucionales WHERE alerta_id=$1 ORDER BY fecha_respuesta", alerta_id)
    return {'success': True, 'respuestas': [row_to_dict(r) for r in rows], 'total': len(rows)}

@app.get("/health")
async def health():
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"status": "ok", "db": "postgresql", "timestamp": datetime.now().isoformat()}
    except:
        return {"status": "degraded", "db": "disconnected"}

@app.get("/")
async def root():
    return {"app": "🆘 Ami SOS", "version": "1.0.0", "docs": "/docs"}