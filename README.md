# Poppy

Bot de Telegram para descubrir eventos exclusivos en Santiago de Chile.

## Qué hace
- Busca resultados web relacionados con inauguraciones, aperturas, degustaciones gratis, catas y lanzamientos.
- Usa Groq para extraer únicamente eventos verificables.
- Filtra para dejar solo eventos de **Santiago de Chile**, **posteriores al 18 de marzo de 2026** y dentro del **año 2026**.
- Descarta eventos sin fecha clara, fuera de Santiago o sin rasgos de exclusividad.

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

## Ejecutar
```bash
python bot.py
```
