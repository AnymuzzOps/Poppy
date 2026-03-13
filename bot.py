"""
Crypto Paper Trading Bot
Stack: Binance API + Groq AI + Telegram
Deploy: Railway.app (24/7)
"""

import os
import json
import time
import logging
import asyncio
from datetime import datetime, timezone
from typing import Optional
import requests
import pandas as pd
import numpy as np
from binance.client import Client
from binance.exceptions import BinanceAPIException
from groq import Groq
import telegram
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ─── Config desde env vars ──────────────────────────────────────────────────
BINANCE_API_KEY    = os.environ["BINANCE_API_KEY"]
BINANCE_SECRET_KEY = os.environ["BINANCE_SECRET_KEY"]
GROQ_API_KEY       = os.environ["GROQ_API_KEY"]
TELEGRAM_TOKEN     = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]

INITIAL_BALANCE    = float(os.getenv("INITIAL_BALANCE", "1000"))   # USDT ficticios
SCAN_INTERVAL      = int(os.getenv("SCAN_INTERVAL", "300"))         # segundos entre ciclos
TOP_N_VOLATILE     = int(os.getenv("TOP_N_VOLATILE", "10"))         # top N más volátiles
MAX_POSITION_PCT   = float(os.getenv("MAX_POSITION_PCT", "0.20"))   # max 20% del portafolio por trade
HISTORY_FILE       = "trades_history.json"

# ─── Clientes ───────────────────────────────────────────────────────────────
binance = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)
groq    = Groq(api_key=GROQ_API_KEY)

# ─── Estado del portafolio ──────────────────────────────────────────────────
portfolio = {
    "usdt_balance": INITIAL_BALANCE,
    "positions": {},   # symbol -> {qty, entry_price, entry_time}
    "trades": [],      # historial completo
    "total_pnl": 0.0,
    "cycle": 0,
}

# ─── Telegram helpers ────────────────────────────────────────────────────────
def send_telegram(msg: str):
    """Envío síncrono vía requests para usarlo desde cualquier contexto."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML"
        }, timeout=10)
        r.raise_for_status()
    except Exception as e:
        log.error(f"Telegram error: {e}")

# ─── Binance helpers ─────────────────────────────────────────────────────────
def get_top_volatile_symbols(n: int = TOP_N_VOLATILE) -> list[str]:
    """Retorna los N pares USDT con mayor variación de precio en 24h."""
    try:
        tickers = binance.get_ticker()
    except BinanceAPIException as e:
        log.error(f"Binance ticker error: {e}")
        return []

    usdt_pairs = [
        t for t in tickers
        if t["symbol"].endswith("USDT")
        and not t["symbol"].endswith("DOWNUSDT")
        and not t["symbol"].endswith("UPUSDT")
        and float(t.get("quoteVolume", 0)) > 1_000_000   # liquidez mínima
    ]

    for t in usdt_pairs:
        t["_vol_pct"] = abs(float(t.get("priceChangePercent", 0)))

    usdt_pairs.sort(key=lambda x: x["_vol_pct"], reverse=True)
    symbols = [t["symbol"] for t in usdt_pairs[:n]]
    log.info(f"Top {n} volátiles: {symbols}")
    return symbols


def get_ohlcv(symbol: str, interval: str = "15m", limit: int = 100) -> Optional[pd.DataFrame]:
    """Descarga velas OHLCV y retorna DataFrame."""
    try:
        klines = binance.get_klines(symbol=symbol, interval=interval, limit=limit)
    except BinanceAPIException as e:
        log.error(f"Klines error {symbol}: {e}")
        return None

    df = pd.DataFrame(klines, columns=[
        "open_time","open","high","low","close","volume",
        "close_time","quote_vol","trades","taker_buy_base",
        "taker_buy_quote","ignore"
    ])
    for col in ["open","high","low","close","volume"]:
        df[col] = df[col].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    return df


def get_current_price(symbol: str) -> Optional[float]:
    try:
        t = binance.get_symbol_ticker(symbol=symbol)
        return float(t["price"])
    except BinanceAPIException as e:
        log.error(f"Price error {symbol}: {e}")
        return None

# ─── Indicadores técnicos ────────────────────────────────────────────────────
def calc_rsi(series: pd.Series, period: int = 14) -> float:
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    rsi   = 100 - (100 / (1 + rs))
    return round(float(rsi.iloc[-1]), 2)


def calc_macd(series: pd.Series):
    ema12  = series.ewm(span=12, adjust=False).mean()
    ema26  = series.ewm(span=26, adjust=False).mean()
    macd   = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    hist   = macd - signal
    return (
        round(float(macd.iloc[-1]), 6),
        round(float(signal.iloc[-1]), 6),
        round(float(hist.iloc[-1]), 6),
    )


def calc_bollinger(series: pd.Series, period: int = 20):
    sma    = series.rolling(period).mean()
    std    = series.rolling(period).std()
    upper  = sma + 2 * std
    lower  = sma - 2 * std
    price  = series.iloc[-1]
    pct_b  = (price - lower.iloc[-1]) / (upper.iloc[-1] - lower.iloc[-1])
    return (
        round(float(upper.iloc[-1]), 6),
        round(float(sma.iloc[-1]), 6),
        round(float(lower.iloc[-1]), 6),
        round(float(pct_b), 4),
    )


def analyze_symbol(symbol: str) -> Optional[dict]:
    """Descarga velas y calcula todos los indicadores."""
    df = get_ohlcv(symbol)
    if df is None or len(df) < 30:
        return None

    close   = df["close"]
    volume  = df["volume"]
    price   = close.iloc[-1]
    rsi     = calc_rsi(close)
    macd_v, macd_sig, macd_hist = calc_macd(close)
    bb_upper, bb_mid, bb_lower, pct_b = calc_bollinger(close)

    vol_avg    = float(volume.rolling(20).mean().iloc[-1])
    vol_current = float(volume.iloc[-1])
    vol_ratio  = round(vol_current / vol_avg, 2) if vol_avg else 1.0

    chg_1h  = round((price / float(close.iloc[-4]) - 1) * 100, 2)   # ~1h en 15m
    chg_4h  = round((price / float(close.iloc[-16]) - 1) * 100, 2)  # ~4h

    return {
        "symbol":      symbol,
        "price":       round(price, 6),
        "rsi":         rsi,
        "macd":        macd_v,
        "macd_signal": macd_sig,
        "macd_hist":   macd_hist,
        "bb_upper":    bb_upper,
        "bb_lower":    bb_lower,
        "pct_b":       pct_b,
        "vol_ratio":   vol_ratio,
        "chg_1h":      chg_1h,
        "chg_4h":      chg_4h,
    }

# ─── Groq AI decision ────────────────────────────────────────────────────────
def ai_decision(indicators: dict, has_position: bool) -> dict:
    """
    Llama a Groq con los indicadores y retorna:
    {"action": "buy"|"sell"|"hold", "confidence": 0-100, "reason": "..."}
    """
    position_info = (
        "El bot YA tiene posición abierta en este activo."
        if has_position else
        "El bot NO tiene posición abierta en este activo."
    )

    prompt = f"""Eres un trader cuantitativo experto. Analiza estos indicadores técnicos y decide si comprar, vender o esperar.

Símbolo: {indicators['symbol']}
Precio actual: {indicators['price']}
{position_info}

Indicadores:
- RSI(14): {indicators['rsi']}
- MACD: {indicators['macd']} | Signal: {indicators['macd_signal']} | Histograma: {indicators['macd_hist']}
- Bollinger Bands: Upper={indicators['bb_upper']} | Mid={indicators['bb_mid'] if 'bb_mid' in indicators else 'N/A'} | Lower={indicators['bb_lower']} | %B={indicators['pct_b']}
- Volumen relativo (vs media 20): {indicators['vol_ratio']}x
- Cambio 1h: {indicators['chg_1h']}%
- Cambio 4h: {indicators['chg_4h']}%

Reglas estrictas:
- Solo recomienda BUY si NO hay posición abierta
- Solo recomienda SELL si HAY posición abierta
- HOLD si la señal no es clara o el riesgo es alto

Responde ÚNICAMENTE con JSON válido, sin markdown, sin texto extra:
{{"action": "buy|sell|hold", "confidence": 0-100, "reason": "máximo 2 oraciones"}}"""

    try:
        resp = groq.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=200,
        )
        raw = resp.choices[0].message.content.strip()
        # Limpiar posibles backticks
        raw = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(raw)
        result["action"] = result["action"].lower()
        if result["action"] not in ("buy", "sell", "hold"):
            result["action"] = "hold"
        return result
    except Exception as e:
        log.error(f"Groq error: {e}")
        return {"action": "hold", "confidence": 0, "reason": "Error en IA, se omite la operación."}

# ─── Paper trading engine ────────────────────────────────────────────────────
def execute_buy(symbol: str, price: float, reason: str, confidence: int):
    usdt = portfolio["usdt_balance"]
    invest = usdt * MAX_POSITION_PCT
    if invest < 10:
        log.info(f"Saldo insuficiente para comprar {symbol}: ${usdt:.2f}")
        return

    qty = invest / price
    portfolio["positions"][symbol] = {
        "qty":        qty,
        "entry_price": price,
        "entry_time":  datetime.now(timezone.utc).isoformat(),
        "invested":    invest,
    }
    portfolio["usdt_balance"] -= invest

    trade = {
        "type": "buy", "symbol": symbol, "price": price,
        "qty": qty, "usdt": invest,
        "time": datetime.now(timezone.utc).isoformat(),
        "reason": reason, "confidence": confidence,
    }
    portfolio["trades"].append(trade)
    save_history()

    msg = (
        f"🟢 <b>COMPRA — {symbol}</b>\n"
        f"💰 Precio: <code>${price:.6f}</code>\n"
        f"📦 Cantidad: <code>{qty:.4f}</code>\n"
        f"💵 Invertido: <code>${invest:.2f} USDT</code>\n"
        f"🧠 Confianza IA: <code>{confidence}%</code>\n"
        f"📝 {reason}\n"
        f"💼 Saldo restante: <code>${portfolio['usdt_balance']:.2f} USDT</code>"
    )
    send_telegram(msg)
    log.info(f"BUY {symbol} @ {price} | ${invest:.2f}")


def execute_sell(symbol: str, price: float, reason: str, confidence: int):
    pos = portfolio["positions"].get(symbol)
    if not pos:
        return

    qty       = pos["qty"]
    entry     = pos["entry_price"]
    proceeds  = qty * price
    pnl       = proceeds - pos["invested"]
    pnl_pct   = (pnl / pos["invested"]) * 100

    portfolio["usdt_balance"] += proceeds
    portfolio["total_pnl"]    += pnl
    del portfolio["positions"][symbol]

    trade = {
        "type": "sell", "symbol": symbol,
        "price": price, "qty": qty,
        "entry_price": entry, "pnl": pnl, "pnl_pct": pnl_pct,
        "time": datetime.now(timezone.utc).isoformat(),
        "reason": reason, "confidence": confidence,
    }
    portfolio["trades"].append(trade)
    save_history()

    emoji = "🟢" if pnl >= 0 else "🔴"
    msg = (
        f"{emoji} <b>VENTA — {symbol}</b>\n"
        f"💰 Precio salida: <code>${price:.6f}</code>\n"
        f"📥 Precio entrada: <code>${entry:.6f}</code>\n"
        f"📦 Cantidad: <code>{qty:.4f}</code>\n"
        f"{'✅' if pnl >= 0 else '❌'} PnL: <code>${pnl:+.2f} ({pnl_pct:+.2f}%)</code>\n"
        f"🧠 Confianza IA: <code>{confidence}%</code>\n"
        f"📝 {reason}\n"
        f"💼 Balance USDT: <code>${portfolio['usdt_balance']:.2f}</code>\n"
        f"📊 PnL Total: <code>${portfolio['total_pnl']:+.2f}</code>"
    )
    send_telegram(msg)
    log.info(f"SELL {symbol} @ {price} | PnL ${pnl:+.2f} ({pnl_pct:+.2f}%)")

# ─── Historial ────────────────────────────────────────────────────────────────
def save_history():
    data = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "usdt_balance": portfolio["usdt_balance"],
        "total_pnl": portfolio["total_pnl"],
        "positions": portfolio["positions"],
        "trades": portfolio["trades"][-500:],   # últimas 500 operaciones
    }
    with open(HISTORY_FILE, "w") as f:
        json.dump(data, f, indent=2)


def load_history():
    if not os.path.exists(HISTORY_FILE):
        return
    try:
        with open(HISTORY_FILE) as f:
            data = json.load(f)
        portfolio["usdt_balance"] = data.get("usdt_balance", INITIAL_BALANCE)
        portfolio["total_pnl"]    = data.get("total_pnl", 0.0)
        portfolio["positions"]    = data.get("positions", {})
        portfolio["trades"]       = data.get("trades", [])
        log.info(f"Historial cargado: balance ${portfolio['usdt_balance']:.2f}, {len(portfolio['positions'])} posiciones abiertas")
    except Exception as e:
        log.error(f"Error cargando historial: {e}")

# ─── Resumen de portafolio ────────────────────────────────────────────────────
def portfolio_summary() -> str:
    lines = [
        "📊 <b>RESUMEN DE PORTAFOLIO</b>",
        f"💵 Balance USDT: <code>${portfolio['usdt_balance']:.2f}</code>",
        f"📈 PnL total: <code>${portfolio['total_pnl']:+.2f}</code>",
    ]

    if portfolio["positions"]:
        lines.append("\n🔓 <b>Posiciones abiertas:</b>")
        for sym, pos in portfolio["positions"].items():
            price = get_current_price(sym)
            if price:
                unrealized = (price - pos["entry_price"]) * pos["qty"]
                pct = ((price / pos["entry_price"]) - 1) * 100
                lines.append(
                    f"  • {sym}: <code>${price:.6f}</code> | "
                    f"PnL no realizado: <code>${unrealized:+.2f} ({pct:+.2f}%)</code>"
                )
    else:
        lines.append("🔓 Sin posiciones abiertas")

    total_trades = len([t for t in portfolio["trades"] if t["type"] == "sell"])
    wins         = len([t for t in portfolio["trades"] if t["type"] == "sell" and t.get("pnl", 0) > 0])
    wr           = (wins / total_trades * 100) if total_trades else 0
    lines.append(f"\n🎯 Trades cerrados: {total_trades} | Win rate: {wr:.1f}%")

    return "\n".join(lines)

# ─── Loop principal ──────────────────────────────────────────────────────────
def run_cycle():
    portfolio["cycle"] += 1
    log.info(f"─── Ciclo #{portfolio['cycle']} ───")

    symbols = get_top_volatile_symbols()
    if not symbols:
        log.warning("Sin símbolos, saltando ciclo.")
        return

    # También revisar posiciones abiertas que no estén en el top volátil
    open_symbols = list(portfolio["positions"].keys())
    all_symbols  = list(dict.fromkeys(symbols + open_symbols))

    decisions = []
    for sym in all_symbols:
        indicators = analyze_symbol(sym)
        if not indicators:
            continue

        has_pos = sym in portfolio["positions"]
        decision = ai_decision(indicators, has_pos)
        decisions.append((sym, indicators, decision, has_pos))

        action     = decision["action"]
        confidence = decision.get("confidence", 0)
        reason     = decision.get("reason", "")

        if action == "buy" and not has_pos and confidence >= 65:
            execute_buy(sym, indicators["price"], reason, confidence)
        elif action == "sell" and has_pos and confidence >= 60:
            execute_sell(sym, indicators["price"], reason, confidence)
        else:
            log.info(f"HOLD {sym} | action={action} confidence={confidence}")

        time.sleep(0.3)   # evitar rate limit

    # Resumen cada 12 ciclos (~1h con 300s de intervalo)
    if portfolio["cycle"] % 12 == 0:
        send_telegram(portfolio_summary())


# ─── Telegram commands ────────────────────────────────────────────────────────
async def cmd_status(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(portfolio_summary(), parse_mode="HTML")


async def cmd_trades(update, context: ContextTypes.DEFAULT_TYPE):
    trades = [t for t in portfolio["trades"] if t["type"] == "sell"][-10:]
    if not trades:
        await update.message.reply_text("Sin trades cerrados aún.")
        return
    lines = ["📋 <b>Últimos 10 trades cerrados:</b>"]
    for t in reversed(trades):
        e = "✅" if t.get("pnl", 0) >= 0 else "❌"
        lines.append(
            f"{e} {t['symbol']} | <code>${t.get('pnl', 0):+.2f} ({t.get('pnl_pct', 0):+.2f}%)</code> "
            f"@ {t['time'][:10]}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_help(update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🤖 <b>Crypto Paper Trading Bot</b>\n\n"
        "/status — Portafolio actual y posiciones abiertas\n"
        "/trades — Últimos 10 trades cerrados\n"
        "/help   — Este mensaje"
    )
    await update.message.reply_text(msg, parse_mode="HTML")

# ─── Entrypoint ──────────────────────────────────────────────────────────────
def main():
    load_history()

    # Arrancar Telegram bot en thread separado (polling)
    import threading

    def run_telegram():
        app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
        app.add_handler(CommandHandler("status", cmd_status))
        app.add_handler(CommandHandler("trades", cmd_trades))
        app.add_handler(CommandHandler("help",   cmd_help))
        log.info("Telegram bot iniciado.")
        app.run_polling(close_loop=False)

    t = threading.Thread(target=run_telegram, daemon=True)
    t.start()

    send_telegram(
        "🚀 <b>Paper Trading Bot iniciado</b>\n"
        f"💵 Balance inicial: <code>${portfolio['usdt_balance']:.2f} USDT</code>\n"
        f"🔄 Ciclos cada {SCAN_INTERVAL//60} minutos\n"
        f"🎯 Top {TOP_N_VOLATILE} pares más volátiles\n"
        f"🧠 IA: Groq llama-3.3-70b-versatile"
    )

    while True:
        try:
            run_cycle()
        except Exception as e:
            log.error(f"Error en ciclo: {e}", exc_info=True)
            send_telegram(f"⚠️ Error en ciclo #{portfolio['cycle']}: {e}")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
