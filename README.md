# Monkey Backend

Django + DRF + Celery + Channels backend for **Monkey** — a population of virtual "monkey" traders
that randomly buy/sell random stocks at random times against the real Korean stock market via the
한국투자증권(KIS) Open API's paper-trading environment.

Project docs (overview, setup, operations, roadmap, status) live in ops-docs:
`~/ops-docs/docs/services/monkey/` → https://docs.killerwhalee.com/services/monkey/

Quick start (local):

```bash
uv sync
# create .env — see ops-docs dev.md for the variable list
uv run python manage.py migrate
uv run python manage.py runserver
uv run python manage.py test
```
