"""
🆘 AMI SOS - Backend de Emergencias v3.1
FastAPI + PostgreSQL (asyncpg) para Render.com

3 NIVELES DE ALERTA:
  Tipo 1: Emergencia leve → Solo cuidadores registrados
  Tipo 2: Emergencia grave → Cuidadores + institucionales 1km
  Tipo 3: Emergencia crítica → Cuidadores + institucionales + RED COMUNITARIA 1km

PANEL ADMINISTRATIVO:
  /panel-app/          → Panel web (HTML estático)
  /panel/login         → Autenticación por rol
  /panel/alertas       → Historial filtrado por rol
  /panel/mapa/*        → Alertas activas + Red comunitaria
  /panel/dashboard     → Estadísticas (solo admin)
  /panel/usuarios      → Gestión de cuentas (solo admin)

Autor: TSCAMP SAS
"""

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, date, timedelta
from decimal import Decimal
import os
import json
import math
import httpx
import logging
import time
os.environ['TZ'] = 'America/Bogota'
try:
    time.tzset()
except AttributeError:
    pass  # Windows no tiene tzset
import asyncpg
import re
import base64
import secrets
import bcrypt

# ==================== APP ====================

app = FastAPI(
    title="🆘 Ami SOS API",
    description="Backend de emergencias — 3 niveles de alerta + red comunitaria + panel administrativo",
    version="3.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger("amisos")

# ==================== MOTOR DE DECISIÓN ====================

PROTOCOLOS_EMERGENCIA = {
    "robo_hurto": {
        "circulos": {"cuidadores": True, "comunidad": True, "policia": True, "ambulancia": False, "bomberos": False},
        "mensajes": {
            "cuidadores": "🚨 {nombre} reporta un robo cerca de su ubicación. La policía fue notificada.",
            "comunidad": "⚠️ Robo reportado a {distancia}. SÉ TESTIGO desde distancia segura. NO intervengas. Graba video si es seguro.",
            "policia": "🚨 ROBO en curso — {ubicacion}. Víctima: {nombre}. {descripcion_ia}",
        },
        "nivel_minimo": 2, "llamar_123": True, "llamar_155": False, "escalar_min": 3,
        "instrucciones": "Mantén distancia. Graba como evidencia. NO persigas.",
    },
    "robo_armado": {
        "circulos": {"cuidadores": True, "comunidad": True, "policia": True, "ambulancia": True, "bomberos": False},
        "mensajes": {
            "cuidadores": "🔴 EMERGENCIA CRÍTICA: {nombre} en situación de robo armado. Policía notificada. NO contactes directamente.",
            "comunidad": "🔴 PELIGRO: Robo ARMADO a {distancia}. ALÉJATE inmediatamente. NO te acerques. Policía en camino.",
            "policia": "🔴 URGENTE — ROBO ARMADO en {ubicacion}. Arma confirmada. Víctima: {nombre}. {descripcion_ia}",
            "ambulancia": "⚠️ Alerta preventiva: Robo armado en {ubicacion}. Posibles heridos.",
        },
        "nivel_minimo": 3, "llamar_123": True, "llamar_155": False, "escalar_min": 1,
        "instrucciones": "ALÉJATE. NO intentes ser héroe. Graba solo desde lejos.",
    },
    "persona_sospechosa": {
        "circulos": {"cuidadores": False, "comunidad": True, "policia": True, "ambulancia": False, "bomberos": False},
        "mensajes": {
            "comunidad": "👁️ ALERTA PREVENTIVA a {distancia}: Actividad sospechosa reportada. Mantente alerta. NO confrontes.",
            "policia": "👁️ PREVENTIVO — Actividad sospechosa en {ubicacion}. {descripcion_ia}. Verificar.",
        },
        "nivel_minimo": 1, "llamar_123": False, "llamar_155": False, "escalar_min": 10, "es_preventiva": True,
        "instrucciones": "Observa y reporta. NO te acerques. Envía fotos si puedes.",
    },
    "vehiculo_sospechoso": {
        "circulos": {"cuidadores": False, "comunidad": True, "policia": True, "ambulancia": False, "bomberos": False},
        "mensajes": {
            "comunidad": "👁️ PREVENTIVA a {distancia}: Vehículo sospechoso. Anota placa y descripción desde lejos. NO te acerques.",
            "policia": "👁️ PREVENTIVO — Vehículo sospechoso en {ubicacion}. {descripcion_ia}.",
        },
        "nivel_minimo": 1, "llamar_123": False, "llamar_155": False, "escalar_min": 15, "es_preventiva": True,
        "instrucciones": "Anota placa desde lejos. NO bloquees vía. Reporta movimientos.",
    },
    "agresion_fisica": {
        "circulos": {"cuidadores": True, "comunidad": True, "policia": True, "ambulancia": True, "bomberos": False},
        "mensajes": {
            "cuidadores": "🔴 {nombre} reporta agresión física. Policía y ambulancia notificados.",
            "comunidad": "🔴 Agresión física a {distancia}. Llama al 123. NO intervengas físicamente. Tu presencia como testigo ayuda.",
            "policia": "🔴 AGRESIÓN FÍSICA en {ubicacion}. Víctima: {nombre}. {descripcion_ia}",
            "ambulancia": "🟠 Posibles heridos por agresión en {ubicacion}.",
        },
        "nivel_minimo": 3, "llamar_123": True, "llamar_155": True, "escalar_min": 2,
        "instrucciones": "Sé testigo. Graba desde distancia. NO intervengas físicamente.",
    },
    "violencia_intrafamiliar": {
        "circulos": {"cuidadores": True, "comunidad": True, "policia": True, "ambulancia": False, "bomberos": False},
        "mensajes": {
            "cuidadores": "🟣 URGENTE: {nombre} activó alerta de violencia intrafamiliar. Necesita ayuda AHORA. Policía notificada.",
            "comunidad": "🟣 URGENTE: {nombre} necesita ayuda inmediata. Acércate, toca la puerta, haz presencia. Policía en camino pero TÚ estás más cerca.",
            "policia": "🟣 VIOLENCIA INTRAFAMILIAR — {ubicacion}. Víctima: {nombre}. Protocolo VIF. Línea 155 notificada.",
        },
        "nivel_minimo": 3, "llamar_123": True, "llamar_155": True, "escalar_min": 1,
        "comunidad_solo_preautorizados": True,
        "instrucciones": "Haz presencia visible. Toca la puerta. NO entres a la vivienda. Graba audio.",
    },
    "emergencia_medica": {
        "circulos": {"cuidadores": True, "comunidad": True, "policia": False, "ambulancia": True, "bomberos": False},
        "mensajes": {
            "cuidadores": "🚑 {nombre} tiene emergencia médica. Ambulancia notificada.",
            "comunidad": "🚑 Emergencia médica a {distancia}. ¿Sabes primeros auxilios? Tu ayuda puede salvar una vida.",
            "ambulancia": "🚑 EMERGENCIA MÉDICA en {ubicacion}. {descripcion_ia}",
        },
        "nivel_minimo": 2, "llamar_123": True, "llamar_155": False, "escalar_min": 2,
        "instrucciones": "Primeros auxilios si sabes. Despeja el área. Guía a la ambulancia.",
    },
    "persona_herida": {
        "circulos": {"cuidadores": True, "comunidad": True, "policia": False, "ambulancia": True, "bomberos": False},
        "mensajes": {
            "cuidadores": "🩸 {nombre} está herido/a. Ambulancia en camino.",
            "comunidad": "🩸 Persona herida a {distancia}. Primeros auxilios si puedes. Ambulancia notificada.",
            "ambulancia": "🩸 PERSONA HERIDA en {ubicacion}. {descripcion_ia}",
        },
        "nivel_minimo": 2, "llamar_123": True, "llamar_155": False, "escalar_min": 2,
        "instrucciones": "No mover si hay trauma. Despejar área para ambulancia.",
    },
    "caida_persona": {
        "circulos": {"cuidadores": True, "comunidad": True, "policia": False, "ambulancia": False, "bomberos": False},
        "mensajes": {
            "cuidadores": "⚠️ {nombre} ha sufrido una caída. Verifica su estado.",
            "comunidad": "🤝 Persona necesita ayuda cerca ({distancia}). Ha sufrido una caída. Acércate si puedes.",
        },
        "nivel_minimo": 1, "llamar_123": False, "llamar_155": False, "escalar_min": 5,
        "instrucciones": "Ayuda a levantarse si no hay lesión. Si hay lesión, NO mover y llama 123.",
    },
    "adulto_perdido": {
        "circulos": {"cuidadores": True, "comunidad": True, "policia": True, "ambulancia": False, "bomberos": False},
        "mensajes": {
            "cuidadores": "📍 {nombre} parece estar desorientado/a. Ubicación compartida.",
            "comunidad": "🔍 Adulto mayor desorientado cerca ({distancia}). Acércate amablemente. Nombre: {nombre}.",
            "policia": "🔍 Adulto mayor desorientado en {ubicacion}. Nombre: {nombre}. Apoyo en ubicación.",
        },
        "nivel_minimo": 1, "llamar_123": False, "llamar_155": False, "escalar_min": 10,
        "instrucciones": "Acércate amable y tranquilo. Ayúdalo a contactar familia.",
    },
    "accidente_transito": {
        "circulos": {"cuidadores": True, "comunidad": True, "policia": True, "ambulancia": True, "bomberos": False},
        "mensajes": {
            "cuidadores": "🚗 {nombre} reporta accidente de tránsito. Autoridades notificadas.",
            "comunidad": "🚗 Accidente de tránsito a {distancia}. Señaliza la vía. Asiste heridos SIN moverlos.",
            "policia": "🚗 ACCIDENTE DE TRÁNSITO en {ubicacion}. {descripcion_ia}",
            "ambulancia": "🚗 Accidente en {ubicacion}. Posibles heridos. {descripcion_ia}",
        },
        "nivel_minimo": 2, "llamar_123": True, "llamar_155": False, "escalar_min": 3,
        "instrucciones": "Señaliza vía. Asiste heridos SIN moverlos. NO muevas vehículos.",
    },
    "incendio": {
        "circulos": {"cuidadores": True, "comunidad": True, "policia": True, "ambulancia": True, "bomberos": True},
        "mensajes": {
            "cuidadores": "🔥 {nombre} reporta incendio. Bomberos notificados.",
            "comunidad": "🔥 INCENDIO a {distancia}. EVACÚA la zona. NO intentes apagar. Ayuda a personas a salir.",
            "policia": "🔥 INCENDIO en {ubicacion}. Bomberos notificados. Apoyo en evacuación.",
            "ambulancia": "🔥 Incendio en {ubicacion}. Posibles heridos.",
            "bomberos": "🔥 INCENDIO en {ubicacion}. {descripcion_ia}",
        },
        "nivel_minimo": 3, "llamar_123": True, "llamar_155": False, "escalar_min": 1,
        "instrucciones": "EVACÚA. Ayuda a mayores y niños. NO entres con humo.",
    },
    "inundacion": {
        "circulos": {"cuidadores": True, "comunidad": True, "policia": True, "ambulancia": False, "bomberos": True},
        "mensajes": {
            "cuidadores": "🌊 {nombre} reporta inundación. Mantente comunicado.",
            "comunidad": "🌊 Inundación a {distancia}. Muévete a zonas altas. NO camines por agua en movimiento.",
            "policia": "🌊 Inundación en {ubicacion}. Posible evacuación.",
            "bomberos": "🌊 INUNDACIÓN en {ubicacion}. {descripcion_ia}. Posibles atrapados.",
        },
        "nivel_minimo": 2, "llamar_123": True, "llamar_155": False, "escalar_min": 2,
        "instrucciones": "Zonas altas. NO camines por agua. Ayuda a vecinos mayores.",
    },
    "situacion_riesgo": {
        "circulos": {"cuidadores": True, "comunidad": True, "policia": True, "ambulancia": False, "bomberos": False},
        "mensajes": {
            "cuidadores": "⚠️ {nombre} reporta situación de riesgo. Autoridades notificadas.",
            "comunidad": "⚠️ Situación de riesgo a {distancia}. Mantente alerta. Reporta novedades.",
            "policia": "⚠️ Situación de riesgo en {ubicacion}. {descripcion_ia}. Verificar.",
        },
        "nivel_minimo": 1, "llamar_123": False, "llamar_155": False, "escalar_min": 5,
        "instrucciones": "Mantente alerta a distancia. Reporta novedades.",
    },
    "dano_propiedad": {
        "circulos": {"cuidadores": False, "comunidad": True, "policia": True, "ambulancia": False, "bomberos": False},
        "mensajes": {
            "comunidad": "⚠️ Daño a propiedad a {distancia}. Sirve como testigo si viste algo.",
            "policia": "⚠️ Daño a propiedad en {ubicacion}. {descripcion_ia}.",
        },
        "nivel_minimo": 1, "llamar_123": False, "llamar_155": False, "escalar_min": 15,
        "instrucciones": "Toma fotos. Colabora como testigo.",
    },
    "no_emergencia": {
        "circulos": {"cuidadores": True, "comunidad": False, "policia": False, "ambulancia": False, "bomberos": False},
        "mensajes": {"cuidadores": "ℹ️ {nombre} activó alerta pero no se detectó emergencia. Verifica su estado."},
        "nivel_minimo": 1, "llamar_123": False, "llamar_155": False, "escalar_min": 30,
        "instrucciones": "",
    },
    "imagen_no_clara": {
        "circulos": {"cuidadores": True, "comunidad": False, "policia": False, "ambulancia": False, "bomberos": False},
        "mensajes": {"cuidadores": "⚠️ {nombre} envió alerta pero la evidencia no es clara. Comunícate para verificar."},
        "nivel_minimo": 1, "llamar_123": False, "llamar_155": False, "escalar_min": 5,
        "instrucciones": "",
    },
}

PROTOCOLO_DEFAULT = {
    "circulos": {"cuidadores": True, "comunidad": True, "policia": True, "ambulancia": False, "bomberos": False},
    "mensajes": {
        "cuidadores": "🚨 {nombre} necesita ayuda. Autoridades notificadas.",
        "comunidad": "🚨 Emergencia a {distancia}. Mantente alerta. Acércate solo si es seguro.",
        "policia": "🚨 Emergencia en {ubicacion}. {descripcion_ia}",
    },
    "nivel_minimo": 2, "llamar_123": True, "llamar_155": False, "escalar_min": 3,
    "instrucciones": "Evalúa antes de acercarte. Reporta novedades.",
}


def obtener_protocolo(clasificacion: str, tiene_arma: bool = False, hay_heridos: bool = False) -> dict:
    """Retorna protocolo de respuesta. Ajusta automáticamente si hay armas o heridos."""
    proto = PROTOCOLOS_EMERGENCIA.get(clasificacion, PROTOCOLO_DEFAULT).copy()
    proto["circulos"] = dict(proto["circulos"])
    if tiene_arma and clasificacion == "robo_hurto":
        proto = PROTOCOLOS_EMERGENCIA["robo_armado"].copy()
        proto["circulos"] = dict(proto["circulos"])
    if tiene_arma:
        proto["circulos"]["policia"] = True
        proto["nivel_minimo"] = 3
        proto["escalar_min"] = 1
    if hay_heridos:
        proto["circulos"]["ambulancia"] = True
        if proto["nivel_minimo"] < 2:
            proto["nivel_minimo"] = 2
    return proto


def generar_mensajes_protocolo(protocolo: dict, nombre: str, ubicacion: str, distancia: str = "", descripcion_ia: str = "") -> dict:
    """Genera mensajes finales reemplazando variables."""
    mensajes = {}
    for dest, plantilla in protocolo.get("mensajes", {}).items():
        mensajes[dest] = plantilla.format(
            nombre=nombre, ubicacion=ubicacion,
            distancia=distancia or "cercana", descripcion_ia=descripcion_ia or "")
    return mensajes


# ==================== POSTGRESQL ====================

async def _init_connection(conn):
    await conn.execute("SET timezone = 'America/Bogota'")

async def get_pool():
    if not hasattr(app.state, 'pool') or app.state.pool is None:
        database_url = os.getenv('DATABASE_URL') or os.getenv('INTERNAL_DATABASE_URL')
        if database_url:
            app.state.pool = await asyncpg.create_pool(database_url, min_size=2, max_size=10, init=_init_connection)
        else:
            app.state.pool = await asyncpg.create_pool(
                host=os.getenv('DB_HOST', 'localhost'),
                port=int(os.getenv('DB_PORT', 5432)),
                user=os.getenv('DB_USER'),
                password=os.getenv('DB_PASSWORD'),
                database=os.getenv('DB_NAME'),
                min_size=2, max_size=10,
                init=_init_connection,
            )
    return app.state.pool

# ==================== STARTUP / SHUTDOWN ====================

@app.on_event("startup")
async def startup():
    # Configurar credenciales de Google Cloud desde variable de entorno
    gcp_creds = os.getenv('GOOGLE_APPLICATION_CREDENTIALS_JSON', '')
    if gcp_creds and not os.path.exists('/tmp/gcp-credentials.json'):
        with open('/tmp/gcp-credentials.json', 'w') as f:
            f.write(gcp_creds)
        os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = '/tmp/gcp-credentials.json'
        log.info("✅ Credenciales Google Cloud configuradas")
    
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
            await conn.execute("SET timezone = 'America/Bogota'")
        log.info("✅ Conectado a PostgreSQL (zona horaria: Colombia)")
        await migrar_tablas_panel()
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
    nivel_emergencia: int = 2
    tipo_alerta: str = "emergencia"
    nivel_alerta: str = "critica"
    mensaje: Optional[str] = None
    latitud: Optional[float] = None
    longitud: Optional[float] = None
    fuente_alerta: str = "app"
    receptor_destino: str = "cuidador"
    audio_base64: Optional[str] = None
    bateria_dispositivo: Optional[int] = None

class RespuestaAlerta(BaseModel):
    alerta_id: int
    celular: str
    id_persona: Optional[int] = None
    tipo_respondedor: str = "cuidador"
    latitud: Optional[float] = None
    longitud: Optional[float] = None
    tiempo_estimado_min: Optional[int] = None
    accion: Optional[str] = None

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
    disponible_red: bool = True

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
    disponible: bool = True

class RelayBLE(BaseModel):
    mac_manilla: str
    celular_relay: str
    latitud: float
    longitud: float
    tipo_alerta_ble: int = 3
    rssi: Optional[int] = None

class AlertaBLE(BaseModel):
    """Alerta unificada desde botón Bluetooth (iTag) de cualquier plataforma."""
    celular: str
    plataforma: str = "ami_sos"
    id_persona: Optional[int] = None
    nombre: Optional[str] = None
    nivel_emergencia: int = 2
    tipo_alerta: str = "emergencia"
    mensaje: Optional[str] = None
    latitud: Optional[float] = None
    longitud: Optional[float] = None
    fuente_alerta: str = "boton_ble"
    bateria_dispositivo: Optional[int] = None
    mac_dispositivo: Optional[str] = None

class ReporteUsuario(BaseModel):
    celular_reporta: str
    celular_reportado: str
    motivo: str = "comportamiento"
    descripcion: Optional[str] = None

class VigilanciaRequest(BaseModel):
    celular: str
    nombre: Optional[str] = None
    descripcion: str
    latitud: float
    longitud: float
    tipo_sospecha: str = "general"

class VigilanciaConfirmar(BaseModel):
    vigilancia_id: int
    celular: str
    confirma: bool = True
    comentario: Optional[str] = None
    latitud: Optional[float] = None
    longitud: Optional[float] = None

# --- Modelos del Panel Administrativo ---

class LoginPanel(BaseModel):
    email: str
    password: str

class CrearUsuarioPanel(BaseModel):
    email: str
    password: str
    nombre: str
    rol: str = "cuidador"
    tipo_institucional: Optional[str] = None
    celular: Optional[str] = None

class AnalizarEvidenciaRequest(BaseModel):
    alerta_id: int
    imagen_base64: Optional[str] = None
    imagen_url: Optional[str] = None
    imagen_nombre: Optional[str] = None
    tipo_alerta: Optional[str] = None
    nivel_emergencia: Optional[int] = None
    ubicacion: Optional[str] = None
    mensaje_usuario: Optional[str] = None
    media_type: Optional[str] = "image/jpeg"

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
                'id': r['id'], 'celular': r['celular'],
                'nombre': r['nombre'] or 'Miembro red',
                'distancia_km': round(dist, 2), 'tipo': 'comunidad', 'id_persona': None,
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
    
    nivel = req.nivel_emergencia
    if nivel not in (1, 2, 3):
        nivel = 2
    
    etiquetas = {1: "🟡 LEVE", 2: "🟠 GRAVE", 3: "🔴 CRÍTICA"}
    log.info(f"🚨 ALERTA NIVEL {nivel} {etiquetas[nivel]}: tipo={req.tipo_alerta} fuente={req.fuente_alerta} cel={cel_con}")
    
    async with pool.acquire() as conn:
        nombre = req.nombre
        if not nombre:
            row = await conn.fetchrow("SELECT nombre FROM usuarios_sos WHERE celular IN ($1,$2) LIMIT 1", cel_sin, cel_con)
            nombre = row['nombre'] if row else 'Usuario'
        
        hora = datetime.now().strftime('%H:%M')
        if req.mensaje:
            mensaje = req.mensaje
        elif nivel == 1:
            mensaje = f"🟡 {nombre} necesita ayuda — emergencia leve a las {hora}"
        elif nivel == 2:
            mensaje = f"🟠 EMERGENCIA: {nombre} necesita ayuda urgente — {req.tipo_alerta} a las {hora}"
        else:
            mensaje = f"🔴 EMERGENCIA CRÍTICA: {nombre} está en peligro — {req.tipo_alerta} a las {hora}"
        
        tipos_ok = ('salud','seguridad','violencia','incendio','caida','otro')
        tipo_db = req.tipo_alerta if req.tipo_alerta in tipos_ok else 'otro'
        nivel_alerta_db = 'leve' if nivel == 1 else 'critica'
        fuente_map = {'app':'manual', 'manilla_ble':'boton', 'boton_esp32':'boton', 'voz':'manual', 'relay_ble':'relay'}
        fuente_db = fuente_map.get(req.fuente_alerta, 'manual')
        
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
        
        # NIVEL 1: SOLO CUIDADORES
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
        
        # NIVEL 2+: INSTITUCIONALES 1KM
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
        
        # NIVEL 3: RED COMUNITARIA 1KM
        comunidad = []
        if nivel >= 3 and req.latitud and req.longitud:
            comunidad = await buscar_red_comunitaria(pool, req.latitud, req.longitud, cel_con)
            log.info(f"  🤝 Red comunitaria 1km: {len(comunidad)}")
    
    bg.add_task(
        _enviar_notificaciones_v2, pool, alerta_id, nombre, mensaje,
        req.tipo_alerta, nivel, cel_con, cuidadores, institucionales, comunidad,
        req.latitud, req.longitud
    )
    
    ms = round((time.time() - inicio) * 1000)
    
    return {
        'success': True, 'alerta_id': alerta_id, 'nivel_emergencia': nivel,
        'mensaje': 'Alerta procesada',
        'notificados': {
            'cuidadores': len(cuidadores), 'institucionales': len(institucionales),
            'red_comunitaria': len(comunidad),
            'total': len(cuidadores) + len(institucionales) + len(comunidad),
        },
        'institucionales_detalle': [
            {'nombre': i['nombre'], 'entidad': i.get('entidad',''), 'distancia_km': i['distancia_km']}
            for i in institucionales[:5]
        ],
        'comunidad_cercanos': len(comunidad), 'tiempo_ms': ms,
    }

# ==================== ENVIAR NOTIFICACIONES V2 ====================

async def _enviar_notificaciones_v2(pool, alerta_id, nombre, mensaje, tipo_alerta, nivel,
                                     cel_usuario, cuidadores, institucionales, comunidad, lat, lon):
    notificados = 0
    data_push = {
        'alerta_id': str(alerta_id), 'celular_usuario': cel_usuario,
        'tipo_alerta': tipo_alerta, 'nivel_emergencia': str(nivel),
        'nombre_usuario': nombre, 'click_action': 'FLUTTER_NOTIFICATION_CLICK',
    }
    if lat and lon:
        data_push['latitud'] = str(lat)
        data_push['longitud'] = str(lon)
        data_push['maps_url'] = f"https://maps.google.com/?q={lat},{lon}"
    
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
        
        if dest['rol_dest'] == 'cuidador':
            titulo = f"🚨 {'EMERGENCIA' if nivel >= 2 else 'Alerta'} de {nombre}"
        elif dest['rol_dest'] == 'institucional':
            titulo = f"🚨 Alerta {tipo_alerta.upper()} — Nivel {nivel}"
        else:
            dist_txt = f"{dest.get('distancia_km', '?')}km"
            titulo = f"🔴 EMERGENCIA cerca de ti ({dist_txt})"
            mensaje = f"{nombre} necesita ayuda a {dist_txt}. Puedes: llamar 123, grabar video como evidencia, o acercarte si es seguro."
        
        ok = False
        if token:
            result = await enviar_push(token, titulo, mensaje, data_push)
            ok = result['success']
            if ok:
                notificados += 1
                log.info(f"    ✅ [{dest['rol_dest']}] {dest['nombre']}")
            else:
                log.warning(f"    ❌ [{dest['rol_dest']}] {dest['nombre']}: {result.get('error','')}")
                if 'UNREGISTERED' in str(result.get('error','')) or 'INVALID' in str(result.get('error','')):
                    async with pool.acquire() as conn:
                        await conn.execute("UPDATE tokens_fcm SET valido=FALSE, motivo_invalidez='token_invalido', fecha_invalido=NOW() WHERE celular IN ($1,$2)", cs, cc)
        
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

# ==================== POST /alerta/responder ====================

@app.post("/alerta/responder")
async def responder_alerta(req: RespuestaAlerta):
    pool = await get_pool()
    cs, cc = normalizar_celular(req.celular)
    
    async with pool.acquire() as conn:
        alerta = await conn.fetchrow("SELECT * FROM alertas_panico WHERE id=$1", req.alerta_id)
        if not alerta:
            raise HTTPException(404, "Alerta no encontrada")
        
        if req.id_persona:
            existe = await conn.fetchval(
                "SELECT id FROM respuestas_institucionales WHERE alerta_id=$1 AND celular IN ($2,$3)",
                req.alerta_id, cs, cc)
            if existe:
                raise HTTPException(409, "Ya respondió esta alerta")
        
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
        
        await conn.execute("""
            INSERT INTO respuestas_institucionales 
            (alerta_id, id_persona, celular, entidad, nombre, latitud, longitud, 
             tiempo_estimado_min, estado)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
        """, req.alerta_id, req.id_persona or 0, cc, entidad_resp, nombre_resp,
            req.latitud, req.longitud, req.tiempo_estimado_min,
            req.accion or 'voy_en_camino')
        
        log.info(f"✅ [{req.tipo_respondedor}] {nombre_resp} responde alerta {req.alerta_id}")
        
        u_sin, u_con = normalizar_celular(alerta['celular'])
        tk = await buscar_token(pool, u_sin, u_con)
        if tk['token']:
            acciones_txt = {
                'voy_en_camino': 'va en camino', 'llame_123': 'llamó al 123',
                'grabando_video': 'está grabando evidencia', 'vigilando': 'está vigilando la zona',
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
    pool = await get_pool()
    cs, cc = normalizar_celular(req.celular)
    async with pool.acquire() as conn:
        usr = await conn.fetchrow("SELECT id, nombre FROM usuarios_sos WHERE celular IN ($1,$2) LIMIT 1", cs, cc)
        nombre = usr['nombre'] if usr else None
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
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT celular, latitud, longitud FROM ubicaciones_red
            WHERE disponible = TRUE AND actualizado_at > NOW() - INTERVAL '30 minutes'
        """)
    cercanos = 0
    for r in rows:
        if distancia_km(latitud, longitud, float(r['latitud']), float(r['longitud'])) <= 1.0:
            cercanos += 1
    return {'success': True, 'cercanos_1km': cercanos}

# ==================== RELAY BLE ====================

@app.post("/red/relay")
async def relay_ble(req: RelayBLE, bg: BackgroundTasks):
    pool = await get_pool()
    async with pool.acquire() as conn:
        disp = await conn.fetchrow(
            "SELECT usuario_id, nombre_dispositivo FROM dispositivos_ble WHERE mac_address=$1 AND activo=TRUE",
            req.mac_manilla)
        if not disp:
            raise HTTPException(404, "Dispositivo BLE no registrado")
        usuario = await conn.fetchrow("SELECT nombre, celular FROM usuarios_sos WHERE id=$1", disp['usuario_id'])
        if not usuario:
            raise HTTPException(404, "Usuario del dispositivo no encontrado")
        reciente = await conn.fetchval("""
            SELECT id FROM alertas_panico 
            WHERE celular=$1 AND fuente_alerta='relay' AND fecha_hora > NOW() - INTERVAL '5 minutes'
        """, usuario['celular'])
        if reciente:
            return {'success': True, 'alerta_id': reciente, 'mensaje': 'Alerta ya reportada por relay'}
        
        log.info(f"📡 RELAY BLE: Manilla {req.mac_manilla} detectada por {req.celular_relay}")
        alerta_req = AlertaRequest(
            celular=usuario['celular'], nombre=usuario['nombre'],
            nivel_emergencia=3, tipo_alerta='seguridad', nivel_alerta='critica',
            mensaje=f"🔴 RELAY: {usuario['nombre']} puede estar en peligro. Señal BLE detectada a las {datetime.now().strftime('%H:%M')}",
            latitud=req.latitud, longitud=req.longitud, fuente_alerta='relay_ble',
        )
        return await recibir_alerta(alerta_req, bg)

# ==================== ENDPOINT UNIFICADO BLE ====================

AMI_ADULTOS_URL = os.getenv('AMI_ADULTOS_URL', '')

@app.post("/ble/alerta")
async def alerta_ble_unificada(req: AlertaBLE, bg: BackgroundTasks):
    inicio = time.time()
    cel_sin, cel_con = normalizar_celular(req.celular)
    plataforma = req.plataforma.lower().strip()
    
    log.info(f"🔵 BLE ALERTA [{plataforma.upper()}]: cel={cel_con} nivel={req.nivel_emergencia} mac={req.mac_dispositivo or 'N/A'}")
    
    if plataforma == "ami_sos":
        log.info(f"  → Procesando en Ami SOS (interno)")
        alerta_req = AlertaRequest(
            celular=req.celular, id_persona=req.id_persona, nombre=req.nombre,
            nivel_emergencia=req.nivel_emergencia, tipo_alerta=req.tipo_alerta,
            nivel_alerta="critica" if req.nivel_emergencia >= 2 else "leve",
            mensaje=req.mensaje, latitud=req.latitud, longitud=req.longitud,
            fuente_alerta="boton", receptor_destino="cuidador",
            bateria_dispositivo=req.bateria_dispositivo,
        )
        resultado = await recibir_alerta(alerta_req, bg)
        ms = round((time.time() - inicio) * 1000)
        resultado['plataforma'] = 'ami_sos'
        resultado['fuente'] = 'boton_ble'
        resultado['tiempo_total_ms'] = ms
        return resultado
    
    elif plataforma == "ami":
        log.info(f"  → Reenviando a Ami adultos (PHP)")
        if not AMI_ADULTOS_URL:
            raise HTTPException(500, "AMI_ADULTOS_URL no configurada")
        
        payload_php = {
            "celular": cel_con, "id_persona": req.id_persona,
            "nombre": req.nombre or "", "tipo_alerta": req.tipo_alerta,
            "nivel_alerta": "critica" if req.nivel_emergencia >= 2 else "leve",
            "mensaje": req.mensaje or "", "latitud": req.latitud, "longitud": req.longitud,
            "fuente_alerta": "boton", "receptor_destino": "cuidador", "rol": "usuario",
        }
        
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(AMI_ADULTOS_URL, json=payload_php, timeout=15,
                    headers={"Content-Type": "application/json"})
                ms = round((time.time() - inicio) * 1000)
                if resp.status_code == 200:
                    resultado_php = resp.json()
                    log.info(f"  ✅ Ami adultos respondió OK: alerta_id={resultado_php.get('alerta_id')}")
                    return {
                        "success": True, "plataforma": "ami", "fuente": "boton_ble",
                        "alerta_id": resultado_php.get("alerta_id"),
                        "mensaje": "Alerta enviada a cuidadores de Ami",
                        "notificados": resultado_php.get("notificados", 0),
                        "backend_response": resultado_php, "tiempo_total_ms": ms,
                    }
                else:
                    alerta_id_local = await _guardar_alerta_fallback(req, cel_con, "ami", f"forward_failed_http_{resp.status_code}")
                    return {"success": False, "plataforma": "ami", "error": f"HTTP {resp.status_code}",
                            "fallback_alerta_id": alerta_id_local, "tiempo_total_ms": ms}
        except httpx.TimeoutException:
            alerta_id_local = await _guardar_alerta_fallback(req, cel_con, "ami", "forward_timeout")
            return {"success": False, "plataforma": "ami", "error": "Timeout", "fallback_alerta_id": alerta_id_local}
        except Exception as e:
            alerta_id_local = await _guardar_alerta_fallback(req, cel_con, "ami", f"error: {str(e)[:100]}")
            return {"success": False, "plataforma": "ami", "error": str(e), "fallback_alerta_id": alerta_id_local}
    else:
        raise HTTPException(400, f"Plataforma '{plataforma}' no reconocida. Usa 'ami_sos' o 'ami'.")

async def _guardar_alerta_fallback(req: AlertaBLE, cel_con: str, plataforma: str, motivo: str) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        alerta_id = await conn.fetchval("""
            INSERT INTO alertas_panico 
            (nombre, mensaje, fecha_hora, celular, atendida, id_persona, rol,
             tipo_alerta, latitud, longitud, nivel_alerta, receptor_destino, 
             fuente_alerta, nivel_emergencia)
            VALUES ($1, $2, NOW(), $3, 'no', $4, 'usuario', $5, $6, $7, $8, 
                    'cuidador', 'boton', $9)
            RETURNING id
        """, req.nombre or 'Usuario Ami',
            f"[FALLBACK-{plataforma}] {motivo} — Alerta BLE de {req.nombre or 'Usuario'}",
            cel_con, req.id_persona,
            req.tipo_alerta if req.tipo_alerta in ('salud','seguridad','violencia','incendio','caida','otro') else 'otro',
            req.latitud, req.longitud,
            'critica' if req.nivel_emergencia >= 2 else 'leve', req.nivel_emergencia)
        log.warning(f"  💾 FALLBACK: Alerta guardada localmente ID={alerta_id} ({motivo})")
        return alerta_id

@app.post("/ble/test")
async def test_ble_endpoint(req: AlertaBLE):
    cel_sin, cel_con = normalizar_celular(req.celular)
    return {
        "success": True, "test": True, "mensaje": "Endpoint BLE unificado funcionando",
        "datos_recibidos": {
            "celular": cel_con, "plataforma": req.plataforma,
            "nivel_emergencia": req.nivel_emergencia, "tipo_alerta": req.tipo_alerta,
            "tiene_gps": req.latitud is not None and req.longitud is not None,
            "mac_dispositivo": req.mac_dispositivo,
        },
        "ami_adultos_configurado": bool(AMI_ADULTOS_URL),
        "timestamp": datetime.now().isoformat(),
    }

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

Niveles: 1=leve(cuidadores), 2=grave(+institucionales), 3=crítica(+red comunitaria)"""

    async with httpx.AsyncClient() as client:
        resp = await client.post("https://api.anthropic.com/v1/messages",
            json={'model': 'claude-sonnet-4-5-20250929', 'max_tokens': 500, 'messages': [{'role': 'user', 'content': prompt}]},
            headers={'Content-Type':'application/json','x-api-key':api_key,'anthropic-version':'2023-06-01'}, timeout=30)
        if resp.status_code != 200:
            raise HTTPException(502, f"Error Claude: {resp.status_code}")
        text = resp.json()['content'][0]['text']
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            return {'success': True, 'clasificacion': json.loads(match.group())}
        raise HTTPException(500, "Error parseando respuesta IA")

# ==================== LOGIN / REGISTRO / CONSULTAS ====================

@app.get("/usuario/login/{celular}")
async def login_usuario(celular: str):
    pool = await get_pool()
    cs, cc = normalizar_celular(celular)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, nombre, apellido, celular, ciudad, country_code, bloqueado FROM usuarios_sos WHERE celular IN ($1,$2)", cs, cc)
        if not row:
            raise HTTPException(404, "User not found")
        if row['bloqueado']:
            raise HTTPException(403, "User blocked")
        return {'success': True, 'id': row['id'], 'nombre': row['nombre'],
                'apellido': row['apellido'], 'celular': row['celular'],
                'ciudad': row['ciudad'], 'country_code': row['country_code']}

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

@app.get("/alerta/{alerta_id}")
async def obtener_alerta(alerta_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM alertas_panico WHERE id=$1", alerta_id)
        if not row: raise HTTPException(404, "Alerta no encontrada")
        stats = await conn.fetchrow("SELECT COUNT(*) as total, COUNT(*) FILTER (WHERE estado_envio='enviado') as enviados FROM alertas_enviadas WHERE alerta_id=$1", alerta_id)
        resps = await conn.fetch("SELECT nombre,entidad,celular,fecha_respuesta,tiempo_estimado_min,estado FROM respuestas_institucionales WHERE alerta_id=$1 ORDER BY fecha_respuesta", alerta_id)
    return {
        'success': True, 'alerta': row_to_dict(row),
        'notificaciones': {'total': stats['total'], 'enviadas': stats['enviados']},
        'respuestas': [row_to_dict(r) for r in resps],
    }

@app.get("/alerta/{alerta_id}/respuestas")
async def obtener_respuestas(alerta_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT nombre,entidad,celular,fecha_respuesta,tiempo_estimado_min,latitud,longitud,estado FROM respuestas_institucionales WHERE alerta_id=$1 ORDER BY fecha_respuesta", alerta_id)
    return {'success': True, 'respuestas': [row_to_dict(r) for r in rows], 'total': len(rows)}

# ==================== VIGILANCIA PREVENTIVA ====================

@app.post("/vigilancia")
async def crear_vigilancia(req: VigilanciaRequest):
    pool = await get_pool()
    cs, cc = normalizar_celular(req.celular)
    async with pool.acquire() as conn:
        vid = await conn.fetchval("""
            INSERT INTO vigilancias (celular, nombre, descripcion, tipo_sospecha, latitud, longitud)
            VALUES ($1, $2, $3, $4, $5, $6) RETURNING id
        """, cc, req.nombre, req.descripcion, req.tipo_sospecha, req.latitud, req.longitud)
    
    log.info(f"👁 Vigilancia #{vid}: {req.tipo_sospecha} por {cc}")
    cercanos = await buscar_red_comunitaria(pool, req.latitud, req.longitud, cc)
    notificados = 0
    for persona in cercanos:
        try:
            token_info = await buscar_token(pool, persona['celular'], persona['celular'])
            if token_info and token_info.get('token'):
                await enviar_push(token_info['token'], '👁 Actividad sospechosa cerca',
                    f'{req.nombre or "Alguien"}: {req.descripcion[:80]}',
                    data={'tipo': 'vigilancia', 'vigilancia_id': vid, 'latitud': req.latitud, 'longitud': req.longitud})
                notificados += 1
        except Exception as e:
            log.warning(f"  Push error: {e}")
    
    return {'success': True, 'vigilancia_id': vid, 'notificados': notificados, 'cercanos_total': len(cercanos)}

@app.post("/vigilancia/confirmar")
async def confirmar_vigilancia(req: VigilanciaConfirmar):
    pool = await get_pool()
    cs, cc = normalizar_celular(req.celular)
    async with pool.acquire() as conn:
        vig = await conn.fetchrow("SELECT * FROM vigilancias WHERE id=$1 AND estado='activa'", req.vigilancia_id)
        if not vig:
            raise HTTPException(404, "Vigilancia no encontrada o cerrada")
        try:
            await conn.execute("""
                INSERT INTO confirmaciones_vigilancia (vigilancia_id, celular, confirma, comentario, latitud, longitud)
                VALUES ($1, $2, $3, $4, $5, $6)
            """, req.vigilancia_id, cc, req.confirma, req.comentario, req.latitud, req.longitud)
        except Exception:
            raise HTTPException(409, "Ya confirmó esta vigilancia")
        
        if req.confirma:
            await conn.execute("UPDATE vigilancias SET confirmaciones = confirmaciones + 1 WHERE id=$1", req.vigilancia_id)
        else:
            await conn.execute("UPDATE vigilancias SET rechazos = rechazos + 1 WHERE id=$1", req.vigilancia_id)
        
        updated = await conn.fetchrow(
            "SELECT confirmaciones, rechazos, escalada, celular, nombre, latitud, longitud, descripcion FROM vigilancias WHERE id=$1",
            req.vigilancia_id)
        
        alerta_id = None
        if updated['confirmaciones'] >= 2 and not updated['escalada']:
            alerta_id = await conn.fetchval("""
                INSERT INTO alertas_panico (celular, nombre, nivel_emergencia, tipo_alerta, mensaje, latitud, longitud, fuente_alerta, atendida, fecha_hora)
                VALUES ($1, $2, 2, 'seguridad', $3, $4, $5, 'vigilancia', 'no', NOW()) RETURNING id
            """, updated['celular'], updated['nombre'],
                f"Actividad sospechosa confirmada: {updated['descripcion'][:200]}. {updated['confirmaciones']} testigos.",
                updated['latitud'], updated['longitud'])
            await conn.execute("UPDATE vigilancias SET escalada=TRUE, alerta_id=$1 WHERE id=$2", alerta_id, req.vigilancia_id)
            log.warning(f"🚨 VIGILANCIA #{req.vigilancia_id} ESCALADA → Alerta #{alerta_id}")
    
    return {'success': True, 'confirmaciones': updated['confirmaciones'], 'rechazos': updated['rechazos'],
            'escalada': alerta_id is not None, 'alerta_id': alerta_id}

@app.get("/vigilancia/{vigilancia_id}")
async def obtener_vigilancia(vigilancia_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        vig = await conn.fetchrow("SELECT * FROM vigilancias WHERE id=$1", vigilancia_id)
        if not vig: raise HTTPException(404, "No encontrada")
        confs = await conn.fetch("SELECT celular, confirma, comentario, fecha FROM confirmaciones_vigilancia WHERE vigilancia_id=$1 ORDER BY fecha", vigilancia_id)
    return {'success': True, 'vigilancia': row_to_dict(vig), 'confirmaciones': [row_to_dict(c) for c in confs]}

@app.get("/vigilancia/activas")
async def vigilancias_activas(latitud: float, longitud: float):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, celular, nombre, descripcion, tipo_sospecha, latitud, longitud,
                   confirmaciones, rechazos, escalada, fecha
            FROM vigilancias WHERE estado = 'activa' AND fecha > NOW() - INTERVAL '2 hours'
        """)
    cercanas = []
    for r in rows:
        dist = distancia_km(latitud, longitud, float(r['latitud']), float(r['longitud']))
        if dist <= 1.0:
            v = row_to_dict(r)
            v['distancia_km'] = round(dist, 2)
            cercanas.append(v)
    cercanas.sort(key=lambda x: x['distancia_km'])
    return {'success': True, 'vigilancias': cercanas, 'total': len(cercanas)}

# ==================== REPORTAR USUARIO ====================

@app.post("/usuario/reportar")
async def reportar_usuario(req: ReporteUsuario):
    pool = await get_pool()
    cs_reporta, cc_reporta = normalizar_celular(req.celular_reporta)
    cs_reportado, cc_reportado = normalizar_celular(req.celular_reportado)
    async with pool.acquire() as conn:
        if cc_reporta == cc_reportado:
            raise HTTPException(400, "No puede reportarse a sí mismo")
        existe = await conn.fetchval(
            "SELECT id FROM reportes_usuario WHERE celular_reportado=$1 AND celular_reporta=$2", cc_reportado, cc_reporta)
        if existe:
            raise HTTPException(409, "Ya reportó a este usuario")
        await conn.execute("""
            INSERT INTO reportes_usuario (celular_reportado, celular_reporta, motivo, descripcion)
            VALUES ($1, $2, $3, $4)
        """, cc_reportado, cc_reporta, req.motivo, req.descripcion)
        total_reportes = await conn.fetchval("SELECT COUNT(*) FROM reportes_usuario WHERE celular_reportado=$1", cc_reportado)
        bloqueado = False
        if total_reportes >= 3:
            await conn.execute("""
                UPDATE usuarios_sos SET bloqueado=TRUE, motivo_bloqueo=$1, fecha_bloqueo=NOW()
                WHERE celular IN ($2, $3)
            """, f"auto_block_{total_reportes}_reports", cs_reportado, cc_reportado)
            await conn.execute("UPDATE ubicaciones_red SET disponible=FALSE WHERE celular=$1", cc_reportado)
            bloqueado = True
            log.warning(f"🚫 AUTO-BLOQUEO: {cc_reportado} con {total_reportes} reportes")
    return {'success': True, 'total_reportes': total_reportes, 'usuario_bloqueado': bloqueado}

@app.get("/usuario/{celular}/reportes")
async def ver_reportes(celular: str):
    pool = await get_pool()
    cs, cc = normalizar_celular(celular)
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM reportes_usuario WHERE celular_reportado IN ($1,$2)", cs, cc)
        bloqueado = await conn.fetchval("SELECT bloqueado FROM usuarios_sos WHERE celular IN ($1,$2)", cs, cc)
    return {'success': True, 'total_reportes': total, 'bloqueado': bloqueado or False}

# ================================================================
# PANEL ADMINISTRATIVO — MIGRACIÓN DE TABLAS
# ================================================================

async def migrar_tablas_panel():
    """Crea las tablas del panel administrativo si no existen."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS usuarios_panel (
                id SERIAL PRIMARY KEY,
                email VARCHAR(100) UNIQUE NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                nombre VARCHAR(100) NOT NULL,
                rol VARCHAR(20) NOT NULL DEFAULT 'cuidador',
                tipo_institucional VARCHAR(30),
                celular VARCHAR(20),
                activo BOOLEAN DEFAULT TRUE,
                ultimo_login TIMESTAMP,
                creado_en TIMESTAMP DEFAULT NOW(),
                actualizado_en TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sesiones_panel (
                id SERIAL PRIMARY KEY,
                usuario_id INTEGER REFERENCES usuarios_panel(id) ON DELETE CASCADE,
                token VARCHAR(255) UNIQUE NOT NULL,
                ip_address VARCHAR(45),
                user_agent TEXT,
                expira_en TIMESTAMP NOT NULL,
                creado_en TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS auditoria_panel (
                id SERIAL PRIMARY KEY,
                usuario_id INTEGER REFERENCES usuarios_panel(id),
                accion VARCHAR(50) NOT NULL,
                detalle TEXT,
                ip_address VARCHAR(45),
                creado_en TIMESTAMP DEFAULT NOW()
            )
        """)
        
        indices = [
            ("idx_alertas_panico_fecha", "alertas_panico", "fecha_hora DESC"),
            ("idx_alertas_panico_atendida", "alertas_panico", "atendida"),
            ("idx_alertas_panico_tipo", "alertas_panico", "tipo_alerta"),
            ("idx_alertas_panico_nivel", "alertas_panico", "nivel_emergencia"),
            ("idx_sesiones_token", "sesiones_panel", "token"),
            ("idx_auditoria_fecha", "auditoria_panel", "creado_en DESC"),
        ]
        for nombre, tabla, columnas in indices:
            try:
                await conn.execute(f"CREATE INDEX IF NOT EXISTS {nombre} ON {tabla}({columnas})")
            except Exception as e:
                log.warning(f"  ⚠️ Índice {nombre}: {e}")
        
        try:
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_ubicaciones_red_actualizado ON ubicaciones_red(actualizado_at)")
        except Exception:
            pass
        
        # Tabla de análisis de evidencia con IA
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS analisis_evidencia (
                id SERIAL PRIMARY KEY,
                alerta_id INTEGER NOT NULL,
                archivo_nombre VARCHAR(255),
                archivo_url TEXT,
                clasificacion VARCHAR(50),
                urgencia VARCHAR(20) NOT NULL DEFAULT 'media',
                descripcion TEXT,
                objetos_detectados TEXT,
                personas_detectadas INTEGER DEFAULT 0,
                hay_heridos BOOLEAN DEFAULT FALSE,
                hay_armas BOOLEAN DEFAULT FALSE,
                hay_fuego_humo BOOLEAN DEFAULT FALSE,
                hay_vehiculos BOOLEAN DEFAULT FALSE,
                hay_dano_propiedad BOOLEAN DEFAULT FALSE,
                accion_sugerida TEXT,
                despachar_ambulancia BOOLEAN DEFAULT FALSE,
                despachar_policia BOOLEAN DEFAULT FALSE,
                despachar_bomberos BOOLEAN DEFAULT FALSE,
                llamar_123 BOOLEAN DEFAULT FALSE,
                confianza DECIMAL(3,2) DEFAULT 0.00,
                modelo_ia VARCHAR(50) DEFAULT 'claude-sonnet-4-5',
                tokens_usados INTEGER DEFAULT 0,
                tiempo_analisis_ms INTEGER DEFAULT 0,
                contenido_sensible BOOLEAN DEFAULT FALSE,
                tipo_contenido_sensible VARCHAR(50),
                creado_en TIMESTAMP DEFAULT NOW(),
                
                -- Revisión humana
                estado_revision VARCHAR(20) DEFAULT 'pendiente',
                revision_clasificacion VARCHAR(50),
                revision_urgencia VARCHAR(20),
                revision_accion TEXT,
                revision_notas TEXT,
                revision_despachar_ambulancia BOOLEAN,
                revision_despachar_policia BOOLEAN,
                revision_despachar_bomberos BOOLEAN,
                revisado_por INTEGER,
                revisado_nombre VARCHAR(100),
                revisado_en TIMESTAMP
            )
        """)
        try:
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_analisis_alerta ON analisis_evidencia(alerta_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_analisis_urgencia ON analisis_evidencia(urgencia)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_analisis_revision ON analisis_evidencia(estado_revision)")
        except Exception:
            pass
        # Agregar columnas de revisión si tabla ya existía
        for col, tipo in [
            ('estado_revision', "VARCHAR(20) DEFAULT 'pendiente'"),
            ('revision_clasificacion', 'VARCHAR(50)'),
            ('revision_urgencia', 'VARCHAR(20)'),
            ('revision_accion', 'TEXT'),
            ('revision_notas', 'TEXT'),
            ('revision_despachar_ambulancia', 'BOOLEAN'),
            ('revision_despachar_policia', 'BOOLEAN'),
            ('revision_despachar_bomberos', 'BOOLEAN'),
            ('revisado_por', 'INTEGER'),
            ('revisado_nombre', 'VARCHAR(100)'),
            ('revisado_en', 'TIMESTAMP'),
        ]:
            try:
                await conn.execute(f"ALTER TABLE analisis_evidencia ADD COLUMN IF NOT EXISTS {col} {tipo}")
            except Exception:
                pass
        log.info("✅ Tabla analisis_evidencia verificada")
        
        log.info("🟢 Migración panel administrativo completa")

# ================================================================
# PANEL ADMINISTRATIVO — HELPERS DE AUTH
# ================================================================

async def _verificar_token_panel(request: Request) -> dict:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Token requerido")
    token = auth[7:]
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT s.usuario_id, s.expira_en, u.nombre, u.rol, u.tipo_institucional, u.email, u.activo
            FROM sesiones_panel s JOIN usuarios_panel u ON u.id = s.usuario_id
            WHERE s.token = $1
        """, token)
    if not row:
        raise HTTPException(401, "Token inválido")
    if row['expira_en'] < datetime.now():
        raise HTTPException(401, "Token expirado")
    if not row['activo']:
        raise HTTPException(403, "Usuario desactivado")
    return {
        "usuario_id": row['usuario_id'], "nombre": row['nombre'],
        "rol": row['rol'], "tipo_institucional": row['tipo_institucional'], "email": row['email'],
    }

async def _auditar(usuario_id: int, accion: str, detalle: str = None, ip: str = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO auditoria_panel (usuario_id, accion, detalle, ip_address)
            VALUES ($1, $2, $3, $4)
        """, usuario_id, accion, detalle, ip)

def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def _verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())

# ================================================================
# PANEL ADMINISTRATIVO — AUTH ENDPOINTS
# ================================================================

@app.post("/panel/setup")
async def panel_setup(req: CrearUsuarioPanel):
    """Crear primer admin. Solo funciona si NO hay admins."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchval("SELECT COUNT(*) FROM usuarios_panel WHERE rol = 'admin'")
        if existing > 0:
            raise HTTPException(403, "Ya existe un admin. Usa /panel/login")
        new_id = await conn.fetchval("""
            INSERT INTO usuarios_panel (email, password_hash, nombre, rol)
            VALUES ($1, $2, $3, 'admin') RETURNING id
        """, req.email.lower().strip(), _hash_password(req.password), req.nombre)
    return {"success": True, "mensaje": "Admin creado. Ahora usa /panel/login", "usuario_id": new_id}

@app.post("/panel/login")
async def panel_login(req: LoginPanel, request: Request):
    pool = await get_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow("""
            SELECT id, email, password_hash, nombre, rol, tipo_institucional, activo
            FROM usuarios_panel WHERE email = $1
        """, req.email.lower().strip())
    if not user:
        raise HTTPException(401, "Credenciales inválidas")
    if not user['activo']:
        raise HTTPException(403, "Usuario desactivado")
    if not _verify_password(req.password, user['password_hash']):
        raise HTTPException(401, "Credenciales inválidas")
    
    token = secrets.token_urlsafe(48)
    expira = datetime.now() + timedelta(hours=12)
    ip = request.client.host if request.client else None
    
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO sesiones_panel (usuario_id, token, ip_address, user_agent, expira_en)
            VALUES ($1, $2, $3, $4, $5)
        """, user['id'], token, ip, request.headers.get("User-Agent", ""), expira)
        await conn.execute("UPDATE usuarios_panel SET ultimo_login = NOW() WHERE id = $1", user['id'])
    
    await _auditar(user['id'], "login", f"IP: {ip}", ip)
    return {
        "success": True, "token": token, "expira_en": expira.isoformat(),
        "usuario": {"id": user['id'], "nombre": user['nombre'], "email": user['email'],
                     "rol": user['rol'], "tipo_institucional": user['tipo_institucional']},
    }

@app.post("/panel/logout")
async def panel_logout(request: Request):
    user = await _verificar_token_panel(request)
    token = request.headers.get("Authorization", "")[7:]
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM sesiones_panel WHERE token = $1", token)
    await _auditar(user['usuario_id'], "logout")
    return {"success": True}

@app.get("/panel/me")
async def panel_me(request: Request):
    user = await _verificar_token_panel(request)
    return {"success": True, "usuario": user}

# ================================================================
# PANEL — ALERTAS
# ================================================================

@app.get("/panel/alertas")
async def panel_alertas(
    request: Request, page: int = 1, limit: int = 50,
    estado: Optional[str] = None, tipo: Optional[str] = None,
    nivel: Optional[int] = None, desde: Optional[str] = None, hasta: Optional[str] = None,
):
    user = await _verificar_token_panel(request)
    conditions = []
    params = []
    param_idx = 1
    
    if user['rol'] in ('policia', 'ambulancia', 'bomberos'):
        tipo_map = {'policia': ['seguridad', 'violencia'], 'ambulancia': ['salud', 'caida'], 'bomberos': ['incendio']}
        tipos = tipo_map.get(user['rol'], [])
        if tipos:
            placeholders = ', '.join(f'${param_idx + i}' for i in range(len(tipos)))
            conditions.append(f"tipo_alerta IN ({placeholders})")
            params.extend(tipos)
            param_idx += len(tipos)
    
    if estado == 'atendida':
        conditions.append(f"atendida = ${param_idx}"); params.append('si'); param_idx += 1
    elif estado == 'no_atendida':
        conditions.append(f"atendida = ${param_idx}"); params.append('no'); param_idx += 1
    if tipo:
        conditions.append(f"tipo_alerta = ${param_idx}"); params.append(tipo); param_idx += 1
    if nivel:
        conditions.append(f"nivel_emergencia = ${param_idx}"); params.append(nivel); param_idx += 1
    if desde:
        conditions.append(f"fecha_hora >= ${param_idx}"); params.append(desde); param_idx += 1
    if hasta:
        conditions.append(f"fecha_hora <= ${param_idx}"); params.append(hasta); param_idx += 1
    
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    offset = (page - 1) * limit
    
    pool = await get_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval(f"SELECT COUNT(*) FROM alertas_panico {where}", *params)
        rows = await conn.fetch(f"""
            SELECT id, nombre, mensaje, fecha_hora, celular, atendida, 
                   tipo_alerta, latitud, longitud, nivel_alerta, nivel_emergencia,
                   fuente_alerta, receptor_destino
            FROM alertas_panico {where} ORDER BY fecha_hora DESC LIMIT {limit} OFFSET {offset}
        """, *params)
    
    await _auditar(user['usuario_id'], "ver_alertas", f"page={page}")
    return {
        "success": True, "alertas": [row_to_dict(r) for r in rows],
        "total": total, "page": page, "pages": (total + limit - 1) // limit if total > 0 else 0,
    }

@app.get("/panel/alertas/{alerta_id}")
async def panel_alerta_detalle(alerta_id: int, request: Request):
    user = await _verificar_token_panel(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        alerta = await conn.fetchrow("SELECT * FROM alertas_panico WHERE id = $1", alerta_id)
    if not alerta:
        raise HTTPException(404, "Alerta no encontrada")
    await _auditar(user['usuario_id'], "ver_alerta_detalle", f"alerta_id={alerta_id}")
    return {"success": True, "alerta": row_to_dict(alerta)}

@app.post("/panel/alertas/{alerta_id}/atender")
async def panel_atender_alerta(alerta_id: int, request: Request):
    user = await _verificar_token_panel(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE alertas_panico SET atendida = 'si' WHERE id = $1", alerta_id)
    await _auditar(user['usuario_id'], "atender_alerta", f"alerta_id={alerta_id}")
    return {"success": True, "mensaje": f"Alerta {alerta_id} marcada como atendida"}

# ================================================================
# PANEL — MAPA EN TIEMPO REAL
# ================================================================

@app.get("/panel/mapa/alertas-activas")
async def panel_mapa_alertas_activas(request: Request):
    user = await _verificar_token_panel(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, nombre, tipo_alerta, nivel_emergencia, nivel_alerta,
                   latitud, longitud, fecha_hora, celular, fuente_alerta
            FROM alertas_panico
            WHERE atendida = 'no' AND latitud IS NOT NULL AND longitud IS NOT NULL
              AND fecha_hora >= NOW() - INTERVAL '24 hours'
            ORDER BY fecha_hora DESC
        """)
    return {"success": True, "alertas": [row_to_dict(r) for r in rows]}

@app.get("/panel/mapa/red-comunitaria")
async def panel_mapa_red(request: Request):
    user = await _verificar_token_panel(request)
    if user['rol'] != 'admin':
        raise HTTPException(403, "Solo admin puede ver la red comunitaria")
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT celular, latitud, longitud, disponible, actualizado_at
            FROM ubicaciones_red
            WHERE actualizado_at >= NOW() - INTERVAL '1 hour' AND latitud IS NOT NULL
        """)
    return {"success": True, "miembros": [row_to_dict(r) for r in rows], "total": len(rows)}

# ================================================================
# PANEL — DASHBOARD
# ================================================================

@app.get("/panel/dashboard")
async def panel_dashboard(request: Request):
    user = await _verificar_token_panel(request)
    if user['rol'] != 'admin':
        raise HTTPException(403, "Solo admin")
    pool = await get_pool()
    async with pool.acquire() as conn:
        total_alertas = await conn.fetchval("SELECT COUNT(*) FROM alertas_panico")
        alertas_hoy = await conn.fetchval("SELECT COUNT(*) FROM alertas_panico WHERE fecha_hora >= CURRENT_DATE")
        no_atendidas = await conn.fetchval("SELECT COUNT(*) FROM alertas_panico WHERE atendida = 'no'")
        por_tipo = await conn.fetch("SELECT tipo_alerta, COUNT(*) as total FROM alertas_panico GROUP BY tipo_alerta ORDER BY total DESC")
        por_nivel = await conn.fetch("SELECT nivel_emergencia, COUNT(*) as total FROM alertas_panico GROUP BY nivel_emergencia ORDER BY nivel_emergencia")
        por_dia = await conn.fetch("""
            SELECT DATE(fecha_hora) as dia, COUNT(*) as total FROM alertas_panico
            WHERE fecha_hora >= CURRENT_DATE - INTERVAL '7 days' GROUP BY DATE(fecha_hora) ORDER BY dia
        """)
        por_fuente = await conn.fetch("SELECT fuente_alerta, COUNT(*) as total FROM alertas_panico GROUP BY fuente_alerta ORDER BY total DESC")
        red_activa = await conn.fetchval("SELECT COUNT(*) FROM ubicaciones_red WHERE actualizado_at >= NOW() - INTERVAL '30 minutes'") or 0
    
    await _auditar(user['usuario_id'], "ver_dashboard")
    return {
        "success": True,
        "stats": {
            "total_alertas": total_alertas, "alertas_hoy": alertas_hoy,
            "no_atendidas": no_atendidas, "red_activa": red_activa,
            "por_tipo": [dict(r) for r in por_tipo],
            "por_nivel": [dict(r) for r in por_nivel],
            "por_dia": [{"dia": str(r['dia']), "total": r['total']} for r in por_dia],
            "por_fuente": [dict(r) for r in por_fuente],
        }
    }

# ================================================================
# PANEL — GESTIÓN DE USUARIOS
# ================================================================

@app.get("/panel/usuarios")
async def panel_usuarios_lista(request: Request):
    user = await _verificar_token_panel(request)
    if user['rol'] != 'admin':
        raise HTTPException(403, "Solo admin")
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, email, nombre, rol, tipo_institucional, celular, activo, ultimo_login, creado_en
            FROM usuarios_panel ORDER BY creado_en DESC
        """)
    return {"success": True, "usuarios": [row_to_dict(r) for r in rows]}

@app.post("/panel/usuarios")
async def panel_crear_usuario(req: CrearUsuarioPanel, request: Request):
    user = await _verificar_token_panel(request)
    if user['rol'] != 'admin':
        raise HTTPException(403, "Solo admin")
    roles_validos = ['admin', 'policia', 'ambulancia', 'bomberos', 'cuidador']
    if req.rol not in roles_validos:
        raise HTTPException(400, f"Rol inválido. Opciones: {roles_validos}")
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            new_id = await conn.fetchval("""
                INSERT INTO usuarios_panel (email, password_hash, nombre, rol, tipo_institucional, celular)
                VALUES ($1, $2, $3, $4, $5, $6) RETURNING id
            """, req.email.lower().strip(), _hash_password(req.password), req.nombre,
                req.rol, req.tipo_institucional, req.celular)
    except Exception as e:
        if "unique" in str(e).lower():
            raise HTTPException(409, "Email ya registrado")
        raise
    await _auditar(user['usuario_id'], "crear_usuario", f"nuevo_id={new_id} rol={req.rol}")
    return {"success": True, "usuario_id": new_id}

@app.put("/panel/usuarios/{uid}/toggle")
async def panel_toggle_usuario(uid: int, request: Request):
    user = await _verificar_token_panel(request)
    if user['rol'] != 'admin':
        raise HTTPException(403, "Solo admin")
    pool = await get_pool()
    async with pool.acquire() as conn:
        nuevo = await conn.fetchval("UPDATE usuarios_panel SET activo = NOT activo, actualizado_en = NOW() WHERE id = $1 RETURNING activo", uid)
    if nuevo is None:
        raise HTTPException(404, "Usuario no encontrado")
    await _auditar(user['usuario_id'], "toggle_usuario", f"uid={uid} activo={nuevo}")
    return {"success": True, "activo": nuevo}

# ================================================================
# PANEL — EVIDENCIAS (proxy Firebase Storage)
# ================================================================

@app.get("/panel/evidencias/{alerta_id}")
async def panel_evidencias(alerta_id: int, request: Request):
    user = await _verificar_token_panel(request)
    await _auditar(user['usuario_id'], "ver_evidencia", f"alerta_id={alerta_id}")
    try:
        from google.cloud import storage as gcs
        client = gcs.Client()
        bucket_name = os.getenv('FIREBASE_STORAGE_BUCKET', '')
        if not bucket_name:
            return {"success": True, "evidencias": [], "total": 0, "nota": "FIREBASE_STORAGE_BUCKET no configurado"}
        bucket = client.bucket(bucket_name)
        
        # Buscar en AMBAS rutas posibles (Flutter usa emergencias/, legacy usa alertas/)
        evidencias = []
        prefijos = [
            f"alertas/{alerta_id}/",
        ]
        # Buscar en emergencias/*/alert_{alerta_id}/
        # Listar fechas disponibles
        try:
            fecha_blobs = list(bucket.list_blobs(prefix="emergencias/", delimiter="/"))
            for blob in bucket.list_blobs(prefix="emergencias/", delimiter="/"):
                pass  # iterator consume
            # Buscar directamente con patrón alert_{id}
            all_blobs = list(bucket.list_blobs(prefix=f"emergencias/"))
            for blob in all_blobs:
                if f"alert_{alerta_id}/" in blob.name:
                    if blob.content_type and ('image' in blob.content_type or 'video' in blob.content_type):
                        url = blob.generate_signed_url(expiration=timedelta(hours=1))
                        evidencias.append({
                            "nombre": blob.name.split("/")[-1], "url": url, "ruta": blob.name,
                            "tipo": "imagen" if "image" in blob.content_type else "video",
                            "tamano": blob.size,
                            "subido_en": blob.time_created.isoformat() if blob.time_created else None,
                        })
        except Exception as e:
            log.warning(f"Error buscando en emergencias/: {e}")
        
        # También buscar en ruta legacy
        try:
            for blob in bucket.list_blobs(prefix=f"alertas/{alerta_id}/"):
                if blob.content_type and ('image' in blob.content_type or 'video' in blob.content_type):
                    url = blob.generate_signed_url(expiration=timedelta(hours=1))
                    evidencias.append({
                        "nombre": blob.name.split("/")[-1], "url": url, "ruta": blob.name,
                        "tipo": "imagen" if "image" in blob.content_type else "video",
                        "tamano": blob.size,
                        "subido_en": blob.time_created.isoformat() if blob.time_created else None,
                    })
        except Exception:
            pass
        
        return {"success": True, "evidencias": evidencias, "total": len(evidencias)}
    except ImportError:
        return {"success": True, "evidencias": [], "total": 0, "nota": "google-cloud-storage no instalado"}
    except Exception as e:
        log.warning(f"Error accediendo evidencias: {e}")
        return {"success": True, "evidencias": [], "total": 0, "nota": str(e)}

# ================================================================
# PANEL — AUDITORÍA
# ================================================================

@app.get("/panel/auditoria")
async def panel_auditoria(request: Request, page: int = 1, limit: int = 50):
    user = await _verificar_token_panel(request)
    if user['rol'] != 'admin':
        raise HTTPException(403, "Solo admin")
    offset = (page - 1) * limit
    pool = await get_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM auditoria_panel")
        rows = await conn.fetch(f"""
            SELECT a.id, a.accion, a.detalle, a.ip_address, a.creado_en, u.nombre, u.email, u.rol
            FROM auditoria_panel a
            LEFT JOIN usuarios_panel u ON u.id = a.usuario_id
            ORDER BY a.creado_en DESC LIMIT {limit} OFFSET {offset}
        """)
    return {
        "success": True, "registros": [row_to_dict(r) for r in rows],
        "total": total, "page": page, "pages": (total + limit - 1) // limit if total > 0 else 0,
    }

# ================================================================
# 🧠 ANÁLISIS DE EVIDENCIA CON IA (Claude Vision)
# ================================================================

async def analizar_imagen_con_ia(imagen_base64: str, media_type: str = "image/jpeg", contexto: dict = None) -> dict:
    """Envía imagen a Claude Vision y obtiene análisis de emergencia."""
    api_key = os.getenv('ANTHROPIC_API_KEY')
    if not api_key:
        return {"error": "ANTHROPIC_API_KEY no configurada"}
    
    ctx = contexto or {}
    tipo = ctx.get('tipo_alerta', 'desconocido')
    nivel = ctx.get('nivel_emergencia', 'desconocido')
    ubicacion = ctx.get('ubicacion', 'Bogotá, Colombia')
    mensaje = ctx.get('mensaje_usuario', '')
    
    system_prompt = """Eres un analista de emergencias de Bogotá, Colombia. 
Analizas imágenes de alertas ciudadanas para los operadores del 123.

REGLAS:
- Sé preciso y objetivo. No especules más allá de lo visible.
- Si la imagen es borrosa/oscura, dilo claramente.
- Si detectas posible contenido de abuso infantil (CSAM), marca contenido_sensible=true 
  y tipo_contenido_sensible="posible_csam" SIN describir la imagen.
- Si no parece emergencia (meme, paisaje, selfie), clasifica como "no_emergencia".
- Prioriza siempre la seguridad de las personas.

Responde ÚNICAMENTE en JSON válido, sin markdown."""

    user_prompt = f"""Analiza esta imagen de una alerta ciudadana.

CONTEXTO:
- Tipo reportado: {tipo}
- Nivel reportado: {nivel}
- Ubicación: {ubicacion}
- Mensaje: {mensaje or 'Sin mensaje'}

Responde SOLO con este JSON:
{{"clasificacion":"accidente_transito|robo_hurto|agresion_fisica|incendio|inundacion|dano_propiedad|persona_herida|persona_sospechosa|vehiculo_sospechoso|situacion_riesgo|caida_persona|emergencia_medica|no_emergencia|imagen_no_clara","urgencia":"critica|alta|media|baja|no_emergencia","descripcion":"máximo 2 líneas","objetos_detectados":"lista separada por coma","personas_detectadas":0,"hay_heridos":false,"hay_armas":false,"hay_fuego_humo":false,"hay_vehiculos":false,"hay_dano_propiedad":false,"accion_sugerida":"1-2 líneas para el operador del 123","despachar_ambulancia":false,"despachar_policia":false,"despachar_bomberos":false,"llamar_123":false,"confianza":0.0,"contenido_sensible":false,"tipo_contenido_sensible":null,"coincide_con_tipo_reportado":true,"nivel_sugerido":1}}"""

    inicio = time.time()
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post("https://api.anthropic.com/v1/messages",
                json={
                    'model': 'claude-sonnet-4-5-20250929', 'max_tokens': 1000,
                    'system': system_prompt,
                    'messages': [{'role': 'user', 'content': [
                        {'type': 'image', 'source': {'type': 'base64', 'media_type': media_type, 'data': imagen_base64}},
                        {'type': 'text', 'text': user_prompt}
                    ]}]
                },
                headers={'Content-Type': 'application/json', 'x-api-key': api_key, 'anthropic-version': '2023-06-01'},
                timeout=60)
            
            ms = round((time.time() - inicio) * 1000)
            if resp.status_code != 200:
                return {"error": f"Claude API HTTP {resp.status_code}", "tiempo_ms": ms}
            
            data = resp.json()
            text = data['content'][0]['text']
            tokens = data.get('usage', {}).get('input_tokens', 0) + data.get('usage', {}).get('output_tokens', 0)
            
            match = re.search(r'\{[\s\S]*\}', text)
            if match:
                analisis = json.loads(match.group())
                analisis['tokens_usados'] = tokens
                analisis['tiempo_analisis_ms'] = ms
                analisis['modelo_ia'] = 'claude-sonnet-4-5'
                return analisis
            return {"error": "No se pudo parsear respuesta", "tiempo_ms": ms}
    except httpx.TimeoutException:
        return {"error": "Timeout (60s)", "tiempo_ms": round((time.time() - inicio) * 1000)}
    except Exception as e:
        return {"error": str(e), "tiempo_ms": round((time.time() - inicio) * 1000)}


async def descargar_imagen_firebase(bucket_name: str, ruta_archivo: str) -> dict:
    """Descarga imagen de Firebase Storage y la convierte a base64."""
    try:
        from google.cloud import storage as gcs
        client = gcs.Client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(ruta_archivo)
        contenido = blob.download_as_bytes()
        
        content_type = blob.content_type or "image/jpeg"
        if ruta_archivo.lower().endswith('.png'): content_type = "image/png"
        elif ruta_archivo.lower().endswith('.webp'): content_type = "image/webp"
        
        return {"success": True, "base64": base64.b64encode(contenido).decode('utf-8'),
                "media_type": content_type, "tamano_bytes": len(contenido)}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def guardar_analisis(alerta_id: int, archivo_nombre: str, archivo_url: str, analisis: dict) -> int:
    """Guarda resultado de análisis de IA en la base de datos."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("""
            INSERT INTO analisis_evidencia (
                alerta_id, archivo_nombre, archivo_url,
                clasificacion, urgencia, descripcion, objetos_detectados,
                personas_detectadas, hay_heridos, hay_armas, hay_fuego_humo,
                hay_vehiculos, hay_dano_propiedad,
                accion_sugerida, despachar_ambulancia, despachar_policia, despachar_bomberos,
                llamar_123, confianza, modelo_ia, tokens_usados, tiempo_analisis_ms,
                contenido_sensible, tipo_contenido_sensible
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24)
            RETURNING id
        """, alerta_id, archivo_nombre, archivo_url,
            analisis.get('clasificacion', 'sin_clasificar'), analisis.get('urgencia', 'media'),
            analisis.get('descripcion', ''), analisis.get('objetos_detectados', ''),
            analisis.get('personas_detectadas', 0), analisis.get('hay_heridos', False),
            analisis.get('hay_armas', False), analisis.get('hay_fuego_humo', False),
            analisis.get('hay_vehiculos', False), analisis.get('hay_dano_propiedad', False),
            analisis.get('accion_sugerida', ''), analisis.get('despachar_ambulancia', False),
            analisis.get('despachar_policia', False), analisis.get('despachar_bomberos', False),
            analisis.get('llamar_123', False), analisis.get('confianza', 0.0),
            analisis.get('modelo_ia', 'claude-sonnet-4-5'), analisis.get('tokens_usados', 0),
            analisis.get('tiempo_analisis_ms', 0), analisis.get('contenido_sensible', False),
            analisis.get('tipo_contenido_sensible'))


# --- Endpoint: Analizar UNA imagen ---

@app.post("/evidencia/analizar")
async def analizar_evidencia(req: AnalizarEvidenciaRequest):
    """
    🧠 Analizar imagen con Claude Vision.
    Acepta base64 directo o ruta de Firebase Storage.
    """
    inicio = time.time()
    imagen_b64 = None
    media_type = req.media_type or "image/jpeg"
    archivo_nombre = req.imagen_nombre or "evidencia"
    archivo_url = ""
    
    if req.imagen_base64:
        imagen_b64 = req.imagen_base64
        if ',' in imagen_b64:
            parts = imagen_b64.split(',')
            imagen_b64 = parts[1]
            if 'png' in parts[0]: media_type = "image/png"
            elif 'webp' in parts[0]: media_type = "image/webp"
        log.info(f"🧠 Analizando imagen base64 para alerta #{req.alerta_id}")
    elif req.imagen_url:
        bucket_name = os.getenv('FIREBASE_STORAGE_BUCKET', '')
        if not bucket_name:
            raise HTTPException(500, "FIREBASE_STORAGE_BUCKET no configurado")
        resultado = await descargar_imagen_firebase(bucket_name, req.imagen_url)
        if not resultado.get('success'):
            raise HTTPException(502, f"Error descargando: {resultado.get('error')}")
        imagen_b64 = resultado['base64']
        media_type = resultado['media_type']
        archivo_url = req.imagen_url
        archivo_nombre = req.imagen_url.split('/')[-1] if '/' in req.imagen_url else req.imagen_url
    else:
        raise HTTPException(400, "Se requiere imagen_base64 o imagen_url")
    
    if len(imagen_b64) > 20_000_000:
        raise HTTPException(413, "Imagen muy grande (máximo ~15MB)")
    
    # Contexto
    contexto = {'tipo_alerta': req.tipo_alerta or 'desconocido',
                'nivel_emergencia': req.nivel_emergencia or 'desconocido',
                'ubicacion': req.ubicacion or 'Bogotá, Colombia',
                'mensaje_usuario': req.mensaje_usuario or ''}
    
    pool = await get_pool()
    async with pool.acquire() as conn:
        alerta = await conn.fetchrow("SELECT tipo_alerta, nivel_emergencia, mensaje FROM alertas_panico WHERE id=$1", req.alerta_id)
        if alerta:
            if not req.tipo_alerta: contexto['tipo_alerta'] = alerta['tipo_alerta'] or 'desconocido'
            if not req.nivel_emergencia: contexto['nivel_emergencia'] = alerta['nivel_emergencia'] or 'desconocido'
    
    analisis = await analizar_imagen_con_ia(imagen_b64, media_type, contexto)
    
    if 'error' in analisis:
        return {"success": False, "alerta_id": req.alerta_id, "error": analisis['error']}
    
    analisis_id = await guardar_analisis(req.alerta_id, archivo_nombre, archivo_url, analisis)
    ms = round((time.time() - inicio) * 1000)
    
    log.info(f"  ✅ Análisis #{analisis_id}: {analisis.get('clasificacion')} | urgencia={analisis.get('urgencia')}")
    if analisis.get('hay_armas'): log.warning(f"  🔴 ARMAS DETECTADAS alerta #{req.alerta_id}")
    if analisis.get('hay_heridos'): log.warning(f"  🔴 HERIDOS DETECTADOS alerta #{req.alerta_id}")
    
    return {
        "success": True, "alerta_id": req.alerta_id, "analisis_id": analisis_id,
        "clasificacion": analisis.get('clasificacion'), "urgencia": analisis.get('urgencia'),
        "descripcion": analisis.get('descripcion'), "accion_sugerida": analisis.get('accion_sugerida'),
        "despachar": {"ambulancia": analisis.get('despachar_ambulancia', False),
                      "policia": analisis.get('despachar_policia', False),
                      "bomberos": analisis.get('despachar_bomberos', False)},
        "deteccion": {"personas": analisis.get('personas_detectadas', 0),
                      "heridos": analisis.get('hay_heridos', False),
                      "armas": analisis.get('hay_armas', False),
                      "fuego_humo": analisis.get('hay_fuego_humo', False)},
        "confianza": analisis.get('confianza'),
        "contenido_sensible": analisis.get('contenido_sensible', False),
        "nivel_sugerido": analisis.get('nivel_sugerido'),
        "tiempo_total_ms": ms, "tokens_usados": analisis.get('tokens_usados', 0),
    }


# --- Endpoint: Analizar TODAS las imágenes de una alerta ---

@app.post("/evidencia/analizar-todas/{alerta_id}")
async def analizar_todas_evidencias(alerta_id: int, bg: BackgroundTasks):
    """Analiza TODAS las imágenes de Firebase Storage para una alerta (background)."""
    bucket_name = os.getenv('FIREBASE_STORAGE_BUCKET', '')
    if not bucket_name:
        raise HTTPException(500, "FIREBASE_STORAGE_BUCKET no configurado")
    
    pool = await get_pool()
    async with pool.acquire() as conn:
        alerta = await conn.fetchrow("SELECT id, tipo_alerta, nivel_emergencia FROM alertas_panico WHERE id=$1", alerta_id)
    if not alerta:
        raise HTTPException(404, "Alerta no encontrada")
    
    try:
        from google.cloud import storage as gcs
        client = gcs.Client()
        bucket = client.bucket(bucket_name)
        
        # Buscar en AMBAS rutas: emergencias/*/alert_{id}/ y alertas/{id}/
        imagenes = []
        
        # Ruta Flutter: emergencias/*/alert_{alerta_id}/
        for blob in bucket.list_blobs(prefix="emergencias/"):
            if f"alert_{alerta_id}/" in blob.name:
                if blob.content_type and 'image' in blob.content_type:
                    imagenes.append(blob)
        
        # Ruta legacy: alertas/{alerta_id}/
        for blob in bucket.list_blobs(prefix=f"alertas/{alerta_id}/"):
            if blob.content_type and 'image' in blob.content_type:
                imagenes.append(blob)
        
        if not imagenes:
            return {"success": True, "alerta_id": alerta_id, "imagenes": 0, "mensaje": "Sin imágenes para analizar"}
        
        log.info(f"🧠 Encontradas {len(imagenes)} imágenes para alerta #{alerta_id}")
        
        bg.add_task(_analizar_batch, alerta_id, bucket_name,
                    [b.name for b in imagenes], alerta['tipo_alerta'], alerta['nivel_emergencia'])
        
        return {"success": True, "alerta_id": alerta_id, "imagenes": len(imagenes),
                "mensaje": f"Análisis de {len(imagenes)} imágenes iniciado en background",
                "archivos": [b.name.split('/')[-1] for b in imagenes]}
    except ImportError:
        raise HTTPException(500, "google-cloud-storage no instalado. Agrega al requirements.txt")
    except Exception as e:
        raise HTTPException(502, f"Error Firebase Storage: {str(e)}")


async def _analizar_batch(alerta_id, bucket_name, rutas, tipo_alerta, nivel_emergencia):
    """Background: analiza múltiples imágenes."""
    import asyncio
    resultados = []
    for ruta in rutas:
        try:
            desc = await descargar_imagen_firebase(bucket_name, ruta)
            if not desc.get('success'): continue
            analisis = await analizar_imagen_con_ia(desc['base64'], desc['media_type'],
                {'tipo_alerta': tipo_alerta, 'nivel_emergencia': nivel_emergencia, 'ubicacion': 'Bogotá'})
            if 'error' not in analisis:
                aid = await guardar_analisis(alerta_id, ruta.split('/')[-1], ruta, analisis)
                resultados.append(analisis)
                log.info(f"  ✅ {ruta.split('/')[-1]}: {analisis.get('clasificacion')} ({analisis.get('urgencia')})")
            await asyncio.sleep(1)
        except Exception as e:
            log.error(f"  ❌ {ruta}: {e}")
    log.info(f"🧠 Batch alerta #{alerta_id}: {len(resultados)}/{len(rutas)} analizadas")


# --- Consultar análisis de una alerta ---

# --- Consultar análisis de una alerta ---

@app.get("/evidencia/listar/{alerta_id}")
async def listar_evidencias_firebase(alerta_id: int):
    """Lista archivos en Firebase Storage para una alerta (debug/test)."""
    bucket_name = os.getenv('FIREBASE_STORAGE_BUCKET', '')
    if not bucket_name:
        raise HTTPException(500, "FIREBASE_STORAGE_BUCKET no configurado")
    try:
        from google.cloud import storage as gcs
        client = gcs.Client()
        bucket = client.bucket(bucket_name)
        archivos = []
        # Buscar en emergencias/*/alert_{id}/
        for blob in bucket.list_blobs(prefix="emergencias/"):
            if f"alert_{alerta_id}/" in blob.name:
                archivos.append({"nombre": blob.name, "tipo": blob.content_type, "tamano": blob.size})
        # Buscar en alertas/{id}/
        for blob in bucket.list_blobs(prefix=f"alertas/{alerta_id}/"):
            archivos.append({"nombre": blob.name, "tipo": blob.content_type, "tamano": blob.size})
        return {"success": True, "alerta_id": alerta_id, "archivos": archivos, "total": len(archivos)}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/evidencia/analisis/{alerta_id}")
async def obtener_analisis(alerta_id: int):
    """Consultar todos los análisis de IA para una alerta."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, archivo_nombre, clasificacion, urgencia, descripcion,
                   personas_detectadas, hay_heridos, hay_armas, hay_fuego_humo,
                   accion_sugerida, despachar_ambulancia, despachar_policia, despachar_bomberos,
                   confianza, contenido_sensible, creado_en
            FROM analisis_evidencia WHERE alerta_id=$1 ORDER BY creado_en DESC
        """, alerta_id)
    
    analisis_list = [row_to_dict(r) for r in rows]
    resumen = {"total": len(analisis_list), "urgencia_maxima": "no_emergencia",
               "hay_heridos": False, "hay_armas": False}
    urgencias = {'critica': 5, 'alta': 4, 'media': 3, 'baja': 2, 'no_emergencia': 1}
    max_u = 0
    for a in analisis_list:
        u = urgencias.get(a.get('urgencia', ''), 0)
        if u > max_u: max_u = u; resumen['urgencia_maxima'] = a['urgencia']
        if a.get('hay_heridos'): resumen['hay_heridos'] = True
        if a.get('hay_armas'): resumen['hay_armas'] = True
    
    return {"success": True, "alerta_id": alerta_id, "resumen": resumen, "analisis": analisis_list}


# --- Panel: ver análisis ---

@app.get("/panel/analisis/{alerta_id}")
async def panel_analisis(alerta_id: int, request: Request):
    user = await _verificar_token_panel(request)
    await _auditar(user['usuario_id'], "ver_analisis_ia", f"alerta_id={alerta_id}")
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM analisis_evidencia WHERE alerta_id=$1 ORDER BY creado_en DESC", alerta_id)
    return {"success": True, "alerta_id": alerta_id, "analisis": [row_to_dict(r) for r in rows]}


# ================================================================
# 🔔 WEBHOOK: App Flutter notifica que subió evidencia
# ================================================================

class EvidenciaSubida(BaseModel):
    """La app Flutter llama este endpoint después de subir foto/video a Firebase Storage."""
    alerta_id: int
    celular: str
    ruta_firebase: str              # Ruta completa en Firebase: emergencias/2026-02-26/alert_28/foto.jpg
    tipo_archivo: str = "imagen"    # imagen, video
    nombre_archivo: Optional[str] = None
    media_type: Optional[str] = "image/jpeg"

@app.post("/evidencia/notificar")
async def notificar_evidencia_subida(req: EvidenciaSubida, bg: BackgroundTasks):
    """
    🔔 WEBHOOK AUTOMÁTICO
    
    La app Flutter llama este endpoint DESPUÉS de subir una foto/video a Firebase Storage.
    Si es imagen → lanza análisis con Claude Vision automáticamente en background.
    Si es video → registra pero no analiza (Claude no procesa video).
    
    Uso desde Flutter:
        await http.post('/evidencia/notificar', body: {
            "alerta_id": 28,
            "celular": "573001234567",
            "ruta_firebase": "emergencias/2026-02-26/alert_28/foto1.jpg",
            "tipo_archivo": "imagen"
        });
    """
    log.info(f"📸 Evidencia subida: alerta #{req.alerta_id} → {req.ruta_firebase} ({req.tipo_archivo})")
    
    pool = await get_pool()
    
    # Registrar en BD que se recibió evidencia
    async with pool.acquire() as conn:
        # Verificar que la alerta existe
        alerta = await conn.fetchrow(
            "SELECT id, tipo_alerta, nivel_emergencia, nombre FROM alertas_panico WHERE id=$1",
            req.alerta_id)
        if not alerta:
            raise HTTPException(404, "Alerta no encontrada")
    
    resultado = {
        "success": True,
        "alerta_id": req.alerta_id,
        "ruta": req.ruta_firebase,
        "tipo": req.tipo_archivo,
        "analisis_iniciado": False,
    }
    
    # Si es imagen → analizar automáticamente con IA en background
    if req.tipo_archivo == "imagen" and req.ruta_firebase.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
        bucket_name = os.getenv('FIREBASE_STORAGE_BUCKET', '')
        if bucket_name and os.getenv('ANTHROPIC_API_KEY'):
            bg.add_task(
                _analizar_evidencia_auto,
                req.alerta_id,
                bucket_name,
                req.ruta_firebase,
                alerta['tipo_alerta'],
                alerta['nivel_emergencia']
            )
            resultado['analisis_iniciado'] = True
            resultado['mensaje'] = "Imagen recibida. Análisis IA iniciado automáticamente."
            log.info(f"  🧠 Análisis IA iniciado en background para {req.ruta_firebase}")
        else:
            resultado['mensaje'] = "Imagen recibida. Análisis IA no disponible (falta configuración)."
    elif req.tipo_archivo == "video":
        resultado['mensaje'] = "Video recibido. Los videos se almacenan como evidencia pero no se analizan con IA."
    else:
        resultado['mensaje'] = "Archivo recibido y registrado."
    
    return resultado


async def _analizar_evidencia_auto(alerta_id: int, bucket_name: str, ruta: str,
                                    tipo_alerta: str, nivel_emergencia: int):
    """Background: descarga imagen de Firebase y la analiza con Claude Vision."""
    try:
        # Descargar imagen
        desc = await descargar_imagen_firebase(bucket_name, ruta)
        if not desc.get('success'):
            log.warning(f"  ❌ No se pudo descargar {ruta}: {desc.get('error')}")
            return
        
        # Analizar con IA
        contexto = {
            'tipo_alerta': tipo_alerta or 'desconocido',
            'nivel_emergencia': nivel_emergencia or 2,
            'ubicacion': 'Bogotá, Colombia',
        }
        
        analisis = await analizar_imagen_con_ia(desc['base64'], desc['media_type'], contexto)
        
        if 'error' not in analisis:
            analisis_id = await guardar_analisis(alerta_id, ruta.split('/')[-1], ruta, analisis)
            log.info(f"  ✅ AUTO-ANÁLISIS #{analisis_id}: {analisis.get('clasificacion')} | urgencia={analisis.get('urgencia')} | confianza={analisis.get('confianza')}")
            
            # Si urgencia crítica o alta → actualizar alerta con nota de IA
            if analisis.get('urgencia') in ('critica', 'alta'):
                pool = await get_pool()
                async with pool.acquire() as conn:
                    await conn.execute("""
                        UPDATE alertas_panico 
                        SET mensaje = mensaje || $1
                        WHERE id = $2
                    """, f" [🧠 IA: {analisis.get('clasificacion')} - {analisis.get('urgencia')}]", alerta_id)
                log.warning(f"  🔴 URGENCIA {analisis.get('urgencia').upper()} detectada por IA en alerta #{alerta_id}")
        else:
            log.warning(f"  ❌ Error auto-análisis: {analisis['error']}")
    
    except Exception as e:
        log.error(f"  ❌ Error en auto-análisis de {ruta}: {e}")


# ================================================================
# 📊 PANEL: Dashboard de análisis IA (estadísticas)
# ================================================================

@app.get("/panel/analisis-stats")
async def panel_analisis_stats(request: Request):
    """Estadísticas globales de análisis de IA (solo admin)."""
    user = await _verificar_token_panel(request)
    if user['rol'] != 'admin':
        raise HTTPException(403, "Solo admin")
    pool = await get_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM analisis_evidencia") or 0
        por_clasificacion = await conn.fetch("SELECT clasificacion, COUNT(*) as total FROM analisis_evidencia GROUP BY clasificacion ORDER BY total DESC")
        por_urgencia = await conn.fetch("SELECT urgencia, COUNT(*) as total FROM analisis_evidencia GROUP BY urgencia ORDER BY total DESC")
        con_armas = await conn.fetchval("SELECT COUNT(*) FROM analisis_evidencia WHERE hay_armas = TRUE") or 0
        con_heridos = await conn.fetchval("SELECT COUNT(*) FROM analisis_evidencia WHERE hay_heridos = TRUE") or 0
        promedio_confianza = await conn.fetchval("SELECT AVG(confianza) FROM analisis_evidencia")
        promedio_tiempo = await conn.fetchval("SELECT AVG(tiempo_analisis_ms) FROM analisis_evidencia")
        pendientes = await conn.fetchval("SELECT COUNT(*) FROM analisis_evidencia WHERE estado_revision = 'pendiente'") or 0
        confirmados = await conn.fetchval("SELECT COUNT(*) FROM analisis_evidencia WHERE estado_revision = 'confirmado'") or 0
        corregidos = await conn.fetchval("SELECT COUNT(*) FROM analisis_evidencia WHERE estado_revision = 'corregido'") or 0
    return {
        "success": True,
        "stats": {
            "total_analisis": total,
            "por_clasificacion": [dict(r) for r in por_clasificacion],
            "por_urgencia": [dict(r) for r in por_urgencia],
            "con_armas": con_armas, "con_heridos": con_heridos,
            "promedio_confianza": round(float(promedio_confianza or 0), 2),
            "promedio_tiempo_ms": round(float(promedio_tiempo or 0)),
            "revision": {"pendientes": pendientes, "confirmados": confirmados, "corregidos": corregidos},
        }
    }


# ================================================================
# 👁️ REVISIÓN HUMANA: Confirmar, corregir o escalar análisis de IA
# ================================================================

class RevisionAnalisis(BaseModel):
    """Revisión humana de un análisis de IA."""
    accion: str                                # "confirmar", "corregir", "escalar", "descartar"
    clasificacion: Optional[str] = None        # Solo si corrige
    urgencia: Optional[str] = None             # Solo si corrige
    accion_sugerida: Optional[str] = None      # Solo si corrige
    despachar_ambulancia: Optional[bool] = None
    despachar_policia: Optional[bool] = None
    despachar_bomberos: Optional[bool] = None
    notas: Optional[str] = None                # Notas del revisor


@app.get("/panel/revisiones-pendientes")
async def panel_revisiones_pendientes(request: Request, limit: int = 20):
    """Lista análisis de IA pendientes de revisión humana."""
    user = await _verificar_token_panel(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT ae.id, ae.alerta_id, ae.archivo_nombre, ae.clasificacion, ae.urgencia,
                   ae.descripcion, ae.accion_sugerida, ae.confianza,
                   ae.hay_heridos, ae.hay_armas, ae.hay_fuego_humo,
                   ae.despachar_ambulancia, ae.despachar_policia, ae.despachar_bomberos,
                   ae.personas_detectadas, ae.contenido_sensible, ae.estado_revision,
                   ae.creado_en,
                   ap.nombre, ap.celular, ap.tipo_alerta as alerta_tipo, ap.nivel_emergencia,
                   ap.latitud, ap.longitud
            FROM analisis_evidencia ae
            LEFT JOIN alertas_panico ap ON ae.alerta_id = ap.id
            WHERE ae.estado_revision = 'pendiente'
            ORDER BY 
                CASE ae.urgencia 
                    WHEN 'critica' THEN 1 WHEN 'alta' THEN 2 
                    WHEN 'media' THEN 3 WHEN 'baja' THEN 4 ELSE 5 
                END,
                ae.creado_en DESC
            LIMIT $1
        """, limit)
    return {
        "success": True,
        "pendientes": [row_to_dict(r) for r in rows],
        "total": len(rows),
    }


@app.put("/panel/analisis/{analisis_id}/revisar")
async def panel_revisar_analisis(analisis_id: int, req: RevisionAnalisis, request: Request):
    """
    👁️ REVISIÓN HUMANA de un análisis de IA.
    
    Acciones:
    - confirmar: La IA acertó, se confirma el análisis tal cual
    - corregir: La IA se equivocó, el humano corrige clasificación/urgencia/acciones
    - escalar: Requiere atención superior, se marca para supervisor
    - descartar: Falsa alarma o imagen irrelevante
    """
    user = await _verificar_token_panel(request)
    
    if req.accion not in ('confirmar', 'corregir', 'escalar', 'descartar'):
        raise HTTPException(400, "Acción debe ser: confirmar, corregir, escalar, descartar")
    
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Verificar que el análisis existe
        analisis = await conn.fetchrow("SELECT * FROM analisis_evidencia WHERE id=$1", analisis_id)
        if not analisis:
            raise HTTPException(404, "Análisis no encontrado")
        
        if req.accion == 'confirmar':
            await conn.execute("""
                UPDATE analisis_evidencia SET
                    estado_revision = 'confirmado',
                    revision_clasificacion = clasificacion,
                    revision_urgencia = urgencia,
                    revision_accion = accion_sugerida,
                    revision_despachar_ambulancia = despachar_ambulancia,
                    revision_despachar_policia = despachar_policia,
                    revision_despachar_bomberos = despachar_bomberos,
                    revision_notas = $1,
                    revisado_por = $2,
                    revisado_nombre = $3,
                    revisado_en = NOW()
                WHERE id = $4
            """, req.notas, user['usuario_id'], user['nombre'], analisis_id)
        
        elif req.accion == 'corregir':
            await conn.execute("""
                UPDATE analisis_evidencia SET
                    estado_revision = 'corregido',
                    revision_clasificacion = COALESCE($1, clasificacion),
                    revision_urgencia = COALESCE($2, urgencia),
                    revision_accion = COALESCE($3, accion_sugerida),
                    revision_despachar_ambulancia = COALESCE($4, despachar_ambulancia),
                    revision_despachar_policia = COALESCE($5, despachar_policia),
                    revision_despachar_bomberos = COALESCE($6, despachar_bomberos),
                    revision_notas = $7,
                    revisado_por = $8,
                    revisado_nombre = $9,
                    revisado_en = NOW()
                WHERE id = $10
            """, req.clasificacion, req.urgencia, req.accion_sugerida,
                req.despachar_ambulancia, req.despachar_policia, req.despachar_bomberos,
                req.notas, user['usuario_id'], user['nombre'], analisis_id)
        
        elif req.accion == 'escalar':
            await conn.execute("""
                UPDATE analisis_evidencia SET
                    estado_revision = 'escalado',
                    revision_notas = $1,
                    revisado_por = $2,
                    revisado_nombre = $3,
                    revisado_en = NOW()
                WHERE id = $4
            """, req.notas or 'Escalado a supervisor', user['usuario_id'], user['nombre'], analisis_id)
        
        elif req.accion == 'descartar':
            await conn.execute("""
                UPDATE analisis_evidencia SET
                    estado_revision = 'descartado',
                    revision_urgencia = 'no_emergencia',
                    revision_notas = $1,
                    revisado_por = $2,
                    revisado_nombre = $3,
                    revisado_en = NOW()
                WHERE id = $4
            """, req.notas or 'Descartado - falsa alarma', user['usuario_id'], user['nombre'], analisis_id)
    
    await _auditar(user['usuario_id'], f"revision_ia_{req.accion}", 
                   f"analisis_id={analisis_id}, alerta_id={analisis['alerta_id']}")
    
    log.info(f"👁️ Análisis #{analisis_id} → {req.accion.upper()} por {user['nombre']}")
    
    return {
        "success": True,
        "analisis_id": analisis_id,
        "accion": req.accion,
        "revisado_por": user['nombre'],
        "mensaje": f"Análisis {req.accion} exitosamente",
    }


# ==================== DEBUG (temporal) ====================

@app.get("/debug/alerta/{alerta_id}")
async def debug_alerta(alerta_id: int):
    """Diagnóstico completo de una alerta — TEMPORAL, quitar en producción."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        alerta = await conn.fetchrow("SELECT * FROM alertas_panico WHERE id=$1", alerta_id)
        if not alerta:
            return {"error": "Alerta no encontrada"}
        
        cel = alerta['celular']
        cs, cc = normalizar_celular(cel)
        
        # Cuidadores registrados
        cuidadores = await conn.fetch("""
            SELECT cc.celular, cc.nombre FROM contactos_confianza cc
            INNER JOIN usuarios_sos u ON u.id = cc.usuario_id
            WHERE u.celular IN ($1,$2) AND cc.activo=TRUE
        """, cs, cc)
        
        cuidadores2 = await conn.fetch(
            "SELECT celular_cuidador FROM cuidadores_autorizados WHERE celular_cuidado IN ($1,$2)", cs, cc)
        
        # Tokens FCM
        token_usuario = await conn.fetchrow(
            "SELECT token, valido, actualizado FROM tokens_fcm WHERE celular IN ($1,$2) ORDER BY actualizado DESC LIMIT 1", cs, cc)
        
        # Envíos realizados
        envios = await conn.fetch(
            "SELECT celular_cuidador_institucional, nombre_cuidador_institucional, estado_envio, rol_destinatario FROM alertas_enviadas WHERE alerta_id=$1", alerta_id)
        
        # Respuestas
        respuestas = await conn.fetch(
            "SELECT celular, nombre, entidad, estado FROM respuestas_institucionales WHERE alerta_id=$1", alerta_id)
        
        return {
            "alerta": row_to_dict(alerta),
            "celular_usuario": {"sin_prefijo": cs, "con_prefijo": cc},
            "cuidadores_contactos_confianza": [dict(r) for r in cuidadores],
            "cuidadores_autorizados": [dict(r) for r in cuidadores2],
            "total_cuidadores": len(cuidadores) + len(cuidadores2),
            "token_fcm_usuario": row_to_dict(token_usuario) if token_usuario else None,
            "envios_realizados": [dict(r) for r in envios],
            "respuestas": [dict(r) for r in respuestas],
        }

# ==================== HEALTH ====================

@app.get("/health")
async def health():
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"status": "ok", "db": "postgresql", "version": "3.1.0", "timestamp": datetime.now().isoformat()}
    except:
        return {"status": "degraded", "db": "disconnected"}

@app.get("/")
async def root():
    return {"app": "🆘 Ami SOS", "version": "3.1.0", "features": [
        "3 alert levels", "community network", "BLE relay",
        "admin panel", "role-based access", "audit trail"
    ], "docs": "/docs", "panel": "/panel-app/"}

# ==================== SERVIR PANEL WEB ====================

try:
    app.mount("/panel-app", StaticFiles(directory="panel", html=True), name="panel")
    log.info("✅ Panel web montado en /panel-app/")
except Exception:
    log.warning("⚠️ Carpeta 'panel/' no encontrada. Panel web no disponible.")