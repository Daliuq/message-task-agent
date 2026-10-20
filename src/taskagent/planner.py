from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .store import Store


TIMEZONE_NAME = "Asia/Shanghai"
SLOT_MINUTES = 15


def _timezone() -> ZoneInfo:
    try:
        return ZoneInfo(TIMEZONE_NAME)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError("缺少 IANA 时区数据库；请安装 planner 可选依赖 tzdata。") from exc


def _minute(value: str, *, allow_24: bool = False) -> int:
    try:
        hour_text, minute_text = value.strip().split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"时间必须为 HH:MM：{value}") from exc
    if minute not in range(60) or hour not in range(24 + int(allow_24)):
        raise ValueError(f"时间超出范围：{value}")
    if hour == 24 and (minute != 0 or not allow_24):
        raise ValueError("24 点只能写成 24:00，且仅允许作为结束时间")
    return hour * 60 + minute


def parse_window(value: str) -> tuple[int, int]:
    parts = value.split("-", 1)
    if len(parts) != 2:
        raise ValueError(f"时间窗格式应为 HH:MM-HH:MM：{value}")
    start, end = _minute(parts[0]), _minute(parts[1], allow_24=True)
    if end <= start:
        raise ValueError(f"时间窗结束必须晚于开始：{value}")
    return start, end


def parse_busy(value: str) -> dict[str, Any]:
    interval, sep, label = value.partition("=")
    start, end = parse_window(interval)
    return {"start_minute": start, "end_minute": end,
            "title": label.strip() if sep and label.strip() else "固定安排"}


def _local_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    timezone = _timezone()
    return parsed.replace(tzinfo=timezone) if parsed.tzinfo is None else parsed.astimezone(timezone)


def _local_stamp(day: date, minute: int) -> str:
    return (datetime.combine(day, time.min, tzinfo=_timezone()) + timedelta(minutes=minute)).isoformat(timespec="minutes")


def _weight(task: dict[str, Any], day: date) -> int:
    base = {1: 1000, 2: 700, 3: 450, 4: 250, 5: 100}[int(task.get("priority", 3))]
    due_at = task.get("due_at")
    if not due_at:
        return base
    due_day = _local_datetime(due_at).date()
    days = (due_day - day).days
    urgency = 600 if days <= 0 else 400 if days == 1 else 200 if days <= 3 else 100 if days <= 7 else 0
    return base + urgency


def _available_starts(windows: list[tuple[int, int]], duration: int) -> list[int]:
    starts: set[int] = set()
    for begin, end in windows:
        if end - begin < duration:
            continue
        starts.update(range(begin, end - duration + 1, SLOT_MINUTES))
    return sorted(starts)


def plan_day(
    store: Store,
    planning_date: str,
    windows: list[str],
    *,
    busy: list[str] | None = None,
    time_limit_seconds: float = 5.0,
) -> dict[str, Any]:
    """Build and persist a single-day optional-task schedule using CP-SAT."""
    try:
        from ortools.sat.python import cp_model
    except ImportError as exc:
        raise RuntimeError("缺少 OR-Tools，请先安装：python -m pip install -e '.[planner]'") from exc

    try:
        day = date.fromisoformat(planning_date)
    except ValueError as exc:
        raise ValueError("日期必须为 YYYY-MM-DD") from exc
    if not windows:
        raise ValueError("至少指定一个明确可用时间窗 --window HH:MM-HH:MM")
    parsed_windows = [parse_window(value) for value in windows]
    parsed_busy = [parse_busy(value) for value in (busy or [])]

    # For a plan made for today, do not place work in the past.
    now = datetime.now(_timezone())
    if now.date() == day:
        current_minute = now.hour * 60 + now.minute
        first_future_slot = ((current_minute + SLOT_MINUTES - 1) // SLOT_MINUTES) * SLOT_MINUTES
        parsed_windows = [(max(start, first_future_slot), end) for start, end in parsed_windows
                          if max(start, first_future_slot) < end]

    candidates: list[dict[str, Any]] = []
    preliminary: dict[str, tuple[str, str]] = {}
    for task in store.tasks():
        if task["status"] not in {"open", "deferred"}:
            continue
        duration: int | None = None
        fixed_start: int | None = None
        fixed_end: int | None = None
        if task.get("fixed_start_at") and task.get("fixed_end_at"):
            fixed_start_dt = _local_datetime(task["fixed_start_at"])
            fixed_end_dt = _local_datetime(task["fixed_end_at"])
            if fixed_start_dt.date() != day or fixed_end_dt.date() != day:
                preliminary[task["id"]] = ("outside_horizon", "固定时间不在本次规划日期内")
                continue
            fixed_start = fixed_start_dt.hour * 60 + fixed_start_dt.minute
            fixed_end = fixed_end_dt.hour * 60 + fixed_end_dt.minute
            duration = fixed_end - fixed_start
            if duration <= 0:
                preliminary[task["id"]] = ("invalid_fixed_time", "固定开始/结束时间无效")
                continue
        elif task.get("estimated_minutes"):
            duration = int(task["estimated_minutes"])
        else:
            preliminary[task["id"]] = ("needs_estimate", "缺少预计耗时，暂不编排")
            continue

        if fixed_start is not None and fixed_end is not None:
            starts = [fixed_start] if any(fixed_start >= start and fixed_end <= end
                                          for start, end in parsed_windows) else []
        else:
            starts = _available_starts(parsed_windows, duration)
        if not starts:
            preliminary[task["id"]] = ("no_available_window", "指定可用时间内没有足够长的时段")
            continue

        due_minute: int | None = None
        if task.get("due_at"):
            due = _local_datetime(task["due_at"])
            if due.date() < day:
                preliminary[task["id"]] = ("overdue", f"截止时间已过：{due.isoformat(timespec='minutes')}")
                continue
            if due.date() == day:
                due_minute = due.hour * 60 + due.minute
                starts = [start for start in starts if start + duration <= due_minute]
                if not starts:
                    preliminary[task["id"]] = ("deadline_conflict", "可用时间窗与截止时间冲突，无法在截止前完成")
                    continue

        candidates.append({**task, "duration": duration, "starts": starts,
                           "fixed_start": fixed_start, "fixed_end": fixed_end,
                           "weight": _weight(task, day)})

    model = cp_model.CpModel()
    intervals = []
    busy_intervals: list[dict[str, Any]] = []
    for index, block in enumerate(parsed_busy):
        start, end = int(block["start_minute"]), int(block["end_minute"])
        interval = model.new_interval_var(start, end - start, end, f"busy_{index}")
        intervals.append(interval)
        busy_intervals.append({**block, "interval": interval})

    decision_vars: dict[str, Any] = {}
    chosen_start_vars: dict[str, Any] = {}
    for index, task in enumerate(candidates):
        selected = model.new_bool_var(f"selected_{index}")
        start = model.new_int_var_from_domain(cp_model.Domain.FromValues(task["starts"]), f"start_{index}")
        end = model.new_int_var(0, 1440, f"end_{index}")
        model.add(end == start + task["duration"])
        interval = model.new_optional_interval_var(start, task["duration"], end, selected,
                                                   f"task_{index}")
        intervals.append(interval)
        chosen_start = model.new_int_var(0, 1440, f"chosen_start_{index}")
        model.add(chosen_start == start).only_enforce_if(selected)
        model.add(chosen_start == 0).only_enforce_if(selected.Not())
        decision_vars[task["id"]] = (selected, start, end)
        chosen_start_vars[task["id"]] = chosen_start

    if intervals:
        model.add_no_overlap(intervals)
    model.maximize(sum(task["weight"] * 1000 * decision_vars[task["id"]][0]
                       - chosen_start_vars[task["id"]] for task in candidates))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(0.1, float(time_limit_seconds))
    solver.parameters.num_search_workers = 8
    solver.parameters.random_seed = 0
    solve_status = solver.solve(model)
    status_name = solver.status_name(solve_status)
    has_solution = solve_status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    selected_tasks: list[dict[str, Any]] = []
    if has_solution:
        for task in candidates:
            selected, start_var, end_var = decision_vars[task["id"]]
            if solver.boolean_value(selected):
                start_minute = solver.value(start_var)
                end_minute = solver.value(end_var)
                selected_tasks.append({**task, "start_minute": start_minute, "end_minute": end_minute})
    selected_tasks.sort(key=lambda item: (item["start_minute"], item["title"]))

    result_items: list[dict[str, Any]] = []
    scheduled: list[dict[str, Any]] = []
    not_scheduled: list[dict[str, Any]] = []
    selected_by_id = {item["id"]: item for item in selected_tasks}
    for task in store.tasks():
        if task["status"] not in {"open", "deferred"}:
            continue
        task_id = task["id"]
        if task_id in selected_by_id:
            chosen = selected_by_id[task_id]
            start_at = _local_stamp(day, chosen["start_minute"])
            end_at = _local_stamp(day, chosen["end_minute"])
            reason = (f"已纳入推荐时间表；优先级 {task['priority']}，预计 {chosen['duration']} 分钟，"
                      f"综合优先级与截止时间求解。")
            item = {"task_id": task_id, "title": task["title"], "decision": "scheduled",
                    "starts_at": start_at, "ends_at": end_at, "reason": reason}
            result_items.append(item)
            scheduled.append(item)
            continue

        if task_id in preliminary:
            decision, reason = preliminary[task_id]
        elif not has_solution:
            decision = "not_solved"
            reason = f"求解器未返回可行排程（{status_name}）；任务未安排且仍保持待办。"
        else:
            candidate = next(value for value in candidates if value["id"] == task_id)
            conflicting = []
            for chosen in selected_tasks:
                if any(start < chosen["end_minute"] and start + candidate["duration"] > chosen["start_minute"]
                       for start in candidate["starts"]):
                    conflicting.append(chosen["title"])
            for block in busy_intervals:
                if any(start < block["end_minute"] and start + candidate["duration"] > block["start_minute"]
                       for start in candidate["starts"]):
                    conflicting.append(block["title"])
            decision = "unselected"
            if conflicting:
                names = "、".join(dict.fromkeys(conflicting))
                reason = f"与已安排的「{names}」存在时段冲突；本次综合优先级和截止时间后未选。事项仍保持待办。"
            else:
                reason = "本次整体排程选择了得分更高的可行事项；当前未安排，事项仍保持待办。"
        item = {"task_id": task_id, "title": task["title"], "decision": decision,
                "starts_at": None, "ends_at": None, "reason": reason}
        result_items.append(item)
        not_scheduled.append(item)

    objective_value = solver.objective_value if has_solution else None
    objective = {"date": day.isoformat(), "timezone": "Asia/Shanghai", "windows": windows,
                 "busy": busy or [], "selected_count": len(scheduled), "considered_count": len(result_items),
                 "objective_value": objective_value, "solver_status": status_name,
                 "weights": "priority base + deadline urgency; lower priority number is higher"}
    explanation = ("求解器已证明当前模型下为最优解。" if status_name == "OPTIMAL"
                  else "当前时间限制内找到推荐可行解。" if status_name == "FEASIBLE"
                  else f"求解器未找到可行排程：{status_name}。")
    plan_id = store.save_plan(status_name.lower(), objective, explanation, result_items)
    return {"plan_run_id": plan_id, "planning_date": day.isoformat(), "timezone": "Asia/Shanghai",
            "solver_status": status_name, "recommendation": explanation,
            "availability_windows": windows, "fixed_busy_blocks": busy or [],
            "scheduled": scheduled, "not_scheduled": not_scheduled,
            "objective_value": objective_value}
