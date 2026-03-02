# ================================================================
# ARCHIVO: ia_provider.py
# Capa de abstracción multi-proveedor de IA para Ami (Python/FastAPI)
# Soporta: Anthropic -> Google (Gemini) -> OpenAI (GPT)
# ================================================================

import os
import json
import httpx
import logging
import asyncio
import re
from typing import List, Dict, Any, Optional

log = logging.getLogger("ia_provider")

# 1. CONFIGURACIÓN DE MODELOS POR PROVEEDOR
IA_MODELS = {
    'anthropic': {
        'haiku': 'claude-3-5-haiku-20241022',
        'sonnet': 'claude-3-5-sonnet-20241022',
        'opus': 'claude-3-opus-20240229',
        'default': 'claude-3-5-sonnet-20241022',
    },
    'openai': {
        'haiku': 'gpt-4o-mini',
        'sonnet': 'gpt-4o',
        'opus': 'gpt-4o',
        'default': 'gpt-4o',
    },
    'google': {
        'haiku': 'gemini-1.5-flash',
        'sonnet': 'gemini-1.5-pro',
        'opus': 'gemini-1.5-pro',
        'default': 'gemini-1.5-pro',
    }
}

# 2. PRECIOS POR MILLÓN DE TOKENS (USD) - Referencia para logs
IA_PRICING = {
    'claude-3-5-haiku-20241022': {'input': 1.00, 'output': 5.00},
    'claude-3-5-sonnet-20241022': {'input': 3.00, 'output': 15.00},
    'gpt-4o-mini': {'input': 0.15, 'output': 0.60},
    'gpt-4o': {'input': 2.50, 'output': 10.00},
    'gemini-1.5-flash': {'input': 0.10, 'output': 0.40},
    'gemini-1.5-pro': {'input': 1.25, 'output': 5.00},
}

# 3. FUNCIÓN PRINCIPAL (ORQUESTADOR CON FALLBACK)
async def ia_completion(
    system_prompt: str, 
    messages: List[Dict[str, str]], 
    options: Dict[str, Any] = None, 
    pool = None, 
    user_id: int = None, 
    tipo_operacion: str = 'conversacion'
) -> Dict[str, Any]:
    """
    Intenta obtener respuesta de IA siguiendo el orden de prioridad:
    1. Anthropic -> 2. Google -> 3. OpenAI
    """
    options = options or {}
    tier = options.get('tier', 'sonnet')
    timeout = options.get('timeout', 45)
    
    # Orden de cascada (Si falla uno, salta al siguiente)
    prioridad_proveedores = ['anthropic', 'google', 'openai']
    
    intentos = 0
    for provider in prioridad_proveedores:
        intentos += 1
        log.info(f"🤖 Intento {intentos}: Probando con {provider.upper()}")
        
        result = await _call_specific_provider(provider, tier, system_prompt, messages, options, timeout)
        
        if result['success']:
            result['fallback'] = (intentos > 1)
            # Registrar consumo en segundo plano (no bloquea la respuesta)
            if pool and user_id:
                asyncio.create_task(_registrar_consumo_db(
                    pool, user_id, tipo_operacion, 
                    result['provider'], result['model'], 
                    result['tokens_in'], result['tokens_out']
                ))
            return result
        
        log.warning(f"⚠️ {provider} falló: {result.get('error')}")

    return {
        'success': False, 
        'error': "Todos los proveedores de IA fallaron después de reintentos.",
        'provider': 'none'
    }

# 4. DISPATCHER DE PROVEEDORES
async def _call_specific_provider(provider, tier, system_prompt, messages, options, timeout):
    if provider == 'anthropic':
        return await _call_anthropic(tier, system_prompt, messages, options, timeout)
    elif provider == 'google':
        return await _call_google(tier, system_prompt, messages, options, timeout)
    elif provider == 'openai':
        return await _call_openai(tier, system_prompt, messages, options, timeout)
    return {'success': False, 'error': f"Proveedor {provider} no implementado"}

# --- IMPLEMENTACIÓN: ANTHROPIC (Claude) ---
async def _call_anthropic(tier, system_prompt, messages, options, timeout):
    api_key = os.getenv('ANTHROPIC_API_KEY')
    if not api_key: return {'success': False, 'error': 'API Key faltante'}
    model = IA_MODELS['anthropic'].get(tier, IA_MODELS['anthropic']['default'])
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                json={
                    'model': model,
                    'max_tokens': options.get('max_tokens', 1024),
                    'system': system_prompt,
                    'messages': messages,
                    'temperature': options.get('temperature', 0.7)
                },
                headers={'x-api-key': api_key, 'anthropic-version': '2023-06-01', 'Content-Type': 'application/json'},
                timeout=timeout
            )
            if resp.status_code != 200: return {'success': False, 'error': f"HTTP {resp.status_code}"}
            data = resp.json()
            return {
                'success': True, 'text': data['content'][0]['text'], 'provider': 'anthropic',
                'model': model, 'tokens_in': data['usage']['input_tokens'], 'tokens_out': data['usage']['output_tokens']
            }
    except Exception as e: return {'success': False, 'error': str(e)}

# --- IMPLEMENTACIÓN: GOOGLE (Gemini) ---
async def _call_google(tier, system_prompt, messages, options, timeout):
    api_key = os.getenv('GOOGLE_AI_API_KEY')
    if not api_key: return {'success': False, 'error': 'API Key faltante'}
    model = IA_MODELS['google'].get(tier, IA_MODELS['google']['default'])
    
    # Adaptar formato de mensajes a Gemini
    contents = []
    for m in messages:
        role = "model" if m['role'] == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": m['content']}]})

    try:
        async with httpx.AsyncClient() as client:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
            resp = await client.post(
                url,
                json={
                    "contents": contents,
                    "systemInstruction": {"parts": [{"text": system_prompt}]},
                    "generationConfig": {"maxOutputTokens": options.get('max_tokens', 1024), "temperature": options.get('temperature', 0.7)}
                },
                timeout=timeout
            )
            data = resp.json()
            if resp.status_code != 200: return {'success': False, 'error': f"HTTP {resp.status_code}"}
            return {
                'success': True, 'text': data['candidates'][0]['content']['parts'][0]['text'], 'provider': 'google',
                'model': model, 'tokens_in': data.get('usageMetadata', {}).get('promptTokenCount', 0),
                'tokens_out': data.get('usageMetadata', {}).get('candidatesTokenCount', 0)
            }
    except Exception as e: return {'success': False, 'error': str(e)}

# --- IMPLEMENTACIÓN: OPENAI (GPT) ---
async def _call_openai(tier, system_prompt, messages, options, timeout):
    api_key = os.getenv('OPENAI_API_KEY')
    if not api_key: return {'success': False, 'error': 'API Key faltante'}
    model = IA_MODELS['openai'].get(tier, IA_MODELS['openai']['default'])
    
    full_messages = [{"role": "system", "content": system_prompt}] + messages

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                json={
                    'model': model, 'messages': full_messages,
                    'max_tokens': options.get('max_tokens', 1024), 'temperature': options.get('temperature', 0.7)
                },
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                timeout=timeout
            )
            data = resp.json()
            if resp.status_code != 200: return {'success': False, 'error': f"HTTP {resp.status_code}"}
            return {
                'success': True, 'text': data['choices'][0]['message']['content'], 'provider': 'openai',
                'model': model, 'tokens_in': data['usage']['prompt_tokens'], 'tokens_out': data['usage']['completion_tokens']
            }
    except Exception as e: return {'success': False, 'error': str(e)}

# 5. REGISTRO DE CONSUMO EN BASE DE DATOS
async def _registrar_consumo_db(pool, user_id, tipo_operacion, provider, model, t_in, t_out):
    if not pool: return
    prices = IA_PRICING.get(model, {'input': 3.0, 'output': 15.0})
    cost = (t_in * prices['input'] / 1_000_000) + (t_out * prices['output'] / 1_000_000)
    try:
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO consumo_api (id_usuario, tipo_operacion, proveedor, modelo, tokens_input, tokens_output, costo_usd, fecha)
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
            """, user_id, tipo_operacion, provider, model, t_in, t_out, cost)
    except Exception as e:
        log.error(f"Error registrando consumo IA: {e}")