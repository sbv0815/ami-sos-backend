"""
🆘 Migración v2 — Red comunitaria + 3 niveles
Ejecutar: python migrar_v2.py
"""

import psycopg2
import os
import sys

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    print("❌ ERROR: Variable DATABASE_URL no está definida.")
    sys.exit(1)

def main():
    print("🆘 Ami SOS — Migración v2...\n")
    
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    cur = conn.cursor()
    print("✅ Conectado\n")
    
    migraciones = [
        ("Tabla ubicaciones_red", """
            CREATE TABLE IF NOT EXISTS ubicaciones_red (
                id SERIAL PRIMARY KEY,
                celular VARCHAR(20) NOT NULL UNIQUE,
                id_persona INTEGER,
                nombre VARCHAR(100),
                latitud DECIMAL(10,7) NOT NULL,
                longitud DECIMAL(10,7) NOT NULL,
                disponible BOOLEAN DEFAULT TRUE,
                actualizado_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """),
        ("Índice ubicaciones_red GPS", 
         "CREATE INDEX IF NOT EXISTS idx_red_geo ON ubicaciones_red(latitud, longitud)"),
        ("Índice ubicaciones_red disponible", 
         "CREATE INDEX IF NOT EXISTS idx_red_disponible ON ubicaciones_red(disponible, actualizado_at)"),
        ("Columna nivel_emergencia en alertas_panico",
         "ALTER TABLE alertas_panico ADD COLUMN IF NOT EXISTS nivel_emergencia INTEGER DEFAULT 2"),
        ("Columna disponible_red en usuarios_sos",
         "ALTER TABLE usuarios_sos ADD COLUMN IF NOT EXISTS disponible_red BOOLEAN DEFAULT TRUE"),
    ]
    
    for nombre, sql in migraciones:
        try:
            cur.execute(sql)
            print(f"  ✅ {nombre}")
        except Exception as e:
            print(f"  ⚠️ {nombre}: {e}")
    
    # Verificar
    cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
    tablas = [r[0] for r in cur.fetchall()]
    print(f"\n📋 Tablas totales: {len(tablas)}")
    
    tablas_sos = ['usuarios_sos','contactos_confianza','cuidadores_institucionales',
                  'tokens_fcm','alertas_panico','alertas_enviadas',
                  'respuestas_institucionales','cuidadores_autorizados',
                  'dispositivos_ble','ubicaciones_red']
    
    creadas = [t for t in tablas_sos if t in tablas]
    print(f"🆘 Ami SOS: {len(creadas)}/{len(tablas_sos)} tablas")
    
    # Verificar columnas nuevas
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='alertas_panico' AND column_name='nivel_emergencia'")
    if cur.fetchone():
        print("  ✅ alertas_panico.nivel_emergencia existe")
    
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='usuarios_sos' AND column_name='disponible_red'")
    if cur.fetchone():
        print("  ✅ usuarios_sos.disponible_red existe")
    
    print("\n🎉 Migración v2 completa")
    
    cur.close()
    conn.close()

if __name__ == "__main__":
    main()