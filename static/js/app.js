// 빗썸 원화 자동매매 콘솔 — 프론트엔드
// 원칙: 서버가 실패를 알려주면 그대로 화면에 보여준다. 임의로 채워 넣지 않는다.

const $ = (id) => document.getElementById(id);
let COINS = [], INTERVALS = [], started = false, timers = [];

const won = (n) => (n == null || isNaN(n)) ? "-" : Math.round(n).toLocaleString("ko-KR");
const pct = (n) => (n == null || isNaN(n)) ? "-" : `${n >= 0 ? "+" : ""}${Number(n).toFixed(2)}%`;
const cls = (n) => (n > 0 ? "up" : n < 0 ? "down" : "");
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

async function handleLogin(e) {
  e.preventDefault();
  const pw = $("authPassword"), submit = $("authSubmit");
  const password = (pw.value || "").trim();   // 붙여넣기 공백 제거
  if (!password) return setAlert($("authError"), "비밀번호를 입력하세요.");
  setAlert($("authError"), null);
  submit.disabled = true; submit.textContent = "확인 중…";
  try {
    const res = await fetch("/api/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
    const body = await res.json().catch(() => null);
    if (!res.ok) return setAlert($("authError"), body?.detail || `로그인 실패 (HTTP ${res.status})`);
    pw.value = "";
    $("authGate").classList.add("hidden");
    $("app").classList.remove("hidden");
    await boot();
  } catch (err) {
    setAlert($("authError"), "로그인 요청이 실패했습니다.");
  } finally {
    submit.disabled = false; submit.textContent = "잠금 해제";
  }
}

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
const DEFAULTS = { rsiPeriod: 14, rsiBuy: 35, rsiSell: 70, slowMa: 30,
                   takeProfitPct: 4, stopLossPct: 2, trailingStopPct: 0 };

function renderParams(hostId, prefix) {
  $(hostId).innerHTML = PARAM_FIELDS.map(f => `
    <div>
      <label class="label" for="${prefix}${f.k}">${f.label}</label>
      <input class="input" type="number" id="${prefix}${f.k}"
             value="${DEFAULTS[f.k]}" step="${f.step}" min="${f.min}">
    </div>`).join("");
}
function readParams(prefix) {
  const p = {};
  PARAM_FIELDS.forEach(f => {
    const v = parseFloat($(prefix + f.k).value);
    if (!isNaN(v)) p[f.k] = v;
  });
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

async function deployBot() {
  const btn = $("deployBtn");
  setAlert($("deployError"), null);
  const mode = $("botMode").value;
  if (mode === "LIVE" && !confirm(
      "실전 모드로 가동합니다.\n\n빗썸 계좌에서 실제 원화로 주문이 나가며 손실이 발생할 수 있습니다.\n계속하시겠습니까?"))
    return;
  btn.disabled = true; btn.textContent = "가동 중…";
  try {
    await api("/api/bot/deploy", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        coin: $("botCoin").value, interval: $("botInterval").value, mode,
        capitalKrw: parseFloat($("botCapital").value), params: readParams("bp_"),
      }),
    });
    await loadBots();
  } catch (e) {
    setAlert($("deployError"), e.message);
  } finally {
    btn.disabled = false; btn.textContent = "봇 가동";
  }
}

function botCard(b) {
  const live = b.mode === "LIVE";
  const badge = !b.isRunning ? `<span class="badge badge-stop">정지됨</span>`
    : live ? `<span class="badge badge-live">실전</span>`
           : `<span class="badge badge-paper">모의</span>`;
  const logs = (b.recentLogs || []).map(l =>
    `<div class="logline"><span class="t">${l.time}</span><span class="lv-${l.level}">${l.message}</span></div>`).join("");
  return `<div class="bot">
    <div class="bot-head">
      <div><span class="bot-title">${b.coinName} (${b.coin})</span> ${badge}
        <span class="muted small">${b.interval}</span></div>
      <div class="inline">
        ${b.isRunning ? `<button class="btn btn-ghost btn-sm" data-stop="${b.botId}">정지</button>` : ""}
        <button class="btn btn-ghost btn-sm" data-del="${b.botId}">삭제</button>
      </div>
    </div>
    <div class="bot-stats">
      <div><div class="stat-k">평가자산</div><div class="stat-v">${won(b.equityKrw)}</div></div>
      <div><div class="stat-k">총 수익률</div><div class="stat-v ${cls(b.totalReturnPct)}">${pct(b.totalReturnPct)}</div></div>
      <div><div class="stat-k">현재가</div><div class="stat-v">${won(b.currentPrice)}</div></div>
      <div><div class="stat-k">보유</div><div class="stat-v">${b.units > 0 ? b.units.toFixed(6) : "-"}</div></div>
      <div><div class="stat-k">평가손익</div><div class="stat-v ${cls(b.unrealizedPnlKrw)}">${b.units > 0 ? won(b.unrealizedPnlKrw) : "-"}</div></div>
      <div><div class="stat-k">RSI</div><div class="stat-v">${b.rsi ?? "-"}</div></div>
      <div><div class="stat-k">거래</div><div class="stat-v">${b.totalTrades}회</div></div>
      <div><div class="stat-k">승률</div><div class="stat-v">${b.totalTrades ? b.winRatePct + "%" : "-"}</div></div>
    </div>
    <div class="bot-decision">판단: ${b.lastDecision || "-"}</div>
    <div class="logs">${logs}</div>
  </div>`;
}

async function loadBots() {
  try {
    const { bots, activeCount, maxActive } = await api("/api/bot/list");
    $("botCount").textContent = `(${activeCount}/${maxActive} 가동)`;
    $("botList").innerHTML = bots.length
      ? bots.map(botCard).join("")
      : `<div class="empty">가동 중인 봇이 없습니다.</div>`;
    $("botList").querySelectorAll("[data-stop]").forEach(el =>
      el.onclick = () => actOnBot("/api/bot/stop", el.dataset.stop, "이 봇을 정지하고 보유 포지션을 시장가로 청산합니다."));
    $("botList").querySelectorAll("[data-del]").forEach(el =>
      el.onclick = () => actOnBot("/api/bot/delete", el.dataset.del, "이 봇을 정지·청산하고 목록에서 삭제합니다."));
  } catch (e) { console.error("봇 목록 실패:", e); }
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
      <h2 class="card-title">${r.coin}/KRW · ${r.interval} · 캔들 ${r.candleCount}개</h2>
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
      ? `<div class="empty">API 키를 등록하면 표시됩니다.</div>`
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
  } finally { btn.disabled = false; }
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

  renderParams("botParams", "bp_");
  renderParams("btParams", "tp_");

  $("botMode").onchange = () =>
    $("liveWarning").classList.toggle("hidden", $("botMode").value !== "LIVE");
  $("deployBtn").onclick = deployBot;
  $("stopAllBtn").onclick = async () => {
    if (!confirm("가동 중인 모든 봇을 정지하고 포지션을 청산합니다.")) return;
    try { await api("/api/bot/stop_all", { method: "POST" }); await loadBots(); }
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

  $("tabs").querySelectorAll(".tab").forEach(tab => tab.onclick = () => {
    $("tabs").querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
    tab.classList.add("active");
    document.querySelectorAll(".panel").forEach(p => p.classList.add("hidden"));
    $(tab.dataset.panel).classList.remove("hidden");
    if (tab.dataset.panel === "panel-chart") loadChart();
    if (tab.dataset.panel === "panel-account") { loadAccount(); loadEgressIp(); }
  });

  await Promise.allSettled([loadPrices(), loadBots(), loadAccount()]);
  timers.push(
    setInterval(loadPrices, 10000),
    setInterval(loadBots, 8000),
    setInterval(renderFreshness, 1000),   // 경과 시간 표시는 매초 갱신
  );

  // 브라우저는 백그라운드 탭의 타이머를 조인다. 다시 보이면 즉시 따라잡는다.
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && started) { loadPrices(); loadBots(); }
  });
}

document.addEventListener("DOMContentLoaded", async () => {
  $("authForm").addEventListener("submit", handleLogin);
  $("authPassword").addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); handleLogin(e); } });
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
