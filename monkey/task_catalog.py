"""Celery tasks an admin can trigger manually from the management UI.

Single source of truth shared by the API (``GlobalMonkeyControlViewSet`` exposes
``tasks``/``run-task`` actions) and the React manage page. Each entry pairs a
stable task name with the task object plus a Korean label/description for the UI.

A non-empty ``warnings`` list marks a *dangerous* task (places real orders, moves
cash, or flips the trading gate); each item describes one consequence of running
it, shown as a bullet list in the confirmation dialog.

``when`` declares the market-state prerequisite shown in the UI and used to disable
the run button when the current state conflicts:
  "market_open"   — task only makes sense while the market is open
  "market_closed" — task only makes sense while the market is closed
  None            — no market-state restriction

``manual`` controls whether the task appears in the manual-run UI at all. Set to
``False`` for tasks that are fully automated (e.g. market open/close, index ticks)
and should never be triggered by hand. These tasks are still included in the full
catalog so their labels/descriptions populate the schedule and interval tables.
"""

from market import tasks as market_tasks
from monkey import tasks as monkey_tasks

# (name, task, label, description, warnings, when, manual)
# manual=False → label/description still appear in schedule/interval tables,
#                but the task is hidden from the manual-run UI entirely.
RUNNABLE_TASKS = [
    (
        "update_token",
        monkey_tasks.update_token,
        "KIS 토큰 갱신",
        "KIS 접근 토큰을 재발급합니다.",
        [],
        None,
        True,
    ),
    (
        "check_holiday",
        monkey_tasks.check_holiday,
        "휴장일 확인",
        "오늘이 개장일인지 KIS 휴장일 API로 확인하고 게이트를 갱신합니다.",
        [],
        None,
        True,
    ),
    (
        "update_market",
        market_tasks.update_market,
        "종목 마스터 갱신",
        "KRX 코스피·코스닥 종목 목록을 내려받아 갱신합니다.",
        [],
        None,
        True,
    ),
    (
        "record_index_tick",
        monkey_tasks.record_index_tick,
        "원숭이 지수 틱 기록",
        "현재 원숭이 지수 값을 시계열 틱으로 기록합니다. 장중에만 유효합니다.",
        [],
        "market_open",
        False,  # automated every N seconds; no value in manual runs
    ),
    (
        "update_all_stock_prices",
        monkey_tasks.update_all_stock_prices,
        "전체 종목 시세 갱신",
        "보유 여부와 관계없이 모든 활성 종목의 현재가를 KIS에서 새로 받아옵니다. 종목 수만큼 호출하므로 시간이 오래 걸립니다.",
        [],
        None,
        True,
    ),
    (
        "update_held_stock_prices",
        monkey_tasks.update_held_stock_prices,
        "보유 종목 시세 갱신",
        "원숭이들이 보유한 종목의 현재가를 KIS에서 새로 받아옵니다. 장중에만 유효합니다.",
        [],
        "market_open",
        True,
    ),
    (
        "run_system_monkey",
        monkey_tasks.run_system_monkey,
        "시스템 원숭이 청산 실행",
        "시스템 원숭이가 보유한 종목 중 하나를 무작위로 골라 전량 매도합니다. 장중에만 실행됩니다.",
        [],
        "market_open",
        True,
    ),
    (
        "finalize_filled_orders",
        monkey_tasks.finalize_filled_orders,
        "체결 완료 주문 반영",
        "체결이 완전히 끝난 접수 주문을 KIS 체결 내역대로 원숭이 잔고·보유에 반영합니다. 장중에만 실행됩니다.",
        [],
        "market_open",
        True,
    ),
    (
        "auto_create_monkeys",
        monkey_tasks.auto_create_monkeys,
        "원숭이 자동 생성",
        "남은 예수금으로 가능한 만큼 새 원숭이를 생성합니다. 장 마감 중에만 실행됩니다.",
        [],
        "market_closed",
        True,
    ),
    (
        "reconcile_executions",
        monkey_tasks.reconcile_executions,
        "체결 내역 정산",
        "장 마감 후 남은 접수 주문을 실제 체결 수량·금액(미체결 포함)대로 마감 처리합니다. 장 마감 후에만 실행됩니다.",
        [],
        "market_closed",
        True,
    ),
    (
        "snapshot_monkeys",
        monkey_tasks.snapshot_monkeys,
        "일일 스냅샷 기록",
        "원숭이별 자산·수익률 일일 스냅샷을 저장합니다. 장 마감 후에만 실행됩니다.",
        [],
        "market_closed",
        True,
    ),
    (
        "market_open",
        monkey_tasks.market_open,
        "장 개시 처리",
        "시간 게이트를 열고 원숭이 주기 작업을 활성화합니다.",
        [
            "시간 게이트가 열려 자동 거래가 시작됩니다.",
            "활성 원숭이들의 주기 작업이 활성화됩니다.",
        ],
        None,
        False,  # automated by beat schedule; manually running out of sequence breaks state
    ),
    (
        "market_close",
        monkey_tasks.market_close,
        "장 마감 처리",
        "시간 게이트를 닫고 원숭이 주기 작업을 비활성화합니다.",
        [
            "시간 게이트가 닫혀 자동 거래가 중단됩니다.",
            "원숭이들의 주기 작업이 비활성화됩니다.",
        ],
        None,
        False,  # automated by beat schedule; manually running out of sequence breaks state
    ),
    (
        "daily_maintenance",
        monkey_tasks.daily_maintenance,
        "일일 정리 작업",
        "장 마감 중에 부진한 원숭이를 사망 처리하고, 미아·상장폐지·사망 보유분을 시스템 원숭이로 이관합니다.",
        [
            "수익률이 사망 기준 미만인 원숭이가 사망 처리됩니다.",
            "장이 열려 있는 동안에는 실행되지 않고 건너뜁니다.",
        ],
        "market_closed",
        True,
    ),
]

# name -> task, for resolving a run-task request (manual-only tasks).
TASK_MAP = {
    name: task
    for name, task, _label, _description, _warnings, _when, manual in RUNNABLE_TASKS
    if manual
}

# Celery task path (e.g. "monkey.tasks.market_open") -> Korean label, so the
# schedule table can label a PeriodicTask by its registered task name.
# Includes ALL tasks (manual or not) so automated tasks still get Korean labels.
LABEL_BY_TASK_PATH = {
    task.name: label
    for _name, task, label, _description, _warnings, _when, _manual in RUNNABLE_TASKS
}

# Celery task path -> Korean description, so the schedule/interval tables can
# reuse the same one-line description shown on the run-task card.
# Includes ALL tasks.
DESCRIPTION_BY_TASK_PATH = {
    task.name: description
    for _name, task, _label, description, _warnings, _when, _manual in RUNNABLE_TASKS
}

# Serializable catalog the API hands to the frontend (no task objects).
# Only manual=True tasks — automated tasks have no run button.
TASK_CATALOG = [
    {
        "name": name,
        "task": task.name,
        "label": label,
        "description": description,
        "dangerous": bool(warnings),
        "warnings": warnings,
        "when": when,
    }
    for name, task, label, description, warnings, when, manual in RUNNABLE_TASKS
    if manual
]
