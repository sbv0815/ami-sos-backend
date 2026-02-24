"""
🆘 Migración v3 — Reportes + Ciudad obligatoria
Ejecutar: python migrar_v3.py
"""

import psycopg2
import os
import sys

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    print("❌ ERROR: Variable DATABASE_URL no está definida.")
    sys.exit(1)

def main():
    print("🆘 Ami SOS — Migración v3...\n")
    
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    cur = conn.cursor()
    print("✅ Conectado\n")
    
    migraciones = [
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
        ("Índice reportes por reportado",
         "CREATE INDEX IF NOT EXISTS idx_reportes_reportado ON reportes_usuario(celular_reportado)"),
        ("Columna bloqueado en usuarios_sos",
         "ALTER TABLE usuarios_sos ADD COLUMN IF NOT EXISTS bloqueado BOOLEAN DEFAULT FALSE"),
        ("Columna motivo_bloqueo en usuarios_sos",
         "ALTER TABLE usuarios_sos ADD COLUMN IF NOT EXISTS motivo_bloqueo VARCHAR(100)"),
        ("Columna fecha_bloqueo en usuarios_sos",
         "ALTER TABLE usuarios_sos ADD COLUMN IF NOT EXISTS fecha_bloqueo TIMESTAMP"),
        ("Columna country_code en usuarios_sos",
         "ALTER TABLE usuarios_sos ADD COLUMN IF NOT EXISTS country_code VARCHAR(5) DEFAULT 'CO'"),
    ]
    
    for nombre, sql in migraciones:
        try:
            cur.execute(sql)
            print(f"  ✅ {nombre}")
        except Exception as e:
            print(f"  ⚠️ {nombre}: {e}")
    
    # Verificar
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='usuarios_sos' AND column_name='bloqueado'")
    if cur.fetchone():
        print("\n  ✅ usuarios_sos.bloqueado existe")
    
    cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename='reportes_usuario'")
    if cur.fetchone():
        print("  ✅ tabla reportes_usuario existe")
    
    print("\n🎉 Migración v3 completa")
    
    cur.close()
    conn.close()

if __name__ == "__main__":
    main()