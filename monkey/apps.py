from django.apps import AppConfig


class MonkeyConfig(AppConfig):
    name = "monkey"

    def ready(self):
        from monkey import celery_signals

        celery_signals.connect()
