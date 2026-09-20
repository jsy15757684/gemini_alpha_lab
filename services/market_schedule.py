"""미국 증시(NYSE / NASDAQ) 운영 시간, 서머타임 및 휴장일 스케줄러.

- 정규장 운영 시간:
  - 서머타임(EDT): 한국시간 22:30 ~ 익일 05:00 (미국 동부 09:30 ~ 16:00)
  - 평시(EST): 한국시간 23:30 ~ 익일 06:00 (미국 동부 09:30 ~ 16:00)
- 미국 연방 및 증시 주요 휴장일(공휴일) 판정.
- 장 상태(OPEN, CLOSED, PRE_MARKET, AFTER_MARKET, HOLIDAY) 및 다음 개장 시각 산출.
"""

from datetime import datetime, date, time as dtime, timedelta
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

# 타임존 정의
EASTERN_TZ = ZoneInfo("America/New_York")
KST_TZ = ZoneInfo("Asia/Seoul")


def _easter_date(year: int) -> date:
    """부활절 날짜 계산 (Anonymous Gregorian algorithm) - 성금요일(Good Friday) 산출용."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def get_nyse_holidays(year: int) -> Dict[date, str]:
    """해당 연도의 NYSE/NASDAQ 증시 휴장일 목록을 반환한다."""
    holidays = {}

    def _observed(dt: date, name: str):
        # 토요일이면 금요일에 관측, 일요일이면 월요일에 관측
        if dt.weekday() == 5:
            holidays[dt - timedelta(days=1)] = f"{name} (대체휴일)"
        elif dt.weekday() == 6:
            holidays[dt + timedelta(days=1)] = f"{name} (대체휴일)"
        else:
            holidays[dt] = name

    # 1. New Year's Day (1월 1일)
    _observed(date(year, 1, 1), "신정 (New Year's Day)")

    # 2. Martin Luther King, Jr. Day (1월 셋째 월요일)
    first_jan = date(year, 1, 1)
    mlk_day = first_jan + timedelta(days=(0 - first_jan.weekday() + 7) % 7 + 14)
    holidays[mlk_day] = "마틴 루터 킹 주니어의 날 (MLK Day)"

    # 3. Washington's Birthday / Presidents' Day (2월 셋째 월요일)
    first_feb = date(year, 2, 1)
    presidents_day = first_feb + timedelta(days=(0 - first_feb.weekday() + 7) % 7 + 14)
    holidays[presidents_day] = "대통령의 날 (Presidents' Day)"

    # 4. Good Friday (성금요일: 부활절 직전 금요일)
    easter = _easter_date(year)
    good_friday = easter - timedelta(days=2)
    holidays[good_friday] = "성금요일 (Good Friday)"

    # 5. Memorial Day (5월 마지막 월요일)
    last_may = date(year, 5, 31)
    memorial_day = last_may - timedelta(days=(last_may.weekday() - 0) % 7)
    holidays[memorial_day] = "메모리얼 데이 (Memorial Day)"

    # 6. Juneteenth National Independence Day (6월 19일)
    _observed(date(year, 6, 19), "준틴스 독립기념일 (Juneteenth)")

    # 7. Independence Day (7월 4일)
    _observed(date(year, 7, 4), "미국 독립기념일 (Independence Day)")

    # 8. Labor Day (9월 첫째 월요일)
    first_sep = date(year, 9, 1)
    labor_day = first_sep + timedelta(days=(0 - first_sep.weekday() + 7) % 7)
    holidays[labor_day] = "미국 노동절 (Labor Day)"

    # 9. Thanksgiving Day (11월 넷째 목요일)
    first_nov = date(year, 11, 1)
    thanksgiving = first_nov + timedelta(days=(3 - first_nov.weekday() + 7) % 7 + 21)
    holidays[thanksgiving] = "추수감사절 (Thanksgiving Day)"

    # 10. Christmas Day (12월 25일)
    _observed(date(year, 12, 25), "크리스마스 (Christmas Day)")

    return holidays


def is_us_dst(now_et: Optional[datetime] = None) -> bool:
    """현재 미국 동부 시간이 서머타임(Daylight Saving Time) 적용 중인지 여부."""
    if now_et is None:
        now_et = datetime.now(EASTERN_TZ)
    return bool(now_et.dst())


def get_us_market_status(now_dt: Optional[datetime] = None) -> Dict[str, Any]:
    """현재 시각 기준 미국 증시 상태와 다음 개장 시각을 반환한다."""
    if now_dt is None:
        now_et = datetime.now(EASTERN_TZ)
    else:
        now_et = now_dt.astimezone(EASTERN_TZ)

    is_dst = is_us_dst(now_et)
    reg_open_time = dtime(9, 30)
    reg_close_time = dtime(16, 0)
    pre_open_time = dtime(4, 0)
    post_close_time = dtime(20, 0)

    cur_date = now_et.date()
    cur_time = now_et.time()
    weekday = now_et.weekday()  # 0=월, 6=일

    holidays = get_nyse_holidays(cur_date.year)
    holiday_name = holidays.get(cur_date)

    status = "CLOSED"
    reason = "정규장 마감"

    if weekday in (5, 6):
        status = "CLOSED"
        reason = "주말 휴장 (토/일)"
    elif holiday_name:
        status = "HOLIDAY"
        reason = f"미국 공휴일 휴장: {holiday_name}"
    elif reg_open_time <= cur_time < reg_close_time:
        status = "OPEN"
        reason = "미국 정규장 운영 중"
    elif pre_open_time <= cur_time < reg_open_time:
        status = "PRE_MARKET"
        reason = "프리마켓 진행 중 (정규장 개장 대기)"
    elif reg_close_time <= cur_time < post_close_time:
        status = "AFTER_MARKET"
        reason = "애프터마켓 진행 중"
    else:
        status = "CLOSED"
        reason = "장 마감 (야간 휴장)"

    # 다음 정규장 개장 시각 찾기
    next_open_et = None
    check_date = cur_date

    # 오늘 정규장 시작 전이고 주말/휴일이 아니라면 오늘의 개장이 다음 개장
    if (weekday < 5) and (not holiday_name) and (cur_time < reg_open_time):
        next_open_et = datetime.combine(cur_date, reg_open_time, tzinfo=EASTERN_TZ)
    else:
        # 내일부터 영업일 탐색
        for day_offset in range(1, 14):
            candidate_date = cur_date + timedelta(days=day_offset)
            cand_holidays = get_nyse_holidays(candidate_date.year)
            if candidate_date.weekday() < 5 and candidate_date not in cand_holidays:
                next_open_et = datetime.combine(candidate_date, reg_open_time, tzinfo=EASTERN_TZ)
                break

    next_open_kst = next_open_et.astimezone(KST_TZ) if next_open_et else None
    seconds_to_open = int((next_open_et - now_et).total_seconds()) if next_open_et else 0

    kst_open_str = "22:30 ~ 05:00" if is_dst else "23:30 ~ 06:00"
    reg_close_et = datetime.combine(cur_date, reg_close_time, tzinfo=EASTERN_TZ)
    reg_close_kst = reg_close_et.astimezone(KST_TZ)

    return {
        "status": status,
        "isOpen": (status == "OPEN"),
        "reason": reason,
        "statusText": reason,
        "isDst": is_dst,
        "regularHoursKst": kst_open_str,
        "easternTime": now_et.strftime("%Y-%m-%d %H:%M:%S"),
        "koreanTime": now_et.astimezone(KST_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "currentTimeKst": now_et.astimezone(KST_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "nextOpenKst": next_open_kst.strftime("%Y-%m-%d %H:%M:%S") if next_open_kst else "-",
        "nextOpenTimeOnly": next_open_kst.strftime("%H:%M") if next_open_kst else "-",
        "nextCloseKst": reg_close_kst.strftime("%H:%M") if status == "OPEN" else "-",
        "secondsToOpen": max(0, seconds_to_open),
        "isHoliday": bool(holiday_name),
        "holidayName": holiday_name or None,
    }


def is_us_market_open(now_dt: Optional[datetime] = None) -> bool:
    """단순 정규장 오픈 여부 boolean 반환."""
    return get_us_market_status(now_dt)["isOpen"]
