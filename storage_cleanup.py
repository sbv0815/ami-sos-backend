"""
🆘 Ami SOS — Limpieza de Firebase Storage
Ejecutar diariamente via cron (3:00 AM UTC)

Política:
- emergencias/  → NUNCA borrar (evidencia legal)
- vigilancia/   → Borrar después de 30 días
- temp/         → Borrar después de 7 días
- evidencias/   → Legacy, migrar a emergencias/ y borrar después de 30 días

Uso:
  python storage_cleanup.py              # Dry run (solo muestra qué borraría)
  python storage_cleanup.py --execute    # Ejecutar borrado real
"""

import firebase_admin
from firebase_admin import credentials, storage
from datetime import datetime, timedelta
import sys

# Configuración
BUCKET_NAME = 'ami-sos.firebasestorage.app'

RETENTION_DAYS = {
    'vigilancia': 30,
    'temp': 7,
    'evidencias': 30,   # Legacy — migrar y limpiar
    # 'emergencias': NUNCA borrar
}

# Carpetas protegidas — NUNCA borrar
PROTECTED = {'emergencias'}

def main():
    dry_run = '--execute' not in sys.argv
    
    if dry_run:
        print("🔍 DRY RUN — No se borrará nada. Usa --execute para borrar.\n")
    else:
        print("⚠️ EJECUTANDO BORRADO REAL\n")
    
    # Inicializar Firebase Admin
    # Necesitas el service account key JSON
    try:
        firebase_admin.get_app()
    except ValueError:
        # Si no hay credenciales, usa las default de la máquina
        firebase_admin.initialize_app(options={'storageBucket': BUCKET_NAME})
    
    bucket = storage.bucket(BUCKET_NAME)
    now = datetime.utcnow()
    
    total_files = 0
    total_deleted = 0
    total_bytes_freed = 0
    
    for folder, days in RETENTION_DAYS.items():
        cutoff = now - timedelta(days=days)
        print(f"📁 {folder}/ — Retención: {days} días — Borrar antes de: {cutoff.strftime('%Y-%m-%d')}")
        
        blobs = bucket.list_blobs(prefix=f'{folder}/')
        folder_deleted = 0
        
        for blob in blobs:
            if blob.name.endswith('/'):
                continue  # Skip folder markers
            
            # Verificar fecha
            blob_date = blob.time_created or blob.updated
            if blob_date and blob_date.replace(tzinfo=None) < cutoff:
                total_files += 1
                size_kb = (blob.size or 0) / 1024
                
                if dry_run:
                    print(f"  🗑 BORRARÍA: {blob.name} ({size_kb:.0f} KB) — {blob_date.strftime('%Y-%m-%d')}")
                else:
                    try:
                        blob.delete()
                        folder_deleted += 1
                        total_deleted += 1
                        total_bytes_freed += blob.size or 0
                        print(f"  ✅ BORRADO: {blob.name} ({size_kb:.0f} KB)")
                    except Exception as e:
                        print(f"  ❌ ERROR: {blob.name}: {e}")
        
        if not dry_run and folder_deleted > 0:
            print(f"  → {folder_deleted} archivos borrados\n")
        else:
            print()
    
    # Verificar carpetas protegidas
    for folder in PROTECTED:
        blobs = list(bucket.list_blobs(prefix=f'{folder}/', max_results=5))
        file_count = len([b for b in blobs if not b.name.endswith('/')])
        print(f"🔒 {folder}/ — PROTEGIDA — {file_count}+ archivos (no se tocan)")
    
    print(f"\n{'='*50}")
    if dry_run:
        print(f"📊 Se borrarían {total_files} archivos")
        print(f"💡 Usa: python storage_cleanup.py --execute")
    else:
        mb_freed = total_bytes_freed / (1024 * 1024)
        print(f"📊 {total_deleted} archivos borrados — {mb_freed:.1f} MB liberados")

if __name__ == '__main__':
    main()