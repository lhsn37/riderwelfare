from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")
START_TIME = time(9, 0)
END_TIME = time(23, 59, 59)
INTERVAL_MINUTES = 19


def current_slot(now: datetime | None = None) -> str | None:
    """Return the current fixed 19-minute schedule slot in Korea time."""
    now = now or datetime.now(KST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=KST)
    else:
        now = now.astimezone(KST)

    local_time = now.time().replace(tzinfo=None)
    if local_time < START_TIME or local_time > END_TIME:
        return None

    elapsed = (now.hour * 60 + now.minute) - (START_TIME.hour * 60 + START_TIME.minute)
    index = elapsed // INTERVAL_MINUTES
    scheduled_minute = START_TIME.hour * 60 + START_TIME.minute + index * INTERVAL_MINUTES
    hour, minute = divmod(scheduled_minute, 60)
    return f"{now.date().isoformat()}-{hour:02d}{minute:02d}"


def schedule_label() -> str:
    return "매일 09:00~23:59 · 19분 간격"
