"""
🆘 Migración v4 — Vigilancia Preventiva Comunitaria
Ejecutar: python migrar_v4.py
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
    print("🆘 Ami SOS — Migración v4: Vigilancia Preventiva\n")
    
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    cur = conn.cursor()
    print("✅ Conectado\n")
    
    migraciones = [
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
        ("Índice vigilancias activas",
         "CREATE INDEX IF NOT EXISTS idx_vigilancias_activas ON vigilancias(estado, fecha)"),
        ("Índice vigilancias geo",
         "CREATE INDEX IF NOT EXISTS idx_vigilancias_geo ON vigilancias(latitud, longitud)"),
    ]
    
    for nombre, sql in migraciones:
        try:
            cur.execute(sql)
            print(f"  ✅ {nombre}")
        except Exception as e:
            print(f"  ⚠️ {nombre}: {e}")
    
    print("\n🎉 Migración v4 completa")
    cur.close()
    conn.close()

if __name__ == "__main__":
    main()