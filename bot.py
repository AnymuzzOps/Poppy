import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from typing import Iterable
from urllib.parse import quote_plus, urljoin, urlparse

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
MAX_EVENTS = int(os.getenv("MAX_EVENTS", "6"))

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
MAX_SOURCE_CHARS = 7000
RELATIVE_DATE_TERMS = (
    "hoy",
    "mañana",
    "esta semana",
    "este fin de semana",
    "próximo fin de semana",
    "proximo fin de semana",
    "próxima semana",
    "proxima semana",
)
BLOCKED_DOMAINS = {
    "instagram.com",
    "www.instagram.com",
    "m.instagram.com",
    "tiktok.com",
    "www.tiktok.com",
    "youtube.com",
    "www.youtube.com",
    "youtu.be",
    "facebook.com",
    "www.facebook.com",
    "x.com",
    "www.x.com",
    "twitter.com",
    "www.twitter.com",
}
BLOCKED_SOURCE_TERMS = (
    "multinivel",
    "piramidal",
    "network marketing",
    "gana dinero",
    "oportunidad de negocio",
    "plan de compensación",
    "afiliados",
    "recluta",
    "emprende sin inversión",
    "crypto academy",
)
EXCLUSIVE_TOKENS = (
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
SEARCH_QUERIES = [
    'site:ticketmaster.cl Santiago Chile 2026 inauguración OR lanzamiento VIP',
    'site:eventrid.cl Santiago Chile 2026 degustación gratis OR cata gratis',
    'site:welcu.com Santiago Chile 2026 evento exclusivo inauguración',
    'site:finde.latercera.com Santiago Chile 2026 degustación inauguración',
    '"Santiago de Chile" 2026 inauguración "cupos limitados"',
    '"Santiago de Chile" 2026 "degustación gratis"',
]
MONTHS = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}


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


def get_domain(url: str) -> str:
    return urlparse(url).netloc.lower()


def contains_relative_date(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in RELATIVE_DATE_TERMS)


def has_exclusive_signal(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in EXCLUSIVE_TOKENS)


def is_scam_like(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in BLOCKED_SOURCE_TERMS)


def extract_explicit_dates(text: str) -> list[str]:
    dates: set[str] = set()
    for year, month, day in re.findall(r"\b(202\d)[-/](\d{1,2})[-/](\d{1,2})\b", text):
        try:
            dates.add(datetime(int(year), int(month), int(day)).date().isoformat())
        except ValueError:
            continue
    for day, month_name, year in re.findall(
        r"\b(\d{1,2})\s+de\s+([a-záéíóú]+)\s+de\s+(202\d)\b", text.lower()
    ):
        month = MONTHS.get(month_name)
        if not month:
            continue
        try:
            dates.add(datetime(int(year), month, int(day)).date().isoformat())
        except ValueError:
            continue
    return sorted(dates)


def qualifies_source(url: str, title: str, snippet: str, page_text: str) -> tuple[bool, str]:
    domain = get_domain(url)
    combined = " ".join([title, snippet, page_text]).lower()
    explicit_dates = extract_explicit_dates(combined)

    if domain in BLOCKED_DOMAINS:
        return False, f"dominio bloqueado: {domain}"
    if is_scam_like(combined):
        return False, "patrones de estafa o captación"
    if not has_exclusive_signal(combined):
        return False, "sin señales de exclusividad"
    if "santiago" not in combined and not any(commune in combined for commune in ("providencia", "vitacura", "las condes", "ñuñoa", "nunoa", "lo barnechea")):
        return False, "sin ubicación clara en Santiago"
    if contains_relative_date(combined) and not explicit_dates:
        return False, "solo fecha relativa sin fecha absoluta"
    if not explicit_dates:
        return False, "sin fecha absoluta verificable"
    if not any(date.startswith("2026-") for date in explicit_dates):
        return False, "sin fecha explícita de 2026"
    if all(datetime.fromisoformat(date).date() <= DATE_CUTOFF for date in explicit_dates if date.startswith("2026-")):
        return False, "sin fechas posteriores al corte"
    return True, "ok"


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
                valid, reason = qualifies_source(result.url, result.title, result.snippet, page_text)
                if not valid:
                    log.info("Fuente descartada %s -> %s", result.url, reason)
                    continue
                combined = " ".join([result.title, result.snippet, page_text])
                sources.append(
                    {
                        "query": query,
                        "title": result.title,
                        "url": result.url,
                        "domain": get_domain(result.url),
                        "snippet": result.snippet,
                        "explicit_dates": extract_explicit_dates(combined),
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
            "venue": "Vitacura, Santiago, Chile",
            "category": "inauguración | degustación gratis | lanzamiento VIP",
            "exclusive_reason": "Apertura privada con cupos limitados",
            "summary": "Qué ocurrirá en una frase",
            "audience": "Adultos / foodies / prensa / invitados",
            "price": "Gratis con inscripción",
            "why_verified": "La fuente menciona explícitamente Santiago y la fecha 2026-03-25.",
            "source_url": "https://...",
            "source_title": "Página fuente",
            "source_domain": "eventrid.cl",
            "source_date_text": "25 de marzo de 2026",
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
- Excluir redes sociales, reels, videos, publicaciones ambiguas, artículos sin fecha absoluta de 2026, y cualquier publicación que parezca estafa, captación o multinivel.
- Excluir cualquier evento de 2025, cualquier evento fuera de Santiago de Chile, cualquier evento sin fecha verificable, cualquier evento con fecha igual o anterior a {DATE_CUTOFF.isoformat()}, y cualquier evento genérico sin rasgo de exclusividad.
- Si una fuente parece describir un video, un resumen periodístico de un evento pasado o una obra pública sin convocatoria real, descártala.
- No inventes datos.

Entrega JSON válido con EXACTAMENTE esta forma:
{json.dumps(AI_SCHEMA_EXAMPLE, ensure_ascii=False)}

Reglas adicionales:
- `date` debe estar en formato YYYY-MM-DD.
- `source_date_text` debe ser el texto de fecha observado en la fuente.
- `why_verified` debe explicar por qué sí cumple los filtros.
- Ordena por fecha ascendente.
- Devuelve máximo {MAX_EVENTS} eventos.
- Si no hay suficientes eventos verificados, devuelve una lista vacía en `events`.

Fuentes:
{json.dumps(sources, ensure_ascii=False)}
"""

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        temperature=0.05,
        max_tokens=2200,
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
            for key in (
                "title",
                "venue",
                "category",
                "exclusive_reason",
                "summary",
                "why_verified",
                "source_title",
                "source_domain",
                "source_date_text",
                "audience",
                "price",
            )
        ).lower()
        source_url = str(event.get("source_url", ""))
        source_domain = str(event.get("source_domain") or get_domain(source_url)).lower()
        source_date_text = str(event.get("source_date_text", "")).strip()

        if event_date <= DATE_CUTOFF or event_date.year != 2026:
            continue
        if source_domain in BLOCKED_DOMAINS:
            continue
        if is_scam_like(text_blob):
            continue
        if not source_date_text:
            continue
        if contains_relative_date(source_date_text) and not extract_explicit_dates(source_date_text):
            continue
        if not extract_explicit_dates(source_date_text) and not extract_explicit_dates(text_blob):
            continue
        if "santiago" not in text_blob and not any(commune in text_blob for commune in ("providencia", "vitacura", "las condes", "ñuñoa", "nunoa", "lo barnechea")):
            continue
        if not has_exclusive_signal(text_blob):
            continue
        filtered.append(
            {
                **event,
                "source_domain": source_domain,
                "price": event.get("price", "No especificado"),
                "audience": event.get("audience", "No especificado"),
            }
        )

    filtered.sort(key=lambda item: item["date"])
    return filtered[:MAX_EVENTS]


def format_events_message(data: dict) -> str:
    events = data.get("events", [])
    header = (
        "✨ <b>Agenda exclusiva verificada — Santiago de Chile</b>\n"
        f"📅 Solo eventos posteriores al <code>{DATE_CUTOFF.isoformat()}</code> y dentro de 2026.\n"
        "🧪 Se excluyen reels, TikToks, fechas relativas, artículos viejos y señales de estafa."
    )
    if not events:
        return header + "\n\nNo encontré eventos suficientemente verificados que cumplan todos los filtros."

    lines = [header]
    for idx, event in enumerate(events, start=1):
        lines.append(
            "\n".join(
                [
                    f"<b>{idx}. {event['title']}</b>",
                    f"🗓️ Fecha verificada: <code>{event['date']}</code> ({event.get('source_date_text', 'sin texto fuente')})",
                    f"📍 Lugar: {event['venue']}",
                    f"🏷️ Tipo: {event['category']}",
                    f"🔒 Exclusividad: {event['exclusive_reason']}",
                    f"👥 Público: {event.get('audience', 'No especificado')}",
                    f"💸 Precio: {event.get('price', 'No especificado')}",
                    f"✅ Verificación: {event.get('why_verified', 'Fuente con fecha absoluta y ubicación verificable.')}",
                    f"📝 Resumen: {event['summary']}",
                    f"🌐 Fuente: {event.get('source_title', event.get('source_domain', 'fuente'))} — {event.get('source_domain', '')}",
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
        "Hola. Usa /eventos para recibir una agenda verificada de eventos exclusivos en Santiago, Chile, posteriores al 2026-03-18.",
    )


async def cmd_eventos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Buscando inauguraciones, degustaciones gratis y experiencias exclusivas verificadas en Santiago…"
    )
    try:
        data = discover_events()
        await update.message.reply_text(
            format_events_message(data),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
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
