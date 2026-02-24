"""
🆘 AMI SOS — Migración COMPLETA
Crea TODAS las tablas necesarias en una BD nueva desde cero.

Uso:
  PowerShell: $env:DATABASE_URL = "postgresql://..."
  Luego:      python migrar_completa.py
"""

import psycopg2
import os
import sys

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    print("❌ ERROR: Variable DATABASE_URL no está definida.")
    print('En PowerShell: $env:DATABASE_URL = "postgresql://usuario:password@host/db?sslmode=require"')
    sys.exit(1)

def main():
    print("🆘 AMI SOS — Migración Completa\n")
    print(f"   BD: {DATABASE_URL[:50]}...\n")
    
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    cur = conn.cursor()
    print("✅ Conectado a PostgreSQL\n")
    
    migraciones = [
        # ========== USUARIOS ==========
        ("Tabla usuarios_sos", """
            CREATE TABLE IF NOT EXISTS usuarios_sos (
                id SERIAL PRIMARY KEY,
                nombre VARCHAR(100) NOT NULL,
                apellido VARCHAR(100),
                celular VARCHAR(20) UNIQUE NOT NULL,
                correo VARCHAR(200),
                fecha_nacimiento DATE,
                genero VARCHAR(20) DEFAULT 'masculino',
                condiciones_salud TEXT,
                medicamentos TEXT,
                alergias TEXT,
                ciudad VARCHAR(100),
                country_code VARCHAR(5) DEFAULT 'CO',
                password_hash VARCHAR(255),
                disponible_red BOOLEAN DEFAULT TRUE,
                fcm_token TEXT,
                bloqueado BOOLEAN DEFAULT FALSE,
                motivo_bloqueo VARCHAR(100),
                fecha_bloqueo TIMESTAMP,
                fecha_registro TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """),
        ("Índice usuarios celular",
         "CREATE INDEX IF NOT EXISTS idx_usuarios_celular ON usuarios_sos(celular)"),

        # ========== CONTACTOS DE CONFIANZA ==========
        ("Tabla contactos_confianza", """
            CREATE TABLE IF NOT EXISTS contactos_confianza (
                id SERIAL PRIMARY KEY,
                usuario_id INT NOT NULL REFERENCES usuarios_sos(id),
                nombre VARCHAR(100) NOT NULL,
                celular VARCHAR(20) NOT NULL,
                parentesco VARCHAR(50),
                disponible_emergencias BOOLEAN DEFAULT TRUE,
                activo BOOLEAN DEFAULT TRUE,
                fecha_registro TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """),
        ("Índice contactos usuario",
         "CREATE INDEX IF NOT EXISTS idx_contactos_usuario ON contactos_confianza(usuario_id)"),

        # ========== CUIDADORES AUTORIZADOS ==========
        ("Tabla cuidadores_autorizados", """
            CREATE TABLE IF NOT EXISTS cuidadores_autorizados (
                id SERIAL PRIMARY KEY,
                celular_cuidado VARCHAR(20) NOT NULL,
                celular_cuidador VARCHAR(20) NOT NULL,
                id_persona_cuidador INT,
                nombre_cuidador VARCHAR(100),
                activo BOOLEAN DEFAULT TRUE,
                fecha_registro TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """),
        ("Índice cuidadores_autorizados",
         "CREATE INDEX IF NOT EXISTS idx_cuidadores_cuidado ON cuidadores_autorizados(celular_cuidado)"),

        # ========== CUIDADORES INSTITUCIONALES ==========
        ("Tabla cuidadores_institucionales", """
            CREATE TABLE IF NOT EXISTS cuidadores_institucionales (
                id SERIAL PRIMARY KEY,
                nombre VARCHAR(100) NOT NULL,
                entidad VARCHAR(100),
                celular VARCHAR(20) NOT NULL,
                tipo VARCHAR(30) DEFAULT 'policia',
                id_persona INT,
                latitud DECIMAL(10,7),
                longitud DECIMAL(10,7),
                activo BOOLEAN DEFAULT TRUE,
                fecha_registro TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """),
        ("Índice institucionales activos",
         "CREATE INDEX IF NOT EXISTS idx_institucionales_activos ON cuidadores_institucionales(activo)"),

        # ========== ALERTAS ==========
        ("Tabla alertas_panico", """
            CREATE TABLE IF NOT EXISTS alertas_panico (
                id SERIAL PRIMARY KEY,
                nombre VARCHAR(100),
                mensaje TEXT,
                fecha_hora TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                celular VARCHAR(20) NOT NULL,
                atendida VARCHAR(10) DEFAULT 'no',
                id_persona INT,
                rol VARCHAR(20) DEFAULT 'usuario',
                tipo_alerta VARCHAR(50) DEFAULT 'emergencia',
                latitud DECIMAL(10,7),
                longitud DECIMAL(10,7),
                nivel_alerta VARCHAR(20) DEFAULT 'critica',
                nivel_emergencia INT DEFAULT 2,
                receptor_destino VARCHAR(30) DEFAULT 'cuidador',
                fuente_alerta VARCHAR(30) DEFAULT 'manual',
                bateria_dispositivo INT,
                estado VARCHAR(20) DEFAULT 'activa',
                fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """),
        ("Índice alertas celular",
         "CREATE INDEX IF NOT EXISTS idx_alertas_celular ON alertas_panico(celular)"),
        ("Índice alertas fecha",
         "CREATE INDEX IF NOT EXISTS idx_alertas_fecha ON alertas_panico(fecha_hora DESC)"),

        # ========== ALERTAS ENVIADAS ==========
        ("Tabla alertas_enviadas", """
            CREATE TABLE IF NOT EXISTS alertas_enviadas (
                id SERIAL PRIMARY KEY,
                alerta_id INT NOT NULL REFERENCES alertas_panico(id),
                celular_usuario VARCHAR(20),
                nombre_usuario VARCHAR(100),
                celular_cuidador_institucional VARCHAR(20),
                nombre_cuidador_institucional VARCHAR(100),
                token TEXT,
                mensaje TEXT,
                fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                estado_envio VARCHAR(20) DEFAULT 'pendiente',
                rol_destinatario VARCHAR(30),
                receptor_destino VARCHAR(30)
            )
        """),
        ("Índice alertas_enviadas alerta",
         "CREATE INDEX IF NOT EXISTS idx_enviadas_alerta ON alertas_enviadas(alerta_id)"),

        # ========== RESPUESTAS INSTITUCIONALES ==========
        ("Tabla respuestas_institucionales", """
            CREATE TABLE IF NOT EXISTS respuestas_institucionales (
                id SERIAL PRIMARY KEY,
                alerta_id INT NOT NULL REFERENCES alertas_panico(id),
                id_persona INT,
                celular VARCHAR(20),
                entidad VARCHAR(100),
                nombre VARCHAR(100),
                latitud DECIMAL(10,7),
                longitud DECIMAL(10,7),
                tiempo_estimado_min INT,
                estado VARCHAR(50) DEFAULT 'voy_en_camino',
                fecha_respuesta TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """),
        ("Índice respuestas alerta",
         "CREATE INDEX IF NOT EXISTS idx_respuestas_alerta ON respuestas_institucionales(alerta_id)"),

        # ========== TOKENS FCM ==========
        ("Tabla tokens_fcm", """
            CREATE TABLE IF NOT EXISTS tokens_fcm (
                id SERIAL PRIMARY KEY,
                id_persona INT,
                celular VARCHAR(20) NOT NULL,
                token TEXT NOT NULL,
                valido BOOLEAN DEFAULT TRUE,
                fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                rol VARCHAR(20) DEFAULT 'usuario',
                dispositivo_id VARCHAR(100),
                motivo_invalidez VARCHAR(50),
                fecha_invalido TIMESTAMP
            )
        """),
        ("Índice tokens celular",
         "CREATE INDEX IF NOT EXISTS idx_tokens_celular ON tokens_fcm(celular, valido)"),

        # ========== UBICACIONES RED COMUNITARIA ==========
        ("Tabla ubicaciones_red", """
            CREATE TABLE IF NOT EXISTS ubicaciones_red (
                id SERIAL PRIMARY KEY,
                celular VARCHAR(20) NOT NULL,
                id_persona INT,
                nombre VARCHAR(100),
                latitud DECIMAL(10,7),
                longitud DECIMAL(10,7),
                disponible BOOLEAN DEFAULT TRUE,
                actualizado_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """),
        ("Índice ubicaciones celular",
         "CREATE UNIQUE INDEX IF NOT EXISTS idx_ubicaciones_celular ON ubicaciones_red(celular)"),
        ("Índice ubicaciones disponible",
         "CREATE INDEX IF NOT EXISTS idx_ubicaciones_disponible ON ubicaciones_red(disponible, actualizado_at)"),

        # ========== DISPOSITIVOS BLE ==========
        ("Tabla dispositivos_ble", """
            CREATE TABLE IF NOT EXISTS dispositivos_ble (
                id SERIAL PRIMARY KEY,
                usuario_id INT NOT NULL REFERENCES usuarios_sos(id),
                mac_address VARCHAR(20) UNIQUE NOT NULL,
                nombre_dispositivo VARCHAR(100),
                tipo VARCHAR(30) DEFAULT 'manilla',
                activo BOOLEAN DEFAULT TRUE,
                fecha_registro TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """),
        ("Índice dispositivos mac",
         "CREATE INDEX IF NOT EXISTS idx_dispositivos_mac ON dispositivos_ble(mac_address, activo)"),

        # ========== REPORTES USUARIO ==========
        ("Tabla reportes_usuario", """
            CREATE TABLE IF NOT EXISTS reportes_usuario (
                id SERIAL PRIMARY KEY,
                celular_reportado VARCHAR(20) NOT NULL,
                celular_reporta VARCHAR(20) NOT NULL,
                motivo VARCHAR(50) DEFAULT 'comportamiento',
                descripcion TEXT,
                fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(celular_reportado, celular_reporta)
            )
        """),
        ("Índice reportes reportado",
         "CREATE INDEX IF NOT EXISTS idx_reportes_reportado ON reportes_usuario(celular_reportado)"),

        # ========== VIGILANCIAS ==========
        ("Tabla vigilancias", """
            CREATE TABLE IF NOT EXISTS vigilancias (
                id SERIAL PRIMARY KEY,
                celular VARCHAR(20) NOT NULL,
                nombre VARCHAR(100),
                descripcion TEXT NOT NULL,
                tipo_sospecha VARCHAR(30) DEFAULT 'general',
                latitud DECIMAL(10,7) NOT NULL,
                longitud DECIMAL(10,7) NOT NULL,
                estado VARCHAR(20) DEFAULT 'activa',
                confirmaciones INT DEFAULT 0,
                rechazos INT DEFAULT 0,
                escalada BOOLEAN DEFAULT FALSE,
                alerta_id INT,
                fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                fecha_cierre TIMESTAMP
            )
        """),
        ("Índice vigilancias activas",
         "CREATE INDEX IF NOT EXISTS idx_vigilancias_activas ON vigilancias(estado, fecha)"),
        ("Índice vigilancias geo",
         "CREATE INDEX IF NOT EXISTS idx_vigilancias_geo ON vigilancias(latitud, longitud)"),

        # ========== CONFIRMACIONES VIGILANCIA ==========
        ("Tabla confirmaciones_vigilancia", """
            CREATE TABLE IF NOT EXISTS confirmaciones_vigilancia (
                id SERIAL PRIMARY KEY,
                vigilancia_id INT NOT NULL REFERENCES vigilancias(id),
                celular VARCHAR(20) NOT NULL,
                confirma BOOLEAN NOT NULL,
                comentario TEXT,
                latitud DECIMAL(10,7),
                longitud DECIMAL(10,7),
                fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(vigilancia_id, celular)
            )
        """),
    ]
    
    exitosas = 0
    errores = 0
    
    for nombre, sql in migraciones:
        try:
            cur.execute(sql)
            print(f"  ✅ {nombre}")
            exitosas += 1
        except Exception as e:
            print(f"  ❌ {nombre}: {e}")
            errores += 1
    
    # Verificar tablas creadas
    cur.execute("""
        SELECT table_name FROM information_schema.tables 
        WHERE table_schema = 'public' ORDER BY table_name
    """)
    tablas = [r[0] for r in cur.fetchall()]
    
    print(f"\n📊 Resultado: {exitosas} exitosas, {errores} errores")
    print(f"📋 Tablas en BD: {', '.join(tablas)}")
    print(f"\n🎉 Migración completa terminada")
    
    cur.close()
    conn.close()

if __name__ == "__main__":
    main()