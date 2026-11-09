from calendar import monthrange
from datetime import date, timedelta
from ..extensions import db
from ..models import Task, TaskSchedule


def _safe_day(year: int, month: int, day: int) -> date:
    last = monthrange(year, month)[1]
    return date(year, month, max(1, min(day, last)))


def schedule_due_dates(schedule: TaskSchedule, today: date | None = None):
    today = today or date.today()
    values = []
    if schedule.frequency == "once":
        if schedule.one_time_due_on:
            values.append(schedule.one_time_due_on)
    elif schedule.frequency == "annual":
        month = max(1, min(int(schedule.month_of_year or 1), 12))
        day = int(schedule.day_of_month or 1)
        values.extend([_safe_day(today.year, month, day), _safe_day(today.year + 1, month, day)])
    else:
        day = int(schedule.day_of_month or 25)
        year, month = today.year, today.month
        for offset in (-1, 0, 1, 2):
            y, m = year, month + offset
            while m < 1:
                y -= 1; m += 12
            while m > 12:
                y += 1; m -= 12
            values.append(_safe_day(y, m, day))
    return sorted(set(values))


def ensure_scheduled_tasks(organization_id: int, today: date | None = None):
    today = today or date.today()
    changed = False
    schedules = TaskSchedule.query.filter_by(organization_id=organization_id, enabled=True).all()
    for schedule in schedules:
        for due in schedule_due_dates(schedule, today):
            if due < today - timedelta(days=31) or due > today + timedelta(days=62):
                continue
            external_ref = f"schedule:{schedule.id}:{due.isoformat()}"
            exists = Task.query.filter_by(organization_id=organization_id, external_ref=external_ref).first()
            if exists:
                continue
            db.session.add(Task(
                organization_id=organization_id,
                project_id=schedule.project_id,
                title=schedule.title,
                task_type=schedule.task_type,
                due_date=due,
                status="open",
                priority=schedule.priority,
                source="schedule",
                external_ref=external_ref,
                note=schedule.note,
            ))
            changed = True
    if changed:
        db.session.commit()
    return changed


def reminder_items(organization_id: int, today: date | None = None, limit: int = 12):
    today = today or date.today()
    ensure_scheduled_tasks(organization_id, today)
    schedules = {s.id: s for s in TaskSchedule.query.filter_by(organization_id=organization_id).all()}
    rows = Task.query.filter(
        Task.organization_id == organization_id,
        Task.status != "done",
        Task.due_date.isnot(None),
        Task.due_date <= today + timedelta(days=30),
    ).order_by(Task.due_date.asc()).all()
    result = []
    for task in rows:
        remind_days = 3
        if task.external_ref and task.external_ref.startswith("schedule:"):
            parts = task.external_ref.split(":")
            try:
                schedule = schedules.get(int(parts[1]))
                if schedule:
                    remind_days = max(0, int(schedule.reminder_days or 0))
            except (ValueError, IndexError):
                pass
        days = (task.due_date - today).days
        if days <= remind_days:
            result.append({"task": task, "days": days, "overdue": days < 0, "today": days == 0})
    return result[:limit]
