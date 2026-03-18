import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from typing import Iterable, Optional
from urllib.parse import quote_plus, urljoin

import requests
from groq import Groq
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEFAULT_EVENT_DATE = os.getenv("DEFAULT_EVENT_DATE", "2026-03-18")
SEARCH_RESULTS = int(os.getenv("SEARCH_RESULTS", "8"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "20"))
SEARCH_LOCALE = os.getenv("SEARCH_LOCALE", "cl-es")

client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
session = requests.Session()
session.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }
)

DATE_CUTOFF = datetime.fromisoformat(DEFAULT_EVENT_DATE).date()
MAX_SOURCE_CHARS = 6000

SEARCH_QUERIES = [
    "eventos exclusivos Santiago Chile 2026 inauguración \"marzo 2026\" OR \"abril 2026\"",
    "degustación gratis exclusiva Santiago Chile 2026",
    "inauguración tienda restaurante hotel Santiago Chile 2026 evento exclusivo",
    "cata gratuita Santiago Chile 2026 lanzamiento exclusivo",
]


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


def clean_html(raw: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_url(url: str) -> str:
    if url.startswith("//"):
        return f"https:{url}"
    return url


def duckduckgo_search(query: str, limit: int = SEARCH_RESULTS) -> list[SearchResult]:
    url = f"https://html.duckduckgo.com/html/?kl={quote_plus(SEARCH_LOCALE)}&q={quote_plus(query)}"
    resp = session.get(url, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    html = resp.text

    results: list[SearchResult] = []
    pattern = re.compile(
        r'<a[^>]+class="result__a"[^>]+href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>.*?'
        r'(?:<a[^>]+class="result__snippet"[^>]*>(?P<snippet_a>.*?)</a>|<div[^>]+class="result__snippet"[^>]*>(?P<snippet_div>.*?)</div>)',
        re.S,
    )
    for match in pattern.finditer(html):
        target = normalize_url(unescape(match.group("url")))
        if not target.startswith("http"):
            target = urljoin("https://html.duckduckgo.com", target)
        title = clean_html(match.group("title"))
        snippet = clean_html(match.group("snippet_a") or match.group("snippet_div") or "")
        if title and target:
            results.append(SearchResult(title=title, url=target, snippet=snippet))
        if len(results) >= limit:
            break
    return results


def fetch_page_text(url: str) -> str:
    try:
        resp = session.get(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
        return clean_html(resp.text)[:MAX_SOURCE_CHARS]
    except Exception as exc:
        log.warning("No se pudo leer %s: %s", url, exc)
        return ""


def build_sources() -> list[dict]:
    sources: list[dict] = []
    seen: set[str] = set()

    for query in SEARCH_QUERIES:
        try:
            for result in duckduckgo_search(query):
                if result.url in seen:
                    continue
                seen.add(result.url)
                page_text = fetch_page_text(result.url)
                if not page_text:
                    continue
                sources.append(
                    {
                        "query": query,
                        "title": result.title,
                        "url": result.url,
                        "snippet": result.snippet,
                        "content": page_text,
                    }
                )
        except Exception as exc:
            log.warning("Falló la búsqueda '%s': %s", query, exc)

    return sources


AI_SCHEMA_EXAMPLE = {
    "generated_at": "2026-03-20T12:00:00Z",
    "criteria": {
        "city": "Santiago de Chile",
        "country": "Chile",
        "from_date_exclusive": "2026-03-18",
        "year": 2026,
    },
    "events": [
        {
            "title": "Ejemplo",
            "date": "2026-03-25",
            "venue": "Vitacura, Santiago",
            "category": "inauguración|degustación gratis|lanzamiento|experiencia VIP",
            "exclusive_reason": "Explica por qué es exclusivo",
            "summary": "Qué ocurrirá en una frase",
            "source_url": "https://...",
            "source_title": "Página fuente",
        }
    ],
}


def extract_events_with_ai(sources: list[dict]) -> dict:
    if client is None:
        raise RuntimeError("Falta GROQ_API_KEY para procesar eventos.")
    prompt = f"""
Eres un curador de agenda premium extremadamente estricto.

Fecha de corte obligatoria: solo eventos POSTERIORES a {DATE_CUTOFF.isoformat()}.
Ciudad obligatoria: solo Santiago de Chile.
Año obligatorio: solo 2026.

Objetivo:
- Encontrar eventos exclusivos/premium/privados/de cupos limitados.
- Incluir especialmente inauguraciones, aperturas, lanzamientos, degustaciones gratis, catas gratis y experiencias VIP.
- Excluir cualquier evento de 2025, cualquier evento fuera de Santiago de Chile, cualquier evento sin fecha verificable, cualquier evento con fecha igual o anterior a {DATE_CUTOFF.isoformat()}, y cualquier evento genérico sin rasgo de exclusividad.
- Si una fuente menciona varias ciudades, acepta SOLO si deja claro que el evento es en Santiago de Chile.
- No inventes datos.

Entrega JSON válido con EXACTAMENTE esta forma:
{json.dumps(AI_SCHEMA_EXAMPLE, ensure_ascii=False)}

Reglas adicionales:
- `date` debe estar en formato YYYY-MM-DD.
- Ordena por fecha ascendente.
- Devuelve máximo 8 eventos.
- Si no hay suficientes eventos verificados, devuelve una lista vacía en `events`.
- `exclusive_reason` debe mencionar el elemento de exclusividad: VIP, cupos limitados, apertura privada, degustación gratuita, etc.

Fuentes:
{json.dumps(sources, ensure_ascii=False)}
"""

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        temperature=0.1,
        max_tokens=1800,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}],
    )
    payload = response.choices[0].message.content.strip()
    data = json.loads(payload)
    data["events"] = post_filter_events(data.get("events", []))
    return data


def post_filter_events(events: Iterable[dict]) -> list[dict]:
    filtered: list[dict] = []
    for event in events:
        try:
            event_date = datetime.fromisoformat(str(event.get("date", ""))).date()
        except ValueError:
            continue
        text_blob = " ".join(
            str(event.get(key, ""))
            for key in ("title", "venue", "category", "exclusive_reason", "summary")
        ).lower()
        if event_date <= DATE_CUTOFF:
            continue
        if event_date.year != 2026:
            continue
        if "santiago" not in text_blob:
            continue
        if "chile" not in text_blob and "providencia" not in text_blob and "vitacura" not in text_blob and "las condes" not in text_blob:
            continue
        if not any(
            token in text_blob
            for token in (
                "exclus",
                "vip",
                "privad",
                "cupos limitados",
                "degust",
                "cata",
                "inaugur",
                "apertura",
                "lanzamiento",
                "gratis",
                "premium",
            )
        ):
            continue
        filtered.append(event)

    filtered.sort(key=lambda item: item["date"])
    return filtered[:8]


def format_events_message(data: dict) -> str:
    events = data.get("events", [])
    header = (
        "✨ <b>Eventos exclusivos en Santiago de Chile</b>\n"
        f"📅 Solo eventos posteriores al <code>{DATE_CUTOFF.isoformat()}</code> y dentro de 2026.\n"
    )
    if not events:
        return header + "\nNo encontré eventos suficientemente verificados que cumplan todos los filtros."

    lines = [header]
    for idx, event in enumerate(events, start=1):
        lines.append(
            "\n".join(
                [
                    f"<b>{idx}. {event['title']}</b>",
                    f"🗓️ <code>{event['date']}</code>",
                    f"📍 {event['venue']}",
                    f"🏷️ {event['category']}",
                    f"🔒 {event['exclusive_reason']}",
                    f"📝 {event['summary']}",
                    f"🔗 {event['source_url']}",
                ]
            )
        )
    return "\n\n".join(lines)


def discover_events() -> dict:
    sources = build_sources()
    if not sources:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "criteria": {
                "city": "Santiago de Chile",
                "country": "Chile",
                "from_date_exclusive": DATE_CUTOFF.isoformat(),
                "year": 2026,
            },
            "events": [],
        }
    return extract_events_with_ai(sources)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hola. Usa /eventos para recibir eventos exclusivos de Santiago de Chile posteriores al 2026-03-18.",
    )


async def cmd_eventos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Buscando inauguraciones, degustaciones gratis y experiencias exclusivas en Santiago…")
    try:
        data = discover_events()
        await update.message.reply_text(format_events_message(data), parse_mode="HTML", disable_web_page_preview=True)
    except Exception as exc:
        log.exception("Error buscando eventos")
        await update.message.reply_text(f"Ocurrió un error buscando eventos: {exc}")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/eventos — Busca eventos exclusivos 2026 en Santiago de Chile posteriores al 18 de marzo de 2026.\n"
        "/start — Mensaje inicial.\n"
        "/help — Ayuda."
    )


def main():
    if not GROQ_API_KEY:
        raise RuntimeError("Debes definir GROQ_API_KEY.")
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Debes definir TELEGRAM_TOKEN.")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("eventos", cmd_eventos))
    app.add_handler(CommandHandler("help", cmd_help))
    log.info("Bot de eventos iniciado.")
    app.run_polling()


if __name__ == "__main__":
    main()
