#!/usr/bin/env python3
# -*- coding: utf-8 -*-

###############################################################################
# IMPORTS Y DEPENDENCIAS
###############################################################################
import sys
import time
import os
import logging
import logging.handlers
import functools
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

import requests
import ccxt
import pandas as pd
import ta

###############################################################################
# PARÁMETROS DE LA ESTRATEGIA HÍBRIDA
###############################################################################
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

PARES_A_ANALIZAR = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "ADA/USDT",
    "DOGE/USDT", "TRX/USDT", "XRP/USDT", "SUI/USDT"
]

# --- Parámetros de la Estrategia ---
TIMEFRAME_PRINCIPAL   = '15m' # Análisis principal en 15 minutos
TIMEFRAME_TENDENCIA   = '4h'  # Filtro de tendencia en 4 horas
FUERZA_MIN_LONG       = 7     # Puntuación mínima para considerar una señal LONG
FUERZA_MIN_SHORT      = 7     # Puntuación mínima para considerar una señal SHORT (ajustado para 15m)
FUERZA_MINIMA_ALERTA  = 7     # Notificar a Telegram si la fuerza es >= 7

###############################################################################
# CONFIGURACIÓN DEL LOGGING
###############################################################################
log_dir = Path(__file__).resolve().parent / "logs"
log_dir.mkdir(exist_ok=True)
logfile = log_dir / "analisis_mercado.log"
formatter = logging.Formatter("%(asctime)s %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S")
file_handler = logging.FileHandler(logfile, mode="a", encoding="utf-8")
file_handler.setFormatter(formatter)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
logger = logging.getLogger()
logger.setLevel(logging.INFO)
if not logger.handlers:
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

###############################################################################
# CONFIGURACIÓN DEL EXCHANGE Y HERRAMIENTAS
###############################################################################
exchange = ccxt.mexc({"enableRateLimit": True})
exchange.load_markets()

def retry(excs, tries=3, delay=5, backoff=2):
    def deco(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            m, d = tries, delay
            while m > 1:
                try:
                    return fn(*args, **kwargs)
                except excs as e:
                    logger.warning(f"Reintentando {fn.__name__} por {e}, esperando {d}s")
                    time.sleep(d)
                    m -= 1
                    d *= backoff
            return fn(*args, **kwargs)
        return wrapped
    return deco

@retry(requests.exceptions.RequestException)
def enviar_telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Credenciales de Telegram no configuradas. No se enviará mensaje.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        response = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, 
timeout=10 )
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"Error enviando a Telegram: {e}")

def enviar_latido_si_procede():
    """Envía un mensaje 'estoy vivo' a horas específicas para confirmar que el bot funciona."""
    # Horas específicas para enviar el latido (ej. 8:00 y 20:00)
    horas_de_latido = [8, 20] 
    now = datetime.now()
    
    # Comprueba si es la hora y si estamos en los primeros 15 minutos de la hora
    if now.hour in horas_de_latido and now.minute < 15:
        # Usamos un archivo para no enviar el mensaje múltiples veces en la misma hora
        last_beat_file = Path(__file__).resolve().parent / "last_beat.txt"
        today_str = now.strftime("%Y-%m-%d")
        
        # Comprueba si ya se envió un latido para esta hora hoy
        if last_beat_file.exists():
            last_beat_data = last_beat_file.read_text()
            if f"{today_str}-{now.hour}" in last_beat_data:
                return # Ya se envió, no hacer nada
        
        # Si no se ha enviado, envía el mensaje y guarda la marca de tiempo
        enviar_telegram(f"🤖✅ El bot sigue activo y analizando. Última comprobación: {now.strftime('%H:%M:%S')}")
        last_beat_file.write_text(f"{today_str}-{now.hour}")


###############################################################################
# ANALIZADOR TÉCNICO HÍBRIDO (15m + 4h)
###############################################################################
@retry((ccxt.NetworkError, ccxt.ExchangeError))
def analizar_mercado(symbol: str) -> dict:
    try:
        # 1. Descargar datos de ambas temporalidades
        ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME_PRINCIPAL, limit=300)
        ohlcv_4h = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME_TENDENCIA, limit=250)

        if len(ohlcv_15m) < 200 or len(ohlcv_4h) < 210:
            logger.warning(f"Datos insuficientes para el análisis híbrido de {symbol}.")
            return {"score_long": 0, "score_short": 0, "tipo": "NONE", "fuerza": 0}

        # 2. Preparar DataFrames
        df_15m = pd.DataFrame(ohlcv_15m, columns=["ts", "open", "high", "low", "close", 
"volume"])
        df_4h = pd.DataFrame(ohlcv_4h, columns=["ts", "open", "high", "low", "close", 
"volume"])

        # 3. Calcular indicadores en el DataFrame principal (15m)
        df_15m["ema20"] = ta.trend.ema_indicator(df_15m["close"], 20)
        df_15m["ema50"] = ta.trend.ema_indicator(df_15m["close"], 50) # EMA 50 es común en 15m
        df_15m["adx"] = ta.trend.adx(df_15m["high"], df_15m["low"], df_15m["close"], 14)
        df_15m["macd_hist"] = ta.trend.macd_diff(df_15m["close"])
        df_15m["rsi"] = ta.momentum.rsi(df_15m["close"], 14)
        df_15m["vol_sma20"] = df_15m["volume"].rolling(20).mean()
        df_15m.dropna(inplace=True)

        # 4. Calcular tendencia general en el DataFrame de 4h
        df_4h["ema20"] = ta.trend.ema_indicator(df_4h["close"], 20)
        df_4h["ema200"] = ta.trend.ema_indicator(df_4h["close"], 200)
        df_4h.dropna(inplace=True)
        
        tendencia_general = "NEUTRO"
        if not df_4h.empty:
            last_4h = df_4h.iloc[-1] # Usamos la última vela de 4h disponible
            if last_4h["ema20"] > last_4h["ema200"]: tendencia_general = "ALCISTA"
            elif last_4h["ema20"] < last_4h["ema200"]: tendencia_general = "BAJISTA"

        if len(df_15m) < 50: return {"score_long": 0, "score_short": 0, "tipo": "NONE", "fuerza": 0}

        # 5. Asegurar que se analiza la última vela CERRADA de 15m
        last = df_15m.iloc[-2]
        prev1 = df_15m.iloc[-3]
        prev2 = df_15m.iloc[-4]

        # 6. Definir condiciones para LONG y SHORT en 15m
        # Ajustamos el breakout a un periodo más corto (96 velas = 24 horas en 15m)
        cond_long = {
            "direccion": last["ema20"] > last["ema50"] and last["close"] > last["ema20"],
            "potencia_adx": last["adx"] > 23, # Ligeramente más sensible para 15m
            "macd_hist": (last["macd_hist"] > 0 and (prev1["macd_hist"] <= 0 or prev2["macd_hist"] <= 0)),
            "rsi_50": (last["rsi"] > 50 and (prev1["rsi"] <= 50 or prev2["rsi"] <= 50)),
            "volumen": last["volume"] >= 1.5 * last["vol_sma20"], # Un poco más exigente con el volumen
            "breakout": last["close"] >= 0.99 * df_15m["high"].iloc[-96:].max() # Breakout de las últimas 24h
        }
        cond_short = {
            "direccion": last["ema20"] < last["ema50"] and last["close"] < last["ema20"],
            "potencia_adx": last["adx"] > 23,
            "macd_hist": (last["macd_hist"] < 0 and (prev1["macd_hist"] >= 0 or prev2["macd_hist"] >= 0)),
            "rsi_50": (last["rsi"] < 50 and (prev1["rsi"] >= 50 or prev2["rsi"] >= 50)),
            "volumen": last["volume"] >= 1.5 * last["vol_sma20"],
            "breakout": last["close"] <= 1.01 * df_15m["low"].iloc[-96:].min()
        }

        # 7. Calcular puntuación
        pesos = [2, 2, 2, 1, 2, 1]
        score_long = sum(p for c, p in zip(cond_long.values(), pesos) if c)
        score_short = sum(p for c, p in zip(cond_short.values(), pesos) if c)

        # 8. Determinar señal final, USANDO EL FILTRO DE TENDENCIA DE 4H
        tipo, fuerza = "NONE", 0
        if score_long >= FUERZA_MIN_LONG and score_long > score_short and tendencia_general in ("ALCISTA", "NEUTRO"):
            tipo, fuerza = "LONG", score_long
        elif score_short >= FUERZA_MIN_SHORT and score_short > score_long and tendencia_general in ("BAJISTA", "NEUTRO"):
            tipo, fuerza = "SHORT", score_short
        
        return {"score_long": score_long, "score_short": score_short, "tipo": tipo, "fuerza": fuerza}
    except Exception as e:
        logger.error(f"Error analizando {symbol}: {e}")
        return {"score_long": 0, "score_short": 0, "tipo": "NONE", "fuerza": 0}

###############################################################################
# FUNCIÓN PRINCIPAL
###############################################################################
###############################################################################
# FUNCIÓN PRINCIPAL
###############################################################################
def main():
    # 1. LLAMA A LA FUNCIÓN DEL LATIDO AL PRINCIPIO DE TODO
    enviar_latido_si_procede()

    # 2. AHORA, REEMPLAZA EL RESTO DE TU FUNCIÓN MAIN CON LA LÓGICA DE ALERTAS MEJORADA
    logger.info(f"Iniciando análisis híbrido ({TIMEFRAME_PRINCIPAL} + {TIMEFRAME_TENDENCIA})...")
    
    resultados = {}
    for symbol in PARES_A_ANALIZAR:
        analisis = analizar_mercado(symbol)
        par = symbol.split('/')[0]
        
        score_long = analisis["score_long"]
        score_short = analisis["score_short"]
        
        resultados[par] = {"long": score_long, "short": score_short}

        # Lógica de alerta transparente
        if score_long >= FUERZA_MINIMA_ALERTA:
            if analisis['tipo'] == 'LONG':
                mensaje = f"✅ Señal LONG Confirmada en {symbol} | Fuerza: {score_long}/10"
            else:
                mensaje = f"⚠️ Potencial LONG en {symbol} (Fuerza: {score_long}/10) | Descartado por filtro."
            enviar_telegram(mensaje)
            
        if score_short >= FUERZA_MINIMA_ALERTA:
            if analisis['tipo'] == 'SHORT':
                mensaje = f"✅ Señal SHORT Confirmada en {symbol} | Fuerza: {score_short}/10"
            else:
                mensaje = f"⚠️ Potencial SHORT en {symbol} (Fuerza: {score_short}/10) | Descartado por filtro."
            enviar_telegram(mensaje)

    # Imprime el resumen en la terminal
    ahora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\nResumen del Análisis - {ahora_str}")
    print("="*35)
    print(f"{'Par':<10} {'LONG':<10} {'SHORT':<10}")
    print("-"*35)
    for par, res in resultados.items():
        print(f"{par:<10} {res['long']:<10} {res['short']:<10}")
    print("="*35 + "\n")


###############################################################################
# EJECUCIÓN
###############################################################################
if __name__ == "__main__":
    try:
        main()
        logger.info("🤖 Análisis completado.")
    except Exception as e:
        logger.exception("❌ Error crítico")
        enviar_telegram(f"❌ ERROR CRÍTICO EN EL BOT: {e}")
        sys.exit(1)
