"""Celery tasks an admin can trigger manually from the management UI.

Single source of truth shared by the API (``GlobalMonkeyControlViewSet`` exposes
``tasks``/``run-task`` actions) and the React manage page. Each entry pairs a
stable task name with the task object plus a Korean label/description for the UI.

A non-empty ``warnings`` list marks a *dangerous* task (places real orders, moves
cash, or flips the trading gate); each item describes one consequence of running
it, shown as a bullet list in the confirmation dialog.
"""

from market import tasks as market_tasks
from monkey import tasks as monkey_tasks

# (name, task, label, description, warnings) — order is the order shown in the UI.
# An empty warnings list means the task is safe (read/refresh only).
RUNNABLE_TASKS = [
    (
        "run_monkeys",
        monkey_tasks.run_monkeys,
        "원숭이 거래 실행",
        "활성 상태인 모든 원숭이가 무작위 주문을 한 번씩 시도합니다.",
        [
            "활성 상태인 모든 원숭이가 무작위 종목을 매수 또는 매도합니다.",
            "실제 KIS 모의투자 계좌로 주문이 전송됩니다.",
        ],
    ),
    (
        "liquidate_orphaned_holdings",
        monkey_tasks.liquidate_orphaned_holdings,
        "미아 보유분 청산",
        "제거된 원숭이가 남긴 보유 종목을 매도합니다.",
        [
            "폐사한 원숭이가 남긴 보유 종목을 시장가로 매도합니다.",
            "실제 KIS 계좌로 매도 주문이 전송됩니다.",
        ],
    ),
    (
        "auto_create_monkeys",
        monkey_tasks.auto_create_monkeys,
        "원숭이 자동 생성",
        "남은 예수금으로 가능한 만큼 새 원숭이를 생성합니다.",
        [
            "미배정 예수금을 기본 시작 자본금으로 나눈 만큼 새 원숭이가 생성됩니다.",
            "생성된 원숭이는 즉시 거래 대상에 포함됩니다.",
        ],
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
    ),
    (
        "update_held_stock_prices",
        monkey_tasks.update_held_stock_prices,
        "보유 종목 시세 갱신",
        "원숭이들이 보유한 종목의 현재가를 KIS에서 새로 받아옵니다.",
        [],
    ),
    (
        "reconcile_executions",
        monkey_tasks.reconcile_executions,
        "체결 내역 정산",
        "KIS 일별 체결 내역으로 주문의 실제 체결 수량·가격을 보정합니다.",
        [],
    ),
    (
        "snapshot_monkeys",
        monkey_tasks.snapshot_monkeys,
        "일일 스냅샷 기록",
        "원숭이별 자산·수익률 일일 스냅샷을 저장합니다.",
        [],
    ),
    (
        "record_earning_ratio_tick",
        monkey_tasks.record_earning_ratio_tick,
        "수익률 틱 기록",
        "전체 평균 수익률을 시계열 틱으로 기록합니다.",
        [],
    ),
    (
        "update_market",
        market_tasks.update_market,
        "종목 마스터 갱신",
        "KRX 코스피·코스닥 종목 목록을 내려받아 갱신합니다.",
        [],
    ),
    (
        "update_token",
        monkey_tasks.update_token,
        "KIS 토큰 갱신",
        "KIS 접근 토큰을 재발급합니다.",
        [],
    ),
    (
        "check_holiday",
        monkey_tasks.check_holiday,
        "휴장일 확인",
        "오늘이 개장일인지 KIS 휴장일 API로 확인하고 게이트를 갱신합니다.",
        [],
    ),
]

# name -> task, for resolving a run-task request.
TASK_MAP = {
    name: task for name, task, _label, _description, _warnings in RUNNABLE_TASKS
}

# Celery task path (e.g. "monkey.tasks.market_open") -> Korean label, so the
# schedule table can label a PeriodicTask by its registered task name.
LABEL_BY_TASK_PATH = {
    task.name: label for _name, task, label, _description, _warnings in RUNNABLE_TASKS
}

# Celery task path -> Korean description, so the schedule/interval tables can
# reuse the same one-line description shown on the run-task card.
DESCRIPTION_BY_TASK_PATH = {
    task.name: description
    for _name, task, _label, description, _warnings in RUNNABLE_TASKS
}

# Serializable catalog the API hands to the frontend (no task objects).
TASK_CATALOG = [
    {
        "name": name,
        "task": task.name,
        "label": label,
        "description": description,
        "dangerous": bool(warnings),
        "warnings": warnings,
    }
    for name, task, label, description, warnings in RUNNABLE_TASKS
]
