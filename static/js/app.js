// Gemini Alpha Lab Frontend Engine

let currentSymbol = "NVDA";
let currentQuote = null;
let currentSentiment = null;
let currentBacktest = null;
let priceChartInstance = null;
let equityChartInstance = null;

document.addEventListener("DOMContentLoaded", () => {
    initApp();
});

async function initApp() {
    setupEventListeners();
    await loadPopularAssets();
    await loadMarketplaceBots();
    await loadSymbolData(currentSymbol);
    await loadGurus();
    await loadBrokers();
    await loadActiveBots();
}

function setupEventListeners() {
    const symbolInput = document.getElementById("symbolInput");
    const searchForm = document.getElementById("searchForm");
    const suggestionsBox = document.getElementById("searchSuggestions");

    // Real-time search autocomplete
    let searchTimeout = null;
    if (symbolInput && suggestionsBox) {
        symbolInput.addEventListener("input", (e) => {
            clearTimeout(searchTimeout);
            const query = e.target.value.trim();
            if (!query) {
                suggestionsBox.classList.add("hidden");
                return;
            }

            searchTimeout = setTimeout(async () => {
                try {
                    const res = await fetch(`/api/search?q=${encodeURIComponent(query)}`);
                    const data = await res.json();
                    if (data.results && data.results.length > 0) {
                        suggestionsBox.innerHTML = "";
                        data.results.forEach(item => {
                            const div = document.createElement("div");
                            div.style.padding = "0.6rem 1rem";
                            div.style.cursor = "pointer";
                            div.style.borderBottom = "1px solid var(--border-subtle)";
                            div.style.display = "flex";
                            div.style.justifyContent = "space-between";
                            div.style.alignItems = "center";
                            div.onmouseenter = () => div.style.backgroundColor = "var(--bg-card-hover)";
                            div.onmouseleave = () => div.style.backgroundColor = "transparent";
                            
                            div.innerHTML = `
                                <span style="font-weight: 600; color: #fff;">${item.name}</span>
                                <span style="font-family: var(--font-mono); font-size: 0.8rem; color: var(--accent-cyan);">${item.symbol}</span>
                            `;
                            div.onclick = () => {
                                symbolInput.value = item.symbol;
                                suggestionsBox.classList.add("hidden");
                                loadSymbolData(item.symbol);
                            };
                            suggestionsBox.appendChild(div);
                        });
                        suggestionsBox.classList.remove("hidden");
                    } else {
                        suggestionsBox.classList.add("hidden");
                    }
                } catch (err) {
                    console.error("Search error:", err);
                }
            }, 120);
        });

        document.addEventListener("click", (e) => {
            if (!searchForm.contains(e.target) && !suggestionsBox.contains(e.target)) {
                suggestionsBox.classList.add("hidden");
            }
        });
    }

    // Search form submit
    if (searchForm) {
        searchForm.addEventListener("submit", (e) => {
            e.preventDefault();
            if (suggestionsBox) suggestionsBox.classList.add("hidden");
            const val = document.getElementById("symbolInput").value.trim();
            if (val) loadSymbolData(val);
        });
    }

    // Tabs
    const tabButtons = document.querySelectorAll(".tab-btn");
    tabButtons.forEach(btn => {
        btn.addEventListener("click", () => {
            tabButtons.forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            
            const target = btn.getAttribute("data-tab");
            document.querySelectorAll(".tab-content").forEach(section => {
                section.classList.add("hidden");
            });
            const targetEl = document.getElementById(target);
            if (targetEl) targetEl.classList.remove("hidden");

            if (target === "tab-trader") {
                loadActiveBots();
                loadBrokers();
            }
        });
    });

    // Timeframe buttons
    document.querySelectorAll(".tf-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            document.querySelectorAll(".tf-btn").forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            const tf = btn.getAttribute("data-tf");
            loadChart(currentSymbol, tf);
        });
    });

    // Backtest Run Button
    const runBtBtn = document.getElementById("runBacktestBtn");
    if (runBtBtn) {
        runBtBtn.addEventListener("click", handleRunBacktest);
    }

    // AI Strategy Parse Button
    const parseBtn = document.getElementById("parsePromptBtn");
    if (parseBtn) {
        parseBtn.addEventListener("click", handleParseStrategyPrompt);
    }

    // Generate Report Button
    const genReportBtn = document.getElementById("generateReportBtn");
    if (genReportBtn) {
        genReportBtn.addEventListener("click", handleGenerateReport);
    }

    // Deploy Bot From Quant Button
    const deployQuantBtn = document.getElementById("deployBotFromQuantBtn");
    if (deployQuantBtn) {
        deployQuantBtn.addEventListener("click", () => {
            const params = getQuantParams();
            deployTradingBot(currentSymbol, params);
        });
    }

    // Start New Bot Button
    const startBotBtn = document.getElementById("startNewBotBtn");
    if (startBotBtn) {
        startBotBtn.addEventListener("click", () => {
            const symbol = document.getElementById("botSymbolInput").value.trim().toUpperCase() || currentSymbol;
            const mode = document.getElementById("botModeSelect").value;
            const broker = document.getElementById("botBrokerSelect") ? document.getElementById("botBrokerSelect").value : "BITHUMB";
            const capital = parseFloat(document.getElementById("botCapitalInput").value) || 1000000;
            const params = getQuantParams();
            deployTradingBot(symbol, params, mode, capital, broker);
        });
    }

    // Refresh Bots Button
    const refreshBotsBtn = document.getElementById("refreshBotsBtn");
    if (refreshBotsBtn) {
        refreshBotsBtn.addEventListener("click", loadActiveBots);
    }

    // Stop All Bots Emergency Button
    const stopAllBtn = document.getElementById("stopAllBotsBtn");
    if (stopAllBtn) {
        stopAllBtn.addEventListener("click", handleStopAllBots);
    }
}

async function loadPopularAssets() {
    try {
        const res = await fetch("/api/popular");
        const data = await res.json();
        const marquee = document.getElementById("tickerMarquee");
        const chipsContainer = document.getElementById("quickChips");
        
        if (data.assets && marquee) {
            marquee.innerHTML = "";
            data.assets.forEach(asset => {
                const item = document.createElement("div");
                item.className = "ticker-item";
                item.innerHTML = `<span class="sym">${asset.name || asset.symbol}</span> <span class="price" id="marq-${asset.symbol}">--</span>`;
                item.onclick = () => loadSymbolData(asset.symbol);
                marquee.appendChild(item);
            });
        }

        if (data.assets && chipsContainer) {
            chipsContainer.innerHTML = "";
            data.assets.slice(0, 7).forEach(asset => {
                const chip = document.createElement("button");
                chip.className = `btn-chip ${asset.symbol === currentSymbol ? 'active' : ''}`;
                chip.textContent = asset.name ? asset.name.split(' ')[0] : asset.symbol;
                chip.onclick = () => {
                    document.querySelectorAll(".btn-chip").forEach(c => c.classList.remove("active"));
                    chip.classList.add("active");
                    loadSymbolData(asset.symbol);
                };
                chipsContainer.appendChild(chip);
            });
        }
    } catch (e) {
        console.error("Failed to load popular assets:", e);
    }
}

// ⚡ Ultra-Fast 1-Shot Bundle Loader
async function loadSymbolData(symbolQuery) {
    try {
        // Instant loading indicator
        document.getElementById("activeSymbolTitle").textContent = symbolQuery;
        document.getElementById("activeSymbolName").textContent = "초고속 데이터 분석 중...";

        const res = await fetch(`/api/symbol/bundle?symbol=${encodeURIComponent(symbolQuery)}`);
        const bundle = await res.json();

        currentSymbol = bundle.symbol;
        currentQuote = bundle.quote;
        currentSentiment = bundle.sentiment;
        currentBacktest = bundle.backtest;

        document.getElementById("symbolInput").value = bundle.symbol;
        document.getElementById("activeSymbolTitle").textContent = bundle.symbol;
        document.getElementById("activeSymbolName").textContent = bundle.quote.shortName;

        // 1. Render Quote
        renderQuoteUI(bundle.quote);
        // 2. Render Chart
        renderPriceChart(bundle.chart.candles);
        renderTechSignals(bundle.chart.techSignals);
        // 3. Render Sentiment
        renderSentimentUI(bundle.sentiment);
        // 4. Render Financials
        renderFinancialsUI(bundle.financials);
        // 5. Render Backtest
        renderBacktestUI(bundle.backtest);

    } catch (e) {
        console.error("Bundle load error:", e);
    }
}

function renderQuoteUI(q) {
    if (!q) return;
    const priceEl = document.getElementById("headerPrice");
    const changeEl = document.getElementById("headerChange");
    const nameEl = document.getElementById("activeSymbolName");
    
    if (nameEl) nameEl.textContent = q.shortName;
    if (priceEl) priceEl.textContent = `${q.currentPrice.toLocaleString()} ${q.currency}`;
    
    if (changeEl) {
        const isUp = q.changePercent >= 0;
        changeEl.textContent = `${isUp ? '+' : ''}${q.changePercent}% (${isUp ? '+' : ''}${q.change})`;
        changeEl.className = isUp ? "stat-sub text-emerald" : "stat-sub text-rose";
    }

    const marqEl = document.getElementById(`marq-${q.symbol}`);
    if (marqEl) {
        const isUp = q.changePercent >= 0;
        marqEl.innerHTML = `${q.currentPrice} <span class="pct ${isUp ? 'up' : 'down'}">(${isUp ? '+' : ''}${q.changePercent}%)</span>`;
    }

    document.getElementById("statPER").textContent = q.trailingPE ? `${q.trailingPE}x` : 'N/A';
    document.getElementById("statPBR").textContent = q.priceToBook ? `${q.priceToBook}x` : 'N/A';
    document.getElementById("stat52H").textContent = `${q.fiftyTwoWeekHigh.toLocaleString()} ${q.currency}`;
    document.getElementById("stat52L").textContent = `${q.fiftyTwoWeekLow.toLocaleString()} ${q.currency}`;
    document.getElementById("statTarget").textContent = `${q.targetHighPrice.toLocaleString()} ${q.currency}`;
}

function renderSentimentUI(s) {
    if (!s) return;
    document.getElementById("sentimentScore").textContent = `${s.sentimentScore}/100`;
    document.getElementById("sentimentLabel").textContent = s.sentimentLabel;
    
    const pointer = document.getElementById("sentimentPointer");
    if (pointer) pointer.style.left = `${Math.min(96, Math.max(4, s.sentimentScore))}%`;

    document.getElementById("aiSummaryText").textContent = s.aiSummary;
    document.getElementById("institutionalFlowText").textContent = s.institutionalFlow;

    const bullList = document.getElementById("bullishList");
    const bearList = document.getElementById("bearishList");
    if (bullList) {
        bullList.innerHTML = "";
        s.bullishFactors.forEach(f => {
            const li = document.createElement("li");
            li.textContent = f;
            bullList.appendChild(li);
        });
    }

    if (bearList) {
        bearList.innerHTML = "";
        s.bearishFactors.forEach(f => {
            const li = document.createElement("li");
            li.textContent = f;
            bearList.appendChild(li);
        });
    }
}

function renderFinancialsUI(fin) {
    if (!fin) return;
    document.getElementById("alphaScoreBadge").textContent = `${fin.alphaScore}점 (${fin.grade})`;
    document.getElementById("valuationVerdict").textContent = fin.valuationVerdict;
    
    const finInsights = document.getElementById("financialInsights");
    if (finInsights) {
        finInsights.innerHTML = "";
        fin.coreInsights.forEach(item => {
            const li = document.createElement("li");
            li.textContent = item;
            finInsights.appendChild(li);
        });
    }

    const riskList = document.getElementById("financialRisks");
    if (riskList) {
        riskList.innerHTML = "";
        fin.riskWatchlist.forEach(item => {
            const li = document.createElement("li");
            li.textContent = item;
            riskList.appendChild(li);
        });
    }
}

function renderBacktestUI(result) {
    if (!result) return;
    const isAlphaPositive = result.alphaPct >= 0;
    document.getElementById("btTotalReturn").textContent = `${result.totalReturnPct >= 0 ? '+' : ''}${result.totalReturnPct}%`;
    document.getElementById("btAlpha").textContent = `알파: ${isAlphaPositive ? '+' : ''}${result.alphaPct}%p (vs 벤치마크)`;
    document.getElementById("btWinRate").textContent = `${result.winRatePct}%`;
    document.getElementById("btMDD").textContent = `-${result.maxDrawdownPct}%`;
    document.getElementById("btSharpe").textContent = result.sharpeRatio;
    document.getElementById("btProfitFactor").textContent = result.profitFactor;

    renderEquityChart(result.equityCurve, result.benchmarkCurve);
    renderTradesTable(result.trades);
}

async function loadChart(symbol, timeframe) {
    try {
        const res = await fetch(`/api/chart?symbol=${encodeURIComponent(symbol)}&timeframe=${timeframe}`);
        const data = await res.json();
        
        renderPriceChart(data.candles);
        renderTechSignals(data.techSignals);
    } catch (e) {
        console.error("Chart fetch error:", e);
    }
}

function renderPriceChart(candles) {
    const ctx = document.getElementById("priceChart").getContext("2d");
    if (priceChartInstance) priceChartInstance.destroy();

    const labels = candles.map(c => c.time);
    const closePrices = candles.map(c => c.close);
    const sma20 = candles.map(c => c.sma20);
    const sma60 = candles.map(c => c.sma60);
    const bbUpper = candles.map(c => c.bbUpper);
    const bbLower = candles.map(c => c.bbLower);

    priceChartInstance = new Chart(ctx, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [
                {
                    label: '종가 (Close)',
                    data: closePrices,
                    borderColor: '#3B82F6',
                    backgroundColor: 'rgba(59, 130, 246, 0.05)',
                    borderWidth: 2,
                    fill: true,
                    tension: 0.1,
                    pointRadius: 0
                },
                {
                    label: '20일 이평선 (SMA 20)',
                    data: sma20,
                    borderColor: '#F59E0B',
                    borderWidth: 1.5,
                    borderDash: [4, 4],
                    pointRadius: 0,
                    fill: false
                },
                {
                    label: '60일 이평선 (SMA 60)',
                    data: sma60,
                    borderColor: '#10B981',
                    borderWidth: 1.5,
                    borderDash: [6, 6],
                    pointRadius: 0,
                    fill: false
                },
                {
                    label: '볼린저 상단 (Upper)',
                    data: bbUpper,
                    borderColor: 'rgba(239, 68, 68, 0.4)',
                    borderWidth: 1,
                    pointRadius: 0,
                    fill: false
                },
                {
                    label: '볼린저 하단 (Lower)',
                    data: bbLower,
                    borderColor: 'rgba(16, 185, 129, 0.4)',
                    borderWidth: 1,
                    pointRadius: 0,
                    fill: false
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: {
                mode: 'index',
                intersect: false
            },
            plugins: {
                legend: {
                    labels: { color: '#9CA3AF', font: { size: 11 } }
                },
                tooltip: {
                    backgroundColor: '#1E293B',
                    titleColor: '#F3F4F6',
                    bodyColor: '#9CA3AF',
                    borderColor: '#334155',
                    borderWidth: 1
                }
            },
            scales: {
                x: {
                    grid: { color: 'rgba(255,255,255,0.04)' },
                    ticks: { color: '#6B7280', maxTicksLimit: 8 }
                },
                y: {
                    grid: { color: 'rgba(255,255,255,0.04)' },
                    ticks: { color: '#6B7280' }
                }
            }
        }
    });
}

function renderTechSignals(signals) {
    const listEl = document.getElementById("techSignalList");
    if (!listEl) return;
    listEl.innerHTML = "";

    if (!signals || signals.length === 0) {
        listEl.innerHTML = `<div class="text-muted" style="font-size:0.85rem;">감지된 기술적 특이 시그널이 없습니다.</div>`;
        return;
    }

    signals.forEach(s => {
        const item = document.createElement("div");
        item.style.display = "flex";
        item.style.justifyContent = "space-between";
        item.style.alignItems = "center";
        item.style.padding = "0.5rem 0";
        item.style.borderBottom = "1px solid var(--border-subtle)";
        
        const isBuy = s.type === "BUY";
        item.innerHTML = `
            <span style="font-size:0.85rem; color: var(--text-primary);">${s.desc}</span>
            <span class="${isBuy ? 'badge-win' : 'badge-loss'}" style="font-size:0.75rem;">${s.type} (+${s.weight}pt)</span>
        `;
        listEl.appendChild(item);
    });
}

async function loadSentiment(symbol) {
    try {
        const res = await fetch(`/api/sentiment?symbol=${encodeURIComponent(symbol)}`);
        const data = await res.json();
        currentSentiment = data.sentiment;

        const s = data.sentiment;
        document.getElementById("sentimentScore").textContent = `${s.sentimentScore}/100`;
        document.getElementById("sentimentLabel").textContent = s.sentimentLabel;
        
        // Move pointer
        const pointer = document.getElementById("sentimentPointer");
        if (pointer) pointer.style.left = `${Math.min(96, Math.max(4, s.sentimentScore))}%`;

        document.getElementById("aiSummaryText").textContent = s.aiSummary;
        document.getElementById("institutionalFlowText").textContent = s.institutionalFlow;

        // Bullish & Bearish Lists
        const bullList = document.getElementById("bullishList");
        const bearList = document.getElementById("bearishList");
        bullList.innerHTML = "";
        bearList.innerHTML = "";

        s.bullishFactors.forEach(f => {
            const li = document.createElement("li");
            li.textContent = f;
            bullList.appendChild(li);
        });

        s.bearishFactors.forEach(f => {
            const li = document.createElement("li");
            li.textContent = f;
            bearList.appendChild(li);
        });
    } catch (e) {
        console.error("Sentiment error:", e);
    }
}

async function loadFinancials(symbol) {
    try {
        const res = await fetch(`/api/financials?symbol=${encodeURIComponent(symbol)}`);
        const fin = await res.json();
        
        document.getElementById("alphaScoreBadge").textContent = `${fin.alphaScore}점 (${fin.grade})`;
        document.getElementById("valuationVerdict").textContent = fin.valuationVerdict;
        
        const finInsights = document.getElementById("financialInsights");
        finInsights.innerHTML = "";
        fin.coreInsights.forEach(item => {
            const li = document.createElement("li");
            li.textContent = item;
            finInsights.appendChild(li);
        });

        const riskList = document.getElementById("financialRisks");
        riskList.innerHTML = "";
        fin.riskWatchlist.forEach(item => {
            const li = document.createElement("li");
            li.textContent = item;
            riskList.appendChild(li);
        });
    } catch (e) {
        console.error("Financials error:", e);
    }
}

async function runDefaultBacktest(symbol) {
    await executeBacktest({
        symbol: symbol,
        strategyType: "custom",
        fastMa: 5,
        slowMa: 20,
        rsiBuy: 35.0,
        rsiSell: 70.0,
        takeProfitPct: 12.0,
        stopLossPct: 5.0,
        period: "1y"
    });
}

async function handleRunBacktest() {
    const symbol = currentSymbol;
    const fastMa = parseInt(document.getElementById("btFastMa").value) || 5;
    const slowMa = parseInt(document.getElementById("btSlowMa").value) || 20;
    const rsiBuy = parseFloat(document.getElementById("btRsiBuy").value) || 35;
    const rsiSell = parseFloat(document.getElementById("btRsiSell").value) || 70;
    const takeProfit = parseFloat(document.getElementById("btTakeProfit").value) || 10;
    const stopLoss = parseFloat(document.getElementById("btStopLoss").value) || 5;

    await executeBacktest({
        symbol: symbol,
        strategyType: "custom",
        fastMa: fastMa,
        slowMa: slowMa,
        rsiBuy: rsiBuy,
        rsiSell: rsiSell,
        takeProfitPct: takeProfit,
        stopLossPct: stopLoss,
        period: "1y"
    });
}

async function executeBacktest(payload) {
    const btn = document.getElementById("runBacktestBtn");
    if (btn) btn.innerHTML = `<span class="spinner"></span> 퀀트 시뮬레이션 연산 중...`;

    try {
        const res = await fetch("/api/backtest", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        const result = await res.json();
        currentBacktest = result;

        // Render metrics
        const isAlphaPositive = result.alphaPct >= 0;
        document.getElementById("btTotalReturn").textContent = `${result.totalReturnPct >= 0 ? '+' : ''}${result.totalReturnPct}%`;
        document.getElementById("btAlpha").textContent = `알파: ${isAlphaPositive ? '+' : ''}${result.alphaPct}%p (vs 벤치마크)`;
        document.getElementById("btWinRate").textContent = `${result.winRatePct}%`;
        document.getElementById("btMDD").textContent = `-${result.maxDrawdownPct}%`;
        document.getElementById("btSharpe").textContent = result.sharpeRatio;
        document.getElementById("btProfitFactor").textContent = result.profitFactor;

        // Render Equity Chart
        renderEquityChart(result.equityCurve, result.benchmarkCurve);
        // Render Trades Table
        renderTradesTable(result.trades);

    } catch (e) {
        console.error("Backtest error:", e);
    } finally {
        if (btn) btn.innerHTML = `⚡ 퀀트 백테스팅 실행 (시뮬레이션)`;
    }
}

function renderEquityChart(equityCurve, benchmarkCurve) {
    const ctx = document.getElementById("equityChart").getContext("2d");
    if (equityChartInstance) equityChartInstance.destroy();

    const labels = equityCurve.map(e => e.date);
    const strategyVals = equityCurve.map(e => e.portfolioValue);
    const benchmarkVals = benchmarkCurve.map(b => b.benchmarkValue);

    equityChartInstance = new Chart(ctx, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [
                {
                    label: 'Gemini 퀀트 알파 전략 ($)',
                    data: strategyVals,
                    borderColor: '#10B981',
                    backgroundColor: 'rgba(16, 185, 129, 0.1)',
                    borderWidth: 2.5,
                    fill: true,
                    tension: 0.1,
                    pointRadius: 0
                },
                {
                    label: '단순 매수 보유 (Benchmark $)',
                    data: benchmarkVals,
                    borderColor: '#6B7280',
                    borderWidth: 1.5,
                    borderDash: [5, 5],
                    fill: false,
                    pointRadius: 0
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
                legend: { labels: { color: '#9CA3AF' } }
            },
            scales: {
                x: {
                    grid: { color: 'rgba(255,255,255,0.04)' },
                    ticks: { color: '#6B7280', maxTicksLimit: 8 }
                },
                y: {
                    grid: { color: 'rgba(255,255,255,0.04)' },
                    ticks: { color: '#6B7280' }
                }
            }
        }
    });
}

function renderTradesTable(trades) {
    const tbody = document.getElementById("tradesTableBody");
    tbody.innerHTML = "";

    if (!trades || trades.length === 0) {
        tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; color: var(--text-muted);">해당 기간 내 체결된 매매 내역이 없습니다.</td></tr>`;
        return;
    }

    trades.slice(-10).reverse().forEach((t, idx) => {
        const tr = document.createElement("tr");
        const isWin = t.status === "WIN";
        tr.innerHTML = `
            <td>${t.entryDate} → ${t.exitDate}</td>
            <td>$${t.entryPrice.toLocaleString()}</td>
            <td>$${t.exitPrice.toLocaleString()}</td>
            <td class="${isWin ? 'text-emerald' : 'text-rose'}">${isWin ? '+' : ''}${t.returnPct}%</td>
            <td class="${isWin ? 'text-emerald' : 'text-rose'}">${isWin ? '+' : ''}$${t.profit.toLocaleString()}</td>
            <td><span class="${isWin ? 'badge-win' : 'badge-loss'}">${t.reason}</span></td>
        `;
        tbody.appendChild(tr);
    });
}

async function handleParseStrategyPrompt() {
    const promptInput = document.getElementById("strategyPromptInput");
    const userPrompt = promptInput.value.trim();
    if (!userPrompt) return;

    const parseBtn = document.getElementById("parsePromptBtn");
    parseBtn.innerHTML = `<span class="spinner"></span> Gemini 전략 분석 중...`;

    try {
        const res = await fetch("/api/strategy/parse", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ userPrompt: userPrompt })
        });
        const parsed = await res.json();

        // Populate form inputs
        document.getElementById("btFastMa").value = parsed.fastMa || 5;
        document.getElementById("btSlowMa").value = parsed.slowMa || 20;
        document.getElementById("btRsiBuy").value = parsed.rsiBuy || 35;
        document.getElementById("btRsiSell").value = parsed.rsiSell || 70;
        document.getElementById("btTakeProfit").value = parsed.takeProfitPct || 10;
        document.getElementById("btStopLoss").value = parsed.stopLossPct || 5;

        // Auto run backtest with new parameters
        await handleRunBacktest();
    } catch (e) {
        console.error("Strategy parse error:", e);
    } finally {
        parseBtn.innerHTML = `✨ AI 전략 파싱 & 자동 세팅`;
    }
}

function applyPreset(promptText) {
    document.getElementById("strategyPromptInput").value = promptText;
    handleParseStrategyPrompt();
}

async function handleGenerateReport() {
    if (!currentQuote || !currentBacktest || !currentSentiment) {
        alert("먼저 종목 시세 및 백테스트 결과를 불러와주세요.");
        return;
    }

    const genBtn = document.getElementById("generateReportBtn");
    genBtn.innerHTML = `<span class="spinner"></span> Gemini 고품질 리서치 리포트 작성 중...`;

    try {
        const res = await fetch("/api/report/generate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                symbol: currentSymbol,
                quote: currentQuote,
                backtest: currentBacktest,
                sentiment: currentSentiment
            })
        });
        const report = await res.json();

        // Render Markdown
        const reportContainer = document.getElementById("reportContent");
        reportContainer.innerHTML = marked.parse(report.markdown);

        // Save raw md for copy
        const copyBtn = document.getElementById("copyReportBtn");
        if (copyBtn) {
            copyBtn.setAttribute("data-raw-md", report.markdown);
            copyBtn.classList.remove("hidden");
        }

        const metaEl = document.getElementById("reportMetaInfo");
        if (metaEl) metaEl.classList.remove("hidden");
        const badgeEl = document.getElementById("commercialValueBadge");
        if (badgeEl) badgeEl.textContent = report.commercialValue;

    } catch (e) {
        console.error("Report generate error:", e);
    } finally {
        if (genBtn) genBtn.innerHTML = `💰 프리미엄 유료 분석 리포트 즉시 발행`;
    }
}

function getQuantParams() {
    const chkVol = document.getElementById("chkVolumeSurge");
    const chkSent = document.getElementById("chkAiSentiment");
    const chkTrail = document.getElementById("chkTrailingStop");
    const chkRegime = document.getElementById("chkMarketRegime");
    const chkScale = document.getElementById("chkScaleInOut");

    return {
        fastMa: parseInt(document.getElementById("btFastMa")?.value) || 5,
        slowMa: parseInt(document.getElementById("btSlowMa")?.value) || 20,
        rsiBuy: parseFloat(document.getElementById("btRsiBuy")?.value) || 35,
        rsiSell: parseFloat(document.getElementById("btRsiSell")?.value) || 70,
        takeProfitPct: parseFloat(document.getElementById("btTakeProfit")?.value) || 12,
        stopLossPct: parseFloat(document.getElementById("btStopLoss")?.value) || 5,
        // 5대 스마트 필터
        enableVolumeSurge: chkVol ? chkVol.checked : true,
        volumeSurgeThreshold: 150.0,
        enableAiSentimentGate: chkSent ? chkSent.checked : true,
        minSentimentScore: 60,
        enableTrailingStop: chkTrail ? chkTrail.checked : true,
        trailingStopPct: 3.5,
        enableMarketRegime: chkRegime ? chkRegime.checked : true,
        enableScaleInOut: chkScale ? chkScale.checked : true
    };
}

async function deployTradingBot(symbol, params, mode = "PAPER", capital = 10000, broker = null) {
    try {
        const selectedBroker = broker || (document.getElementById("botBrokerSelect") ? document.getElementById("botBrokerSelect").value : "BITHUMB");
        const payload = {
            symbol: symbol,
            mode: mode,
            broker: selectedBroker,
            capital: Number(capital) || 1000000,
            strategyParams: params
        };

        const res = await fetch("/api/bot/deploy", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        
        const botData = await res.json();
        if (!res.ok) {
            throw new Error(botData.detail || "서버에서 봇 배포를 거부했습니다.");
        }

        const capDisplay = (botData.initialCapital != null ? Number(botData.initialCapital) : Number(capital)).toLocaleString();
        alert(`🚀 [${botData.mode || mode}] ${botData.symbol || symbol} 자동매매 봇이 [${selectedBroker}] 실전 거래소와 연동되어 가동되었습니다!\n• 봇 ID: ${botData.botId}\n• 운용자본: ${capDisplay}`);

        // Switch to Bot Hub Tab
        document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
        document.querySelectorAll(".tab-content").forEach(s => s.classList.add("hidden"));
        
        const botTabBtn = document.querySelector(`[data-tab="tab-trader"]`);
        if (botTabBtn) botTabBtn.classList.add("active");
        const botTab = document.getElementById("tab-trader");
        if (botTab) botTab.classList.remove("hidden");

        await loadActiveBots();
    } catch (e) {
        console.error("Deploy bot error:", e);
        alert("봇 가동 중 오류가 발생했습니다: " + e.message);
    }
}

async function loadActiveBots() {
    try {
        const res = await fetch("/api/bot/list");
        const data = await res.json();
        renderActiveBots(data.bots || []);
    } catch (e) {
        console.error("Load bots error:", e);
    }
}

function renderActiveBots(bots) {
    const container = document.getElementById("activeBotsList");
    if (!container) return;

    if (bots.length === 0) {
        container.innerHTML = `
            <div style="text-align:center; padding: 2.5rem 0; color: var(--text-muted); font-size: 0.9rem;">
                현재 가동 중인 봇이 없습니다. 상단에서 <strong>[🚀 5대 멀티팩터 자동매매 봇 가동 시작]</strong> 또는 마켓플레이스에서 <strong>[⚡ 1-Click 복제 & 가동]</strong>을 눌러주세요.
            </div>
        `;
        return;
    }

    container.innerHTML = "";
    bots.forEach(bot => {
        const isRunning = bot.isRunning;
        const isProfit = (bot.unrealizedPnl || 0) >= 0;
        const p = bot.strategyParams || {};
        
        // 5대 스마트 필터 뱃지
        const badges = [];
        if (p.enableVolumeSurge) badges.push(`<span class="btn-chip" style="font-size:0.7rem; padding:0.15rem 0.5rem; background:rgba(6,182,212,0.15); color:var(--accent-cyan); border-color:var(--accent-cyan);">⚡ 거래량폭증 150%</span>`);
        if (p.enableAiSentimentGate) badges.push(`<span class="btn-chip" style="font-size:0.7rem; padding:0.15rem 0.5rem; background:rgba(59,130,246,0.15); color:var(--accent-blue); border-color:var(--accent-blue);">🤖 AI감성 ${bot.sentimentScore || 70}점</span>`);
        if (p.enableTrailingStop) badges.push(`<span class="btn-chip" style="font-size:0.7rem; padding:0.15rem 0.5rem; background:rgba(16,185,129,0.15); color:var(--accent-emerald); border-color:var(--accent-emerald);">🛡️ ATR 트레일링 스탑</span>`);
        if (p.enableScaleInOut) badges.push(`<span class="btn-chip" style="font-size:0.7rem; padding:0.15rem 0.5rem; background:rgba(245,158,11,0.15); color:var(--accent-amber); border-color:var(--accent-amber);">💰 50% 분할 익절</span>`);

        const totalAssetStr = (bot.currentTotalAsset != null ? Number(bot.currentTotalAsset) : 0).toLocaleString();
        const unrealizedPnlStr = (bot.unrealizedPnl != null ? Number(bot.unrealizedPnl) : 0).toLocaleString();

        const card = document.createElement("div");
        card.style.backgroundColor = "var(--bg-card-subtle)";
        card.style.border = `1px solid ${isRunning ? 'var(--accent-emerald)' : 'var(--border-subtle)'}`;
        card.style.borderRadius = "var(--radius-md)";
        card.style.padding = "1.25rem";

        card.innerHTML = `
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.75rem; flex-wrap: wrap; gap: 0.5rem;">
                <div style="display: flex; align-items: center; gap: 0.75rem;">
                    <span style="font-size: 1.25rem; font-weight: 800;">${bot.symbol}</span>
                    <span class="${bot.mode === 'LIVE' ? 'badge-loss' : 'badge-win'}" style="font-size: 0.75rem;">
                        ${bot.mode === 'LIVE' ? `🔥 실전 [${bot.broker || 'Live'}]` : '🛡️ 모의투자 Paper'}
                    </span>
                    <span style="font-size: 0.75rem; color: var(--text-muted); font-family: var(--font-mono);">${bot.botId}</span>
                </div>
                <div style="display: flex; align-items: center; gap: 0.5rem;">
                    <span style="font-size: 0.85rem; color: ${isRunning ? 'var(--accent-emerald)' : 'var(--text-muted)'}; font-weight: 600;">
                        ● ${isRunning ? '5대 멀티팩터 실시간 감시 중' : '정지됨 (전량 청산)'}
                    </span>
                    ${isRunning 
                        ? `<button class="btn-chip" style="color: var(--accent-rose); border-color: var(--accent-rose); font-size:0.75rem; font-weight:700;" onclick="stopTradingBot('${bot.botId}')">🛑 즉시 청산 & 중지</button>` 
                        : `<button class="btn-chip" style="color: var(--text-muted); font-size:0.75rem;" onclick="deleteTradingBot('${bot.botId}')">🗑️ 삭제</button>`
                    }
                </div>
            </div>

            <!-- Active Multi-Factor Badges -->
            <div style="display: flex; gap: 0.4rem; margin-bottom: 1rem; flex-wrap: wrap;">
                ${badges.join('')}
            </div>

            <div class="grid-4" style="margin-bottom: 1rem;">
                <div class="stat-box">
                    <div class="stat-label">총 자산 가치</div>
                    <div class="stat-value">$${totalAssetStr}</div>
                    <div class="stat-sub ${(bot.totalRoiPct || 0) >= 0 ? 'text-emerald' : 'text-rose'}">
                        수익률: ${(bot.totalRoiPct || 0) >= 0 ? '+' : ''}${bot.totalRoiPct || 0}%
                    </div>
                </div>
                <div class="stat-box">
                    <div class="stat-label">보유 포지션</div>
                    <div class="stat-value">${bot.position || 0}주/개</div>
                    <div class="stat-sub text-muted">진입가: $${bot.entryPrice || 0} (현재가: $${bot.currentPrice || 0})</div>
                </div>
                <div class="stat-box">
                    <div class="stat-label">미실현 평가손익</div>
                    <div class="stat-value ${isProfit ? 'text-emerald' : 'text-rose'}">
                        ${isProfit ? '+' : ''}$${unrealizedPnlStr}
                    </div>
                    <div class="stat-sub ${isProfit ? 'text-emerald' : 'text-rose'}">
                        (${isProfit ? '+' : ''}${bot.unrealizedPnlPct || 0}%)
                    </div>
                </div>
                <div class="stat-box">
                    <div class="stat-label">실시간 수급 & 승률</div>
                    <div class="stat-value">${bot.volumeRatio || 120}%</div>
                    <div class="stat-sub text-cyan">거래량 폭증 지수 | 승률: ${bot.winRate || 0}%</div>
                </div>
            </div>

            <div style="background-color: var(--bg-main); padding: 0.75rem; border-radius: var(--radius-sm); border: 1px solid var(--border-subtle); max-height: 120px; overflow-y: auto;">
                <div style="font-size: 0.75rem; font-weight: 700; color: var(--text-muted); margin-bottom: 0.4rem;">실시간 멀티팩터 체결 & 모니터링 로그</div>
                ${(bot.recentLogs || []).map(l => `
                    <div style="font-size: 0.8rem; font-family: var(--font-mono); color: var(--text-secondary); margin-bottom: 0.2rem;">
                        <span style="color: var(--text-muted);">${l.timestamp}</span> [${l.level}] ${l.message}
                    </div>
                `).join('')}
            </div>
        `;
        container.appendChild(card);
    });
}

async function stopTradingBot(botId) {
    if (!confirm("정말 이 봇을 즉시 정지하고 보유 중인 포지션을 시장가로 전량 청산하시겠습니까?")) return;
    try {
        const res = await fetch("/api/bot/stop", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ botId: botId })
        });
        const result = await res.json();
        if (result.success) {
            await loadActiveBots();
        }
    } catch (e) {
        console.error("Stop bot error:", e);
    }
}

async function deleteTradingBot(botId) {
    try {
        await fetch("/api/bot/delete", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ botId: botId })
        });
        await loadActiveBots();
    } catch (e) {
        console.error("Delete bot error:", e);
    }
}

async function handleStopAllBots() {
    if (!confirm("🚨 경고: 현재 가동 중인 모든 AI 자동매매 봇을 일괄 정지하고 모든 포지션을 비상 시장가 청산하시겠습니까?")) return;
    try {
        const res = await fetch("/api/bot/stop_all", { method: "POST" });
        const data = await res.json();
        alert(`🛑 총 ${data.stoppedCount}개의 봇이 안전하게 일괄 정지 및 전량 청산되었습니다.`);
        await loadActiveBots();
    } catch (e) {
        console.error("Stop all bots error:", e);
    }
}

// Broker Integration Management
async function loadBrokers() {
    try {
        const res = await fetch("/api/broker/list");
        const data = await res.json();
        renderBrokers(data.brokers || []);
    } catch (e) {
        console.error("Load brokers error:", e);
    }
}

function renderBrokers(brokers) {
    const container = document.getElementById("brokerListContainer");
    if (!container) return;

    container.innerHTML = "";
    brokers.forEach(b => {
        const item = document.createElement("div");
        item.style.cssText = "display: flex; justify-content: space-between; align-items: center; padding: 0.6rem 0.8rem; background: var(--bg-card-subtle); border-radius: var(--radius-sm); border: 1px solid var(--border-subtle);";
        
        item.innerHTML = `
            <div style="display: flex; align-items: center; gap: 0.5rem; font-size: 0.85rem;">
                <span style="font-weight: 700;">${b.name}</span>
                ${b.connected ? `<span style="font-size: 0.75rem; color: var(--text-muted); font-family: var(--font-mono);">${b.apiKey}</span>` : ''}
            </div>
            <div>
                ${b.connected 
                    ? `<span class="badge-win" style="font-size: 0.75rem; margin-right: 0.3rem;">연동 완료</span>
                       <button class="btn-chip" style="font-size: 0.7rem; padding: 0.15rem 0.4rem; color: var(--accent-rose); border-color: var(--accent-rose);" onclick="disconnectBroker('${b.code}')">해제</button>`
                    : `<button class="btn-chip" style="font-size: 0.75rem; padding: 0.2rem 0.6rem; color: var(--accent-cyan); border-color: var(--accent-cyan);" onclick="openBrokerModal('${b.code}', '${b.name}')">API 키 등록</button>`
                }
            </div>
        `;
        container.appendChild(item);
    });
}

function openBrokerModal(code, name) {
    const modal = document.getElementById("brokerModal");
    const title = document.getElementById("brokerModalTitle");
    const codeInput = document.getElementById("modalBrokerCode");
    if (modal && title && codeInput) {
        title.textContent = `[${name}] API Key 연동 등록`;
        codeInput.value = code;
        document.getElementById("modalApiKey").value = "";
        document.getElementById("modalSecretKey").value = "";
        document.getElementById("modalAccountNo").value = "";
        modal.classList.remove("hidden");
    }
}

function closeBrokerModal() {
    const modal = document.getElementById("brokerModal");
    if (modal) modal.classList.add("hidden");
}

async function handleSaveBrokerKey(e) {
    e.preventDefault();
    const code = document.getElementById("modalBrokerCode").value;
    const apiKey = document.getElementById("modalApiKey").value.trim();
    const secretKey = document.getElementById("modalSecretKey").value.trim();
    const accountNo = document.getElementById("modalAccountNo").value.trim();

    try {
        if (code === "BITHUMB" && secretKey) {
            // Test Bithumb connection directly
            try {
                const testRes = await fetch("/api/broker/test_bithumb", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ brokerCode: "BITHUMB", apiKey: apiKey, secretKey: secretKey })
                });
                const testData = await testRes.json();
                if (testData.success) {
                    alert(`✅ [빗썸 연동 성공!]\n• 보유 원화(KRW): ${testData.totalKrw.toLocaleString()}원\n• 출금/주문가능: ${testData.availableKrw.toLocaleString()}원\n• BTC 보유량: ${testData.btcBalance} BTC`);
                }
            } catch (e) {
                console.log("Bithumb test log:", e);
            }
        }

        const res = await fetch("/api/broker/connect", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                brokerCode: code,
                apiKey: apiKey,
                secretKey: secretKey,
                accountNo: accountNo
            })
        });
        const result = await res.json();
        if (result.success) {
            closeBrokerModal();
            alert(`🔒 [${code}] 거래소/브로커 API 키가 안전하게 암호화 연동되었습니다!`);
            await loadBrokers();
        }
    } catch (err) {
        console.error("Save broker key error:", err);
        alert("브로커 연동 중 오류가 발생했습니다.");
    }
}

async function disconnectBroker(code) {
    if (!confirm(`[${code}] 브로커 연동을 해제하시겠습니까?`)) return;
    try {
        await fetch("/api/broker/disconnect", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ brokerCode: code })
        });
        await loadBrokers();
    } catch (e) {
        console.error("Disconnect error:", e);
    }
}

// Wall Street Gurus Intelligence Engine
let cachedGurus = [];

async function loadGurus() {
    try {
        const res = await fetch("/api/gurus");
        const data = await res.json();
        cachedGurus = data.gurus || [];
        renderGurus(cachedGurus);
    } catch (e) {
        console.error("Load gurus error:", e);
    }
}

function renderGurus(gurus) {
    const grid = document.getElementById("guruCardsGrid");
    if (!grid) return;

    grid.innerHTML = "";
    gurus.forEach(guru => {
        const card = document.createElement("div");
        card.className = "card";
        card.style.display = "flex";
        card.style.flexDirection = "column";
        card.style.justifyContent = "space-between";
        card.style.border = "1px solid var(--border-subtle)";
        card.style.transition = "transform 0.2s, border-color 0.2s";
        card.onmouseenter = () => card.style.borderColor = "var(--accent-amber)";
        card.onmouseleave = () => card.style.borderColor = "var(--border-subtle)";

        const picksHtml = guru.recommendedPicks.map(p => `
            <div style="display: flex; justify-content: space-between; align-items: center; padding: 0.4rem 0.6rem; background: var(--bg-main); border-radius: var(--radius-sm); margin-bottom: 0.4rem; border: 1px solid var(--border-subtle);">
                <div style="cursor: pointer;" onclick="loadSymbolData('${p.symbol}')">
                    <span style="font-weight: 700; color: #fff; font-size: 0.85rem;">${p.name}</span>
                    <span style="font-family: var(--font-mono); font-size: 0.75rem; color: var(--accent-cyan); margin-left: 0.3rem;">${p.symbol}</span>
                </div>
                <span class="badge-win" style="font-size: 0.75rem; padding: 0.1rem 0.4rem;">${p.targetReturn}</span>
            </div>
            <div style="font-size: 0.75rem; color: var(--text-muted); margin-bottom: 0.5rem; padding-left: 0.2rem; line-height: 1.4;">
                ${p.reason}
            </div>
        `).join('');

        card.innerHTML = `
            <div>
                <div style="display: flex; align-items: center; gap: 0.75rem; margin-bottom: 0.75rem;">
                    <span style="font-size: 2rem;">${guru.avatar}</span>
                    <div>
                        <div style="font-size: 1.05rem; font-weight: 800; color: #fff;">${guru.name}</div>
                        <div style="font-size: 0.75rem; color: var(--accent-amber); font-weight: 600;">${guru.title}</div>
                    </div>
                </div>

                <div style="background: var(--bg-card-subtle); padding: 0.75rem; border-radius: var(--radius-sm); border-left: 3px solid var(--accent-amber); margin-bottom: 1rem; font-size: 0.8rem; color: var(--text-secondary); line-height: 1.5;">
                    "${guru.philosophy}"
                </div>

                <div style="margin-bottom: 1rem;">
                    <div class="stat-label" style="font-size: 0.75rem; margin-bottom: 0.4rem; color: var(--accent-cyan);">핵심 투자 공식 (Magic Filter)</div>
                    <div style="font-size: 0.8rem; font-weight: 600; color: #E5E7EB;">${guru.keyMetrics}</div>
                </div>

                <div>
                    <div class="stat-label" style="font-size: 0.75rem; margin-bottom: 0.5rem;">구루 엄선 1순위 추천 종목 포트폴리오</div>
                    ${picksHtml}
                </div>
            </div>

            <div style="display: flex; gap: 0.5rem; margin-top: 1.25rem; padding-top: 1rem; border-top: 1px solid var(--border-subtle);">
                <button class="btn-secondary" style="flex: 1; font-size: 0.75rem; padding: 0.5rem;" onclick="applyGuruStrategy('${guru.id}')">
                    ⚡ 백테스트 연동
                </button>
                <button class="btn-primary" style="flex: 1; font-size: 0.75rem; padding: 0.5rem; background: linear-gradient(135deg, #F59E0B, #D97706);" onclick="deployGuruBot('${guru.id}')">
                    🚀 구루 봇 즉시 가동
                </button>
            </div>
        `;
        grid.appendChild(card);
    });
}

function applyGuruStrategy(guruId) {
    const guru = cachedGurus.find(g => g.id === guruId);
    if (!guru) return;

    const p = guru.strategyParams;
    const topPick = guru.recommendedPicks[0].symbol;

    // Set parameters
    if (document.getElementById("btFastMa")) document.getElementById("btFastMa").value = p.fastMa;
    if (document.getElementById("btSlowMa")) document.getElementById("btSlowMa").value = p.slowMa;
    if (document.getElementById("btRsiBuy")) document.getElementById("btRsiBuy").value = p.rsiBuy;
    if (document.getElementById("btRsiSell")) document.getElementById("btRsiSell").value = p.rsiSell;
    if (document.getElementById("btTakeProfit")) document.getElementById("btTakeProfit").value = p.takeProfitPct;
    if (document.getElementById("btStopLoss")) document.getElementById("btStopLoss").value = p.stopLossPct;

    // Switch to Quant Tab
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".tab-content").forEach(s => s.classList.add("hidden"));
    
    const quantTabBtn = document.querySelector(`[data-tab="tab-quant"]`);
    if (quantTabBtn) quantTabBtn.classList.add("active");
    const quantTab = document.getElementById("tab-quant");
    if (quantTab) quantTab.classList.remove("hidden");

    loadSymbolData(topPick).then(() => {
        handleRunBacktest();
    });
}

function deployGuruBot(guruId) {
    const guru = cachedGurus.find(g => g.id === guruId);
    if (!guru) return;

    const p = guru.strategyParams;
    const topPick = guru.recommendedPicks[0].symbol;
    
    if (confirm(`🏛️ [${guru.name}] 투자 기법으로 추천 1위 종목 [${topPick}] 자동매매 봇을 즉시 가동하시겠습니까?`)) {
        deployTradingBot(topPick, p, "PAPER", 10000);
    }
}

// Commercial AI Bot Marketplace & Leaderboard Engine
let cachedMarketplaceBots = [];
let currentCategoryFilter = "ALL";

async function loadMarketplaceBots() {
    try {
        const res = await fetch("/api/marketplace/bots");
        const data = await res.json();
        cachedMarketplaceBots = data.bots || [];
        renderMarketplace(cachedMarketplaceBots);
    } catch (e) {
        console.error("Load marketplace error:", e);
    }
}

function renderMarketplace(bots) {
    const grid = document.getElementById("marketplaceGrid");
    if (!grid) return;

    const filtered = currentCategoryFilter === "ALL" 
        ? bots 
        : bots.filter(b => b.type === currentCategoryFilter);

    grid.innerHTML = "";
    filtered.forEach(bot => {
        const card = document.createElement("div");
        card.className = "card";
        card.style.display = "flex";
        card.style.flexDirection = "column";
        card.style.justifyContent = "space-between";
        card.style.border = "1px solid var(--border-subtle)";
        card.style.background = "linear-gradient(180deg, #18202F 0%, #151A23 100%)";
        card.style.transition = "transform 0.2s, border-color 0.2s, box-shadow 0.2s";
        card.onmouseenter = () => {
            card.style.transform = "translateY(-3px)";
            card.style.borderColor = "var(--accent-amber)";
            card.style.boxShadow = "0 8px 30px rgba(0,0,0,0.6)";
        };
        card.onmouseleave = () => {
            card.style.transform = "none";
            card.style.borderColor = "var(--border-subtle)";
            card.style.boxShadow = "var(--shadow-card)";
        };

        card.innerHTML = `
            <div>
                <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 0.75rem;">
                    <div>
                        <span class="badge-win" style="font-size: 0.75rem; background: rgba(245,158,11,0.15); color: var(--accent-amber);">${bot.badge}</span>
                        <div style="font-size: 1.15rem; font-weight: 800; color: #fff; margin-top: 0.35rem;">${bot.name}</div>
                        <div style="font-size: 0.75rem; color: var(--accent-cyan); font-weight: 600;">타겟 자산: ${bot.targetAsset} | ${bot.category}</div>
                    </div>
                </div>

                <p style="font-size: 0.82rem; color: var(--text-secondary); line-height: 1.5; margin-bottom: 1.25rem;">
                    ${bot.description}
                </p>

                <!-- Key Performance Metrics Grid -->
                <div class="grid-3" style="margin-bottom: 1.25rem; gap: 0.5rem;">
                    <div class="stat-box" style="padding: 0.6rem;">
                        <div class="stat-label" style="font-size: 0.7rem;">연환산 수익률 (APY)</div>
                        <div class="stat-value text-emerald" style="font-size: 1.25rem;">+${bot.apy}%</div>
                    </div>
                    <div class="stat-box" style="padding: 0.6rem;">
                        <div class="stat-label" style="font-size: 0.7rem;">승률 (Win Rate)</div>
                        <div class="stat-value text-cyan" style="font-size: 1.25rem;">${bot.winRate}%</div>
                    </div>
                    <div class="stat-box" style="padding: 0.6rem;">
                        <div class="stat-label" style="font-size: 0.7rem;">최대 낙폭 (MDD)</div>
                        <div class="stat-value text-rose" style="font-size: 1.25rem;">-${bot.mdd}%</div>
                    </div>
                </div>

                <div style="display: flex; justify-content: space-between; align-items: center; font-size: 0.75rem; color: var(--text-muted); padding: 0.4rem 0.6rem; background: rgba(0,0,0,0.25); border-radius: var(--radius-sm); border: 1px solid var(--border-subtle);">
                    <span>👥 실시간 운용자: <strong>${bot.activeUsers.toLocaleString()}명</strong></span>
                    <span>샤프 비율: <strong style="color: var(--accent-emerald);">${bot.sharpe}</strong></span>
                </div>
            </div>

            <div style="display: flex; gap: 0.5rem; margin-top: 1.25rem; padding-top: 1rem; border-top: 1px solid var(--border-subtle);">
                <button class="btn-secondary" style="flex: 1; font-size: 0.8rem; padding: 0.55rem;" onclick="testBotInQuant('${bot.id}')">
                    📊 백테스트
                </button>
                <button class="btn-primary" style="flex: 1.5; font-size: 0.8rem; padding: 0.55rem; background: linear-gradient(135deg, #F59E0B, #D97706);" onclick="copyAndDeployBot('${bot.id}')">
                    ⚡ 1-Click 복제 & 가동
                </button>
            </div>
        `;
        grid.appendChild(card);
    });
}

function filterMarketplace(category) {
    currentCategoryFilter = category;
    document.querySelectorAll(".control-bar, .tabs-header, #tab-marketplace .btn-chip").forEach(btn => {
        if (btn.getAttribute("onclick")?.includes(`'${category}'`)) {
            btn.classList.add("active");
        } else if (btn.getAttribute("onclick")?.includes("filterMarketplace")) {
            btn.classList.remove("active");
        }
    });
    renderMarketplace(cachedMarketplaceBots);
}

let pendingBotConfig = null;

function copyAndDeployBot(botId) {
    const bot = cachedMarketplaceBots.find(b => b.id === botId);
    if (!bot) return;
    openCopyBotModal(bot.name, bot.targetAsset, bot.config, bot.description);
}

function deployGuruBot(guruId) {
    const guru = cachedGurus.find(g => g.id === guruId);
    if (!guru) return;
    const topPick = guru.recommendedPicks[0].symbol;
    openCopyBotModal(`${guru.name} 구루 봇`, topPick, guru.strategyParams, guru.philosophy);
}

function openCopyBotModal(name, defaultSymbol, config, description = "") {
    pendingBotConfig = config;
    const modal = document.getElementById("copyBotModal");
    const title = document.getElementById("copyBotModalTitle");
    const desc = document.getElementById("copyBotModalDesc");
    const symInput = document.getElementById("copyModalSymbol");

    if (modal && title && desc && symInput) {
        title.textContent = `⚡ [${name}] 복제 & 가동 설정`;
        desc.textContent = description || "연동할 거래소와 매매 모드, 운용 자본을 설정하면 즉시 24시간 실전 자동매매가 시작됩니다.";
        symInput.value = defaultSymbol;
        modal.classList.remove("hidden");
    }
}

function closeCopyBotModal() {
    const modal = document.getElementById("copyBotModal");
    if (modal) modal.classList.add("hidden");
    pendingBotConfig = null;
}

async function handleConfirmCopyDeploy(e) {
    e.preventDefault();
    if (!pendingBotConfig) return;

    const symbol = document.getElementById("copyModalSymbol").value.trim().toUpperCase();
    const mode = document.getElementById("copyModalMode").value;
    const broker = document.getElementById("copyModalBroker").value;
    const capital = parseFloat(document.getElementById("copyModalCapital").value) || 1000000;

    closeCopyBotModal();
    await deployTradingBot(symbol, pendingBotConfig, mode, capital, broker);
}

function testBotInQuant(botId) {
    const bot = cachedMarketplaceBots.find(b => b.id === botId);
    if (!bot) return;

    const p = bot.config;
    // Set parameters
    if (document.getElementById("btFastMa")) document.getElementById("btFastMa").value = p.fastMa;
    if (document.getElementById("btSlowMa")) document.getElementById("btSlowMa").value = p.slowMa;
    if (document.getElementById("btRsiBuy")) document.getElementById("btRsiBuy").value = p.rsiBuy;
    if (document.getElementById("btRsiSell")) document.getElementById("btRsiSell").value = p.rsiSell;
    if (document.getElementById("btTakeProfit")) document.getElementById("btTakeProfit").value = p.takeProfitPct;
    if (document.getElementById("btStopLoss")) document.getElementById("btStopLoss").value = p.stopLossPct;

    // Switch to Quant Tab
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".tab-content").forEach(s => s.classList.add("hidden"));
    
    const quantTabBtn = document.querySelector(`[data-tab="tab-quant"]`);
    if (quantTabBtn) quantTabBtn.classList.add("active");
    const quantTab = document.getElementById("tab-quant");
    if (quantTab) quantTab.classList.remove("hidden");

    loadSymbolData(bot.targetAsset).then(() => {
        handleRunBacktest();
    });
}

window.filterMarketplace = filterMarketplace;
window.copyAndDeployBot = copyAndDeployBot;
window.openCopyBotModal = openCopyBotModal;
window.closeCopyBotModal = closeCopyBotModal;
window.handleConfirmCopyDeploy = handleConfirmCopyDeploy;
window.testBotInQuant = testBotInQuant;
window.applyGuruStrategy = applyGuruStrategy;
window.deployGuruBot = deployGuruBot;
window.stopTradingBot = stopTradingBot;
window.deleteTradingBot = deleteTradingBot;
window.handleStopAllBots = handleStopAllBots;
window.openBrokerModal = openBrokerModal;
window.closeBrokerModal = closeBrokerModal;
window.handleSaveBrokerKey = handleSaveBrokerKey;
window.disconnectBroker = disconnectBroker;
window.applyPreset = applyPreset;

// Auto-refresh active bots every 3 seconds
setInterval(() => {
    const botTab = document.getElementById("tab-trader");
    if (botTab && !botTab.classList.contains("hidden")) {
        loadActiveBots();
    }
}, 3000);



