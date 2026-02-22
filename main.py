"""
🆘 AMI SOS - Backend de Emergencias
FastAPI + Python para Render.com

Endpoints:
  POST /alerta              → Recibir alerta (app o manilla BLE)
  POST /alerta/clasificar   → Clasificar emergencia con Claude IA
  POST /alerta/responder    → Cuidador institucional responde
  GET  /alerta/{id}         → Estado de una alerta
  GET  /alerta/{id}/respuestas → Quiénes respondieron
  GET  /health              → Health check para Render

Autor: TSCAMP SAS
Versión: 1.0
"""

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime
import os
import json
import math
import httpx
import logging
import time

# ==================== CONFIGURACIÓN ====================

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

# ==================== BASE DE DATOS ====================

import aiomysql

async def get_db_pool():
    """Crear pool de conexiones MySQL."""
    if not hasattr(app.state, 'db_pool') or app.state.db_pool is None:
        app.state.db_pool = await aiomysql.create_pool(
            host=os.getenv('DB_HOST', 'localhost'),
            port=int(os.getenv('DB_PORT', 3306)),
            user=os.getenv('DB_USER'),
            password=os.getenv('DB_PASSWORD'),
            db=os.getenv('DB_NAME'),
            charset='utf8mb4',
            autocommit=True,
            minsize=2,
            maxsize=10,
        )
    return app.state.db_pool

@app.on_event("startup")
async def startup():
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1")
        log.info("✅ Conectado a MySQL")
    except Exception as e:
        log.error(f"❌ Error conectando a MySQL: {e}")

@app.on_event("shutdown")
async def shutdown():
    if hasattr(app.state, 'db_pool') and app.state.db_pool:
        app.state.db_pool.close()
        await app.state.db_pool.wait_closed()

# ==================== MODELOS ====================

class AlertaRequest(BaseModel):
    celular: str
    id_persona: Optional[int] = None
    nombre: Optional[str] = None
    tipo_alerta: str = "emergencia"  # seguridad, salud, emergencia, violencia
    nivel_alerta: str = "critica"    # leve, critica
    mensaje: Optional[str] = None
    latitud: Optional[float] = None
    longitud: Optional[float] = None
    fuente_alerta: str = "app"       # app, manilla_ble, boton_esp32, voz
    receptor_destino: str = "cuidador"  # cuidador, ambulancia, policia, bomberos
    audio_base64: Optional[str] = None  # Audio para clasificación IA
    bateria_dispositivo: Optional[int] = None  # Batería de la manilla

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

# ==================== UTILIDADES ====================

def normalizar_celular(celular: str) -> tuple:
    """Retorna (sin_57, con_57)."""
    import re
    celular = re.sub(r'\D', '', celular)
    sin_57 = celular.lstrip('57') if celular.startswith('57') and len(celular) > 10 else celular
    con_57 = f"57{sin_57}"
    return sin_57, con_57

def calcular_distancia_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distancia Haversine entre dos puntos en km."""
    R = 6371  # Radio de la Tierra en km
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    c = 2 * math.asin(math.sqrt(a))
    return R * c

# ==================== FIREBASE ====================

async def obtener_access_token_firebase():
    """Genera JWT y obtiene access token de Google OAuth2."""
    import jwt as pyjwt
    
    client_email = os.getenv('FIREBASE_CLIENT_EMAIL')
    private_key = os.getenv('FIREBASE_PRIVATE_KEY', '').replace('\\n', '\n')
    
    if not client_email or not private_key:
        log.error("Firebase no configurado")
        return None
    
    now = int(time.time())
    payload = {
        'iss': client_email,
        'scope': 'https://www.googleapis.com/auth/firebase.messaging',
        'aud': 'https://oauth2.googleapis.com/token',
        'iat': now,
        'exp': now + 3600,
    }
    
    token = pyjwt.encode(payload, private_key, algorithm='RS256')
    
    async with httpx.AsyncClient() as client:
        resp = await client.post('https://oauth2.googleapis.com/token', data={
            'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
            'assertion': token,
        })
        data = resp.json()
        return data.get('access_token')

async def enviar_push_firebase(token_fcm: str, titulo: str, cuerpo: str, data: dict = None) -> dict:
    """Envía notificación push via Firebase Cloud Messaging v1."""
    project_id = os.getenv('FIREBASE_PROJECT_ID')
    access_token = await obtener_access_token_firebase()
    
    if not access_token:
        return {'success': False, 'error': 'No se pudo obtener access token'}
    
    mensaje = {
        'message': {
            'token': token_fcm,
            'notification': {'title': titulo, 'body': cuerpo},
            'data': {k: str(v) for k, v in (data or {}).items()},
            'android': {
                'priority': 'high',
                'notification': {
                    'sound': 'default',
                    'channel_id': 'canal_alertas_sos',
                    'click_action': 'FLUTTER_NOTIFICATION_CLICK',
                }
            },
            'apns': {
                'payload': {
                    'aps': {'sound': 'default', 'badge': 1, 'content-available': 1}
                }
            }
        }
    }
    
    url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
    
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=mensaje, headers={
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
        }, timeout=15)
        
        if resp.status_code == 200:
            return {'success': True}
        else:
            return {'success': False, 'error': f"HTTP {resp.status_code}: {resp.text}"}

# ==================== BUSCAR TOKENS FCM ====================

async def buscar_token_fcm(pool, celular_sin_57: str, celular_con_57: str, id_persona: int = None) -> dict:
    """Busca token FCM en todas las tablas posibles."""
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            # 1. tokens_fcm
            await cur.execute(
                "SELECT token FROM tokens_fcm WHERE celular IN (%s, %s) ORDER BY fecha DESC LIMIT 1",
                (celular_sin_57, celular_con_57)
            )
            row = await cur.fetchone()
            if row and row['token']:
                return {'token': row['token'], 'fuente': 'tokens_fcm'}
            
            # 2. usuarios_clara
            await cur.execute(
                "SELECT fcm_token FROM usuarios_clara WHERE celular IN (%s, %s) AND fcm_token IS NOT NULL AND fcm_token != '' LIMIT 1",
                (celular_sin_57, celular_con_57)
            )
            row = await cur.fetchone()
            if row and row['fcm_token']:
                return {'token': row['fcm_token'], 'fuente': 'usuarios_clara'}
            
            # 3. Por id_persona
            if id_persona:
                await cur.execute(
                    "SELECT token FROM tokens_fcm WHERE id_persona = %s ORDER BY fecha DESC LIMIT 1",
                    (id_persona,)
                )
                row = await cur.fetchone()
                if row and row['token']:
                    return {'token': row['token'], 'fuente': 'tokens_fcm_id'}
    
    return {'token': None, 'fuente': None}

# ==================== ENDPOINT PRINCIPAL: RECIBIR ALERTA ====================

@app.post("/alerta")
async def recibir_alerta(req: AlertaRequest, background_tasks: BackgroundTasks):
    """
    Recibe una alerta de emergencia y:
    1. Guarda en BD
    2. Busca cuidadores de primera línea
    3. Busca cuidadores institucionales en radio 1km (si hay GPS)
    4. Envía notificaciones Firebase a todos
    5. Retorna inmediatamente, las notificaciones van en background
    """
    inicio = time.time()
    pool = await get_db_pool()
    
    cel_sin_57, cel_con_57 = normalizar_celular(req.celular)
    
    log.info(f"🚨 ALERTA: tipo={req.tipo_alerta} fuente={req.fuente_alerta} cel={cel_con_57}")
    
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            # Obtener nombre si no viene
            nombre = req.nombre
            if not nombre:
                await cur.execute(
                    "SELECT nombre FROM usuarios_clara WHERE celular IN (%s, %s) LIMIT 1",
                    (cel_sin_57, cel_con_57)
                )
                row = await cur.fetchone()
                nombre = row['nombre'] if row else 'Usuario'
            
            # Generar mensaje
            hora = datetime.now().strftime('%H:%M')
            mensaje = req.mensaje or f"🚨 ALERTA: {nombre} ha activado una alerta de {req.tipo_alerta} a las {hora}"
            
            # Mapear tipos válidos para BD
            tipo_db = req.tipo_alerta if req.tipo_alerta in ('salud','seguridad','violencia','incendio','caida','otro') else 'otro'
            nivel_db = req.nivel_alerta if req.nivel_alerta in ('leve','critica') else 'critica'
            fuente_map = {'app':'manual', 'manilla_ble':'boton', 'boton_esp32':'boton', 'voz':'manual'}
            fuente_db = fuente_map.get(req.fuente_alerta, 'manual')
            
            # Guardar alerta
            await cur.execute("""
                INSERT INTO alertas_panico 
                (nombre, mensaje, fecha_hora, celular, atendida, id_persona, rol,
                 tipo_alerta, latitud, longitud, nivel_alerta, receptor_destino, fuente_alerta)
                VALUES (%s, %s, NOW(), %s, 'no', %s, 'usuario',
                        %s, %s, %s, %s, %s, %s)
            """, (nombre, mensaje, cel_con_57, req.id_persona,
                  tipo_db, req.latitud, req.longitud, nivel_db, req.receptor_destino, fuente_db))
            
            alerta_id = cur.lastrowid
            log.info(f"  💾 Alerta guardada ID: {alerta_id}")
            
            # ============================================
            # BUSCAR CUIDADORES PRIMERA LÍNEA
            # ============================================
            cuidadores = []
            
            # contactos_clara (emergencia)
            await cur.execute("""
                SELECT DISTINCT cc.celular, cc.nombre
                FROM contactos_clara cc
                INNER JOIN usuarios_clara uc ON uc.id = cc.usuario_clara_id
                WHERE uc.celular IN (%s, %s)
                  AND cc.tipo = 'emergencia'
                  AND cc.disponible_emergencias = 1
                  AND cc.activo = 1
            """, (cel_sin_57, cel_con_57))
            
            for row in await cur.fetchall():
                cuidadores.append({
                    'celular': row['celular'],
                    'nombre': row['nombre'] or 'Contacto',
                    'tipo': 'primera_linea',
                    'id_persona': None,
                })
            
            # cuidadores_autorizados
            await cur.execute("""
                SELECT DISTINCT ca.celular_cuidador, 
                       COALESCE(p.nombre, 'Cuidador') as nombre_cuidador,
                       ca.id_persona_cuidador
                FROM cuidadores_autorizados ca
                LEFT JOIN personas p ON p.id = ca.id_persona_cuidador
                WHERE ca.celular_cuidado IN (%s, %s)
            """, (cel_sin_57, cel_con_57))
            
            for row in await cur.fetchall():
                cel_c = row['celular_cuidador']
                if not any(c['celular'] == cel_c for c in cuidadores):
                    cuidadores.append({
                        'celular': cel_c,
                        'nombre': row['nombre_cuidador'],
                        'tipo': 'primera_linea',
                        'id_persona': row['id_persona_cuidador'],
                    })
            
            log.info(f"  👥 Cuidadores primera línea: {len(cuidadores)}")
            
            # ============================================
            # BUSCAR CUIDADORES INSTITUCIONALES EN RADIO 1KM
            # ============================================
            institucionales = []
            
            if req.latitud and req.longitud:
                # Buscar institucionales activos con ubicación
                await cur.execute("""
                    SELECT id, nombre, entidad, celular, tipo, id_persona,
                           latitud, longitud
                    FROM cuidadores_institucionales
                    WHERE activo = 1
                      AND latitud IS NOT NULL
                      AND longitud IS NOT NULL
                """)
                
                todos = await cur.fetchall()
                
                for inst in todos:
                    if inst['latitud'] and inst['longitud']:
                        distancia = calcular_distancia_km(
                            req.latitud, req.longitud,
                            float(inst['latitud']), float(inst['longitud'])
                        )
                        
                        if distancia <= 1.0:  # Radio 1km
                            # Filtrar por tipo según la alerta
                            incluir = False
                            tipo_alerta = req.tipo_alerta.lower()
                            tipo_inst = (inst['tipo'] or '').lower()
                            
                            if tipo_alerta in ('emergencia', 'caida'):
                                incluir = True  # Todos responden
                            elif tipo_alerta in ('seguridad', 'violencia'):
                                incluir = tipo_inst in ('policia', 'seguridad', '')
                            elif tipo_alerta == 'salud':
                                incluir = tipo_inst in ('ambulancia', 'salud', '')
                            elif tipo_alerta == 'incendio':
                                incluir = tipo_inst in ('bomberos', 'emergencia', '')
                            else:
                                incluir = True
                            
                            if incluir:
                                institucionales.append({
                                    'celular': inst['celular'],
                                    'nombre': inst['nombre'],
                                    'entidad': inst['entidad'],
                                    'tipo': inst['tipo'] or 'general',
                                    'id_persona': inst['id_persona'],
                                    'distancia_km': round(distancia, 2),
                                })
                
                # Ordenar por distancia
                institucionales.sort(key=lambda x: x['distancia_km'])
                log.info(f"  🏛️ Institucionales en 1km: {len(institucionales)}")
            else:
                log.info("  📍 Sin GPS — no se buscan institucionales")
    
    # ============================================
    # ENVIAR NOTIFICACIONES EN BACKGROUND
    # ============================================
    background_tasks.add_task(
        enviar_notificaciones_alerta,
        pool, alerta_id, nombre, mensaje, req.tipo_alerta,
        cel_con_57, cuidadores, institucionales, req.latitud, req.longitud
    )
    
    elapsed = round((time.time() - inicio) * 1000)
    log.info(f"  ⚡ Respuesta en {elapsed}ms")
    
    return {
        'success': True,
        'alerta_id': alerta_id,
        'mensaje': 'Alerta procesada',
        'cuidadores_primera_linea': len(cuidadores),
        'cuidadores_institucionales': len(institucionales),
        'institucionales_detalle': [
            {'nombre': i['nombre'], 'entidad': i['entidad'], 'distancia_km': i['distancia_km']}
            for i in institucionales[:5]
        ],
        'tiempo_respuesta_ms': elapsed,
    }

async def enviar_notificaciones_alerta(
    pool, alerta_id, nombre, mensaje, tipo_alerta,
    celular_usuario, cuidadores, institucionales, lat, lon
):
    """Envía todas las notificaciones en background."""
    notificados = 0
    total = len(cuidadores) + len(institucionales)
    
    # Datos extra para la notificación
    data_push = {
        'alerta_id': str(alerta_id),
        'celular_usuario': celular_usuario,
        'tipo_alerta': tipo_alerta,
        'nombre_usuario': nombre,
        'click_action': 'FLUTTER_NOTIFICATION_CLICK',
    }
    if lat and lon:
        data_push['latitud'] = str(lat)
        data_push['longitud'] = str(lon)
        data_push['maps_url'] = f"https://maps.google.com/?q={lat},{lon}"
    
    todos = []
    for c in cuidadores:
        todos.append({**c, 'es_institucional': False})
    for i in institucionales:
        todos.append({**i, 'es_institucional': True})
    
    for dest in todos:
        cel_sin, cel_con = normalizar_celular(dest['celular'])
        
        # Buscar token
        token_info = await buscar_token_fcm(pool, cel_sin, cel_con, dest.get('id_persona'))
        token = token_info['token']
        
        titulo = "🚨 EMERGENCIA" if not dest['es_institucional'] else f"🚨 Alerta {tipo_alerta.upper()}"
        
        resultado_push = False
        error_msg = 'sin_token_fcm'
        
        if token:
            result = await enviar_push_firebase(token, titulo, mensaje, data_push)
            resultado_push = result['success']
            error_msg = result.get('error', '')
            
            if resultado_push:
                notificados += 1
                log.info(f"    ✅ Push enviado a {dest['nombre']}")
            else:
                log.warning(f"    ❌ Push falló para {dest['nombre']}: {error_msg}")
                
                # Marcar token inválido si es UNREGISTERED
                if 'UNREGISTERED' in str(error_msg) or 'INVALID' in str(error_msg):
                    async with pool.acquire() as conn:
                        async with conn.cursor() as cur:
                            await cur.execute(
                                "UPDATE tokens_fcm SET valido=0 WHERE celular IN (%s,%s)",
                                (cel_sin, cel_con)
                            )
        else:
            log.warning(f"    ⚠️ Sin token para {dest['nombre']}")
        
        # Registrar en alertas_enviadas
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                rol = 'institucional' if dest['es_institucional'] else 'cuidador'
                receptor = dest.get('entidad', dest.get('tipo', 'cuidador'))
                
                await cur.execute("""
                    INSERT INTO alertas_enviadas
                    (alerta_id, celular_usuario, nombre_usuario,
                     celular_cuidador_institucional, nombre_cuidador_institucional,
                     token, mensaje, fecha, estado_envio, rol_destinatario, receptor_destino)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), %s, %s, %s)
                """, (
                    alerta_id, celular_usuario, dest['nombre'],
                    dest['celular'], dest['nombre'],
                    token or '', mensaje,
                    'enviado' if resultado_push else 'fallido',
                    rol, receptor
                ))
    
    log.info(f"  📊 Resumen: {notificados}/{total} notificados")

# ==================== RESPUESTA INSTITUCIONAL ====================

@app.post("/alerta/responder")
async def responder_alerta(req: RespuestaInstitucional):
    """Cuidador institucional confirma que responde a la alerta."""
    pool = await get_db_pool()
    
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            # Verificar cuidador institucional
            cel_sin, cel_con = normalizar_celular(req.celular)
            await cur.execute(
                "SELECT nombre, entidad FROM cuidadores_institucionales WHERE id_persona=%s AND celular IN (%s,%s)",
                (req.id_persona, cel_sin, cel_con)
            )
            cuidador = await cur.fetchone()
            if not cuidador:
                raise HTTPException(status_code=404, detail="Cuidador institucional no encontrado")
            
            # Verificar que no haya respondido ya
            await cur.execute(
                "SELECT id FROM respuestas_institucionales WHERE alerta_id=%s AND id_persona=%s",
                (req.alerta_id, req.id_persona)
            )
            if await cur.fetchone():
                raise HTTPException(status_code=409, detail="Ya respondió esta alerta")
            
            # Registrar respuesta
            await cur.execute("""
                INSERT INTO respuestas_institucionales
                (alerta_id, id_persona, celular, entidad, nombre, latitud, longitud, 
                 tiempo_estimado_min, fecha_respuesta)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
            """, (
                req.alerta_id, req.id_persona, cel_con,
                cuidador['entidad'], cuidador['nombre'],
                req.latitud, req.longitud, req.tiempo_estimado_min
            ))
            
            log.info(f"✅ {cuidador['nombre']} ({cuidador['entidad']}) responde alerta {req.alerta_id}")
            
            # Notificar al usuario que alguien va en camino
            await cur.execute(
                "SELECT celular FROM alertas_panico WHERE id=%s", (req.alerta_id,)
            )
            alerta = await cur.fetchone()
            if alerta:
                cel_usr_sin, cel_usr_con = normalizar_celular(alerta['celular'])
                token_info = await buscar_token_fcm(pool, cel_usr_sin, cel_usr_con)
                if token_info['token']:
                    tiempo_msg = f" (llega en ~{req.tiempo_estimado_min} min)" if req.tiempo_estimado_min else ""
                    await enviar_push_firebase(
                        token_info['token'],
                        "🟢 Ayuda en camino",
                        f"{cuidador['nombre']} de {cuidador['entidad']} va en camino{tiempo_msg}",
                        {'alerta_id': str(req.alerta_id), 'tipo': 'respuesta_institucional'}
                    )
    
    return {
        'success': True,
        'mensaje': 'Respuesta registrada',
        'nombre': cuidador['nombre'],
        'entidad': cuidador['entidad'],
    }

# ==================== CLASIFICAR EMERGENCIA CON CLAUDE ====================

@app.post("/alerta/clasificar")
async def clasificar_emergencia(req: ClasificarRequest):
    """
    Claude clasifica el tipo de emergencia desde texto o audio transcrito.
    Retorna tipo, nivel, y acciones recomendadas.
    """
    api_key = os.getenv('ANTHROPIC_API_KEY')
    if not api_key:
        raise HTTPException(status_code=500, detail="API key de Anthropic no configurada")
    
    texto = req.texto or ""
    if not texto and not req.audio_base64:
        raise HTTPException(status_code=400, detail="Se requiere texto o audio")
    
    prompt = f"""Clasifica esta emergencia reportada por una persona en Colombia.

CONTEXTO DEL USUARIO: {req.contexto_usuario or 'No disponible'}
REPORTE: "{texto}"

Responde SOLO en JSON:
{{
  "tipo_alerta": "seguridad|salud|violencia|incendio|caida|otro",
  "nivel_alerta": "leve|critica",
  "descripcion_corta": "descripción de 1 línea",
  "acciones": ["acción 1", "acción 2"],
  "llamar_123": true/false,
  "llamar_155": true/false,
  "confianza": 0.0-1.0
}}

REGLAS:
- violencia → llamar_155=true (línea mujer)
- salud grave (dolor pecho, no respira) → llamar_123=true
- seguridad (robo, acoso) → llamar_123=true si es activo
- Si no hay suficiente información, confianza baja y tipo="otro"
"""

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            json={
                'model': 'claude-sonnet-4-20250514',
                'max_tokens': 500,
                'messages': [{'role': 'user', 'content': prompt}],
            },
            headers={
                'Content-Type': 'application/json',
                'x-api-key': api_key,
                'anthropic-version': '2023-06-01',
            },
            timeout=30,
        )
        
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Error Claude: {resp.status_code}")
        
        data = resp.json()
        text = data['content'][0]['text']
        
        # Parsear JSON
        import re
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            result = json.loads(match.group())
            return {'success': True, 'clasificacion': result}
        else:
            raise HTTPException(status_code=500, detail="Error parseando respuesta IA")

# ==================== CONSULTAR ALERTA ====================

@app.get("/alerta/{alerta_id}")
async def obtener_alerta(alerta_id: int):
    """Obtener estado de una alerta."""
    pool = await get_db_pool()
    
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT * FROM alertas_panico WHERE id=%s", (alerta_id,))
            alerta = await cur.fetchone()
            if not alerta:
                raise HTTPException(status_code=404, detail="Alerta no encontrada")
            
            # Contar notificaciones
            await cur.execute(
                "SELECT COUNT(*) as total, SUM(estado_envio='enviado') as enviados FROM alertas_enviadas WHERE alerta_id=%s",
                (alerta_id,)
            )
            stats = await cur.fetchone()
            
            # Respuestas institucionales
            await cur.execute(
                "SELECT nombre, entidad, fecha_respuesta, tiempo_estimado_min FROM respuestas_institucionales WHERE alerta_id=%s",
                (alerta_id,)
            )
            respuestas = await cur.fetchall()
            
            # Serializar datetime
            for key, val in alerta.items():
                if isinstance(val, datetime):
                    alerta[key] = val.isoformat()
            
            return {
                'success': True,
                'alerta': alerta,
                'notificaciones': {
                    'total': stats['total'] or 0,
                    'enviadas': int(stats['enviados'] or 0),
                },
                'respuestas_institucionales': respuestas or [],
            }

@app.get("/alerta/{alerta_id}/respuestas")
async def obtener_respuestas(alerta_id: int):
    """Quiénes han respondido a esta alerta."""
    pool = await get_db_pool()
    
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("""
                SELECT nombre, entidad, celular, fecha_respuesta, 
                       tiempo_estimado_min, latitud, longitud
                FROM respuestas_institucionales 
                WHERE alerta_id=%s
                ORDER BY fecha_respuesta ASC
            """, (alerta_id,))
            
            respuestas = await cur.fetchall()
            for r in respuestas:
                for key, val in r.items():
                    if isinstance(val, datetime):
                        r[key] = val.isoformat()
            
            return {'success': True, 'respuestas': respuestas, 'total': len(respuestas)}

# ==================== HEALTH CHECK ====================

@app.get("/health")
async def health():
    """Health check para Render."""
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1")
        return {"status": "ok", "db": "connected", "timestamp": datetime.now().isoformat()}
    except:
        return {"status": "degraded", "db": "disconnected", "timestamp": datetime.now().isoformat()}

@app.get("/")
async def root():
    return {
        "app": "🆘 Ami SOS",
        "version": "1.0.0",
        "description": "Backend de emergencias para mujeres y adultos mayores",
        "endpoints": [
            "POST /alerta",
            "POST /alerta/clasificar",
            "POST /alerta/responder",
            "GET /alerta/{id}",
            "GET /alerta/{id}/respuestas",
            "GET /health",
        ]
    }