# Poppy

Bot de Telegram para descubrir eventos exclusivos en Santiago de Chile.

## Qué hace
- Busca resultados web relacionados con inauguraciones, aperturas, degustaciones gratis, catas y lanzamientos.
- Prioriza sitios más estructurados y bloquea redes sociales/video para evitar reels, TikToks y publicaciones ambiguas.
- Usa Groq para extraer únicamente eventos verificables.
- Filtra para dejar solo eventos de **Santiago de Chile**, **posteriores al 18 de marzo de 2026** y dentro del **año 2026**.
- Descarta eventos con solo fechas relativas, publicaciones viejas y señales de estafa o captación.

## Comandos
- `/start`
- `/eventos`
- `/help`

## Variables de entorno
- `GROQ_API_KEY`
- `TELEGRAM_TOKEN`
- `DEFAULT_EVENT_DATE` (opcional, por defecto `2026-03-18`)
- `SEARCH_RESULTS` (opcional, por defecto `8`)
- `HTTP_TIMEOUT` (opcional, por defecto `20`)
- `SEARCH_LOCALE` (opcional, por defecto `cl-es`)
- `MAX_EVENTS` (opcional, por defecto `6`)

## Ejecutar
```bash
python bot.py
```
