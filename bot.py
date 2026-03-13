"""
Crypto Paper Trading Bot
Stack: Binance Public API (sin auth) + Groq AI + Telegram
Deploy: Railway.app (24/7)
"""

import os
import json
import time
import logging
import threading
from datetime import datetime, timezone
from typing import Optional
import requests
import pandas as pd
import numpy as np
from groq import Groq
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ─── Config ──────────────────────────────────────────────────────────────────
GROQ_API_KEY     = os.environ["GROQ_API_KEY"]
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

INITIAL_BALANCE  = float(os.getenv("INITIAL_BALANCE", "1000"))
SCAN_INTERVAL    = int(os.getenv("SCAN_INTERVAL", "300"))
TOP_N_VOLATILE   = int(os.getenv("TOP_N_VOLATILE", "10"))
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.20"))
MIN_CONFIDENCE   = int(os.getenv("MIN_CONFIDENCE", "60"))
HISTORY_FILE     = "trades_history.json"
BINANCE_BASE     = "https://api.binance.com/api/v3"

# ─── Clientes ────────────────────────────────────────────────────────────────
groq_client = Groq(api_key=GROQ_API_KEY)

# ─── Estado del portafolio ───────────────────────────────────────────────────
portfolio = {
    "usdt_balance": INITIAL_BALANCE,
    "positions":    {},
    "trades":       [],
    "total_pnl":    0.0,
    "cycle":        0,
}

# ─── Binance Public API ──────────────────────────────────────────────────────
def binance_get(endpoint: str, params: dict = {}) -> Optional[any]:
    try:
        r = requests.get(f"{BINANCE_BASE}/{endpoint}", params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"Binance API error [{endpoint}]: {e}")
        return None


def get_top_volatile_symbols(n: int = TOP_N_VOLATILE) -> list[str]:
    tickers = binance_get("ticker/24hr")
    if not tickers:
        return []
    usdt_pairs = [
        t for t in tickers
        if t["symbol"].endswith("USDT")
        and not any(x in t["symbol"] for x in ["DOWN", "UP", "BEAR", "BULL"])
        and float(t.get("quoteVolume", 0)) > 1_000_000
    ]
    usdt_pairs.sort(key=lambda x: abs(float(x.get("priceChangePercent", 0))), reverse=True)
    symbols = [t["symbol"] for t in usdt_pairs[:n]]
    log.info(f"Top {n} volátiles: {symbols}")
    return symbols


def get_ohlcv(symbol: str, interval: str = "15m", limit: int = 100) -> Optional[pd.DataFrame]:
    klines = binance_get("klines", {"symbol": symbol, "interval": interval, "limit": limit})
    if not klines:
        return None
    df = pd.DataFrame(klines, columns=[
        "open_time","open","high","low","close","volume",
        "close_time","quote_vol","trades","taker_buy_base","taker_buy_quote","ignore"
    ])
    for col in ["open","high","low","close","volume"]:
        df[col] = df[col].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    return df


def get_current_price(symbol: str) -> Optional[float]:
    data = binance_get("ticker/price", {"symbol": symbol})
    return float(data["price"]) if data else None

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
    sma   = series.rolling(period).mean()
    std   = series.rolling(period).std()
    upper = sma + 2 * std
    lower = sma - 2 * std
    price = series.iloc[-1]
    pct_b = (price - lower.iloc[-1]) / (upper.iloc[-1] - lower.iloc[-1])
    return (
        round(float(upper.iloc[-1]), 6),
        round(float(sma.iloc[-1]), 6),
        round(float(lower.iloc[-1]), 6),
        round(float(pct_b), 4),
    )


def analyze_symbol(symbol: str) -> Optional[dict]:
    df = get_ohlcv(symbol)
    if df is None or len(df) < 30:
        return None
    close       = df["close"]
    volume      = df["volume"]
    price       = close.iloc[-1]
    rsi         = calc_rsi(close)
    macd_v, macd_sig, macd_hist = calc_macd(close)
    bb_upper, bb_mid, bb_lower, pct_b = calc_bollinger(close)
    vol_avg     = float(volume.rolling(20).mean().iloc[-1])
    vol_current = float(volume.iloc[-1])
    vol_ratio   = round(vol_current / vol_avg, 2) if vol_avg else 1.0
    chg_1h      = round((price / float(close.iloc[-4]) - 1) * 100, 2)
    chg_4h      = round((price / float(close.iloc[-16]) - 1) * 100, 2)
    return {
        "symbol":      symbol,
        "price":       round(price, 6),
        "rsi":         rsi,
        "macd":        macd_v,
        "macd_signal": macd_sig,
        "macd_hist":   macd_hist,
        "bb_upper":    bb_upper,
        "bb_mid":      bb_mid,
        "bb_lower":    bb_lower,
        "pct_b":       pct_b,
        "vol_ratio":   vol_ratio,
        "chg_1h":      chg_1h,
        "chg_4h":      chg_4h,
    }

# ─── Groq AI ─────────────────────────────────────────────────────────────────
def ai_decision(indicators: dict, has_position: bool) -> dict:
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
- Bollinger Bands: Upper={indicators['bb_upper']} | Mid={indicators['bb_mid']} | Lower={indicators['bb_lower']} | %B={indicators['pct_b']}
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
        resp = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=200,
        )
        raw = resp.choices[0].message.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(raw)
        result["action"] = result["action"].lower()
        if result["action"] not in ("buy", "sell", "hold"):
            result["action"] = "hold"
        return result
    except Exception as e:
        log.error(f"Groq error: {e}")
        return {"action": "hold", "confidence": 0, "reason": "Error en IA."}

# ─── Telegram ────────────────────────────────────────────────────────────────
def send_telegram(msg: str):
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

# ─── Paper trading ────────────────────────────────────────────────────────────
def execute_buy(symbol: str, price: float, reason: str, confidence: int):
    usdt   = portfolio["usdt_balance"]
    invest = usdt * MAX_POSITION_PCT
    if invest < 10:
        return
    qty = invest / price
    portfolio["positions"][symbol] = {
        "qty": qty, "entry_price": price,
        "entry_time": datetime.now(timezone.utc).isoformat(),
        "invested": invest,
    }
    portfolio["usdt_balance"] -= invest
    portfolio["trades"].append({
        "type": "buy", "symbol": symbol, "price": price,
        "qty": qty, "usdt": invest,
        "time": datetime.now(timezone.utc).isoformat(),
        "reason": reason, "confidence": confidence,
    })
    save_history()
    send_telegram(
        f"🟢 <b>COMPRA — {symbol}</b>\n"
        f"💰 Precio: <code>${price:.6f}</code>\n"
        f"📦 Cantidad: <code>{qty:.4f}</code>\n"
        f"💵 Invertido: <code>${invest:.2f} USDT</code>\n"
        f"🧠 Confianza IA: <code>{confidence}%</code>\n"
        f"📝 {reason}\n"
        f"💼 Saldo restante: <code>${portfolio['usdt_balance']:.2f} USDT</code>"
    )
    log.info(f"BUY {symbol} @ {price} | ${invest:.2f}")


def execute_sell(symbol: str, price: float, reason: str, confidence: int):
    pos = portfolio["positions"].get(symbol)
    if not pos:
        return
    qty      = pos["qty"]
    entry    = pos["entry_price"]
    proceeds = qty * price
    pnl      = proceeds - pos["invested"]
    pnl_pct  = (pnl / pos["invested"]) * 100
    portfolio["usdt_balance"] += proceeds
    portfolio["total_pnl"]    += pnl
    del portfolio["positions"][symbol]
    portfolio["trades"].append({
        "type": "sell", "symbol": symbol,
        "price": price, "qty": qty,
        "entry_price": entry, "pnl": pnl, "pnl_pct": pnl_pct,
        "time": datetime.now(timezone.utc).isoformat(),
        "reason": reason, "confidence": confidence,
    })
    save_history()
    emoji = "🟢" if pnl >= 0 else "🔴"
    send_telegram(
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
    log.info(f"SELL {symbol} @ {price} | PnL ${pnl:+.2f} ({pnl_pct:+.2f}%)")

# ─── Historial ────────────────────────────────────────────────────────────────
def save_history():
    with open(HISTORY_FILE, "w") as f:
        json.dump({
            "updated":      datetime.now(timezone.utc).isoformat(),
            "usdt_balance": portfolio["usdt_balance"],
            "total_pnl":    portfolio["total_pnl"],
            "positions":    portfolio["positions"],
            "trades":       portfolio["trades"][-500:],
        }, f, indent=2)


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
        log.info(f"Historial cargado: ${portfolio['usdt_balance']:.2f}, {len(portfolio['positions'])} posiciones")
    except Exception as e:
        log.error(f"Error cargando historial: {e}")

# ─── Resumen ──────────────────────────────────────────────────────────────────
def portfolio_summary() -> str:
    lines = [
        "📊 <b>RESUMEN DE PORTAFOLIO</b>",
        f"💵 Balance USDT: <code>${portfolio['usdt_balance']:.2f}</code>",
        f"📈 PnL total: <code>${portfolio['total_pnl']:+.2f}</code>",
        f"🔄 Ciclo actual: <code>#{portfolio['cycle']}</code>",
    ]
    if portfolio["positions"]:
        lines.append("\n🔓 <b>Posiciones abiertas:</b>")
        for sym, pos in portfolio["positions"].items():
            price = get_current_price(sym)
            if price:
                unrealized = (price - pos["entry_price"]) * pos["qty"]
                pct        = ((price / pos["entry_price"]) - 1) * 100
                lines.append(
                    f"  • {sym}: <code>${price:.6f}</code> | "
                    f"PnL: <code>${unrealized:+.2f} ({pct:+.2f}%)</code>"
                )
    else:
        lines.append("🔓 Sin posiciones abiertas")

    closed = [t for t in portfolio["trades"] if t["type"] == "sell"]
    wins   = [t for t in closed if t.get("pnl", 0) > 0]
    wr     = (len(wins) / len(closed) * 100) if closed else 0
    lines.append(f"\n🎯 Trades cerrados: {len(closed)} | Win rate: {wr:.1f}%")
    return "\n".join(lines)

# ─── Loop de trading (corre en thread secundario) ─────────────────────────────
def trading_loop():
    # Esperar 5 segundos para que Telegram arranque primero
    time.sleep(5)
    send_telegram(
        "🚀 <b>Paper Trading Bot iniciado</b>\n"
        f"💵 Balance: <code>${portfolio['usdt_balance']:.2f} USDT</code>\n"
        f"🔄 Ciclos cada {SCAN_INTERVAL // 60} minutos\n"
        f"🎯 Top {TOP_N_VOLATILE} pares más volátiles\n"
        f"🧠 Confianza mínima: {MIN_CONFIDENCE}%"
    )
    while True:
        try:
            portfolio["cycle"] += 1
            log.info(f"─── Ciclo #{portfolio['cycle']} ───")

            symbols      = get_top_volatile_symbols()
            open_symbols = list(portfolio["positions"].keys())
            all_symbols  = list(dict.fromkeys(symbols + open_symbols))

            for sym in all_symbols:
                indicators = analyze_symbol(sym)
                if not indicators:
                    continue
                has_pos    = sym in portfolio["positions"]
                decision   = ai_decision(indicators, has_pos)
                action     = decision["action"]
                confidence = decision.get("confidence", 0)
                reason     = decision.get("reason", "")

                if action == "buy" and not has_pos and confidence >= MIN_CONFIDENCE:
                    execute_buy(sym, indicators["price"], reason, confidence)
                elif action == "sell" and has_pos and confidence >= MIN_CONFIDENCE:
                    execute_sell(sym, indicators["price"], reason, confidence)
                else:
                    log.info(f"HOLD {sym} | {action} {confidence}%")
                time.sleep(0.5)

            if portfolio["cycle"] % 12 == 0:
                send_telegram(portfolio_summary())

        except Exception as e:
            log.error(f"Error en ciclo: {e}", exc_info=True)
            send_telegram(f"⚠️ Error en ciclo #{portfolio['cycle']}: {e}")

        time.sleep(SCAN_INTERVAL)

# ─── Telegram command handlers ────────────────────────────────────────────────
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(portfolio_summary(), parse_mode="HTML")

async def cmd_trades(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 <b>Crypto Paper Trading Bot</b>\n\n"
        "/status — Portafolio actual y posiciones abiertas\n"
        "/trades — Últimos 10 trades cerrados\n"
        "/help   — Este mensaje",
        parse_mode="HTML"
    )

# ─── Main — Telegram en main thread, trading en background ───────────────────
def main():
    load_history()

    # Trading loop en thread secundario
    t = threading.Thread(target=trading_loop, daemon=True)
    t.start()

    # Telegram polling en el main thread (necesita el event loop principal)
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("trades", cmd_trades))
    app.add_handler(CommandHandler("help",   cmd_help))
    log.info("Telegram bot iniciado (main thread).")
    app.run_polling()


if __name__ == "__main__":
    main()
