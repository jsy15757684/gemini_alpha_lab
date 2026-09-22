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
    #
    # 신정만 토요일 예외다. 다른 휴일은 토요일이면 앞 금요일을 쉬지만,
    # 신정이 토요일인 해에는 앞 금요일(12/31)도 다음 월요일도 쉬지 않는다.
    # 그 금요일이 그 해 마지막 거래일이기 때문이다 (2022년이 그랬다:
    # 2021-12-31 금 개장, 2022-01-03 월 개장).
    _ny = date(year, 1, 1)
    if _ny.weekday() == 6:        # 일요일 → 월요일 대체
        holidays[_ny + timedelta(days=1)] = "신정 (New Year's Day) (대체휴일)"
    elif _ny.weekday() < 5:       # 평일 → 당일
        holidays[_ny] = "신정 (New Year's Day)"
    # 토요일이면 휴장일 없음

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


def get_nyse_half_days(year: int) -> Dict[date, str]:
    """13:00 ET 조기 마감일.

    정규 마감이 16:00 이 아니라 13:00 이다. 이걸 모르면 13:00~16:00 사이에
    장이 열려 있다고 보고 주문을 내는데, 거래소는 이미 닫혀 있다.

    - 추수감사절 다음 날(블랙프라이데이): 항상
    - 독립기념일 전날(7/3): 평일이고 그 자체가 휴장일이 아닐 때
    - 크리스마스 이브(12/24): 평일이고 그 자체가 휴장일이 아닐 때
    """
    full = get_nyse_holidays(year)
    half: Dict[date, str] = {}

    # 추수감사절 다음 날
    for d, name in full.items():
        if "추수감사절" in name:
            half[d + timedelta(days=1)] = "추수감사절 다음 날 (조기 마감)"
            break

    for d, label in ((date(year, 7, 3), "독립기념일 전날 (조기 마감)"),
                     (date(year, 12, 24), "크리스마스 이브 (조기 마감)")):
        if d.weekday() < 5 and d not in full:
            half[d] = label

    return half


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
    pre_open_time = dtime(4, 0)
    post_close_time = dtime(20, 0)

    cur_date = now_et.date()
    cur_time = now_et.time()
    weekday = now_et.weekday()  # 0=월, 6=일

    holidays = get_nyse_holidays(cur_date.year)
    holiday_name = holidays.get(cur_date)

    # 조기 마감일은 16:00 이 아니라 13:00 에 닫는다.
    half_days = get_nyse_half_days(cur_date.year)
    half_day_name = half_days.get(cur_date)
    reg_close_time = dtime(13, 0) if half_day_name else dtime(16, 0)

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
        reason = ("미국 정규장 운영 중 (조기 마감 13:00 ET)" if half_day_name
                  else "미국 정규장 운영 중")
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

    if half_day_name:
        kst_open_str = "22:30 ~ 02:00" if is_dst else "23:30 ~ 03:00"
    else:
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
        "isHalfDay": bool(half_day_name),
        "halfDayName": half_day_name or None,
        "closeTimeEt": reg_close_time.strftime("%H:%M"),
    }


# LOC 접수 창. 개장과 함께 열리고, 거래소 접수 마감 2분 전에 닫는다.
#
# 처음에는 '마감 20분 전' 에만 열었다. 그날 움직임이 반영된 평단으로
# 주문하려는 의도였는데, 그 전제가 틀렸다 — 평단은 매수해야 변하고
# 장중에는 움직이지 않는다. 예산(잔금비례)도 마찬가지다. 즉 늦게 내서
# 얻는 것이 없는데, 8분짜리 단일 실패점만 생겼다. 실제로 첫날 잔고
# 조회가 HTTP 429 로 한 번 튕기자 그날 주문을 통째로 놓쳤다.
#
# 이제 개장 직후부터 낼 수 있다. 실패해도 마감까지 몇 시간이고 다시
# 시도한다. NYSE 는 LOC 를 마감 10분 전(정규장 15:50 ET)까지만 받고
# 그 뒤에는 취소도 안 되므로, 2분 앞서 닫는다.
LOC_WINDOW_CLOSE_MIN = 12  # 마감 N분 전에 창이 닫힌다 (15:48 ET)
LOC_CUTOFF_MIN = 10        # 거래소 접수 마감 (15:50 ET) — 취소도 여기까지
# 체결은 마감 동시호가에서 일어난다. 잔고에 반영될 여유를 조금 둔다.
LOC_SETTLE_GRACE_MIN = 5   # 장 마감 + N분이 지나야 정산한다


def session_date(now_dt: Optional[datetime] = None) -> str:
    """거래일 식별자 (미국 동부 날짜). 하루 한 번 주문을 보장하는 열쇠다."""
    now_et = datetime.now(EASTERN_TZ) if now_dt is None else now_dt.astimezone(EASTERN_TZ)
    return now_et.date().isoformat()


def loc_window(now_dt: Optional[datetime] = None) -> Dict[str, Any]:
    """지금이 LOC 를 낼 시간인지.

    반환값의 `in` 이 True 일 때만 주문한다. `past` 는 그 세션의 접수 창이
    이미 지났다는 뜻으로, 미체결 주문을 정산할 시점 판단에 쓴다.
    """
    now_et = datetime.now(EASTERN_TZ) if now_dt is None else now_dt.astimezone(EASTERN_TZ)
    st = get_us_market_status(now_et)
    close_h, close_m = (int(x) for x in st["closeTimeEt"].split(":"))
    close_et = now_et.replace(hour=close_h, minute=close_m, second=0, microsecond=0)
    shuts_at = close_et - timedelta(minutes=LOC_WINDOW_CLOSE_MIN)
    # 창은 정규장이 열리면 바로 열린다.
    opens_at = now_et.replace(hour=9, minute=30, second=0, microsecond=0)

    tradable = st["status"] == "OPEN"
    settles_at = close_et + timedelta(minutes=LOC_SETTLE_GRACE_MIN)
    return {
        "in": bool(tradable and opens_at <= now_et < shuts_at),
        # 접수 창이 닫혔다 ≠ 체결됐다. LOC 는 마감 동시호가에서 붙는다.
        # 이 둘을 섞으면 접수 12분 뒤에 '미체결' 로 오판하고, 정작 마감에
        # 체결된 물량은 장부에 영영 안 들어온다.
        "past": now_et >= shuts_at,
        "pastClose": now_et >= settles_at,
        "settlesAtEt": settles_at.strftime("%H:%M"),
        "sessionDate": session_date(now_et),
        "opensAtEt": opens_at.strftime("%H:%M"),
        "shutsAtEt": shuts_at.strftime("%H:%M"),
        "closeEt": st["closeTimeEt"],
        "cutoffEt": (close_et - timedelta(minutes=LOC_CUTOFF_MIN)).strftime("%H:%M"),
        "cancellable": now_et < (close_et - timedelta(minutes=LOC_CUTOFF_MIN)),
        "opensAtKst": opens_at.astimezone(KST_TZ).strftime("%H:%M"),
        "isHalfDay": st["isHalfDay"],
        "marketStatus": st["status"],
    }


def is_us_market_open(now_dt: Optional[datetime] = None) -> bool:
    """단순 정규장 오픈 여부 boolean 반환."""
    return get_us_market_status(now_dt)["isOpen"]
