# roulette_db.py
from __future__ import annotations

import json
import random
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timedelta
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

KST = ZoneInfo("Asia/Seoul") if ZoneInfo else None

from pathlib import Path
from typing import Any, Dict, List, Optional


DB_LOCK = threading.RLock()


def now_ts() -> int:
    return int(time.time())


def normalize_phone(phone: str) -> str:
    return "".join(ch for ch in str(phone or "") if ch.isdigit())


def now_kst() -> datetime:
    """현재 한국시간 반환. 서버 시간이 UTC여도 룰렛 기준은 한국시간으로 고정합니다."""
    if KST:
        return datetime.now(KST)
    return datetime.now()


def operation_datetime(now: Optional[datetime] = None) -> datetime:
    """라웰 운영일 기준 datetime. 00:00~05:59는 전날 운영일로 봅니다."""
    now = now or now_kst()
    if KST and now.tzinfo is None:
        now = now.replace(tzinfo=KST)
    if now.hour < 6:
        return now - timedelta(days=1)
    return now


def today_str(now: Optional[datetime] = None) -> str:
    """룰렛/출석 운영일 문자열. 기준: 06:00 ~ 다음날 05:59."""
    return operation_datetime(now).strftime("%Y-%m-%d")


def operation_date_from_epoch(ts_value: int | float | str | None) -> str:
    """created_at(epoch)를 기준으로 06:00 운영일 문자열을 계산합니다."""
    try:
        created = datetime.fromtimestamp(int(ts_value or 0), KST) if KST else datetime.fromtimestamp(int(ts_value or 0))
    except Exception:
        created = now_kst()
    return today_str(created)


def get_operational_cycle_bounds(now: Optional[datetime] = None) -> Dict[str, Any]:
    """
    운영 주차 기준
    - 시작: 수요일 06:00
    - 마감: 다음주 수요일 03:00 직전
    - 화요일 자정 넘어서 수요일 03:00까지는 이전 주차 포함

    주차 판정 경계는 수요일 03:00 으로 처리
    """
    now = now or now_kst()

    # 수요일 03:00 이전 구간은 이전 운영 주차로 묶기 위해 3시간 차감
    pivot = now - timedelta(hours=3)

    # Python weekday: 월0 화1 수2 목3 금4 토5 일6
    wd = pivot.weekday()
    days_since_wed = (wd - 2) % 7

    cycle_anchor = (pivot - timedelta(days=days_since_wed)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    cycle_key = cycle_anchor.strftime("%Y-%m-%d")
    start_at = cycle_anchor.replace(hour=6, minute=0, second=0, microsecond=0)
    close_at = (cycle_anchor + timedelta(days=7)).replace(
        hour=3, minute=0, second=0, microsecond=0
    )

    return {
        "cycle_key": cycle_key,
        "start_date": cycle_anchor.strftime("%Y-%m-%d"),
        "end_date": (cycle_anchor + timedelta(days=7)).strftime("%Y-%m-%d"),
        "start_at": start_at,
        "close_at": close_at,
        "display_start": start_at.strftime("%Y-%m-%d %H:%M:%S"),
        "display_end": close_at.strftime("%Y-%m-%d %H:%M:%S"),
    }


def get_previous_operational_cycle_bounds(now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or now_kst()
    current = get_operational_cycle_bounds(now)
    current_anchor = datetime.strptime(current["cycle_key"], "%Y-%m-%d")
    prev_anchor = current_anchor - timedelta(days=7)

    start_at = prev_anchor.replace(hour=6, minute=0, second=0, microsecond=0)
    close_at = (prev_anchor + timedelta(days=7)).replace(
        hour=3, minute=0, second=0, microsecond=0
    )

    return {
        "cycle_key": prev_anchor.strftime("%Y-%m-%d"),
        "start_date": prev_anchor.strftime("%Y-%m-%d"),
        "end_date": (prev_anchor + timedelta(days=7)).strftime("%Y-%m-%d"),
        "start_at": start_at,
        "close_at": close_at,
        "display_start": start_at.strftime("%Y-%m-%d %H:%M:%S"),
        "display_end": close_at.strftime("%Y-%m-%d %H:%M:%S"),
    }


def current_week_key() -> str:
    return get_operational_cycle_bounds()["cycle_key"]


def is_roulette_open_now(now: Optional[datetime] = None) -> bool:
    """
    수요일 03:00 ~ 05:59 는 운영 공백으로 보고 룰렛 비활성화
    """
    now = now or now_kst()
    return not (now.weekday() == 2 and 3 <= now.hour < 6)


class RouletteDB:
    def __init__(self, store_dir: str):
        self.store_dir = Path(store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)

        self.db_path = self.store_dir / "roulette.db"
        self.backup_dir = self.store_dir / "roulette_backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)

        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _table_columns(self, conn: sqlite3.Connection, table_name: str) -> List[str]:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return [r["name"] for r in rows]

    def _ensure_column(self, conn: sqlite3.Connection, table_name: str, column_name: str, ddl_tail: str):
        cols = self._table_columns(conn, table_name)
        if column_name not in cols:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {ddl_tail}")
            conn.commit()

    def _build_default_segment_rewards(self) -> List[tuple[int, int, int, int, int]]:
        """
        10건 ~ 100건 구간 기본 확률표
        형식: (segment_value, amount, weight, active, sort_order)

        원하면 관리자에서 언제든 수정 가능
        """
        # 구간별 기본 확률표 예시
        # 총합이 꼭 100일 필요는 없고, 비율만 맞으면 됨
        # 여기서는 보기 편하게 100 기준으로 맞춤
        weight_map = {
            10:  [50, 30, 15, 4, 1],
            20:  [45, 30, 15, 7, 3],
            30:  [40, 30, 17, 10, 3],
            40:  [36, 30, 19, 11, 4],
            50:  [33, 29, 20, 13, 5],
            60:  [30, 28, 21, 15, 6],
            70:  [28, 27, 21, 16, 8],
            80:  [26, 26, 22, 17, 9],
            90:  [24, 25, 23, 18, 10],
            100: [22, 24, 24, 19, 11],
        }
        amounts = [1000, 2000, 3000, 5000, 10000]

        rows: List[tuple[int, int, int, int, int]] = []
        for segment_value in range(10, 101, 10):
            weights = weight_map.get(segment_value, weight_map[100])
            for idx, amount in enumerate(amounts, start=1):
                rows.append((segment_value, amount, int(weights[idx - 1]), 1, idx))
        return rows

    def _init_db(self):
        with DB_LOCK:
            conn = self._connect()
            cur = conn.cursor()

            cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """)

            # 예전 단일 rewards 테이블 유지 (호환용)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS roulette_rewards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                amount INTEGER NOT NULL,
                weight INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 0
            )
            """)

            # 새 구간별 확률 테이블
            cur.execute("""
            CREATE TABLE IF NOT EXISTS roulette_segment_rewards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                segment_value INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                weight INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 0
            )
            """)

            cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_segment_rewards_segment
            ON roulette_segment_rewards(segment_value, sort_order, amount)
            """)

            cur.execute("""
            CREATE TABLE IF NOT EXISTS roulette_spins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                spin_date TEXT NOT NULL,
                week_key TEXT NOT NULL,
                phone TEXT NOT NULL,
                rider_name TEXT,
                today_completed INTEGER NOT NULL DEFAULT 0,
                eligible_count INTEGER NOT NULL DEFAULT 0,
                used_count_before INTEGER NOT NULL DEFAULT 0,
                spin_index INTEGER NOT NULL DEFAULT 1,
                segment_value INTEGER NOT NULL DEFAULT 10,
                reward_amount INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                note TEXT DEFAULT ''
            )
            """)

            self._ensure_column(conn, "roulette_spins", "spin_index", "spin_index INTEGER NOT NULL DEFAULT 1")
            self._ensure_column(conn, "roulette_spins", "segment_value", "segment_value INTEGER NOT NULL DEFAULT 10")

            cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_roulette_spins_phone_date
            ON roulette_spins(phone, spin_date)
            """)

            cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_roulette_spins_week_key
            ON roulette_spins(week_key)
            """)

            cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_roulette_spins_created_at
            ON roulette_spins(created_at DESC)
            """)

            cur.execute("""
            CREATE TABLE IF NOT EXISTS backup_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                backup_file TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """)

            conn.commit()

            self._set_if_missing(conn, "roulette_enabled", "true")
            self._set_if_missing(conn, "spin_unit", "10")
            self._set_if_missing(conn, "max_segment_value", "100")

            # 구버전 단일 rewards 기본값
            row = cur.execute("SELECT COUNT(*) AS c FROM roulette_rewards").fetchone()
            if int(row["c"] or 0) == 0:
                cur.executemany("""
                    INSERT INTO roulette_rewards (amount, weight, active, sort_order)
                    VALUES (?, ?, ?, ?)
                """, [
                    (1000, 40, 1, 1),
                    (2000, 30, 1, 2),
                    (3000, 15, 1, 3),
                    (5000, 10, 1, 4),
                    (10000, 5, 1, 5),
                ])
                conn.commit()

            # 새 구간별 rewards 기본값
            row = cur.execute("SELECT COUNT(*) AS c FROM roulette_segment_rewards").fetchone()
            if int(row["c"] or 0) == 0:
                cur.executemany("""
                    INSERT INTO roulette_segment_rewards
                    (segment_value, amount, weight, active, sort_order)
                    VALUES (?, ?, ?, ?, ?)
                """, self._build_default_segment_rewards())
                conn.commit()

            # 기존 자정 기준으로 저장된 룰렛 이력을 06:00 운영일 기준으로 자동 보정
            self._normalize_existing_spin_dates_6am(conn)

            conn.close()

    def _normalize_existing_spin_dates_6am(self, conn: sqlite3.Connection) -> int:
        """기존 DB의 룰렛 이력을 06:00 운영일 기준으로 보정합니다.

        과거 코드가 자정 기준으로 저장한 00:00~05:59 당첨 이력은
        다음날 날짜로 들어가 있어 오늘 사용횟수에 잘못 포함됩니다.
        created_at 기준으로 spin_date와 week_key를 다시 계산해 보정합니다.
        """
        try:
            rows = conn.execute("""
                SELECT id, spin_date, week_key, created_at
                FROM roulette_spins
                WHERE created_at IS NOT NULL AND created_at > 0
            """).fetchall()
        except Exception:
            return 0

        updated = 0
        for row in rows:
            try:
                created_dt = datetime.fromtimestamp(int(row["created_at"]), KST) if KST else datetime.fromtimestamp(int(row["created_at"]))
                new_spin_date = today_str(created_dt)
                new_week_key = get_operational_cycle_bounds(created_dt)["cycle_key"]

                if str(row["spin_date"] or "") != new_spin_date or str(row["week_key"] or "") != new_week_key:
                    conn.execute("""
                        UPDATE roulette_spins
                        SET spin_date = ?, week_key = ?
                        WHERE id = ?
                    """, (new_spin_date, new_week_key, int(row["id"])))
                    updated += 1
            except Exception:
                continue

        if updated:
            conn.commit()
        return updated

    # -------------------------
    # settings
    # -------------------------
    def get_setting(self, key: str, default: str = "") -> str:
        with DB_LOCK:
            conn = self._connect()
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            conn.close()
            return row["value"] if row else default

    def set_setting(self, key: str, value: str):
        with DB_LOCK:
            conn = self._connect()
            conn.execute("""
                INSERT INTO settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """, (key, value))
            conn.commit()
            conn.close()

    def get_enabled(self) -> bool:
        return self.get_setting("roulette_enabled", "true").lower() == "true"

    def set_enabled(self, enabled: bool):
        self.set_setting("roulette_enabled", "true" if enabled else "false")

    def get_spin_unit(self) -> int:
        try:
            return max(1, int(self.get_setting("spin_unit", "10")))
        except Exception:
            return 10

    def set_spin_unit(self, unit: int):
        self.set_setting("spin_unit", str(max(1, int(unit))))

    def get_max_segment_value(self) -> int:
        try:
            value = int(self.get_setting("max_segment_value", "100"))
            return max(10, value)
        except Exception:
            return 100

    def set_max_segment_value(self, value: int):
        self.set_setting("max_segment_value", str(max(10, int(value))))

    # -------------------------
    # legacy rewards (호환용)
    # -------------------------
    def get_rewards(self) -> List[Dict[str, Any]]:
        with DB_LOCK:
            conn = self._connect()
            rows = conn.execute("""
                SELECT id, amount, weight, active, sort_order
                FROM roulette_rewards
                ORDER BY sort_order ASC, amount ASC, id ASC
            """).fetchall()
            conn.close()
            return [dict(r) for r in rows]

    def set_rewards(self, rewards: List[Dict[str, Any]]):
        cleaned: List[tuple[int, int, int, int]] = []
        for idx, r in enumerate(rewards, start=1):
            amount = int(r.get("amount", 0))
            weight = int(r.get("weight", 0))
            active = 1 if bool(r.get("active", True)) else 0
            if amount <= 0:
                continue
            cleaned.append((amount, max(0, weight), active, idx))

        if not cleaned:
            raise ValueError("보상 항목이 비어 있습니다.")

        with DB_LOCK:
            conn = self._connect()
            cur = conn.cursor()
            cur.execute("DELETE FROM roulette_rewards")
            cur.executemany("""
                INSERT INTO roulette_rewards (amount, weight, active, sort_order)
                VALUES (?, ?, ?, ?)
            """, cleaned)
            conn.commit()
            conn.close()

    # -------------------------
    # segment rewards
    # -------------------------
    def get_all_segment_rewards(self) -> List[Dict[str, Any]]:
        with DB_LOCK:
            conn = self._connect()
            rows = conn.execute("""
                SELECT id, segment_value, amount, weight, active, sort_order
                FROM roulette_segment_rewards
                ORDER BY segment_value ASC, sort_order ASC, amount ASC, id ASC
            """).fetchall()
            conn.close()
            return [dict(r) for r in rows]

    def get_segment_rewards(self, segment_value: int) -> List[Dict[str, Any]]:
        segment_value = int(segment_value)
        with DB_LOCK:
            conn = self._connect()
            rows = conn.execute("""
                SELECT id, segment_value, amount, weight, active, sort_order
                FROM roulette_segment_rewards
                WHERE segment_value = ?
                ORDER BY sort_order ASC, amount ASC, id ASC
            """, (segment_value,)).fetchall()
            conn.close()
            return [dict(r) for r in rows]

    def get_segment_reward_map(self) -> Dict[int, List[Dict[str, Any]]]:
        rows = self.get_all_segment_rewards()
        result: Dict[int, List[Dict[str, Any]]] = {}
        for r in rows:
            seg = int(r["segment_value"])
            result.setdefault(seg, []).append(r)
        return result

    def resolve_segment_value(self, requested_segment_value: int) -> int:
        """
        요청 구간이 정확히 없으면 가장 가까운 낮은 구간 사용
        예:
        - 40 있으면 40 사용
        - 70 있으면 70 사용
        - 110이면 100 사용
        - 5면 10 사용
        """
        requested_segment_value = int(requested_segment_value)

        with DB_LOCK:
            conn = self._connect()
            rows = conn.execute("""
                SELECT DISTINCT segment_value
                FROM roulette_segment_rewards
                ORDER BY segment_value ASC
            """).fetchall()
            conn.close()

        values = [int(r["segment_value"]) for r in rows]
        if not values:
            raise ValueError("구간 확률 설정이 없습니다.")

        candidates = [v for v in values if v <= requested_segment_value]
        if candidates:
            return max(candidates)
        return min(values)

    def set_segment_rewards(self, segment_rewards: List[Dict[str, Any]]):
        """
        입력 형식 예:
        [
          {"segment_value":10, "amount":1000, "weight":50, "active":True},
          {"segment_value":10, "amount":2000, "weight":30, "active":True},
          ...
          {"segment_value":100, "amount":10000, "weight":11, "active":True},
        ]
        """
        cleaned: List[tuple[int, int, int, int, int]] = []

        for r in segment_rewards:
            segment_value = int(r.get("segment_value", 0))
            amount = int(r.get("amount", 0))
            weight = int(r.get("weight", 0))
            active = 1 if bool(r.get("active", True)) else 0
            sort_order = int(r.get("sort_order", 0))

            if segment_value <= 0 or amount <= 0:
                continue

            # 10단위로 강제 보정
            segment_value = max(10, min(100, (segment_value // 10) * 10))
            if segment_value == 0:
                segment_value = 10

            if sort_order <= 0:
                # 금액 오름차순 기준으로 기본 정렬값
                sort_order = {
                    1000: 1,
                    2000: 2,
                    3000: 3,
                    5000: 4,
                    10000: 5,
                }.get(amount, 999)

            cleaned.append((segment_value, amount, max(0, weight), active, sort_order))

        if not cleaned:
            raise ValueError("구간별 보상 항목이 비어 있습니다.")

        # 10~100 구간 저장 가능하게 유효성 체크
        present_segments = sorted({row[0] for row in cleaned})
        invalid_segments = [seg for seg in present_segments if seg < 10 or seg > 100 or seg % 10 != 0]
        if invalid_segments:
            raise ValueError("구간은 10~100까지 10단위만 가능합니다.")

        with DB_LOCK:
            conn = self._connect()
            cur = conn.cursor()
            cur.execute("DELETE FROM roulette_segment_rewards")
            cur.executemany("""
                INSERT INTO roulette_segment_rewards
                (segment_value, amount, weight, active, sort_order)
                VALUES (?, ?, ?, ?, ?)
            """, cleaned)
            conn.commit()
            conn.close()

    def _pick_reward_by_segment(self, segment_value: int) -> tuple[int, int]:
        """
        반환:
          (resolved_segment_value, reward_amount)
        """
        resolved_segment = self.resolve_segment_value(segment_value)
        rewards = self.get_segment_rewards(resolved_segment)
        rewards = [r for r in rewards if int(r["active"]) == 1 and int(r["weight"]) > 0]

        if not rewards:
            raise ValueError("활성화된 구간 보상 항목이 없습니다.")

        amounts = [int(r["amount"]) for r in rewards]
        weights = [int(r["weight"]) for r in rewards]

        if sum(weights) <= 0:
            raise ValueError("구간 확률 합계가 0입니다.")

        picked = int(random.choices(amounts, weights=weights, k=1)[0])
        return resolved_segment, picked

    # -------------------------
    # spin helpers
    # -------------------------
    def get_today_spin_count(self, phone: str, spin_date: Optional[str] = None) -> int:
        phone = normalize_phone(phone)
        spin_date = spin_date or today_str()
        with DB_LOCK:
            conn = self._connect()
            row = conn.execute("""
                SELECT COUNT(*) AS c
                FROM roulette_spins
                WHERE phone = ? AND spin_date = ?
            """, (phone, spin_date)).fetchone()
            conn.close()
            return int(row["c"] or 0)

    def get_recent_spins(self, phone: str, limit: int = 5) -> List[Dict[str, Any]]:
        phone = normalize_phone(phone)
        with DB_LOCK:
            conn = self._connect()
            rows = conn.execute("""
                SELECT *
                FROM roulette_spins
                WHERE phone = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
            """, (phone, limit)).fetchall()
            conn.close()
            return [dict(r) for r in rows]

    def get_weekly_total(self, phone: str, week_key: Optional[str] = None) -> int:
        phone = normalize_phone(phone)
        week_key = week_key or current_week_key()
        with DB_LOCK:
            conn = self._connect()
            row = conn.execute("""
                SELECT COALESCE(SUM(reward_amount), 0) AS total
                FROM roulette_spins
                WHERE phone = ? AND week_key = ?
            """, (phone, week_key)).fetchone()
            conn.close()
            return int(row["total"] or 0)

    def _get_next_segment_value(self, used_count: int, spin_unit: int) -> int:
        """
        1번째 룰렛 = 10건 구간
        2번째 룰렛 = 20건 구간
        ...
        10번째 룰렛 = 100건 구간
        11번째 이상도 최대 100건 구간 확률 사용
        """
        spin_index = used_count + 1
        raw_segment = spin_index * spin_unit
        max_segment = self.get_max_segment_value()
        return min(max_segment, raw_segment)

    def get_status(self, phone: str, rider_name: str, today_completed: int) -> Dict[str, Any]:
        phone = normalize_phone(phone)
        rider_name = rider_name or ""

        enabled = self.get_enabled()
        spin_unit = self.get_spin_unit()
        used_count = self.get_today_spin_count(phone)
        eligible_count = max(0, int(today_completed) // spin_unit)
        remain_count = max(0, eligible_count - used_count)

        cycle = get_operational_cycle_bounds()
        week_key = cycle["cycle_key"]
        weekly_total = self.get_weekly_total(phone, week_key)
        recent_spins = self.get_recent_spins(phone, limit=5)

        next_spin_index = used_count + 1
        next_segment_value = self._get_next_segment_value(used_count, spin_unit) if remain_count > 0 else 0

        can_spin = enabled and remain_count > 0 and is_roulette_open_now()

        return {
            "enabled": enabled,
            "spin_unit": spin_unit,
            "today_completed": int(today_completed),
            "eligible_count": eligible_count,
            "used_count": used_count,
            "remain_count": remain_count,
            "can_spin": can_spin,
            "week_key": week_key,
            "weekly_total": weekly_total,
            "recent_spins": recent_spins,
            "rider_name": rider_name,
            "phone": phone,
            "cycle": cycle,
            "roulette_open_now": is_roulette_open_now(),
            "next_spin_index": next_spin_index,
            "next_segment_value": next_segment_value,
            "segment_reward_map": self.get_segment_reward_map(),
            "max_segment_value": self.get_max_segment_value(),
        }

    # -------------------------
    # spins
    # -------------------------
    def spin(self, phone: str, rider_name: str, today_completed: int) -> Dict[str, Any]:
        phone = normalize_phone(phone)
        rider_name = rider_name or ""
        spin_date = today_str()
        cycle = get_operational_cycle_bounds()
        week_key = cycle["cycle_key"]

        with DB_LOCK:
            conn = self._connect()
            cur = conn.cursor()

            enabled = self.get_enabled()
            if not enabled:
                conn.close()
                raise ValueError("룰렛이 비활성화 상태입니다.")

            if not is_roulette_open_now():
                conn.close()
                raise ValueError("현재는 룰렛 이용 가능 시간이 아닙니다.")

            spin_unit = self.get_spin_unit()
            eligible_count = max(0, int(today_completed) // spin_unit)

            row = cur.execute("""
                SELECT COUNT(*) AS c
                FROM roulette_spins
                WHERE phone = ? AND spin_date = ?
            """, (phone, spin_date)).fetchone()
            used_count = int(row["c"] or 0)

            remain_count = max(0, eligible_count - used_count)
            if remain_count <= 0:
                conn.close()
                raise ValueError("룰렛 가능 횟수가 없습니다.")

            spin_index = used_count + 1
            requested_segment_value = self._get_next_segment_value(used_count, spin_unit)
            resolved_segment_value, reward = self._pick_reward_by_segment(requested_segment_value)
            created = now_ts()

            cur.execute("""
                INSERT INTO roulette_spins (
                    spin_date, week_key, phone, rider_name,
                    today_completed, eligible_count, used_count_before,
                    spin_index, segment_value,
                    reward_amount, created_at, note
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                spin_date,
                week_key,
                phone,
                rider_name,
                int(today_completed),
                eligible_count,
                used_count,
                spin_index,
                resolved_segment_value,
                reward,
                created,
                ""
            ))
            conn.commit()

            row = cur.execute("SELECT last_insert_rowid() AS id").fetchone()
            spin_id = int(row["id"])

            conn.close()

        new_used = used_count + 1
        new_remain = max(0, eligible_count - new_used)
        weekly_total = self.get_weekly_total(phone, week_key)

        return {
            "ok": True,
            "spin_id": spin_id,
            "reward": reward,
            "eligible_count": eligible_count,
            "used_count": new_used,
            "remain_count": new_remain,
            "weekly_total": weekly_total,
            "spin_date": spin_date,
            "week_key": week_key,
            "created_at": created,
            "cycle": cycle,
            "spin_index": spin_index,
            "segment_value": resolved_segment_value,
        }

    # -------------------------
    # history / admin
    # -------------------------
    def get_history(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        keyword: str = "",
        limit: int = 300,
    ) -> List[Dict[str, Any]]:
        sql = """
            SELECT *
            FROM roulette_spins
            WHERE 1=1
        """
        params: List[Any] = []

        if start_date:
            sql += " AND spin_date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND spin_date <= ?"
            params.append(end_date)
        if keyword:
            kw = f"%{keyword.strip()}%"
            sql += " AND (rider_name LIKE ? OR phone LIKE ?)"
            params.extend([kw, kw])

        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(int(limit))

        with DB_LOCK:
            conn = self._connect()
            rows = conn.execute(sql, params).fetchall()
            conn.close()
            return [dict(r) for r in rows]

    def get_history_summary(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        keyword: str = "",
    ) -> Dict[str, Any]:
        sql = """
            SELECT
                COUNT(*) AS cnt,
                COALESCE(SUM(reward_amount), 0) AS total
            FROM roulette_spins
            WHERE 1=1
        """
        params: List[Any] = []

        if start_date:
            sql += " AND spin_date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND spin_date <= ?"
            params.append(end_date)
        if keyword:
            kw = f"%{keyword.strip()}%"
            sql += " AND (rider_name LIKE ? OR phone LIKE ?)"
            params.extend([kw, kw])

        with DB_LOCK:
            conn = self._connect()
            row = conn.execute(sql, params).fetchone()
            conn.close()
            return {
                "count": int(row["cnt"] or 0),
                "total_amount": int(row["total"] or 0),
            }

    def update_spin(self, spin_id: int, reward_amount: int, note: str = "") -> Dict[str, Any]:
        reward_amount = int(reward_amount)
        if reward_amount < 0:
            raise ValueError("금액은 0 이상이어야 합니다.")

        with DB_LOCK:
            conn = self._connect()
            cur = conn.cursor()

            row = cur.execute("SELECT * FROM roulette_spins WHERE id = ?", (spin_id,)).fetchone()
            if not row:
                conn.close()
                raise ValueError("해당 당첨 이력이 없습니다.")

            cur.execute("""
                UPDATE roulette_spins
                SET reward_amount = ?, note = ?
                WHERE id = ?
            """, (reward_amount, note or "", spin_id))
            conn.commit()

            updated = cur.execute("SELECT * FROM roulette_spins WHERE id = ?", (spin_id,)).fetchone()
            conn.close()
            return dict(updated)

    def delete_spin(self, spin_id: int):
        with DB_LOCK:
            conn = self._connect()
            cur = conn.cursor()

            row = cur.execute("SELECT id FROM roulette_spins WHERE id = ?", (spin_id,)).fetchone()
            if not row:
                conn.close()
                raise ValueError("해당 당첨 이력이 없습니다.")

            cur.execute("DELETE FROM roulette_spins WHERE id = ?", (spin_id,))
            conn.commit()
            conn.close()

    # -------------------------
    # payout / cycle
    # -------------------------
    def get_cycle_payout_rows(self, week_key: str) -> List[Dict[str, Any]]:
        with DB_LOCK:
            conn = self._connect()
            rows = conn.execute("""
                SELECT
                    phone,
                    MAX(COALESCE(rider_name, '')) AS rider_name,
                    COUNT(*) AS spin_count,
                    COALESCE(SUM(reward_amount), 0) AS total_amount
                FROM roulette_spins
                WHERE week_key = ?
                GROUP BY phone
                HAVING total_amount > 0
                ORDER BY total_amount DESC, rider_name ASC
            """, (week_key,)).fetchall()
            conn.close()
            return [dict(r) for r in rows]

    def get_cycle_summary(self, week_key: str) -> Dict[str, Any]:
        with DB_LOCK:
            conn = self._connect()
            row = conn.execute("""
                SELECT
                    COUNT(*) AS spin_count,
                    COUNT(DISTINCT phone) AS rider_count,
                    COALESCE(SUM(reward_amount), 0) AS total_amount
                FROM roulette_spins
                WHERE week_key = ?
            """, (week_key,)).fetchone()
            conn.close()
            return {
                "week_key": week_key,
                "spin_count": int(row["spin_count"] or 0),
                "rider_count": int(row["rider_count"] or 0),
                "total_amount": int(row["total_amount"] or 0),
            }

    def get_cycle_spins(self, week_key: str, limit: int = 1000) -> List[Dict[str, Any]]:
        with DB_LOCK:
            conn = self._connect()
            rows = conn.execute("""
                SELECT *
                FROM roulette_spins
                WHERE week_key = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
            """, (week_key, int(limit))).fetchall()
            conn.close()
            return [dict(r) for r in rows]

    # -------------------------
    # backup / export
    # -------------------------
    def create_backup(self) -> Dict[str, Any]:
        ts = now_kst().strftime("%Y%m%d_%H%M%S")
        backup_file = self.backup_dir / f"roulette_backup_{ts}.db"

        with DB_LOCK:
            shutil.copy2(self.db_path, backup_file)

            conn = self._connect()
            conn.execute("""
                INSERT INTO backup_logs (backup_file, created_at)
                VALUES (?, ?)
            """, (backup_file.name, now_ts()))
            conn.commit()
            conn.close()

        return {
            "ok": True,
            "backup_file": str(backup_file),
            "backup_name": backup_file.name,
        }

    def save_json_backup_on_disk(self, prefix: str = "roulette_backup") -> Dict[str, Any]:
        ts = now_kst().strftime("%Y%m%d_%H%M%S")
        backup_file = self.backup_dir / f"{prefix}_{ts}.json"
        payload = self.export_json()
        backup_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "ok": True,
            "backup_file": str(backup_file),
            "backup_name": backup_file.name,
        }

    def ensure_daily_backup(self) -> Dict[str, Any]:
        today = today_str()
        with DB_LOCK:
            last_backup_date = self.get_setting("last_daily_backup_date", "")
            if last_backup_date == today:
                return {
                    "ok": True,
                    "skipped": True,
                    "reason": "already_backed_up_today",
                    "backup_date": today,
                }

            db_backup = self.create_backup()
            json_backup = self.save_json_backup_on_disk(prefix="roulette_auto_backup")
            self.set_setting("last_daily_backup_date", today)
            return {
                "ok": True,
                "skipped": False,
                "backup_date": today,
                "db_backup_name": db_backup.get("backup_name", ""),
                "json_backup_name": json_backup.get("backup_name", ""),
            }

    def get_backup_logs(self, limit: int = 50) -> List[Dict[str, Any]]:
        with DB_LOCK:
            conn = self._connect()
            rows = conn.execute("""
                SELECT *
                FROM backup_logs
                ORDER BY created_at DESC, id DESC
                LIMIT ?
            """, (int(limit),)).fetchall()
            conn.close()
            return [dict(r) for r in rows]

    def export_json(self) -> Dict[str, Any]:
        with DB_LOCK:
            conn = self._connect()

            settings_rows = conn.execute("""
                SELECT *
                FROM settings
                ORDER BY key ASC
            """).fetchall()

            reward_rows = conn.execute("""
                SELECT id, amount, weight, active, sort_order
                FROM roulette_rewards
                ORDER BY sort_order ASC, id ASC
            """).fetchall()

            segment_rows = conn.execute("""
                SELECT id, segment_value, amount, weight, active, sort_order
                FROM roulette_segment_rewards
                ORDER BY segment_value ASC, sort_order ASC, id ASC
            """).fetchall()

            spin_rows = conn.execute("""
                SELECT *
                FROM roulette_spins
                ORDER BY created_at DESC, id DESC
            """).fetchall()

            backup_rows = conn.execute("""
                SELECT *
                FROM backup_logs
                ORDER BY created_at DESC, id DESC
            """).fetchall()

            conn.close()

        return {
            "settings": [dict(r) for r in settings_rows],
            "rewards": [dict(r) for r in reward_rows],
            "segment_rewards": [dict(r) for r in segment_rows],
            "spins": [dict(r) for r in spin_rows],
            "backups": [dict(r) for r in backup_rows],
            "current_cycle": get_operational_cycle_bounds(),
            "previous_cycle": get_previous_operational_cycle_bounds(),
        }

    def export_all_json(self) -> Dict[str, Any]:
        return self.export_json()

    def export_cycle_json(self, week_key: str) -> Dict[str, Any]:
        return {
            "week_key": week_key,
            "summary": self.get_cycle_summary(week_key),
            "payout_rows": self.get_cycle_payout_rows(week_key),
            "spins": self.get_cycle_spins(week_key),
        }

    def import_json(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        settings_rows = payload.get("settings") or []
        reward_rows = payload.get("rewards") or []
        segment_rows = payload.get("segment_rewards") or []
        spin_rows = payload.get("spins") or []
        backup_rows = payload.get("backups") or []

        with DB_LOCK:
            conn = self._connect()
            cur = conn.cursor()

            if settings_rows:
                cur.execute("DELETE FROM settings")
                for row in settings_rows:
                    cur.execute("""
                        INSERT INTO settings (key, value)
                        VALUES (?, ?)
                    """, (
                        str(row.get("key") or ""),
                        str(row.get("value") or ""),
                    ))

            if reward_rows:
                cur.execute("DELETE FROM roulette_rewards")
                for row in reward_rows:
                    cur.execute("""
                        INSERT INTO roulette_rewards (amount, weight, active, sort_order)
                        VALUES (?, ?, ?, ?)
                    """, (
                        int(row.get("amount") or 0),
                        int(row.get("weight") or 0),
                        1 if row.get("active") else 0,
                        int(row.get("sort_order") or 0),
                    ))

            if segment_rows:
                cur.execute("DELETE FROM roulette_segment_rewards")
                for row in segment_rows:
                    cur.execute("""
                        INSERT INTO roulette_segment_rewards
                        (segment_value, amount, weight, active, sort_order)
                        VALUES (?, ?, ?, ?, ?)
                    """, (
                        int(row.get("segment_value") or 0),
                        int(row.get("amount") or 0),
                        int(row.get("weight") or 0),
                        1 if row.get("active") else 0,
                        int(row.get("sort_order") or 0),
                    ))

            restored_spins = 0
            for row in spin_rows:
                spin_date = str(row.get("spin_date") or "")
                week_key = str(row.get("week_key") or "").strip()
                if not week_key and spin_date:
                    try:
                        d = datetime.strptime(spin_date, "%Y-%m-%d")
                        week_key = get_operational_cycle_bounds(d.replace(hour=12, minute=0, second=0, microsecond=0))["cycle_key"]
                    except Exception:
                        week_key = current_week_key()
                elif not week_key:
                    week_key = current_week_key()

                spin_index = int(row.get("spin_index") or 1)
                eligible_count = int(row.get("eligible_count") or 0)
                used_count_before = int(row.get("used_count_before") or 0)
                if eligible_count <= 0:
                    eligible_count = max(1, spin_index)
                if used_count_before < 0:
                    used_count_before = max(0, spin_index - 1)

                cur.execute("""
                    INSERT OR REPLACE INTO roulette_spins (
                        id,
                        spin_date,
                        week_key,
                        phone,
                        rider_name,
                        today_completed,
                        eligible_count,
                        used_count_before,
                        spin_index,
                        segment_value,
                        reward_amount,
                        created_at,
                        note
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    int(row.get("id") or 0),
                    spin_date,
                    week_key,
                    str(row.get("phone") or ""),
                    str(row.get("rider_name") or ""),
                    int(row.get("today_completed") or 0),
                    eligible_count,
                    used_count_before,
                    spin_index,
                    int(row.get("segment_value") or 10),
                    int(row.get("reward_amount") or 0),
                    int(row.get("created_at") or 0),
                    str(row.get("note") or ""),
                ))
                restored_spins += 1

            if backup_rows:
                cur.execute("DELETE FROM backup_logs")
                for row in backup_rows:
                    cur.execute("""
                        INSERT INTO backup_logs (backup_file, created_at)
                        VALUES (?, ?)
                    """, (
                        str(row.get("backup_file") or ""),
                        int(row.get("created_at") or 0),
                    ))

            conn.commit()
            conn.close()

        return {
            "ok": True,
            "restored_spins": restored_spins,
            "segment_reward_rows": len(segment_rows),
        }


if __name__ == "__main__":
    db = RouletteDB("./data")

    print("현재 운영 주차:")
    print(json.dumps(get_operational_cycle_bounds(), ensure_ascii=False, indent=2, default=str))

    print("\n직전 운영 주차:")
    print(json.dumps(get_previous_operational_cycle_bounds(), ensure_ascii=False, indent=2, default=str))

    print("\n구간별 확률표 샘플(10/20/30/100):")
    sample = {
        "10": db.get_segment_rewards(10),
        "20": db.get_segment_rewards(20),
        "30": db.get_segment_rewards(30),
        "100": db.get_segment_rewards(100),
    }
    print(json.dumps(sample, ensure_ascii=False, indent=2, default=str))

    phone = "010-1234-5678"
    rider_name = "테스트기사"

    print("\n상태 조회:")
    status = db.get_status(phone, rider_name, today_completed=37)
    print(json.dumps(status, ensure_ascii=False, indent=2, default=str))

    # 테스트 스핀 예시
    # result = db.spin(phone, rider_name, today_completed=37)
    # print("\n룰렛 결과:")
    # print(json.dumps(result, ensure_ascii=False, indent=2, default=str))