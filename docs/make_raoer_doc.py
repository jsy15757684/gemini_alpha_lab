# -*- coding: utf-8 -*-
"""이 시스템에 구현된 라오어 무한매수법 정리 문서."""
import os
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                TableStyle, PageBreak, KeepTogether)

FONT = "AppleGothic"
pdfmetrics.registerFont(TTFont(FONT, "/System/Library/Fonts/Supplemental/AppleGothic.ttf"))
pdfmetrics.registerFontFamily(FONT, normal=FONT, bold=FONT, italic=FONT, boldItalic=FONT)

INK = colors.HexColor("#14181f")
MUTE = colors.HexColor("#5b6472")
LINE = colors.HexColor("#d6dae1")
ACC = colors.HexColor("#1d4ed8")
BAD = colors.HexColor("#b42318")
GOOD = colors.HexColor("#067647")
BG = colors.HexColor("#f5f7fa")

def S(name, size, leading, color=INK, space_before=0, space_after=4, left=0, **kw):
    return ParagraphStyle(name, fontName=FONT, fontSize=size, leading=leading,
                          textColor=color, spaceBefore=space_before,
                          spaceAfter=space_after, leftIndent=left, **kw)

TITLE = S("t", 21, 28, space_after=3)
SUB = S("s", 10.5, 15, MUTE, space_after=16)
H1 = S("h1", 14, 19, INK, space_before=16, space_after=7)
H2 = S("h2", 11, 15.5, ACC, space_before=11, space_after=4)
BODY = S("b", 9.5, 15, INK, space_after=5)
SMALL = S("sm", 8.5, 13, MUTE, space_after=4)
BULLET = S("bu", 9.5, 15, INK, space_after=3, left=11, firstLineIndent=-11)
NOTE = S("n", 9, 14, INK, space_after=4, left=8)

# 표 안의 '문자열' 은 reportlab 이 줄바꿈하지 않는다 — 길면 셀 밖으로 잘린다.
# wrap 으로 지정한 열은 Paragraph 로 감싸 줄바꿈되게 한다.
CELL = None

def tbl(data, widths, align=None, head=True, fs=8.5, wrap=None):
    global CELL
    if CELL is None:
        CELL = ParagraphStyle("cell", fontName=FONT, fontSize=fs, leading=fs + 3.5,
                              textColor=INK)
    if wrap:
        cs = ParagraphStyle("cellx", parent=CELL, fontSize=fs, leading=fs + 3.5)
        data = [row[:] for row in data]
        for r, row in enumerate(data):
            if head and r == 0:
                continue
            for c in wrap:
                if isinstance(row[c], str):
                    row[c] = Paragraph(row[c], cs)
    t = Table(data, colWidths=widths, hAlign="LEFT")
    cmds = [
        ("FONTNAME", (0, 0), (-1, -1), FONT),
        ("FONTSIZE", (0, 0), (-1, -1), fs),
        ("TEXTCOLOR", (0, 0), (-1, -1), INK),
        ("LINEBELOW", (0, 0), (-1, 0), 0.7, LINE),
        ("LINEBELOW", (0, 1), (-1, -2), 0.25, colors.HexColor("#eceef2")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]
    if head:
        cmds += [("BACKGROUND", (0, 0), (-1, 0), BG),
                 ("TEXTCOLOR", (0, 0), (-1, 0), MUTE)]
    for c in (align or []):
        cmds.append(("ALIGN", (c, 1), (c, -1), "RIGHT"))
    t.setStyle(TableStyle(cmds))
    return t

def box(paras, color=BG, border=LINE):
    inner = Table([[p] for p in paras], colWidths=[158 * mm], hAlign="LEFT")
    inner.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), color),
        ("BOX", (0, 0), (-1, -1), 0.6, border),
        ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    return inner

def footer(canv, doc):
    canv.saveState()
    canv.setFont(FONT, 7.5)
    canv.setFillColor(MUTE)
    canv.drawString(20 * mm, 12 * mm, "라오어 무한매수법 — 구현 정리 · 2026-09-19")
    canv.drawRightString(190 * mm, 12 * mm, f"{doc.page}")
    canv.setStrokeColor(LINE)
    canv.setLineWidth(0.4)
    canv.line(20 * mm, 15.5 * mm, 190 * mm, 15.5 * mm)
    canv.restoreState()

out = os.path.expanduser("~/gemini_alpha_lab/docs/라오어_무한매수법_구현정리.pdf")
os.makedirs(os.path.dirname(out), exist_ok=True)
doc = SimpleDocTemplate(out, pagesize=A4,
                        leftMargin=20 * mm, rightMargin=20 * mm,
                        topMargin=18 * mm, bottomMargin=20 * mm,
                        title="라오어 무한매수법 구현 정리",
                        author="gemini_alpha_lab")
E = []
W = 170 * mm

# ───────── 표지 / 요약 ─────────
E.append(Paragraph("라오어 무한매수법", TITLE))
E.append(Paragraph("이 시스템에 구현된 내용과 실측 결과 · 2026년 9월 19일", SUB))

E.append(Paragraph("한 장 요약", H1))
E.append(Paragraph(
    "이 프로그램의 무한매수법은 원전의 규칙 네 가지를 그대로 구현하고 있고, "
    "실제 봇 루프를 돌리는 검증 13개 항목으로 확인했습니다. 운용 중인 두 봇의 "
    "장부도 규칙과 소수점 8자리까지 일치합니다.", BODY))
E.append(Paragraph(
    "다만 측정 결과는 이 전략을 <font color='#b42318'>수익을 키우는 도구가 아니라 "
    "낙폭을 줄이는 도구</font>로 봐야 한다고 말합니다. 파라미터 192개 조합을 "
    "학습·검증으로 나눠 재보니 검증 구간에서 단순보유를 이긴 조합이 6개(3%)뿐이었고, "
    "학습 구간 1등을 골랐을 때는 오히려 평균보다 나빴습니다. 국면으로 쪼개면 "
    "급락장에서는 24구간 중 96%를 이겼고 급등장에서는 39구간 중 한 번도 이기지 "
    "못했습니다.", BODY))
E.append(Spacer(1, 6))
E.append(box([
    Paragraph("이 문서를 읽을 때 전제", S("x", 9.5, 14, INK, space_after=4)),
    Paragraph("모든 숫자는 빗썸 공개 API로 받은 과거 캔들에 이 시스템의 백테스트 "
              "엔진을 돌려 얻은 것입니다. 간격과 무관하게 200봉만 받을 수 있어 "
              "일봉 200일(2026년 3~9월)이 가장 긴 창이고, 그 기간은 상승장에 "
              "치우쳐 있습니다. AI 동적 조절은 과거 재현이 불가능해 백테스트에 "
              "넣지 못했습니다 — 아래 수치는 모두 AI를 끈 기준선입니다.", SMALL),
]))

# ───────── 구현된 규칙 ─────────
E.append(Paragraph("1. 구현된 규칙", H1))
E.append(Paragraph(
    "라오어 무한매수법은 자금을 여러 회차로 쪼개 기계적으로 매수하고, 평단 대비 "
    "목표 수익률에 닿으면 전량 매도해 사이클을 다시 시작하는 방식입니다. "
    "이 시스템이 구현한 규칙은 다음 네 가지입니다.", BODY))

E.append(Paragraph("규칙 1 · 1회 매수금 = 운용자본 ÷ 분할수", H2))
E.append(Paragraph("배정한 자본을 분할수로 나눈 금액을 한 회차에 매수합니다. "
                   "40분할에 400,000원이면 1회 10,000원입니다.", BODY))

E.append(Paragraph("규칙 2 · 새 캔들마다 한 회차", H2))
E.append(Paragraph("매수는 새 봉이 떴을 때만 일어납니다. 같은 봉에서 두 번 사지 "
                   "않고, 가격이 내려도 회차를 계속 진행해 평단을 낮춥니다. "
                   "이 때문에 <b>캔들 간격이 곧 자본 투입 속도</b>가 됩니다 "
                   "(분할수 × 간격 = 자본 소진 시간).", BODY))

E.append(Paragraph("규칙 3 · 평단 대비 목표 도달 시 전량 매도", H2))
E.append(Paragraph("현재가가 평단보다 목표 익절률만큼 높아지면 전량 매도하고 "
                   "회차를 0으로 되돌립니다. 목표는 <b>평단 기준</b>입니다 — "
                   "배정자본 기준 수익률은 그보다 낮게 나옵니다.", BODY))

E.append(Paragraph("규칙 4 · 분할수 소진 시 쿼터매도 방어", H2))
E.append(Paragraph("설정한 분할수를 모두 쓰고도 목표에 닿지 못하면, 보유 수량의 "
                   "일부(기본 25%)를 팔아 현금을 확보하고 회차를 그만큼 되돌립니다. "
                   "매도한 비율만큼 원가도 덜어내 남은 포지션의 원가가 부풀지 "
                   "않게 합니다.", BODY))

E.append(PageBreak())
E.append(Paragraph("규칙별 검증 항목", H1))
E.append(Paragraph("조작한 시장을 주입해 <b>실제 봇 루프를 돌려</b> 확인합니다. "
                   "판단 로직을 시험이 다시 구현하면 '작성자의 이해'를 검증할 뿐 "
                   "배포된 코드를 검증하지 못합니다.", SMALL))
E.append(tbl([
    ["검증 항목", "결과"],
    ["1회 매수금이 운용자본 ÷ 분할수 다", "400,000 ÷ 5 = 80,000원"],
    ["같은 봉에서는 두 번 사지 않는다", "T 유지"],
    ["새 봉이 뜨면 다음 회차를 매수한다 (하락해도 계속)", "T=2, 평단 하락 확인"],
    ["평단 대비 목표 익절률에 닿으면 전량 매도한다", "매도 1건, 보유 0"],
    ["익절 후 회차가 초기화된다", "T=0"],
    ["익절한 봉에서 곧바로 재매수하지 않는다", "보유 0, T=0"],
    ["익절 손익이 현금 증감과 일치한다", "+23,853원 = +23,853원"],
    ["다음 봉에서 새 사이클을 시작한다", "T=1"],
    ["분할수를 소진하면 더 사지 않는다", "T=5/5"],
    ["소진 후 다음 봉에서 쿼터매도 방어가 나간다", "쿼터매도 1건"],
    ["쿼터매도가 판 비율이 설정과 같다", "25.0%"],
    ["쿼터매도 후 회차가 롤백된다", "T=5 → 4"],
    ["쿼터매도가 남은 원가를 부풀리지 않는다", "원가 300,000 ≤ 평가 299,880"],
], [108 * mm, 62 * mm], wrap=[0]))
E.append(Spacer(1, 6))
E.append(box([
    Paragraph("이 검증을 넣으면서 결함 하나를 찾아 고쳤습니다", S("x", 9.5, 14, BAD, space_after=4)),
    Paragraph("익절 시 '이 봉을 소비했다'는 기록을 남기지 않아, 몇 초 뒤 다음 "
              "판단에서 <b>막 익절한 그 봉에서 새 회차를 곧바로 매수</b>했습니다. "
              "방금 목표 수익률을 찍은 가격, 즉 그 봉의 고점에서 새 사이클을 "
              "시작하는 동작입니다. 백테스트는 다음 봉을 기다리므로 실전과 "
              "백테스트가 어긋나 있었습니다. 2026-09-19 수정했고, 그 전까지 "
              "익절이 발생한 실전 거래는 없었습니다.", SMALL),
], colors.HexColor("#fef3f2"), colors.HexColor("#fda29b")))

# ───────── AI 계층 ─────────
E.append(Paragraph("2. AI 스마트 조절 계층 (선택)", H1))
E.append(Paragraph("원전 규칙 위에 Gemini AI가 두 가지를 회차마다 조절합니다. "
                   "끄면 순수 기계적 무한매수로 돕니다.", BODY))
E.append(tbl([
    ["조절 대상", "범위", "판단 근거"],
    ["매수 비중 배수", "0.5x ~ 2.0x", "RSI·MACD·거래량·현재 회차 진행도"],
    ["목표 익절률", "5% ~ 20%", "추세 강도와 과열 정도"],
], [42 * mm, 33 * mm, 95 * mm], wrap=[2]))
E.append(Spacer(1, 5))
E.append(Paragraph("실제로 남은 판단 예시입니다.", SMALL))
E.append(box([
    Paragraph("RSI 79.5의 단기 급등 과열 구간으로 고점 매수를 축소하기 위해 0.5배 "
              "최소 분할 매수를 적용하고, 강력한 추세에 맞춰 목표 익절률을 15%로 "
              "상향합니다.", SMALL),
]))
E.append(Spacer(1, 5))
E.append(Paragraph("과열에서 비중을 줄이는 이 동작은 낙폭을 줄이는 방향입니다. "
                   "다만 뒤에 나오는 측정에 따르면 급등장 열세의 원인도 같은 "
                   "지점에 있습니다 — 오를 때 덜 담기 때문입니다. AI를 켜는 것은 "
                   "'낙폭을 줄이고 상승을 덜 먹는' 선택입니다.", BODY))

# ───────── 현재 운용 ─────────
E.append(Paragraph("3. 현재 운용 현황", H1))
E.append(Paragraph("2026-09-19 15:00 KST 기준, 빗썸 실계좌로 두 봇이 돌고 있습니다.", BODY))
E.append(tbl([
    ["", "SOL", "XRP"],
    ["배정 자본", "400,000원", "400,000원"],
    ["진행 회차", "13 / 40", "13 / 40"],
    ["투입 금액", "65,000원 (16%)", "112,000원 (28%)"],
    ["미투입 현금", "335,000원", "288,000원"],
    ["보유 수량", "0.42065910", "57.79468623"],
    ["평단", "154,434.72원", "1,937.38원"],
    ["현재가", "153,100원", "1,950원"],
    ["수익률 (배정 대비)", "-0.15%", "+0.17%"],
    ["수익률 (평단 대비)", "-0.86%", "+0.65%"],
    ["AI 조절", "켜짐 (0.5x 적용 중)", "켜짐 (0.8~1.0x)"],
    ["캔들 간격", "6시간", "6시간"],
    ["목표 익절 / 쿼터", "10% / 25%", "10% / 25%"],
    ["추세 조절", "사용 안 함", "사용 안 함"],
], [46 * mm, 62 * mm, 62 * mm], align=[1, 2]))
E.append(Spacer(1, 6))
E.append(Paragraph("두 수익률이 다른 이유", H2))
E.append(Paragraph("분모가 다릅니다. <b>배정 대비</b>는 아직 쓰지 않은 현금까지 "
                   "분모에 넣고, <b>평단 대비</b>는 실제로 산 물량만 봅니다. "
                   "격차는 정확히 투입비율의 역수입니다 — SOL은 16%만 투입했으니 "
                   "평단 대비 수익률이 배정 대비의 약 6배로 나옵니다. "
                   "익절 판정은 평단 대비 값을 씁니다.", BODY))

E.append(Paragraph("4. 안전장치", H1))
E.append(tbl([
    ["장치", "내용"],
    ["거래소 대조", "재시작 때마다 내부 장부와 빗썸 실제 보유량을 대조하고, "
                    "장부가 더 많다고 주장하면 재가동하지 않습니다."],
    ["체결 일지 분리", "체결은 봇 상태와 별도 파일에 쌓습니다. 봇을 지워도 "
                       "누적 실현 손익이 남습니다."],
    ["상태 파일 보호", "상태 파일을 읽지 못하면 봇을 하나도 띄우지 않고 그 파일에 "
                       "쓰지도 않습니다. 빈 목록으로 진행해 기록을 덮어쓰는 것을 "
                       "막습니다."],
    ["청산 경고", "화면의 정지·삭제는 시장가로 전량 매도합니다. 무엇이 얼마나 "
                  "팔리는지 숫자로 보여주고 확인을 받습니다."],
    ["시세 실패 시 보류", "시세나 캔들을 받지 못하면 추정치로 매매하지 않고 "
                          "판단을 보류합니다."],
], [34 * mm, 136 * mm], wrap=[0, 1]))

E.append(PageBreak())

# ───────── 실측: 국면 ─────────
E.append(Paragraph("5. 실측 — 이 전략이 언제 이기고 언제 지는가", H1))
E.append(Paragraph("60봉 창을 10봉씩 옮기며 112개 구간을 만들고, 각 구간의 "
                   "단순보유 수익률로 국면을 분류했습니다. 설정은 40분할 · "
                   "목표 10% · 쿼터 25% · AI 끔입니다.", BODY))
E.append(tbl([
    ["국면 (단순보유 수익률)", "구간", "단순보유", "전략", "초과수익", "이긴 비율"],
    ["급락  (-10% 이하)", "24", "-18.90%", "-10.11%", "+8.79%p", "96%"],
    ["하락  (-10 ~ -2%)", "24", "-4.38%", "-0.87%", "+3.51%p", "92%"],
    ["횡보  (-2 ~ +2%)", "15", "-0.28%", "-1.36%", "-1.08%p", "20%"],
    ["상승  (+2 ~ +10%)", "10", "+4.76%", "+1.97%", "-2.79%p", "20%"],
    ["급등  (+10% 이상)", "39", "+23.86%", "+6.80%", "-17.06%p", "0%"],
], [48 * mm, 15 * mm, 26 * mm, 26 * mm, 28 * mm, 22 * mm], align=[1, 2, 3, 4, 5]))
E.append(Spacer(1, 6))
E.append(box([
    Paragraph("초과수익과 단순보유 수익률의 상관계수는 -0.939 입니다.", S("x", 9.5, 14, INK, space_after=4)),
    Paragraph("거의 완전한 역상관입니다. 이것은 오작동이 아니라 설계의 결과입니다 — "
              "자본을 쪼개 현금으로 들고 있으니 오를 때 못 따라가고, 떨어질 때 "
              "평단을 낮출 여력이 남습니다. 급등장 39개 구간에서 한 번도 이기지 "
              "못한 것도 같은 이유입니다.", SMALL),
]))

# ───────── 실측: 파라미터 ─────────
E.append(Paragraph("6. 실측 — 설정을 바꾸면 무엇이 달라지는가", H1))
E.append(Paragraph("파라미터 192개 조합을 앞 60% 구간으로 고르고 뒤 40% 구간으로 "
                   "검증했습니다. 결론은 <b>과거 최적값 찾기가 작동하지 않는다</b>는 "
                   "것입니다.", BODY))
E.append(tbl([
    ["", "값"],
    ["검증 구간에서 단순보유를 이긴 조합", "6 / 192 (3%)"],
    ["전체 조합의 검증 초과수익 평균", "-10.15%p"],
    ["학습 1등을 골랐을 때의 검증 초과수익", "-10.95%p"],
], [96 * mm, 40 * mm], align=[1], wrap=[0]))
E.append(Spacer(1, 4))
E.append(Paragraph("학습 구간 1등이 평균보다 나빴습니다. 과거에 가장 좋았던 "
                   "설정을 고르는 행위 자체가 손해였다는 뜻입니다.", SMALL))

E.append(KeepTogether([Paragraph("분할 수", H2), tbl([
    ["분할", "수익률", "최대낙폭", "승률"],
    ["20", "+5.35%", "7.05%", "29.0%"],
    ["40", "+7.81%", "5.11%", "76.6%"],
    ["60", "+5.72%", "4.07%", "79.7%"],
], [20 * mm, 30 * mm, 30 * mm, 26 * mm], align=[1, 2, 3])]))
E.append(Paragraph("분할을 늘리면 낙폭이 꾸준히 줄어듭니다. 40이 수익과 낙폭의 "
                   "균형점이고, 60은 낙폭만 더 줄입니다.", SMALL))

E.append(KeepTogether([Paragraph("목표 익절률", H2), tbl([
    ["목표", "수익률", "최대낙폭", "익절 체결", "쿼터방어", "승률"],
    ["5%", "-0.62%", "5.27%", "1.60회", "0.46", "73.0%"],
    ["7.5%", "-0.25%", "5.73%", "1.14", "0.56", "67.8%"],
    ["10%", "+0.01%", "6.27%", "0.72", "0.63", "59.7%"],
    ["12.5%", "+0.34%", "6.71%", "0.52", "0.70", "54.9%"],
    ["15%", "+0.66%", "6.91%", "0.40", "0.73", "53.1%"],
    ["20%", "+1.54%", "7.12%", "0.27", "0.83", "50.4%"],
], [20 * mm, 26 * mm, 26 * mm, 26 * mm, 24 * mm, 22 * mm], align=[1, 2, 3, 4, 5])]))
E.append(Paragraph("합계로는 목표가 높을수록 수익률이 오릅니다. 그런데 대가가 "
                   "큽니다 — 익절이 실제로 체결된 횟수가 1.60회에서 0.27회로 줄고, "
                   "회차를 다 써서 쿼터방어로 넘어가는 빈도가 거의 두 배가 됩니다.", SMALL))
E.append(Spacer(1, 4))
E.append(KeepTogether([Paragraph("국면별로 쪼개면 방향이 달라집니다.", SMALL), tbl([
    ["국면", "5%", "7.5%", "10%", "12.5%", "15%", "20%"],
    ["급락", "-7.66%", "-8.43%", "-10.11%", "-11.53%", "-11.53%", "-11.53%"],
    ["하락", "-0.55%", "-1.10%", "-0.87%", "-0.62%", "-0.75%", "-1.19%"],
    ["횡보", "-0.80%", "-0.76%", "-1.36%", "-1.10%", "-1.81%", "-2.37%"],
    ["상승", "+1.49%", "+1.19%", "+1.97%", "+0.94%", "+0.45%", "+0.45%"],
    ["급등", "+3.20%", "+5.14%", "+6.80%", "+8.65%", "+10.03%", "+13.04%"],
], [18 * mm, 25 * mm, 25 * mm, 26 * mm, 26 * mm, 25 * mm, 25 * mm],
    align=[1, 2, 3, 4, 5, 6])]))
E.append(Paragraph("급락장에서는 목표 15%가 5%보다 3.9%p 나쁩니다. 상승장(+2~+10%)에서는 "
                   "10%가 최고이고 15%는 오히려 떨어집니다. 15%의 이득은 급등장에만 "
                   "있습니다. <b>기본 10%를 두고 AI가 과열을 감지할 때만 올리는 현재 "
                   "방식이 이 측정과 맞습니다.</b> 기본값을 15%로 고정하는 것은 "
                   "권하지 않습니다.", SMALL))

E.append(PageBreak())

E.append(Paragraph("캔들 간격", H1))
E.append(Paragraph("최적값이 아니라 <b>자본을 며칠에 걸쳐 넣을지</b>를 정하는 "
                   "값입니다. 회차 매수가 새 봉마다 한 번이므로 분할수 × 간격이 "
                   "자본 소진 시간입니다.", BODY))
E.append(tbl([
    ["간격", "40분할 소진 시간"],
    ["1시간", "1.7일"],
    ["6시간", "10일  (현재 설정)"],
    ["12시간", "20일"],
    ["24시간", "40일"],
], [24 * mm, 60 * mm]))
E.append(Spacer(1, 5))
E.append(Paragraph("간격마다 받을 수 있는 히스토리 길이가 달라(1시간봉 8일, "
                   "일봉 200일) 그냥 비교하면 서로 다른 시장을 비교하게 됩니다. "
                   "지표는 각 간격의 전체 히스토리로 예열하고 매매만 같은 달력 "
                   "창에서 돌렸습니다.", SMALL))
E.append(tbl([
    ["창", "간격", "수익률", "단순보유", "초과수익", "낙폭", "쿼터방어"],
    ["최근 8일", "6시간", "+4.11%", "+6.75%", "-2.64%", "1.89%", "0"],
    ["", "12시간", "+1.62%", "+6.61%", "-4.99%", "0.43%", "0"],
    ["", "24시간", "+0.95%", "+7.51%", "-6.56%", "0.32%", "0"],
    ["최근 40일", "6시간", "+8.31%", "+34.45%", "-26.13%", "7.29%", "6.75"],
    ["", "24시간", "+4.21%", "+34.26%", "-30.04%", "4.24%", "0"],
], [22 * mm, 20 * mm, 24 * mm, 26 * mm, 26 * mm, 22 * mm, 24 * mm],
    align=[2, 3, 4, 5, 6]))
E.append(Paragraph("짧은 간격이 수익률과 낙폭을 함께 올립니다. 40일 창에서 "
                   "6시간봉은 회차를 소진해 쿼터방어를 6.75회 쳤고 24시간봉은 "
                   "0회였습니다. 1~4시간봉은 히스토리가 200봉뿐이라 같은 창을 "
                   "만들 수 없어 비교 자체가 불가능했습니다 — 1시간봉은 40회차를 "
                   "1.7일에 소진해 사실상 일시불 매수가 되므로 분할의 전제가 "
                   "사라집니다.", SMALL))

E.append(Paragraph("추세 조절 (기본 꺼짐)", H1))
E.append(Paragraph("급등장 열세를 줄여보려고 넣은 선택 기능입니다. 과최적화를 "
                   "피하려고 문턱을 과거 데이터에서 고르지 않고 통상적 정의를 "
                   "그대로 썼습니다 — 상승추세는 '종가가 30봉 평균 위이고 평균선이 "
                   "오르는 중'입니다.", BODY))
E.append(tbl([
    ["모드", "수익률", "최대낙폭", "단순보유를 이긴 비율"],
    ["사용 안 함 (기본)", "+6.82%", "5.54%", "4%"],
    ["상승추세면 2배 매수", "+8.60%", "7.24%", "5%"],
    ["하락추세면 회차 쉼", "+4.97%", "4.76%", "0%"],
], [44 * mm, 28 * mm, 28 * mm, 40 * mm], align=[1, 2, 3], wrap=[0]))
E.append(Spacer(1, 4))
E.append(Paragraph("같은 설정끼리 1:1로 비교하면 '상승추세 2배 매수'는 수익률 "
                   "+1.78%p를 벌면서 <b>낙폭도 +1.70%p 늘립니다</b>(조합의 77%에서 "
                   "수익 개선). 국면별로는 급등장 +2.07%p, 급락장 -2.39%p이고 "
                   "급락장 낙폭이 14.78%에서 17.74%로 커집니다. 측정 구간이 "
                   "상승장에 치우쳐(112구간 중 급등 39, 급락 24) 합계가 플러스로 "
                   "나온 것이고, <b>하락장이 길면 부호가 뒤집힙니다.</b>", SMALL))
E.append(Paragraph("흔히 말하는 추세 필터인 '하락추세면 쉼'은 수익률을 1.85%p "
                   "깎았고 단순보유를 이긴 조합이 0%였습니다. 떨어질 때 사지 "
                   "않으면 평단을 낮추지 못해, 이 전략이 급락장에서 이기는 원리 "
                   "자체를 없앱니다.", SMALL))

E.append(PageBreak())

# ───────── 한계 ─────────
E.append(Paragraph("7. 이 측정을 믿을 수 있는 범위", H1))
E.append(Paragraph("숫자를 그대로 미래에 대입하면 안 되는 이유를 적어둡니다.", BODY))
E.append(Paragraph("· <b>데이터가 짧습니다.</b> 빗썸 공개 API가 간격과 무관하게 "
                   "200봉만 주므로 일봉 200일(2026년 3~9월), 6시간봉 50일, "
                   "1시간봉 8일이 전부입니다.", BULLET))
E.append(Paragraph("· <b>구간이 상승장에 치우쳤습니다.</b> 112개 구간 중 급등이 "
                   "39개, 급락이 24개입니다. 합계 수치는 이 구성에 좌우됩니다.", BULLET))
E.append(Paragraph("· <b>구간이 독립 표본이 아닙니다.</b> 60봉 창을 10봉씩 옮겨 "
                   "만들었으므로 서로 겹칩니다. 112개를 독립 관측으로 세면 "
                   "유의성을 과대평가합니다.", BULLET))
E.append(Paragraph("· <b>AI 조절이 빠져 있습니다.</b> Gemini 호출은 과거 재현이 "
                   "불가능해 백테스트에 넣지 못했습니다. 실전 봇은 AI를 켜고 "
                   "돌고 있어, 위 숫자와 실제 동작은 그만큼 다릅니다.", BULLET))
E.append(Paragraph("· <b>대부분 미실현 손익입니다.</b> 60봉 창이 한 사이클을 담기에 "
                   "짧아 목표 익절률 비교에서는 거의 모든 칸이 미청산으로 "
                   "끝났습니다. 절대값보다 설정 간 비교와 익절 체결 횟수를 "
                   "근거로 읽어야 합니다.", BULLET))
E.append(Paragraph("· <b>4종목뿐입니다.</b> BTC · ETH · SOL · XRP 만 다룹니다.", BULLET))

E.append(Paragraph("8. 직접 확인하는 방법", H1))
E.append(Paragraph("이 문서의 주장은 모두 저장소에서 재현할 수 있습니다.", BODY))
E.append(tbl([
    ["확인할 것", "방법"],
    ["규칙 구현", "python3 test_strategy_process.py  (51개 항목, 실제 봇 루프)"],
    ["상태 파일 안전장치", "python3 test_store_safety.py  (26개 항목)"],
    ["파라미터 비교", "화면의 [전략 백테스트] 탭 또는 services/backtest.py"],
    ["실전 봇 장부", "화면의 [매매 일지·수익] 탭, data/trades.json"],
    ["구현 위치", "services/trader.py  (실전 루프) · services/backtest.py (검증)"],
], [36 * mm, 134 * mm], wrap=[1]))
E.append(Spacer(1, 8))
E.append(box([
    Paragraph("마지막으로", S("x", 9.5, 14, INK, space_after=4)),
    Paragraph("이 문서는 전략을 권하는 자료가 아닙니다. 구현이 규칙대로 되어 "
              "있는지, 그리고 측정 결과가 무엇을 말하는지 정리한 것입니다. "
              "측정에서 나온 가장 분명한 사실은 <b>이 전략이 상승장에서 단순보유를 "
              "이긴 적이 없고, 급락장에서는 낙폭을 크게 줄였다</b>는 것입니다. "
              "투자 금액과 배분에 관한 판단은 이 문서의 범위가 아닙니다.", SMALL),
]))

doc.build(E, onFirstPage=footer, onLaterPages=footer)
print("생성:", out)
print("크기:", os.path.getsize(out), "바이트")
