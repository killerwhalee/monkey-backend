from __future__ import absolute_import, unicode_literals

import logging
import os

from celery import Celery, Task

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

logger = logging.getLogger("celery")


class BaseTask(Task):
    """Wrap every task result in a uniform, debuggable JSON envelope.

    Regardless of task type the stored result has the same shape::

        {"task", "input": {"args", "kwargs"}, "ok", "output", "error"}

    On failure we log the full traceback (to _logs/celery.log) and return an
    envelope with ``ok=False`` rather than re-raising, so the persisted result is
    always structured and easy to inspect when debugging the service.
    """

    def __call__(self, *args, **kwargs):
        envelope = {
            "task": self.name,
            "input": {"args": list(args), "kwargs": kwargs},
            "ok": True,
            "output": None,
            "error": None,
        }
        try:
            envelope["output"] = self.run(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — boundary: record + report uniformly
            logger.exception("Task %s failed", self.name)
            envelope["ok"] = False
            envelope["error"] = str(exc)
        return envelope


app = Celery("monkey", task_cls=BaseTask)
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    print(f"Request: {self.request!r}")
