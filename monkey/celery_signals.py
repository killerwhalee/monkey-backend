"""Publish task.started / task.finished over WebSocket for manual admin tasks.

Connected from ``MonkeyConfig.ready``. Filtered to the manual-runnable task set
(``TASK_MAP``) so the high-frequency per-monkey/scheduled tasks don't flood the
admin channel. Finish status (ok/error) is read from the ``BaseTask`` envelope.
"""

from celery.signals import task_postrun, task_prerun


def _manual_task_names():
    from monkey.task_catalog import TASK_MAP

    return {task.name for task in TASK_MAP.values()}


def on_task_prerun(task_id=None, task=None, **kwargs):
    name = getattr(task, "name", None)
    if name not in _manual_task_names():
        return
    from monkey import realtime

    realtime.publish_task(name, task_id, "started")


def on_task_postrun(task_id=None, task=None, retval=None, state=None, **kwargs):
    name = getattr(task, "name", None)
    if name not in _manual_task_names():
        return
    ok, error = True, None
    if isinstance(retval, dict) and "ok" in retval:
        ok = bool(retval["ok"])
        error = retval.get("error")
    elif state and state != "SUCCESS":
        ok = False
    from monkey import realtime

    realtime.publish_task(name, task_id, "finished", ok=ok, error=error)


def connect():
    task_prerun.connect(on_task_prerun, dispatch_uid="monkey_task_prerun")
    task_postrun.connect(on_task_postrun, dispatch_uid="monkey_task_postrun")
