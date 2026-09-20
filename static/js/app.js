// 빗썸 원화 자동매매 콘솔 — 프론트엔드
// 원칙: 서버가 실패를 알려주면 그대로 화면에 보여준다. 임의로 채워 넣지 않는다.

const $ = (id) => document.getElementById(id);
let COINS = [], INTERVALS = [], ENTRY_RULES = [], started = false, timers = [];

const won = (n) => (n == null || isNaN(n)) ? "-" : Math.round(n).toLocaleString("ko-KR");
const pct = (n) => (n == null || isNaN(n)) ? "-" : `${n >= 0 ? "+" : ""}${Number(n).toFixed(2)}%`;
const cls = (n) => (n > 0 ? "up" : n < 0 ? "down" : "");
// 서버 메시지를 그대로 innerHTML 에 넣지 않는다 (사유 문자열에 파일 경로가 들어온다)
const escapeHtml = (v) => String(v ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const when = (ms) => new Date(ms).toLocaleString("ko-KR", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });

async function api(url, options) {
  const res = await fetch(url, options);
  let body = null;
  try { body = await res.json(); } catch (_) {}
  if (res.status === 401 || (res.status === 503 && body?.code === "AUTH_NOT_CONFIGURED")) {
    showGate(body?.code === "AUTH_NOT_CONFIGURED" ? "not_configured" : "expired");
    throw new Error(body?.detail || "인증이 필요합니다.");
  }
  if (!res.ok) throw new Error(body?.detail || `요청 실패 (HTTP ${res.status})`);
  return body;
}

function setAlert(el, message, kind) {
  if (!el) return;
  if (!message) { el.classList.add("hidden"); el.textContent = ""; return; }
  el.className = `alert alert-${kind || "error"}`;
  el.innerHTML = message;
}

// ───────── 인증 ─────────

function showGate(reason) {
  // 세션이 끊겼는데 타이머가 계속 돌면 401 을 반복하며 조용히 실패한다.
  timers.forEach(clearInterval);
  timers = [];
  started = false;
  $("authGate").classList.remove("hidden");
  $("app").classList.add("hidden");
  const pw = $("authPassword"), submit = $("authSubmit");
  if (reason === "not_configured") {
    $("authSub").textContent = "서버에 접속 비밀번호가 설정되지 않았습니다.";
    const local = ["localhost", "127.0.0.1", "[::1]"].includes(location.hostname);
    setAlert($("authError"), local
      ? "프로젝트 폴더의 <b>.env</b> 에 <b>APP_ACCESS_PASSWORD</b> 를 넣고 서버를 재시작하세요."
      : "배포 환경의 환경변수에 <b>APP_ACCESS_PASSWORD</b> 를 설정하세요.");
    pw.disabled = submit.disabled = true;
    return;
  }
  pw.disabled = submit.disabled = false;
  pw.value = "";
  if (reason === "expired") setAlert($("authError"), "세션이 만료되었습니다. 다시 로그인하세요.");
  pw.focus();
}

function togglePasswordVisibility() {
  const pw = $("authPassword");
  const btn = $("togglePwBtn");
  if (!pw) return;
  if (pw.type === "password") {
    pw.type = "text";
    if (btn) btn.textContent = "🙈";
  } else {
    pw.type = "password";
    if (btn) btn.textContent = "👁️";
  }
}
window.togglePasswordVisibility = togglePasswordVisibility;

async function handleLogin(e) {
  if (e && e.preventDefault) e.preventDefault();
  const pw = $("authPassword"), submit = $("authSubmit");
  const password = (pw?.value || "").trim();   // 붙여넣기 공백 제거
  if (!password) return setAlert($("authError"), "비밀번호를 입력하세요.");
  setAlert($("authError"), null);
  if (submit) { submit.disabled = true; submit.textContent = "확인 중…"; }
  try {
    const res = await fetch("/api/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
    const body = await res.json().catch(() => null);
    if (!res.ok) return setAlert($("authError"), body?.detail || `로그인 실패 (HTTP ${res.status})`);
    if (pw) pw.value = "";
    $("authGate").classList.add("hidden");
    $("app").classList.remove("hidden");
    try {
      await boot();
    } catch (bootErr) {
      console.error("화면 초기화 중 오류:", bootErr);
    }
  } catch (err) {
    console.error("로그인 통신 오류:", err);
    setAlert($("authError"), err?.message || "로그인 요청이 실패했습니다.");
  } finally {
    if (submit) { submit.disabled = false; submit.textContent = "잠금 해제"; }
  }
}
window.handleLogin = handleLogin;

// ───────── 파라미터 폼 ─────────

const PARAM_FIELDS = [
  { k: "rsiPeriod", label: "RSI 기간", step: 1, min: 2 },
  { k: "rsiBuy", label: "RSI 매수선", step: 1, min: 1 },
  { k: "rsiSell", label: "RSI 매도선", step: 1, min: 2 },
  { k: "slowMa", label: "추세 MA 봉수", step: 1, min: 3 },
  { k: "takeProfitPct", label: "익절 %", step: 0.5, min: 0.1 },
  { k: "stopLossPct", label: "손절 %", step: 0.5, min: 0.1 },
  { k: "trailingStopPct", label: "트레일링 % (0=끔)", step: 0.5, min: 0 },
];
const DEFAULTS = { rsiPeriod: 14, rsiBuy: 35, rsiSell: 75, slowMa: 30,
                   takeProfitPct: 3.8, stopLossPct: 1.8, trailingStopPct: 1.2 };

function renderParams(hostId, prefix) {
  $(hostId).innerHTML = PARAM_FIELDS.map(f => `
    <div>
      <label class="label" for="${prefix}${f.k}">${f.label}</label>
      <input class="input" type="number" id="${prefix}${f.k}"
             value="${DEFAULTS[f.k]}" step="${f.step}" min="${f.min}">
    </div>`).join("");
}
function renderEntryRules(hostId, prefix) {
  const host = $(hostId);
  if (!host) return;
  host.innerHTML = `
    <div class="rules-head">
      <span class="label" style="margin:0">진입 조건</span>
      <select class="input input-sm" id="${prefix}entryMode">
        <option value="any">하나라도 충족 시 진입</option>
        <option value="all">전부 충족 시 진입</option>
      </select>
    </div>
    <div class="rule-grid">
      ${ENTRY_RULES.map(r => `
        <label class="rule" title="${r.desc}">
          <input type="checkbox" id="${prefix}rule_${r.key}" ${r.key === "rsiCrossUp" ? "checked" : ""}>
          <span>${r.label}</span>
        </label>`).join("")}
    </div>
    <div class="muted small" id="${prefix}ruleNote"></div>`;

  const update = () => {
    const on = ENTRY_RULES.filter(r => $(`${prefix}rule_${r.key}`).checked);
    const note = $(`${prefix}ruleNote`);
    if (!on.length) {
      note.innerHTML = `<span class="down">조건을 하나 이상 선택하세요. 선택이 없으면 RSI 상향돌파가 적용됩니다.</span>`;
    } else {
      note.textContent = on.map(r => `${r.label}: ${r.desc}`).join(" · ");
    }
  };
  ENTRY_RULES.forEach(r => $(`${prefix}rule_${r.key}`).onchange = update);
  update();
}

function readParams(prefix) {
  const p = {};
  PARAM_FIELDS.forEach(f => {
    const el = $(prefix + f.k);
    if (el) {
      const v = parseFloat(el.value);
      if (!isNaN(v)) p[f.k] = v;
    }
  });
  const rules = ENTRY_RULES.filter(r => $(`${prefix}rule_${r.key}`)?.checked).map(r => r.key);
  if (rules.length) p.entryRules = rules;
  const mode = $(`${prefix}entryMode`);
  if (mode) p.entryMode = mode.value;

  if (prefix === "bp_") {
    const stratType = $("botStrategyType") ? $("botStrategyType").value : "raoer_v4";
    if (stratType === "raoer_v4" || stratType === "raoer_v1") {
      // 엔진 메뉴가 곧 버전이다. 같은 것을 두 군데서 고르면 어긋나므로
      // 하위 '버전' 선택 칸은 없앴다.
      p.strategyType = "raoer_infinite";
      p.raoerVersion = stratType === "raoer_v4" ? "v4" : "v1";
      p.splitCount = parseInt($("bp_splitCount")?.value || "40", 10);
      p.targetProfitPct = parseFloat($("bp_targetProfitPct")?.value || "10.0");
      p.quarterCutPct = parseFloat($("bp_quarterCutPct")?.value || "25.0");
      p.raoerUseAi = $("bp_raoerUseAi") ? $("bp_raoerUseAi").checked : false;
      p.raoerMinProfitPct = parseFloat($("bp_raoerMinProfitPct")?.value || "5.0");
      p.raoerMaxProfitPct = parseFloat($("bp_raoerMaxProfitPct")?.value || "20.0");
      p.raoerMaxMultiplier = parseFloat($("bp_raoerMaxMultiplier")?.value || "2.0");
      p.raoerTrendMode = $("bp_raoerTrendMode")?.value || "off";
    } else if (stratType === "usdt_premium") {
      p.strategyType = "usdt_premium";
      p.useGemini = false;
      p.usdtBuyPremiumPct = parseFloat($("bp_usdtBuyPremiumPct")?.value || "-0.8");
      p.usdtSellPremiumPct = parseFloat($("bp_usdtSellPremiumPct")?.value || "2.0");
      // 이 전략의 기본 손절은 0(사용 안 함)이다. 공용 기본값(1.8)이 그대로
      // 넘어가면 손절-재매수 루프에 빠진다.
      p.stopLossPct = parseFloat($("bp_usdtStopLossPct")?.value || "0");
    } else {
      p.strategyType = "quant_ai";
      p.useGemini = false;
    }
  } else if (prefix === "tp_") {
    // 백테스트는 무한매수만 남았다 (지표 계열을 실매매에서 뺐으니 여기서도 뺐다).
    p.strategyType = "raoer_infinite";
    p.raoerVersion = $("tp_raoerVersion")?.value || "v4";
    p.splitCount = parseInt($("tp_splitCount")?.value || "40", 10);
    p.targetProfitPct = parseFloat($("tp_targetProfitPct")?.value || "10.0");
    p.quarterCutPct = parseFloat($("tp_quarterCutPct")?.value || "25.0");
  }
  return p;
}

// ───────── 시세 ─────────

let lastPriceAt = 0;

// 마지막 갱신 시각을 계속 갱신해 '멈춘 건지 살아있는 건지' 를 화면에서 알 수 있게 한다.
// 예전에는 세션이 끊기거나 탭이 백그라운드로 밀려도 이전 값이 그대로 남아
// 시세가 살아 있는 것처럼 보였다.
function renderFreshness() {
  const el = $("freshness");
  if (!el) return;
  if (!lastPriceAt) { el.textContent = "갱신 대기"; el.className = "fresh stale"; return; }
  const age = Math.round((Date.now() - lastPriceAt) / 1000);
  const t = new Date(lastPriceAt).toLocaleTimeString("ko-KR");
  const stale = age > 60;
  el.textContent = stale ? `${t} · ${age}초 전 (갱신 지연)` : `${t} · ${age}초 전`;
  el.className = stale ? "fresh stale" : "fresh";
}

async function loadPrices() {
  try {
    const { prices } = await api("/api/prices");
    $("tickerBar").innerHTML = prices.map(p => p.error
      ? `<div class="tick"><div class="tick-name">${p.name}</div>
           <div class="tick-price down" style="font-size:.8rem">조회 실패</div>
           <div class="tick-chg muted" title="${p.error}">${p.error.slice(0, 26)}</div></div>`
      : `<div class="tick"><div class="tick-name">${p.name} (${p.coin})</div>
           <div class="tick-price">${won(p.price)}</div>
           <div class="tick-chg ${cls(p.changePercent)}">${pct(p.changePercent)}</div></div>`
    ).join("");
    lastPriceAt = Date.now();
  } catch (e) {
    console.error("시세 조회 실패:", e);
  } finally {
    renderFreshness();
  }
}

// ───────── 봇 ─────────

let currentMarket = "crypto"; // "crypto" | "stock"
// 취급 종목은 서버(/api/namuh/stocks)가 유일한 출처다. 화면에 목록을 박아두면
// 서버와 어긋난다 — 실제로 화면 3종 / 서버 5종으로 갈라져 있었다. 서버가 그
// 목록으로 배포를 검증하므로, 화면이 다른 것을 보여주면 고를 수 없는 종목이
// 뜨거나 고를 수 있는 종목이 숨는다.
let STOCKS = [];

function setMarket(market) {
  currentMarket = market;
  const isStock = market === "stock";
  const btnCrypto = $("btnMarketCrypto");
  const btnStock = $("btnMarketStock");
  if (btnCrypto && btnStock) {
    btnCrypto.className = isStock ? "btn btn-sm btn-ghost" : "btn btn-sm btn-primary";
    btnStock.className = isStock ? "btn btn-sm btn-primary" : "btn btn-sm btn-ghost";
  }
  if ($("lblBotCoin")) $("lblBotCoin").textContent = isStock ? "미국 ETF 종목" : "코인";
  if ($("lblBotCapital")) $("lblBotCapital").textContent = isStock ? "운용 자본 ($ USD)" : "운용 자본 (원)";
  if ($("botCapital")) {
    $("botCapital").value = isStock ? "1000" : "1000000";
    $("botCapital").min = isStock ? "10" : "10000";
    $("botCapital").step = isStock ? "10" : "10000";
  }
  if ($("optBotLive")) {
    $("optBotLive").textContent = isStock ? "실전 (나무증권 실주문)" : "실전 (빗썸 실주문)";
  }

  const list = isStock ? STOCKS : COINS;
  if ($("botCoin")) {
    $("botCoin").innerHTML = list.length
      ? list.map(c => `<option value="${escapeHtml(c.code)}">${escapeHtml(c.name)} (${escapeHtml(c.code)})</option>`).join("")
      : `<option value="">${isStock ? "종목 목록을 받지 못했습니다" : "코인 목록 없음"}</option>`;
    $("botCoin").disabled = !list.length;
  }
}

async function deployBot() {
  const btn = $("deployBtn");
  setAlert($("deployError"), null);
  const mode = $("botMode").value;
  const isStock = currentMarket === "stock";
  const isUsdt = $("botStrategyType")?.value === "usdt_premium";

  if (mode === "LIVE") {
    const brokerName = isStock ? "농협 나무증권" : "빗썸";
    const currName = isStock ? "미국 달러(USD)" : "원화";
    if (!confirm(
        `실전 모드로 가동합니다.\n\n${brokerName} 계좌에서 실제 ${currName}로 주문이 나가며 손실이 발생할 수 있습니다.\n계속하시겠습니까?`))
      return;
  }
  btn.disabled = true; btn.textContent = "가동 중…";
  try {
    await api("/api/bot/deploy", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        coin: isUsdt ? "USDT" : $("botCoin").value,
        broker: isStock ? "namuh" : "bithumb",
        // 캔들을 안 쓰는 전략이라 화면에서 칸을 숨겼다. 서버는 유효한 값을
        // 요구하므로 고정값을 보낸다 (캔들 갱신 주기로만 쓰인다).
        interval: isUsdt ? "1h" : $("botInterval").value, mode,
        capitalKrw: parseFloat($("botCapital").value), params: readParams("bp_"),
      }),
    });
    await loadBots();
    // 봇 탭으로 이동
    const botTab = document.querySelector('.tab[data-panel="panel-bots"]');
    if (botTab) botTab.click();
  } catch (e) {
    setAlert($("deployError"), e.message);
  } finally {
    btn.disabled = false; btn.textContent = "봇 가동";
  }
}

// 배정자본 중 실제로 시장에 들어간 비율. 이 값이 클수록 '배정 대비' 와
// '평단 대비' 수익률이 가까워진다 — 두 숫자가 다른 이유가 이것이다.
function investedPct(b) {
  const init = Number(b.initialKrw || 0);
  const inv = Number(b.investedKrw || 0);
  if (!init || !inv) return "0%";
  return `${Math.round(inv / init * 100)}%`;
}

function botCard(b) {
  const live = b.mode === "LIVE";
  const isNamuh = b.broker === "namuh";
  const isUsd = b.currency === "USD" || isNamuh;
  const fmtCurr = (v) => isUsd
    ? `$${Number(v||0).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`
    : won(v);
  const fmtPnl = (v) => isUsd
    ? `${Number(v||0) >= 0 ? '+' : ''}$${Number(v||0).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`
    : `${Number(v||0) >= 0 ? '+' : ''}${won(v)}원`;
  const fmtUnits = (u) => isUsd ? Number(u||0).toFixed(4) : Number(u||0).toFixed(6);

  const badge = !b.isRunning ? `<span class="badge badge-stop">정지됨</span>`
    : live ? `<span class="badge badge-live">실전</span>`
           : `<span class="badge badge-paper">모의</span>`;
  const brokerBadge = isNamuh
    ? `<span class="badge" style="background:rgba(16,185,129,.2); color:#34d399; border:1px solid rgba(16,185,129,.4);">🇺🇸 나무증권</span>`
    : `<span class="badge" style="background:rgba(249,115,22,.2); color:#fb923c; border:1px solid rgba(249,115,22,.4);">🪙 빗썸</span>`;

  let stratBadge = "";
  if (b.strategyType === "raoer_infinite") {
    const vTag = (b.raoerVersion || b.params?.raoerVersion || "v4").toUpperCase() === "V4" ? "V4.0" : "V1.0";
    if (b.params?.raoerUseAi) {
      stratBadge = `<span class="badge" style="background:rgba(59,130,246,.2); color:#60a5fa; border:1px solid rgba(59,130,246,.4);">✨ AI 무한매수 ${vTag} (T=${b.turn||0}/${b.splitCount||40})</span>`;
    } else {
      stratBadge = `<span class="badge" style="background:rgba(16,185,129,.18); color:#34d399; border:1px solid rgba(16,185,129,.4);">🔄 무한매수 ${vTag} (T=${b.turn||0}/${b.splitCount||40})</span>`;
    }
  } else if (b.params?.useGemini) {
    // 하이브리드는 화면에서 없앴지만, 예전에 만든 봇이 남아 있을 수 있다.
    const gemMode = b.params?.geminiMode === "ai_only" ? "✨ AI 전용" : "🧬 하이브리드(구)";
    stratBadge = `<span class="badge" style="background:rgba(59,130,246,.2); color:var(--accent); border:1px solid rgba(59,130,246,.4);">${gemMode} (${b.params?.geminiMinConfidence}%)</span>`;
  }

  const logs = (b.recentLogs || []).map(l =>
    `<div class="logline"><span class="t">${l.time}</span><span class="lv-${l.level}">${l.message}</span></div>`).join("");
  return `<div class="bot">
    <div class="bot-head">
      <div><span class="bot-title">${b.coinName} (${b.coin})</span> ${brokerBadge} ${badge} ${stratBadge}
        <span class="muted small">${b.interval}</span></div>
      <div class="inline">
        ${b.isRunning ? `<button class="btn btn-ghost btn-sm" data-stop="${b.botId}">정지</button>` : ""}
        <button class="btn btn-ghost btn-sm" data-del="${b.botId}">삭제</button>
      </div>
    </div>
    <div class="bot-stats">
      <div><div class="stat-k">평가자산</div><div class="stat-v">${fmtCurr(b.equityKrw)}</div></div>
      <div title="배정자본 ${fmtCurr(b.initialKrw)} 대비 평가자산 증감. 분할매수 초반에는 '평단 대비' 보다 작게 나온다. 지금은 ${fmtCurr(b.investedKrw)}(${investedPct(b)})만 시장에 들어가 있다.">
        <div class="stat-k">수익률 <span class="muted" style="font-weight:400;">· 배정 대비</span></div>
        <div class="stat-v ${cls(b.totalReturnPct)}">${pct(b.totalReturnPct)}</div>
        <div class="muted" style="font-size:0.68rem;">${investedPct(b)} 투입</div></div>
      <div><div class="stat-k">현재가 ${b.priceAgeSec != null
          ? `<span class="${b.priceAgeSec > (b.pricePollSec||10)*3 ? 'down' : 'muted'}">${Math.round(b.priceAgeSec)}초 전</span>`
          : ""}</div><div class="stat-v">${fmtCurr(b.currentPrice)}</div></div>
      <div><div class="stat-k">보유 / 평단</div><div class="stat-v">${b.units > 0 ? fmtUnits(b.units) : "-"} ${b.units > 0 ? `<span class="small muted">(${fmtCurr(b.entryPrice)})</span>` : ""}</div></div>
      <div title="산 물량만 놓고 본 손익. 익절·손절 판정이 쓰는 값이 이쪽이다.">
        <div class="stat-k">평가손익 <span class="muted" style="font-weight:400;">· 평단 대비</span></div>
        <div class="stat-v ${cls(b.unrealizedPnlKrw)}">${b.units > 0
          ? `${fmtPnl(b.unrealizedPnlKrw)} <span style="font-size:0.7rem; font-weight:400;">${pct(b.unrealizedPnlPct)}</span>`
          : "-"}</div></div>
      <div title="AI 가 정한 익절 목표와 매수 비중. 목표 수익률은 '평단 대비' 기준이다 — 배정 대비 수익률은 그보다 낮게 찍힌다."><div class="stat-k">${b.strategyType === 'raoer_infinite' ? (b.params?.raoerUseAi ? '회차 <span class="muted" style="font-weight:400;">· 목표/비중</span>' : '진행 회차') : (b.params?.useGemini ? 'AI 신뢰도' : 'RSI')}</div><div class="stat-v">${b.strategyType === 'raoer_infinite' ? (b.params?.raoerUseAi ? `${b.turn||0}/${b.splitCount||40} <span class="small" style="color:var(--accent); font-size:0.75rem;">(+${b.lastAiAnalysis?.dynamicTargetProfitPct || b.params?.targetProfitPct}% / ${b.lastAiAnalysis?.sizingMultiplier || 1.0}x)</span>` : `${b.turn||0} / ${b.splitCount||40}`) : (b.params?.useGemini ? (b.lastAiAnalysis?.confidence ? b.lastAiAnalysis.confidence + "%" : "-") : (b.rsi ?? "-"))}</div></div>
      <div><div class="stat-k">거래 (익절)</div><div class="stat-v">${b.totalTrades}회</div></div>
      <div><div class="stat-k">승률</div><div class="stat-v">${b.totalTrades ? b.winRatePct + "%" : "-"}</div></div>
    </div>
    <div class="bot-decision">판단: ${b.lastDecision || "-"}</div>
    <div class="logs">${logs}</div>
  </div>`;
}

let restoreNoticeShown = false;

async function loadBots() {
  try {
    const { bots, activeCount, maxActive, restoreSummary } = await api("/api/bot/list");
    $("botCount").textContent = `(${activeCount}/${maxActive} 가동)`;

    // 재시작 후 대조에 걸려 보류된 봇이 있으면 알림 표시, 없으면 숨김
    const el = $("globalNotice");
    if (restoreSummary?.fatal) {
      // 상태 파일을 못 읽어 봇을 하나도 못 띄운 경우. 닫기 버튼을 두지 않는다 —
      // 빗썸에 포지션이 남아 있으면 지금 아무도 감시하지 않는 상태다.
      el.classList.remove("hidden");
      el.className = "alert alert-danger";
      el.innerHTML = `
        <b>⛔ 봇 상태를 복원하지 못했습니다.</b><br>
        ${(restoreSummary.notes || []).map(n => escapeHtml(n)).join("<br>")}`;
    } else if (restoreSummary?.held > 0 && !restoreNoticeShown) {
      el.classList.remove("hidden");
      el.className = "alert alert-danger";
      el.innerHTML = `
        <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:0.75rem;">
          <div>
            <b>재시작 후 ${restoreSummary.held}개 봇이 보류되었습니다.</b> 
            내부 기록과 빗썸 실제 보유량이 맞지 않거나 대조에 실패했습니다. 
            빗썸에서 실제 보유량을 확인하고 정리하세요.<br>
            ${(restoreSummary.notes || []).map(n => `· ${n}`).join("<br>")}
          </div>
          <button id="dismissNoticeBtn" class="btn btn-ghost btn-sm" style="white-space:nowrap; padding:0.2rem 0.6rem; font-size:0.75rem; border-color:var(--down); color:var(--down);">닫기 ✕</button>
        </div>`;
      const btn = $("dismissNoticeBtn");
      if (btn) {
        btn.onclick = async () => {
          restoreNoticeShown = true;
          el.classList.add("hidden");
          el.innerHTML = "";
          try {
            await api("/api/bot/dismiss_restore_notice", { method: "POST" });
          } catch (e) { /* ignore */ }
        };
      }
    } else if (!restoreSummary || restoreSummary.held === 0) {
      if (el && el.classList.contains("alert-danger")) {
        el.classList.add("hidden");
        el.innerHTML = "";
      }
    }
    $("botList").innerHTML = bots.length
      ? bots.map(botCard).join("")
      : `<div class="empty">가동 중인 봇이 없습니다.</div>`;
    const byId = Object.fromEntries(bots.map(b => [b.botId, b]));
    $("botList").querySelectorAll("[data-stop]").forEach(el =>
      el.onclick = () => actOnBot("/api/bot/stop", el.dataset.stop,
        liquidationNotice(byId[el.dataset.stop] || {}, "정지")));
    $("botList").querySelectorAll("[data-del]").forEach(el =>
      el.onclick = () => actOnBot("/api/bot/delete", el.dataset.del,
        liquidationNotice(byId[el.dataset.del] || {}, "정지·삭제")));
  } catch (e) { console.error("봇 목록 실패:", e); }
}

// 정지·삭제는 되돌릴 수 없다. 무엇이 얼마나 팔리는지 숫자로 보여준다.
// "청산합니다" 만으로는 그냥 넘기기 쉽다.
function liquidationNotice(b, action) {
  const isNamuh = b.broker === "namuh";
  const isUsd = b.currency === "USD" || isNamuh;
  const brokerTitle = isNamuh ? "나무증권 (해외주식)" : "빗썸";
  const name = `${b.coin} 봇 (${brokerTitle} · ${b.mode === "LIVE" ? "실전" : "모의투자"})`;
  const held = Number(b.units || 0) > 0;

  if (!held) {
    return `${name}\n\n보유 포지션이 없습니다. 주문은 나가지 않습니다.\n\n${action}하시겠습니까?`;
  }
  const value = Number(b.currentPrice || 0) * Number(b.units || 0);
  const pnl = Number(b.unrealizedPnlKrw || 0);
  const pnlPct = Number(b.unrealizedPnlPct || 0);
  const fmtCurr = (v) => isUsd ? `$${Number(v||0).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})}` : `${won(v)}원`;
  const fmtUnits = (u) => isUsd ? Number(u||0).toFixed(4) : Number(u||0).toFixed(8);
  const lines = [
    name,
    "",
    `보유 수량 : ${fmtUnits(b.units)} ${b.coin}`,
    `평단가    : ${fmtCurr(b.entryPrice)}`,
    `현재가    : ${fmtCurr(b.currentPrice)}`,
    `평가 금액 : ${fmtCurr(value)}`,
    `평가 손익 : ${pnl >= 0 ? "+" : ""}${fmtCurr(pnl)} (${pnlPct >= 0 ? "+" : ""}${Number(pnlPct).toFixed(2)}%)`,
    "",
  ];
  if (b.mode === "LIVE") {
    lines.push(`⚠️ 위 물량 전부를 ${brokerTitle}에 시장가 매도 주문으로 냅니다.`);
    lines.push("   되돌릴 수 없고, 평가 손익이 그대로 확정됩니다.");
    if (b.strategyType === "raoer_infinite" && Number(b.turn || 0) > 1) {
      lines.push("");
      lines.push(`   무한매수 ${b.turn}/${b.splitCount}회차 진행 중입니다.`);
      lines.push("   지금 청산하면 평단을 낮추던 과정이 중단됩니다.");
    }
  } else {
    lines.push("모의투자 봇이라 실제 주문은 나가지 않습니다.");
  }
  lines.push("");
  lines.push(`${action}하시겠습니까?`);
  return lines.join("\n");
}

async function actOnBot(url, botId, message) {
  if (!confirm(message)) return;
  try {
    await api(url, { method: "POST", headers: { "Content-Type": "application/json" },
                     body: JSON.stringify({ botId }) });
    await loadBots();
  } catch (e) { alert(e.message); }
}

// ───────── 백테스트 ─────────

async function runBacktest() {
  const btn = $("runBacktestBtn");
  setAlert($("btError"), null);
  $("btResult").classList.add("hidden");
  btn.disabled = true; btn.textContent = "실행 중…";
  try {
    const r = await api("/api/backtest", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        coin: $("btCoin").value, interval: $("btInterval").value,
        initialKrw: parseFloat($("btCapital").value), params: readParams("tp_"),
      }),
    });
    renderBacktest(r);
  } catch (e) {
    setAlert($("btError"), e.message);
  } finally {
    btn.disabled = false; btn.textContent = "백테스트 실행";
  }
}

// 결과 머리글에 무엇으로 돌렸는지 적는다. 예전에는 진입 규칙만 찍어서,
// 무한매수로 돌린 결과에도 'RSI 상향돌파 또는 거래량 급증' 이 붙었다 —
// 실제로는 무한매수가 맞게 돌았는데 화면만 다른 전략처럼 보였다.
function btStrategyLabel(p) {
  if (p?.strategyType === "raoer_infinite") {
    const vTag = (p.raoerVersion || "v4").toUpperCase();
    return `무한매수 ${vTag} · ${p.splitCount}분할 · 목표 익절 +${p.targetProfitPct}% · `
      + `쿼터방어 ${p.quarterCutPct}%`
      + (p.raoerTrendMode && p.raoerTrendMode !== "off" ? ` · 추세 ${p.raoerTrendMode}` : "");
  }
  const rules = (p?.entryRules || []).map(k =>
    (ENTRY_RULES.find(x => x.key === k) || {}).label || k);
  return rules.length
    ? `진입: ${rules.join(p.entryMode === "all" ? " AND " : " 또는 ")}` : "";
}

function renderBacktest(r) {
  const rows = (r.trades || []).slice().reverse().map(t => `
    <tr>
      <td>${when(t.entryTime)}</td><td>${when(t.exitTime)}</td>
      <td>${won(t.entryPrice)}</td><td>${won(t.exitPrice)}</td>
      <td class="${cls(t.returnPct)}">${pct(t.returnPct)}</td>
      <td class="${cls(t.pnlKrw)}">${won(t.pnlKrw)}</td>
      <td class="reason">${t.exitReason}</td>
    </tr>`).join("");

  $("btResult").innerHTML = `<div class="card">
    <div class="card-head">
      <h2 class="card-title">${r.coin}/KRW · ${r.interval} · 캔들 ${r.candleCount}개
        <span class="muted small">${btStrategyLabel(r.params)}</span></h2>
      <span class="muted small">${when(r.periodFrom)} ~ ${when(r.periodTo)}</span>
    </div>
    <div class="metrics">
      <div class="metric"><div class="metric-k">전략 수익률</div>
        <div class="metric-v ${cls(r.totalReturnPct)}">${pct(r.totalReturnPct)}</div></div>
      <div class="metric"><div class="metric-k">그냥 보유했다면</div>
        <div class="metric-v ${cls(r.benchmarkReturnPct)}">${pct(r.benchmarkReturnPct)}</div></div>
      <div class="metric"><div class="metric-k">초과수익</div>
        <div class="metric-v ${cls(r.alphaPct)}">${pct(r.alphaPct)}</div></div>
      <div class="metric"><div class="metric-k">최대 낙폭</div>
        <div class="metric-v down">-${r.maxDrawdownPct}%</div></div>
      <div class="metric"><div class="metric-k">거래 횟수</div>
        <div class="metric-v">${r.totalTrades}</div></div>
      <div class="metric"><div class="metric-k">승률</div>
        <div class="metric-v">${r.totalTrades ? r.winRatePct + "%" : "-"}</div></div>
      <div class="metric"><div class="metric-k">손익비</div>
        <div class="metric-v">${r.profitFactor ?? "-"}</div></div>
      <div class="metric"><div class="metric-k">최종 자산</div>
        <div class="metric-v">${won(r.finalKrw)}</div></div>
    </div>
    ${r.totalTrades === 0 ? `<div class="alert alert-warn">
      이 구간에서 진입 신호가 한 번도 발생하지 않았습니다. RSI 매수선을 높이거나
      캔들 간격을 늘려 더 긴 기간을 보세요.</div>` : ""}
    <div class="alert alert-warn">${r.note}</div>
    ${rows ? `<div class="tbl-wrap"><table>
      <thead><tr><th>진입</th><th>청산</th><th>진입가</th><th>청산가</th>
        <th>수익률</th><th>손익(원)</th><th>청산 사유</th></tr></thead>
      <tbody>${rows}</tbody></table></div>` : ""}
  </div>`;
  $("btResult").classList.remove("hidden");
}

// ───────── 차트 ─────────

async function loadChart() {
  const host = $("chartArea");
  host.innerHTML = `<div class="empty">불러오는 중…</div>`;
  try {
    const r = await api(`/api/candles?coin=${$("chartCoin").value}&interval=${$("chartInterval").value}`);
    const bars = r.candles.filter(b => b.ready);
    if (!bars.length) return host.innerHTML = `<div class="empty">지표를 만들 캔들이 부족합니다.</div>`;
    const last = bars[bars.length - 1];
    host.innerHTML = `
      <div class="metrics">
        <div class="metric"><div class="metric-k">현재가(마지막 종가)</div><div class="metric-v">${won(last.close)}</div></div>
        <div class="metric"><div class="metric-k">RSI(${r.params.rsiPeriod})</div><div class="metric-v">${last.rsi.toFixed(1)}</div></div>
        <div class="metric"><div class="metric-k">MA(${r.params.fastMa})</div><div class="metric-v">${won(last.smaFast)}</div></div>
        <div class="metric"><div class="metric-k">MA(${r.params.slowMa})</div><div class="metric-v">${won(last.smaSlow)}</div></div>
      </div>
      ${sparkline(bars.map(b => b.close), "가격")}
      ${sparkline(bars.map(b => b.rsi), "RSI", 0, 100)}
      <div class="muted small" style="margin-top:.6rem">
        캔들 ${bars.length}개 · ${when(bars[0].time)} ~ ${when(last.time)} · 출처 ${r.dataSource}</div>`;
  } catch (e) {
    host.innerHTML = `<div class="alert alert-error">${e.message}</div>`;
  }
}

function sparkline(values, label, forceMin, forceMax) {
  const w = 800, h = 120, pad = 4;
  const min = forceMin ?? Math.min(...values), max = forceMax ?? Math.max(...values);
  const span = (max - min) || 1;
  const pts = values.map((v, i) => {
    const x = pad + i * (w - pad * 2) / Math.max(1, values.length - 1);
    const y = h - pad - (v - min) / span * (h - pad * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  return `<div style="margin-top:.8rem">
    <div class="stat-k">${label} (${label === "RSI" ? "0~100" : won(min) + " ~ " + won(max)})</div>
    <svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="${label} 추이">
      <polyline points="${pts}" fill="none" stroke="var(--accent)" stroke-width="1.6"/>
    </svg></div>`;
}

// ───────── Gemini AI ─────────

async function loadGeminiScan() {
  const btn = $("geminiScanBtn");
  const statusEl = $("geminiScanStatus");
  const host = $("geminiCards");
  const interval = $("geminiScanInterval") ? $("geminiScanInterval").value : "1h";

  btn.disabled = true;
  btn.textContent = "AI 분석 중…";
  statusEl.innerHTML = `<span class="muted">Google Gemini AI가 5개 코인을 정밀 분석하고 있습니다. 잠시만 기다려주세요…</span>`;

  try {
    const data = await api(`/api/gemini/scan?interval=${interval}`);
    statusEl.innerHTML = `<span class="muted">스캔 완료 시각: ${data.scanned_at} (모델: <code>${data.model}</code> · ${interval} 기준)</span>`;
    
    if (!data.results || !data.results.length) {
      host.innerHTML = `<div class="empty">분석 결과가 없습니다.</div>`;
      return;
    }

    host.innerHTML = data.results.map(r => {
      const isBuy = r.action === "BUY";
      const isSell = r.action === "SELL";
      const badgeCls = isBuy ? "gemini-badge-BUY" : isSell ? "gemini-badge-SELL" : "gemini-badge-HOLD";
      const barColor = isBuy ? "var(--up)" : isSell ? "var(--down)" : "var(--muted)";
      const cardCls = isBuy ? "action-BUY" : isSell ? "action-SELL" : "action-HOLD";
      const conf = r.confidence || 0;

      const reasonsHtml = (r.reasons || []).map(re => `<li>${re}</li>`).join("");

      return `
        <div class="gemini-card ${cardCls}">
          <div class="gemini-card-head">
            <div>
              <span class="gemini-coin-title">${r.name || r.coin} <span class="muted small">(${r.coin})</span></span>
              <div class="muted small" style="margin-top:2px;">현재가: <b class="mono" style="color:var(--text);">${won(r.current_price)}원</b></div>
            </div>
            <div class="gemini-action-badge ${badgeCls}">${r.action}</div>
          </div>

          <div class="gemini-conf-wrap">
            <div style="display:flex; justify-content:space-between; font-size:.78rem;">
              <span class="stat-k">AI 신뢰도</span>
              <span class="mono" style="font-weight:700; color:${barColor};">${conf}%</span>
            </div>
            <div class="gemini-conf-bar-bg">
              <div class="gemini-conf-bar-fill" style="width:${conf}%; background:${barColor};"></div>
            </div>
          </div>

          <div class="gemini-summary">
            ${r.summary || "분석 요약 없음"}
          </div>

          <ul class="gemini-reasons">
            ${reasonsHtml}
          </ul>

          <div class="gemini-targets">
            <div>권장 익절: <b class="up">+${r.target_profit_pct || 3.5}%</b></div>
            <div>권장 손절: <b class="down">-${r.stop_loss_pct || 2.0}%</b></div>
          </div>

          <div class="gemini-card-foot">
            <button class="btn btn-ghost btn-sm" id="btn-reanalyze-${r.coin}" onclick="analyzeSingleCoin('${r.coin}', '${interval}')" title="이 코인만 1초 만에 단독 재분석">
              🔄 단독 분석
            </button>
          </div>
        </div>
      `;
    }).join("");

  } catch (err) {
    statusEl.innerHTML = `<span class="down">스캔 실패: ${err.message}</span>`;
    host.innerHTML = `<div class="alert alert-error">Gemini AI 스캔 중 오류가 발생했습니다: ${err.message}<br><small>Gemini API 키가 올바르게 설정되어 있는지 확인하세요.</small></div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = "⚡ 전체 AI 스캔";
  }
}

async function analyzeSingleCoin(coin, interval) {
  const btn = $(`btn-reanalyze-${coin}`);
  if (btn) { btn.disabled = true; btn.textContent = "분석 중…"; }
  try {
    const res = await api("/api/gemini/analyze", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ coin, interval, forceRefresh: true })
    });
    // 스캔 결과 새로고침 없이 즉시 완료 안내
    await loadGeminiScan();
  } catch (err) {
    alert(`${coin} 단독 분석 실패: ${err.message}`);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "🔄 단독 분석"; }
  }
}


async function loadGeminiStatus() {
  try {
    const s = await api("/api/gemini/status");
    $("geminiStatus").innerHTML = [
      ["연동 상태", s.configured ? "등록됨" : "미등록"],
      ["API 키", s.maskedKey || "-"],
      ["보관 위치", s.source === "env" ? "환경변수 (.env)" : s.source === "disk" ? "서버 파일 (data/gemini_key.json)" : "-"],
      ["기본 모델", `<code>${s.model}</code>`],
    ].map(([k, v]) => `<div class="kv-row"><span class="kv-k">${k}</span><span class="kv-v">${v}</span></div>`).join("");

    if (s.model && $("geminiModelSelect")) {
      $("geminiModelSelect").value = s.model;
    }
    if (s.configured && s.readOnly) {
      $("geminiKeyForm").classList.add("hidden");
    } else {
      $("geminiKeyForm").classList.remove("hidden");
    }
    if (s.configured && !s.readOnly && $("clearGeminiBtn")) {
      $("clearGeminiBtn").classList.remove("hidden");
    } else if ($("clearGeminiBtn")) {
      $("clearGeminiBtn").classList.add("hidden");
    }
  } catch (e) {
    console.error("Gemini 상태 조회 실패:", e);
  }
}

async function geminiKeyAction(save) {
  const apiKey = $("geminiApiKeyInput").value.trim();
  const model = $("geminiModelSelect").value;
  if (!apiKey) return setAlert($("geminiKeyResult"), "Gemini API Key 를 입력하세요.");
  const btn = save ? $("saveGeminiBtn") : $("testGeminiBtn");
  btn.disabled = true;
  setAlert($("geminiKeyResult"), null);
  try {
    const r = await api(save ? "/api/gemini/save" : "/api/gemini/test", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ apiKey, model }),
    });
    if (save || r.success) {
      setAlert($("geminiKeyResult"), `연결 성공 — ${r.message || "정상 작동 확인"}` + (save ? " · 저장되었습니다." : ""), "ok");
      $("geminiApiKeyInput").value = "";
      await loadGeminiStatus();
    } else {
      setAlert($("geminiKeyResult"), r.message);
    }
  } catch (e) {
    setAlert($("geminiKeyResult"), e.message);
  } finally {
    btn.disabled = false;
  }
}

async function clearGeminiKey() {
  if (!confirm("저장된 Gemini API 키를 삭제하시겠습니까?")) return;
  try {
    await api("/api/gemini/clear", { method: "POST" });
    setAlert($("geminiKeyResult"), "Gemini API 키가 삭제되었습니다.", "ok");
    await loadGeminiStatus();
  } catch (e) {
    setAlert($("geminiKeyResult"), e.message);
  }
}

function toggleStrategyUI() {
  const type = $("botStrategyType")?.value || "raoer_v4";
  const raoerOpts = $("raoerBotOptions");
  const gemOptions = $("geminiBotOptions");
  const quantSettings = $("quantBotSettings");
  const raoerAiSub = $("raoerAiSubOptions");
  const raoerUseAiCheck = $("bp_raoerUseAi");

  const usdtOpts = $("usdtBotOptions");
  if (usdtOpts) usdtOpts.classList.toggle("hidden", type !== "usdt_premium");

  // USDT 환차익은 대상이 USDT 로 고정된다. 다른 코인을 고른 채로
  // 가동하면 엉뚱한 종목에 환차익 로직이 걸린다.
  //
  // USDT 는 자동매매 종목 목록(/api/coins)에 없다 — 차익거래 전용이라
  // 빼 뒀다. 그래서 칸을 잠그기만 하면 라벨은 'USDT 고정' 인데 화면에는
  // 비트코인이 그대로 남아, 무엇이 걸리는지 알 수 없었다.
  // 이 전략을 고를 때만 USDT 를 넣어 실제로 보여주고, 빠져나가면 되돌린다.
  const coinSel = $("botCoin");
  const fixed = type === "usdt_premium";
  if (coinSel) {
    let usdtOpt = coinSel.querySelector('option[value="USDT"]');
    if (fixed) {
      if (!usdtOpt) {
        usdtOpt = document.createElement("option");
        usdtOpt.value = "USDT";
        usdtOpt.textContent = "테더 (USDT)";
        coinSel.appendChild(usdtOpt);
      }
      if (coinSel.value !== "USDT") {
        coinSel.dataset.prevCoin = coinSel.value;
        coinSel.value = "USDT";
      }
    } else if (usdtOpt) {
      usdtOpt.remove();
      coinSel.value = coinSel.dataset.prevCoin || coinSel.options[0]?.value || "";
    }
    coinSel.disabled = fixed;
    const label = coinSel.closest("div")?.querySelector(".label");
    if (label) label.textContent = fixed ? "대상 코인 (USDT 고정)" : "코인";
  }

  // 이 전략은 캔들을 판단에 쓰지 않는다 — 현재가와 공시환율만 본다.
  // 고를 이유가 없는 칸을 남겨두면 '설정했는데 반영이 안 된다' 로 읽힌다.
  const intervalField = $("botIntervalField");
  if (intervalField) intervalField.classList.toggle("hidden", fixed);

  const isRaoer = (type === "raoer_v4" || type === "raoer_v1");
  if (raoerOpts) raoerOpts.classList.toggle("hidden", !isRaoer);
  // Gemini 매매 전략을 제거해 이 설정을 쓰는 전략이 없다. 항상 감춘다.
  if (gemOptions) gemOptions.classList.add("hidden");
  // 진입 규칙·지표 설정을 쓰는 전략이 이 탭에 더는 없다. '전통 기술적 지표'
  // 와 '퀀트 하이브리드' 를 뺐고, 'AI 전용' 은 지표 조건을 보지 않는다
  // (trader.py 의 ai_only 분기는 decide() 를 부르지 않는다).
  // 보이면 '설정했는데 반영이 안 된다' 가 되므로 항상 감춘다.
  if (quantSettings) quantSettings.classList.add("hidden");
  if (raoerAiSub && raoerUseAiCheck) {
    raoerAiSub.classList.toggle("hidden", !raoerUseAiCheck.checked || !isRaoer);
  }
}

// ───────── 계정 ─────────

async function loadAccount() {
  try {
    const a = await api("/api/account");
    const pill = $("accountPill");
    if (!a.connected) {
      pill.className = "pill"; pill.textContent = "빗썸 미연동";
    } else if (a.balanceOk) {
      pill.className = "pill ok"; pill.textContent = `빗썸 연동 · ${won(a.krwAvailable)}원`;
    } else {
      pill.className = "pill bad"; pill.textContent = "빗썸 인증 실패";
    }

    $("accountStatus").innerHTML = [
      ["연동 상태", a.connected ? "등록됨" : "미등록"],
      ["키", a.maskedKey || "-"],
      ["보관 위치", a.source === "env" ? "환경변수" : a.source === "disk" ? "서버 파일(평문)" : "-"],
      ["인증 확인", a.connected ? (a.balanceOk ? `성공 (API ${a.apiVersion})` : "실패") : "-"],
    ].map(([k, v]) => `<div class="kv-row"><span class="kv-k">${k}</span><span class="kv-v">${v}</span></div>`).join("")
      + `<div class="muted small" style="margin-top:.5rem">${a.storageNote}</div>`;

    if (a.connected && !a.editable) $("keyForm").classList.add("hidden");
    else $("keyForm").classList.remove("hidden");

    $("balanceBox").innerHTML = !a.connected
      ? `<div class="empty">빗썸 API 키를 등록하면 표시됩니다.</div>`
      : !a.balanceOk
        ? `<div class="alert alert-error">${a.error}</div>`
        : `<div class="kv">
             <div class="kv-row"><span class="kv-k">주문가능 원화</span><span class="kv-v">${won(a.krwAvailable)}원</span></div>
             <div class="kv-row"><span class="kv-k">총 보유 원화</span><span class="kv-v">${won(a.krwTotal)}원</span></div>
             ${Object.entries(a.coins || {}).map(([c, v]) =>
               `<div class="kv-row"><span class="kv-k">${c}</span><span class="kv-v">${Number(v).toFixed(8)}</span></div>`).join("")}
           </div>`;
  } catch (e) { console.error("계정 조회 실패:", e); }
}

async function loadEgressIp() {
  try {
    const r = await api("/api/system/egress_ip");
    $("egressIp").textContent = r.registerThisIp || "확인 불가";
    $("egressNote").textContent = r.hint || r.error ||
      (r.proxyConfigured ? `프록시 ${r.proxyHost} 경유 — 이 IP 를 등록하세요.`
                         : "이 서버가 빗썸으로 나갈 때 쓰는 IP 입니다.");
  } catch (e) {
    $("egressIp").textContent = "확인 불가";
    $("egressNote").textContent = e.message;
  }
}

async function keyAction(save) {
  const apiKey = $("apiKeyInput").value.trim(), secretKey = $("secretKeyInput").value.trim();
  if (!apiKey || !secretKey) return setAlert($("keyResult"), "Connect Key 와 Secret Key 를 모두 입력하세요.");
  const btn = save ? $("saveKeyBtn") : $("testKeyBtn");
  btn.disabled = true;
  setAlert($("keyResult"), null);
  try {
    const r = await api(save ? "/api/account/save" : "/api/account/test", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ apiKey, secretKey }),
    });
    if (save || r.success) {
      setAlert($("keyResult"),
        `연결 성공 — 주문가능 ${won(r.krwAvailable)}원 / 총 ${won(r.krwTotal)}원` +
        (save ? " · 저장했습니다." : ""), "ok");
      $("secretKeyInput").value = "";
      await loadAccount();
    } else {
      setAlert($("keyResult"), r.message);
    }
  } catch (e) {
    setAlert($("keyResult"), e.message);
  } finally {
    btn.disabled = false;
  }
}

// ───────── 나무증권 계정 ─────────

async function loadNamuhAccount() {
  try {
    const a = await api("/api/namuh/account");
    const pill = $("namuhPill");
    if (pill) {
      if (!a.connected) {
        pill.className = "pill"; pill.textContent = "나무증권 미연동";
      } else if (a.balanceOk) {
        const usdFmt = Number(a.usdAvailable || 0).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
        pill.className = "pill ok"; pill.textContent = `나무증권 연동 · $${usdFmt}`;
      } else {
        pill.className = "pill bad"; pill.textContent = "나무증권 인증 실패";
      }
    }

    const stEl = $("namuhStatus");
    if (stEl) {
      stEl.innerHTML = [
        ["연동 상태", a.connected ? "등록됨" : "미등록"],
        ["앱 키", a.maskedKey || "-"],
        ["계좌번호", a.accountNo ? `${a.accountNo.slice(0, 4)}****` : "-"],
        ["보관 위치", a.source === "env" ? "환경변수" : a.source === "disk" ? "서버 파일(평문)" : "-"],
        ["인증 확인", a.connected ? (a.balanceOk ? "성공 (OAuth2 토큰 정상)" : "실패") : "-"],
      ].map(([k, v]) => `<div class="kv-row"><span class="kv-k">${k}</span><span class="kv-v">${v}</span></div>`).join("")
        + `<div class="muted small" style="margin-top:.5rem">${a.storageNote || ''}</div>`;
    }

    if (a.connected && a.editable) {
      $("clearNamuhKeyBtn")?.classList.remove("hidden");
    } else {
      $("clearNamuhKeyBtn")?.classList.add("hidden");
    }

    const balBox = $("namuhBalanceBox");
    if (balBox) {
      if (!a.connected) {
        balBox.innerHTML = `<div class="empty">나무증권 API 키를 등록하면 표시됩니다.</div>`;
      } else if (!a.balanceOk) {
        balBox.innerHTML = `<div class="alert alert-error">${escapeHtml(a.error || '계좌 조회 실패')}</div>`;
      } else {
        const holdings = a.holdings || [];
        const usdAvail = Number(a.usdAvailable || 0).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
        const usdTot = Number(a.usdTotal || 0).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
        balBox.innerHTML = `
          <div class="kv">
            <div class="kv-row"><span class="kv-k">주문가능 외화 (USD)</span><span class="kv-v" style="font-weight:700; color:var(--accent);">$${usdAvail}</span></div>
            <div class="kv-row"><span class="kv-k">총 외화 예수금</span><span class="kv-v">$${usdTot}</span></div>
            ${holdings.length ? holdings.map(h => `
              <div class="kv-row">
                <span class="kv-k"><b>${h.symbol}</b> (${h.name || h.symbol})</span>
                <span class="kv-v">${Number(h.quantity || 0).toFixed(4)}주 @ $${Number(h.avgPrice || 0).toFixed(2)}</span>
              </div>`).join("") : `<div class="kv-row"><span class="kv-k">보유 ETF</span><span class="kv-v muted">보유 주식 없음</span></div>`}
          </div>`;
      }
    }
  } catch (e) { console.error("나무증권 계정 조회 실패:", e); }
}

async function namuhKeyAction(save) {
  const appKey = $("namuhAppKeyInput")?.value.trim() || "";
  const appSecret = $("namuhAppSecretInput")?.value.trim() || "";
  const accountNo = $("namuhAccountNoInput")?.value.trim() || "";
  if (!appKey || !appSecret) return setAlert($("namuhKeyResult"), "App Key 와 App Secret 을 모두 입력하세요.");

  const btn = save ? $("saveNamuhKeyBtn") : $("testNamuhKeyBtn");
  if (btn) btn.disabled = true;
  setAlert($("namuhKeyResult"), null);
  try {
    const r = await api(save ? "/api/namuh/save" : "/api/namuh/test", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ appKey, appSecret, accountNo }),
    });
    if (save || r.success) {
      setAlert($("namuhKeyResult"),
        `연결 성공 — 주문가능 $${Number(r.usdAvailable || 0).toFixed(2)}` +
        (save ? " · 저장했습니다." : ""), "ok");
      if ($("namuhAppSecretInput")) $("namuhAppSecretInput").value = "";
      await loadNamuhAccount();
    } else {
      setAlert($("namuhKeyResult"), r.message || "연결 실패");
    }
  } catch (e) {
    setAlert($("namuhKeyResult"), e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function clearNamuhKey() {
  if (!confirm("나무증권 API 키를 삭제하시겠습니까?")) return;
  try {
    await api("/api/namuh/clear", { method: "POST" });
    setAlert($("namuhKeyResult"), "나무증권 키가 삭제되었습니다.", "ok");
    await loadNamuhAccount();
  } catch (e) {
    setAlert($("namuhKeyResult"), e.message);
  }
}

// ───────── 매매 일지 & 수익 정산 ─────────

let RAW_TRADES_DATA = { summary: {}, trades: [] };

async function loadTradeHistory() {
  try {
    const data = await api("/api/bot/trades");
    RAW_TRADES_DATA = data || { summary: {}, trades: [] };
    const s = RAW_TRADES_DATA.summary || {};

    // 장부를 못 읽었으면 숫자를 '누적' 이라고 부를 수 없다. 먼저 알린다.
    const warnEl = $("ledgerWarning");
    if (warnEl) {
      if (RAW_TRADES_DATA.ledgerWarning) {
        warnEl.classList.remove("hidden");
        warnEl.innerHTML = `
          <b>⛔ 체결 일지를 읽지 못했습니다.</b> 아래 숫자는 이번 재시작 이후 체결만
          반영한 것이라 누적치가 아닙니다. 과거 기록을 덮어쓰지 않도록 저장은
          막아 두었습니다.<br>
          <span class="muted small">${escapeHtml(RAW_TRADES_DATA.ledgerWarning)}</span>`;
      } else {
        warnEl.classList.add("hidden");
        warnEl.innerHTML = "";
      }
    }

    if ($("mTotalPnl")) {
      const pnl = s.totalRealizedPnlKrw || 0;
      $("mTotalPnl").innerHTML = `<span class="${cls(pnl)}">${pnl >= 0 ? "+" : ""}${won(pnl)}원</span>`;
    }
    if ($("mTotalTrades")) {
      const tot = s.totalTrades || 0;
      const win = s.winningTrades || 0;
      const lose = Math.max(0, tot - win);
      $("mTotalTrades").textContent = `${tot}회 (${win}승 ${lose}패)`;
    }
    if ($("mWinRate")) {
      const wr = s.winRatePct || 0;
      $("mWinRate").innerHTML = `<span class="${wr >= 50 ? 'up' : wr > 0 ? 'down' : ''}">${wr.toFixed(1)}%</span>`;
    }
    if ($("mTotalBuy")) $("mTotalBuy").textContent = `${won(s.totalBuyKrw || 0)}원`;
    if ($("mTotalSell")) $("mTotalSell").textContent = `${won(s.totalSellKrw || 0)}원`;

    // 코인별 요약 테이블
    const coinBody = $("coinSummaryBody");
    if (coinBody) {
      const byCoin = s.byCoin || [];
      if (!byCoin.length) {
        coinBody.innerHTML = `<tr><td colspan="5" class="empty">체결된 매매 내역이 없습니다.</td></tr>`;
      } else {
        coinBody.innerHTML = byCoin.map(c => {
          const pnl = c.realizedPnlKrw || 0;
          const isUsd = c.currency === "USD";
          const pnlStr = isUsd ? `${pnl >= 0 ? '+' : ''}$${Number(pnl).toFixed(2)}` : `${pnl >= 0 ? '+' : ''}${won(pnl)}원`;
          return `
            <tr>
              <td><b>${c.coinName || c.coin}</b> <span class="muted small">(${c.coin})</span></td>
              <td>${c.totalTrades || 0}회</td>
              <td>${c.winningTrades || 0}회</td>
              <td class="${(c.winRatePct || 0) >= 50 ? 'up' : ''}">${(c.winRatePct || 0).toFixed(1)}%</td>
              <td class="${cls(pnl)}">${pnlStr}</td>
            </tr>
          `;
        }).join("");
      }
    }

    // 코인 필터 옵션 채우기
    const filterCoin = $("tradeFilterCoin");
    if (filterCoin && filterCoin.options.length <= 1 && COINS.length) {
      COINS.forEach(c => {
        const opt = document.createElement("option");
        opt.value = c.code;
        opt.textContent = `${c.name} (${c.code})`;
        filterCoin.appendChild(opt);
      });
    }

    renderTradeRecords();
  } catch (e) {
    console.error("매매 일지 조회 실패:", e);
  }
}

// 체결사유에서 AI 설명 본문을 잘라낸다.
//
// 저장된 사유는 이렇게 길다:
//   무한매수 27/40회차 매수 (평단 대비 +0.76%) [AI 1.0x 배수: RSI 46.9 및
//   MACD 음수권 회복 흐름의 단기 기술적 반등 국면으로, 표준 1.0배 매수
//   유지 및 빠른 현금화를 위한 목표 익절률 7.5% 설정]
//
// 일지 표에서는 '[AI 1.0x 배수]' 까지만 보여준다. 한 줄에 열이 10개라
// 설명 본문이 들어가면 나머지 숫자를 못 읽는다. 원문은 지우지 않는다 —
// 장부에 그대로 남고, 칸에 마우스를 올리면 전문이 뜨고, CSV 에도 전문이
// 나간다. 봇 로그에도 남아 있다.
function shortReason(reason) {
  const r = String(reason || "");
  // '[AI 0.5x 배수: ...]' → '[AI 0.5x 배수]'  (콜론 뒤를 버린다)
  return r.replace(/\[(AI\s*[^\]:]*?)\s*:\s*[^\]]*\]/g, "[$1]");
}

function renderTradeRecords() {
  const host = $("tradesTableBody");
  if (!host) return;

  const fCoin = $("tradeFilterCoin") ? $("tradeFilterCoin").value : "ALL";
  const fAction = $("tradeFilterAction") ? $("tradeFilterAction").value : "ALL";
  const fMode = $("tradeFilterMode") ? $("tradeFilterMode").value : "ALL";

  const filtered = (RAW_TRADES_DATA.trades || []).filter(t => {
    if (fCoin !== "ALL" && t.coin !== fCoin) return false;
    if (fAction !== "ALL" && !t.action.startsWith(fAction)) return false;
    if (fMode !== "ALL" && t.mode !== fMode) return false;
    return true;
  });

  if (!filtered.length) {
    host.innerHTML = `<tr><td colspan="10" class="empty">조건에 맞는 체결 내역이 없습니다.</td></tr>`;
    return;
  }

  host.innerHTML = filtered.map(t => {
    const isBuy = t.action.includes("BUY");
    const isSell = t.action.includes("SELL");

    let actBadge = `<span class="badge">${t.action}</span>`;
    if (t.action === "BUY") actBadge = `<span class="badge" style="background:rgba(34,197,94,0.18); color:var(--up);">일반매수</span>`;
    else if (t.action === "BUY_CHUNK") actBadge = `<span class="badge" style="background:rgba(34,197,94,0.18); color:var(--up);">분할매수 (T=${t.turn || 1})</span>`;
    // VR 전략은 제거했지만 옛 장부에 기록이 남아 있을 수 있다.
    else if (t.action === "BUY_VR") actBadge = `<span class="badge" style="background:rgba(34,197,94,0.18); color:var(--up);">VR매수</span>`;
    else if (t.action === "SELL") actBadge = `<span class="badge" style="background:rgba(239,68,68,0.18); color:var(--down);">전량매도(익절)</span>`;
    else if (t.action === "SELL_QUARTER") actBadge = `<span class="badge" style="background:rgba(245,158,11,0.18); color:var(--warn);">쿼터방어(손절)</span>`;
    else if (t.action === "SELL_VR") actBadge = `<span class="badge" style="background:rgba(239,68,68,0.18); color:var(--down);">VR매도</span>`;

    const pnl = t.pnlKrw != null && isSell ? t.pnlKrw : null;
    const pnlPct = t.returnPct != null && isSell ? t.returnPct : null;
    const isUsd = t.currency === "USD" || t.broker === "namuh";
    const fmtPrice = isUsd ? `$${Number(t.price||0).toFixed(2)}` : `${won(t.price)}원`;
    const fmtUnits = isUsd ? Number(t.units||0).toFixed(4) : Number(t.units||0).toFixed(8);
    const fmtAmt = isUsd ? `$${Number(t.amountKrw||0).toFixed(2)}` : `${won(t.amountKrw)}원`;
    const fmtPnlVal = pnl != null ? (isUsd ? `${pnl >= 0 ? '+' : ''}$${Number(pnl).toFixed(2)}` : `${pnl >= 0 ? '+' : ''}${won(pnl)}원`) : '-';

    return `
      <tr>
        <td class="muted small">${t.time}</td>
        <td><b>${t.coinName || t.coin}</b> <span class="muted small">(${t.coin})</span></td>
        <td><span class="badge ${t.mode === 'LIVE' ? 'badge-live' : 'badge-paper'}">${t.mode}</span></td>
        <td>${actBadge}</td>
        <td>${fmtPrice}</td>
        <td>${fmtUnits}</td>
        <td>${fmtAmt}</td>
        <td class="${pnl != null ? cls(pnl) : ''}">${fmtPnlVal}</td>
        <td class="${pnlPct != null ? cls(pnlPct) : ''}">${pnlPct != null ? pct(pnlPct) : '-'}</td>
        <td class="reason" title="${escapeHtml(t.reason || '')}">${escapeHtml(shortReason(t.reason)) || '-'}</td>
      </tr>
    `;
  }).join("");
}

function exportTradesToCsv() {
  const trades = RAW_TRADES_DATA.trades || [];
  if (!trades.length) return alert("내보낼 체결 내역이 없습니다.");

  const headers = ["체결시각", "코인", "코인명", "봇ID", "모드", "주문구분", "회차", "체결단가(KRW)", "체결수량", "체결금액(KRW)", "실현손익(KRW)", "수익률(%)", "체결사유"];
  const rows = trades.map(t => [
    `"${t.time || ''}"`,
    `"${t.coin || ''}"`,
    `"${t.coinName || ''}"`,
    `"${t.botId || ''}"`,
    `"${t.mode || ''}"`,
    `"${t.action || ''}"`,
    t.turn || 0,
    t.price || 0,
    Number(t.units || 0).toFixed(8),
    t.amountKrw || 0,
    t.pnlKrw || 0,
    t.returnPct || 0,
    `"${(t.reason || '').replace(/"/g, '""')}"`
  ]);

  const csvContent = "\uFEFF" + [headers.join(","), ...rows.map(r => r.join(","))].join("\r\n");
  const blob = new Blob([csvContent], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  const now = new Date().toISOString().slice(0, 19).replace(/[-:T]/g, "");
  a.href = url;
  a.download = `bithumb_bot_trade_journal_${now}.csv`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

// ───────── 부팅 ─────────

async function boot() {
  if (started) return;
  started = true;

  const meta = await api("/api/coins");
  COINS = meta.coins; INTERVALS = meta.intervals;
  const coinOpts = COINS.map(c => `<option value="${c.code}">${c.name} (${c.code})</option>`).join("");
  const ivOpts = INTERVALS.map(i => `<option value="${i}"${i === "24h" ? " selected" : ""}>${i}</option>`).join("");
  ["botCoin", "btCoin", "chartCoin"].forEach(id => $(id).innerHTML = coinOpts);
  ["botInterval", "btInterval", "chartInterval"].forEach(id => $(id).innerHTML = ivOpts);

  ENTRY_RULES = meta.entryRules || [];
  renderEntryRules("botRules", "bp_");
  renderParams("botParams", "bp_");

  // 전략 선택기 UI 바인딩
  if ($("botStrategyType")) {
    $("botStrategyType").onchange = toggleStrategyUI;
  }
  if ($("bp_raoerUseAi")) {
    $("bp_raoerUseAi").onchange = toggleStrategyUI;
  }
  toggleStrategyUI();
  if ($("geminiMinConf")) {
    $("geminiMinConf").oninput = () => {
      $("geminiConfVal").textContent = $("geminiMinConf").value + "%";
    };
  }

  // Gemini AI 스캐너 바인딩
  if ($("geminiScanBtn")) $("geminiScanBtn").onclick = loadGeminiScan;
  if ($("geminiScanInterval")) $("geminiScanInterval").onchange = loadGeminiScan;
  if ($("testGeminiBtn")) $("testGeminiBtn").onclick = () => geminiKeyAction(false);
  if ($("saveGeminiBtn")) $("saveGeminiBtn").onclick = () => geminiKeyAction(true);
  if ($("clearGeminiBtn")) $("clearGeminiBtn").onclick = clearGeminiKey;

  // 매매 일지 바인딩
  if ($("refreshTradesBtn")) $("refreshTradesBtn").onclick = loadTradeHistory;
  if ($("exportTradesCsvBtn")) $("exportTradesCsvBtn").onclick = exportTradesToCsv;
  if ($("tradeFilterCoin")) $("tradeFilterCoin").onchange = renderTradeRecords;
  if ($("tradeFilterAction")) $("tradeFilterAction").onchange = renderTradeRecords;
  if ($("tradeFilterMode")) $("tradeFilterMode").onchange = renderTradeRecords;

  $("botMode").onchange = () =>
    $("liveWarning").classList.toggle("hidden", $("botMode").value !== "LIVE");
  $("deployBtn").onclick = deployBot;
  $("stopAllBtn").onclick = async () => {
    // 무엇이 팔리는지 집계해서 보여준다.
    let bots = [];
    try { bots = (await api("/api/bot/list")).bots || []; } catch (e) { /* 아래서 일반 문구 */ }
    const live = bots.filter(b => b.isRunning && b.mode === "LIVE" && Number(b.units || 0) > 0);
    let msg;
    if (!bots.length) {
      msg = "가동 중인 봇이 없습니다. 계속하시겠습니까?";
    } else if (!live.length) {
      msg = `가동 중인 봇 ${bots.filter(b => b.isRunning).length}대를 정지합니다.\n`
          + "실전 포지션이 없어 실제 주문은 나가지 않습니다.\n\n계속하시겠습니까?";
    } else {
      const total = live.reduce((s, b) => s + Number(b.currentPrice || 0) * Number(b.units || 0), 0);
      const pnl = live.reduce((s, b) => s + Number(b.unrealizedPnlKrw || 0), 0);
      msg = ["⚠️ 실전 포지션 " + live.length + "건을 시장가로 전량 매도합니다.", ""]
        .concat(live.map(b => `  · ${b.coin} ${Number(b.units).toFixed(8)} `
                            + `(평가 ${won(Number(b.currentPrice || 0) * Number(b.units || 0))}원, `
                            + `${Number(b.unrealizedPnlKrw || 0) >= 0 ? "+" : ""}${won(b.unrealizedPnlKrw)}원)`))
        .concat(["", `합계 평가 ${won(total)}원 · 손익 ${pnl >= 0 ? "+" : ""}${won(pnl)}원`,
                 "", "되돌릴 수 없습니다. 계속하시겠습니까?"]).join("\n");
    }
    if (!confirm(msg)) return;
    try { await api("/api/bot/stop_all", { method: "POST" }); await loadBots(); await loadTradeHistory(); }
    catch (e) { alert(e.message); }
  };
  $("runBacktestBtn").onclick = runBacktest;
  $("chartCoin").onchange = $("chartInterval").onchange = loadChart;
  $("testKeyBtn").onclick = () => keyAction(false);
  $("saveKeyBtn").onclick = () => keyAction(true);
  $("copyIpBtn").onclick = () => {
    const ip = $("egressIp").textContent.trim();
    if (!/^\d{1,3}(\.\d{1,3}){3}$/.test(ip)) return alert("등록할 IP 를 아직 확인하지 못했습니다.");
    navigator.clipboard.writeText(ip).then(() => alert(`IP ${ip} 복사 완료`))
      .catch(() => prompt("아래 IP 를 복사해 빗썸에 등록하세요:", ip));
  };

  // 나무증권 UI 및 키 바인딩
  if ($("btnMarketCrypto")) $("btnMarketCrypto").onclick = () => setMarket("crypto");
  if ($("btnMarketStock")) $("btnMarketStock").onclick = () => setMarket("stock");
  if ($("testNamuhKeyBtn")) $("testNamuhKeyBtn").onclick = () => namuhKeyAction(false);
  if ($("saveNamuhKeyBtn")) $("saveNamuhKeyBtn").onclick = () => namuhKeyAction(true);
  if ($("clearNamuhKeyBtn")) $("clearNamuhKeyBtn").onclick = clearNamuhKey;

  try {
    const sData = await api("/api/namuh/stocks");
    if (sData && sData.stocks) {
      STOCKS = sData.stocks.map(s => ({ code: s.code, name: s.name }));
    }
  } catch (e) {
    // 목록을 못 받으면 비워 둔다. 오래된 목록을 보여주면 서버가 거부하는
    // 종목을 고르게 되거나, 고를 수 있는 종목이 숨는다.
    STOCKS = [];
    console.warn("나무증권 종목 조회 실패:", e);
  }
  if (currentMarket === "stock") setMarket("stock");

  // ───────── 차익거래 (Arbitrage) 핸들러 ─────────
  // 값을 못 받은 항목은 0 이 아니라 '—' 로 보여준다.
  // 0 으로 그리면 '김프 0%' 처럼 실제 관측값으로 오해된다.
  function dashWon(v) { return (v === null || v === undefined) ? "—" : won(v) + "원"; }
  function dashPct(v) { return (v === null || v === undefined) ? "—" : pct(v); }
  function dashUsd(v) { return (v === null || v === undefined) ? "—" : "$" + Number(v).toLocaleString(); }

  async function loadArbitrageRadar() {
    try {
      const data = await api("/api/arbitrage/radar");
      if (!data) return;

      const errBox = $("arbDataError");
      if (errBox) {
        if (data.dataOk === false && (data.errors || []).length) {
          errBox.innerHTML = "<b>일부 지표를 받지 못했습니다.</b> 받지 못한 값은 —  로 표시합니다.<br>"
            + data.errors.map(e => `· ${e}`).join("<br>");
          errBox.classList.remove("hidden");
        } else {
          errBox.classList.add("hidden");
        }
      }

      $("arbOfficialFx").textContent = dashWon(data.officialFxRate);
      // 이 환율이 언제 값인지 밝힌다. 주말·공휴일에는 직전 영업일 값이 그대로
      // 남아, 24시간 도는 USDT 와 비교한 프리미엄이 착시가 된다.
      const fxEl = $("arbFxAsOf");
      if (fxEl) {
        if (data.officialFxStale) {
          const age = data.officialFxAgeDays;
          fxEl.innerHTML = `<span class="down">⏸ 기준 ${escapeHtml(data.officialFxAsOf || "?")}`
            + (age ? ` (${age}일 전)` : "") + ` · 외환시장 휴장</span>`;
        } else {
          fxEl.textContent = data.officialFxAsOf ? `기준 ${data.officialFxAsOf}` : "";
        }
      }
      // 코인별 김치프리미엄도 같은 공시환율로 나눈 값이라 함께 영향을 받는다.
      // 무전송 스프레드(빗썸↔바이낸스 가격 비)는 환율을 쓰지 않아 영향이 없다.
      const kimEl = $("arbKimchiNote");
      if (kimEl) {
        kimEl.textContent = data.officialFxStale
          ? "— 김치프리미엄 열은 멈춘 환율 기준입니다 (무전송 스프레드는 환율과 무관)" : "";
      }
      const premEl = $("arbUsdtPremNote");
      if (premEl) {
        premEl.textContent = data.officialFxStale
          ? "환율이 멈춰 있어 실제 괴리가 아닙니다" : "";
        premEl.className = data.officialFxStale ? "muted down" : "muted";
        premEl.style.fontSize = "0.68rem";
      }
      $("arbUsdtPrice").textContent = dashWon(data.bithumbUsdtPrice);
      $("arbUsdtPrem").textContent = dashPct(data.usdtPremiumPct);
      $("arbUsdtPrem").className = "metric-v " + (data.usdtPremiumPct === null ? "" : cls(data.usdtPremiumPct));
      $("arbRecommendation").textContent = data.usdtStatus;
      $("arbRecommendation").className = "metric-v";

      const tbody = $("arbRadarBody");
      if (data.coins && data.coins.length > 0) {
        tbody.innerHTML = data.coins.map(c => `
          <tr>
            <td><b>${c.coin}</b> <span class="muted small">${c.name}</span></td>
            <td>${dashWon(c.bithumbPrice)}</td>
            <td>${dashUsd(c.binanceUsdPrice)}</td>
            <td class="${c.kimchiPremiumPct === null ? "" : cls(c.kimchiPremiumPct)}"><b>${dashPct(c.kimchiPremiumPct)}</b></td>
            <td class="${c.spatialSpreadPct === null ? "" : cls(c.spatialSpreadPct)}">${dashPct(c.spatialSpreadPct)}</td>
          </tr>
        `).join("");
      }
    } catch (e) {
      console.warn("지표 갱신 실패:", e);
    }
  }

  async function loadArbitrageBots() {
    try {
      const res = await api("/api/arbitrage/bots");
      const list = $("arbBotList");
      if (!res.bots || res.bots.length === 0) {
        list.innerHTML = '<div class="empty">가동 중인 시뮬레이터가 없습니다.</div>';
        $("arbActiveBotCount").textContent = "0대 가동 중";
        return;
      }
      $("arbActiveBotCount").textContent = `${res.bots.filter(b => b.isRunning).length}대 가동 중`;
      list.innerHTML = res.bots.map(b => {
        const stratNames = {
          usdt_swap: "USDT 환차익 스왑",
          spatial_dual: "무전송 양방향"
        };
        return `
          <div class="bot-card ${b.isRunning ? '' : 'paused'}" style="margin-bottom: 10px;">
            <div class="bot-card-head">
              <div>
                <span class="badge badge-paper" title="실제 주문은 나가지 않습니다">시뮬레이션</span>
                <b style="font-size: 0.95rem; margin-left: 6px;">${stratNames[b.strategy] || b.strategy}</b>
                <span class="muted small">(${b.coin})</span>
              </div>
              <div>
                ${b.isRunning
                    ? `<button class="btn btn-danger btn-xs" onclick="window.stopArbBot('${b.botId}')">정지</button>`
                    : `<span class="muted small" style="margin-right:6px;">정지됨</span><button class="btn btn-ghost btn-xs" onclick="window.deleteArbBot('${b.botId}')">삭제</button>`}
              </div>
            </div>
            <div class="bot-body" style="font-size: 0.85rem; margin-top: 8px;">
              <div style="display: flex; justify-content: space-between; margin-bottom: 4px;">
                <span class="muted">가상 자본:</span> <b>${won(b.initialKrw)}원</b>
                <span class="muted">평가액:</span>
                <b class="${(b.totalReturnPct === null || b.totalReturnPct === undefined) ? '' : cls(b.totalReturnPct)}">${
                  (b.equityKrw === null || b.equityKrw === undefined)
                    ? '— (시세 대기)'
                    : `${won(b.equityKrw)}원 (${pct(b.totalReturnPct)})`}</b>
              </div>
              <div style="display: flex; justify-content: space-between; margin-bottom: 4px;">
                <span class="muted">확정 손익:</span> <b class="${cls(b.realizedPnl)}">${won(b.realizedPnl)}원 (${pct(b.returnPct)})</b>
                <span class="muted">확정 손익률:</span> <span class="${cls(b.returnPct)}">${pct(b.returnPct)}</span>
              </div>
              <div style="display: flex; justify-content: space-between; margin-bottom: 4px;">
                <span class="muted">국내(가상):</span> <span>${won(b.cashKrw)}원 / ${b.coinUnitsDomestic} ${b.coin}</span>
                <span class="muted">해외(가상):</span> <span>$${b.foreignCashUsdt} USDT / ${b.foreignUnits} ${b.coin}</span>
              </div>
              <div style="padding: 6px 8px; background: rgba(0,0,0,0.2); border-radius: 4px; margin-top: 6px; font-size: 0.8rem;">
                <b>상태:</b> ${b.lastStatus}
              </div>
            </div>
          </div>
        `;
      }).join("");
    } catch (e) {
      console.warn("차익거래 봇 목록 조회 실패:", e);
    }
  }

  window.deleteArbBot = async function(botId) {
    if (!confirm("이 시뮬레이터를 삭제하시겠습니까? 기록이 사라집니다.")) return;
    try {
      await api("/api/arbitrage/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ botId })
      });
      await loadArbitrageBots();
    } catch (e) {
      alert(e.message);
    }
  };

  window.stopArbBot = async function(botId) {
    if (!confirm("시뮬레이터를 정지합니다.\n실제 주문은 원래 나가지 않으므로 거래소에는 영향이 없습니다.\n\n계속하시겠습니까?")) return;
    try {
      await api("/api/arbitrage/stop", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ botId })
      });
      await loadArbitrageBots();
    } catch (e) {
      alert(e.message);
    }
  };

  // 전략 선택에 따른 옵션 표시 전환
  if ($("arbStrategyType")) {
    $("arbStrategyType").onchange = () => {
      const s = $("arbStrategyType").value;
      $("cfg_usdt_swap").classList.toggle("hidden", s !== "usdt_swap");
      $("cfg_spatial_dual").classList.toggle("hidden", s !== "spatial_dual");
      $("arbCoinGroup").classList.toggle("hidden", s === "usdt_swap");
    };
  }

  if ($("refreshArbitrageBtn")) $("refreshArbitrageBtn").onclick = () => { loadArbitrageRadar(); loadArbitrageBots(); };

  if ($("deployArbitrageBtn")) {
    $("deployArbitrageBtn").onclick = async () => {
      const strategy = $("arbStrategyType").value;
      const coin = $("arbCoin").value;
      const mode = "SIM";              // 실주문 경로가 없다. 서버도 LIVE 를 거부한다.
      const capitalKrw = Number($("arbCapital").value);

      const config = {
        usdtBuyThreshold: Number($("cfg_usdtBuy")?.value || -0.8),
        usdtSellThreshold: Number($("cfg_usdtSell")?.value || 2.0),
        entryKimchiPct: Number($("cfg_kimchiEntry")?.value || 1.0),
        exitKimchiPct: Number($("cfg_kimchiExit")?.value || 5.0),
        triggerSpreadPct: Number($("cfg_spatialTrigger")?.value || 0.4),
      };

      try {
        await api("/api/arbitrage/deploy", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ strategy, coin, mode, capitalKrw, config })
        });
        alert("시뮬레이션을 시작했습니다. 실제 주문은 나가지 않습니다.");
        await loadArbitrageBots();
      } catch (e) {
        alert(e.message);
      }
    };
  }

  $("tabs").querySelectorAll(".tab").forEach(tab => tab.onclick = () => {
    $("tabs").querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
    tab.classList.add("active");
    document.querySelectorAll(".panel").forEach(p => p.classList.add("hidden"));
    $(tab.dataset.panel).classList.remove("hidden");
    if (tab.dataset.panel === "panel-gemini") loadGeminiScan();
    if (tab.dataset.panel === "panel-arbitrage") { loadArbitrageRadar(); loadArbitrageBots(); }
    if (tab.dataset.panel === "panel-trades") loadTradeHistory();
    if (tab.dataset.panel === "panel-chart") loadChart();
    if (tab.dataset.panel === "panel-account") { loadAccount(); loadNamuhAccount(); loadGeminiStatus(); loadEgressIp(); }
  });

  await Promise.allSettled([loadPrices(), loadBots(), loadTradeHistory(), loadAccount(), loadNamuhAccount(), loadGeminiStatus(), loadGeminiScan(), loadArbitrageRadar(), loadArbitrageBots()]);
  timers.push(
    setInterval(loadPrices, 10000),
    setInterval(loadArbitrageRadar, 8000),
    setInterval(loadArbitrageBots, 6000),
    setInterval(loadBots, 8000),
    setInterval(loadTradeHistory, 10000),
    setInterval(loadNamuhAccount, 30000),
    setInterval(renderFreshness, 1000),
  );

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && started) { loadPrices(); loadBots(); loadTradeHistory(); }
  });
}

document.addEventListener("DOMContentLoaded", async () => {
  $("authForm").addEventListener("submit", handleLogin);
  $("authPassword").addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); handleLogin(e); } });
  if ($("togglePwBtn")) {
    $("togglePwBtn").onclick = () => {
      const pw = $("authPassword");
      if (pw.type === "password") {
        pw.type = "text";
        $("togglePwBtn").textContent = "🙈";
      } else {
        pw.type = "password";
        $("togglePwBtn").textContent = "👁️";
      }
    };
  }
  $("logoutBtn").onclick = async () => {
    try { await fetch("/api/auth/logout", { method: "POST" }); } catch (_) {}
    location.reload();
  };
  try {
    const st = await (await fetch("/api/auth/status")).json();
    if (!st.configured) return showGate("not_configured");
    if (st.authenticated) {
      $("authGate").classList.add("hidden");
      $("app").classList.remove("hidden");
      return boot();
    }
    showGate();
    if (st.lockedForSeconds > 0)
      setAlert($("authError"), `로그인 시도가 너무 많습니다. ${Math.ceil(st.lockedForSeconds / 60)}분 후 다시 시도하세요.`);
  } catch (e) {
    showGate();
    setAlert($("authError"), "서버에 연결할 수 없습니다.");
  }
});
