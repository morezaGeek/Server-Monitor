(function () {
    let throughputChart = null;
    let payloadChart = null;
    let activeService = "google";
    let activeSubService = "google";
    let historicalData = [];       // All services from last DB fetch
    let realTimeHistory = [];      // Throughput line-chart data for active target
    let speedPollId = null;        // 1-second speed poll
    let chartPollId = null;        // 20-second chart refresh
    let cachedCurrentSpeeds = {};  // Latest speeds for ALL services
    let fetchGeneration = 0;       // Race-condition guard

    // ── Helpers ──────────────────────────────────────────────────────────────

    function getThemeColors() {
        const bodyStyle = getComputedStyle(document.body);
        const theme = document.documentElement.getAttribute("data-theme") || "dark";
        const lightThemes = ["light", "catppuccin-latte", "solarized-light", "gruvbox-light", "material-light", "rose-pine-dawn"];
        const isDark = !lightThemes.includes(theme);
        const textColor = bodyStyle.color || (isDark ? "#f8f8f2" : "#0f172a");
        const mutedColor = isDark ? "rgba(255, 255, 255, 0.5)" : "rgba(15, 23, 42, 0.5)";
        return {
            text: textColor,
            muted: mutedColor,
            border: bodyStyle.getPropertyValue("--border-color").trim() || (isDark ? "rgba(98, 114, 164, 0.3)" : "rgba(226, 232, 240, 0.8)"),
            grid: isDark ? "rgba(255, 255, 255, 0.05)" : "rgba(0, 0, 0, 0.03)",
            down: bodyStyle.getPropertyValue("--net-start").trim() || "#10b981",
            up: bodyStyle.getPropertyValue("--cpu-start").trim() || "#818cf8",
            downGlow: "rgba(16, 185, 129, 0.15)",
            upGlow: "rgba(129, 140, 248, 0.15)",
        };
    }

    function formatSpeed(mbps) {
        if (mbps === undefined || mbps === null) return "0.00 Mbps";
        if (mbps < 0.1) return (mbps * 1024).toFixed(0) + " Kbps";
        return mbps.toFixed(2) + " Mbps";
    }

    function formatBytes(bytes) {
        if (!bytes) return "0.00 MB";
        const mb = bytes / (1024 * 1024);
        if (mb >= 1024) return (mb / 1024).toFixed(2) + " GB";
        return mb.toFixed(1) + " MB";
    }

    function getActiveQueryTarget() {
        if (activeService === "google") return activeSubService;
        return activeService;
    }

    // ── Range Helpers ────────────────────────────────────────────────────────

    const RANGE_SECONDS = {
        "1h": 3600, "2h": 7200, "6h": 21600, "12h": 43200,
        "1d": 86400, "2d": 172800, "1w": 604800, "1m": 2592000
    };

    function getRangeSeconds() {
        const sel = document.getElementById("serviceRangeSelect");
        if (!sel) return 3600;
        const val = sel.value;
        if (val === "custom") {
            const num = parseFloat(document.getElementById("serviceCustomRangeInput").value) || 30;
            const unit = document.getElementById("serviceCustomRangeUnit").value;
            if (unit === "hours") return num * 3600;
            if (unit === "days") return num * 86400;
            return num * 60; // minutes
        }
        return RANGE_SECONDS[val] || 3600;
    }

    function getTimeUnit(seconds) {
        if (seconds <= 7200) return "minute";
        if (seconds <= 172800) return "hour";
        return "day";
    }

    // ── Speed Cards (instant update) ─────────────────────────────────────────

    function updateSpeedCards() {
        const target = getActiveQueryTarget();
        const s = cachedCurrentSpeeds[target] || { down_mbps: 0, up_mbps: 0 };
        const downEl = document.getElementById("serviceDownSpeed");
        const upEl = document.getElementById("serviceUpSpeed");
        if (downEl) downEl.innerText = formatSpeed(s.down_mbps);
        if (upEl) upEl.innerText = formatSpeed(s.up_mbps);
    }

    // Re-compute accumulated payload from already-loaded historicalData
    function updatePayloadCard() {
        const target = getActiveQueryTarget();
        const serviceHist = historicalData.filter(d => d.service === target);
        let totalDown = 0, totalUp = 0;
        serviceHist.forEach(d => { totalDown += d.down; totalUp += d.up; });
        const el = document.getElementById("serviceTotalPayload");
        if (el) el.innerText = formatBytes(totalDown + totalUp);
    }

    // ── Charts ───────────────────────────────────────────────────────────────

    function initCharts() {
        const colors = getThemeColors();

        // Throughput Line Chart
        const ctxT = document.getElementById("serviceThroughputChart");
        if (ctxT) {
            throughputChart = new Chart(ctxT, {
                type: "line",
                data: {
                    datasets: [
                        {
                            label: "Download (Speed)",
                            borderColor: colors.down, backgroundColor: colors.downGlow,
                            borderWidth: 2, pointRadius: 0, pointHoverRadius: 5,
                            fill: true, tension: 0.4, data: []
                        },
                        {
                            label: "Upload (Speed)",
                            borderColor: colors.up, backgroundColor: colors.upGlow,
                            borderWidth: 2, pointRadius: 0, pointHoverRadius: 5,
                            fill: true, tension: 0.4, data: []
                        }
                    ]
                },
                options: {
                    responsive: true, maintainAspectRatio: false,
                    animation: false,
                    plugins: {
                        legend: { display: true, labels: { color: colors.text, font: { family: "Outfit, Inter, sans-serif", size: 11 } } },
                        tooltip: {
                            mode: "index", intersect: false,
                            backgroundColor: "rgba(15, 23, 42, 0.9)", titleColor: "#f8fafc",
                            bodyColor: "#cbd5e1", borderColor: "rgba(99, 102, 241, 0.3)", borderWidth: 1,
                            callbacks: { label: ctx => ctx.dataset.label.split(" ")[0] + ": " + formatSpeed(ctx.parsed.y) }
                        }
                    },
                    scales: {
                        x: {
                            type: "time",
                            time: { displayFormats: { second: "HH:mm:ss", minute: "HH:mm", hour: "HH:mm", day: "MMM d", month: "MMM yyyy" } },
                            grid: { display: false },
                            ticks: { color: colors.muted, maxRotation: 0, autoSkip: true, maxTicksLimit: 10 }
                        },
                        y: {
                            grid: { color: colors.grid }, border: { dash: [4, 4] },
                            ticks: { color: colors.muted, callback: v => formatSpeed(v) },
                            min: 0
                        }
                    }
                }
            });
        }

        // Payload Bar Chart
        const ctxP = document.getElementById("servicePayloadChart");
        if (ctxP) {
            payloadChart = new Chart(ctxP, {
                type: "bar",
                data: {
                    datasets: [
                        { label: "Download", backgroundColor: colors.down, borderRadius: 4, data: [] },
                        { label: "Upload", backgroundColor: colors.up, borderRadius: 4, data: [] }
                    ]
                },
                options: {
                    responsive: true, maintainAspectRatio: false,
                    plugins: {
                        legend: { display: true, labels: { color: colors.text, font: { family: "Outfit, Inter, sans-serif", size: 11 } } },
                        tooltip: {
                            mode: "index", intersect: false, backgroundColor: "rgba(15, 23, 42, 0.9)",
                            callbacks: {
                                label: ctx => { const v = ctx.raw.y * 1024 * 1024; return ctx.dataset.label + ": " + formatBytes(v); }
                            }
                        }
                    },
                    scales: {
                        x: {
                            type: "time",
                            time: { displayFormats: { second: "HH:mm:ss", minute: "HH:mm", hour: "HH:mm", day: "MMM d", month: "MMM yyyy" } },
                            grid: { display: false },
                            ticks: { color: colors.muted, maxRotation: 0, autoSkip: true, maxTicksLimit: 10 },
                            stacked: true
                        },
                        y: {
                            grid: { color: colors.grid }, border: { dash: [4, 4] },
                            ticks: { color: colors.muted, callback: v => v >= 1024 ? (v / 1024).toFixed(1) + " GB" : v.toFixed(0) + " MB" },
                            stacked: true, min: 0
                        }
                    }
                }
            });
        }
    }

    // Rebuild BOTH charts from current state (historicalData + realTimeHistory)
    function updateChartsForTarget() {
        const target = getActiveQueryTarget();
        const colors = getThemeColors();
        const rangeSeconds = getRangeSeconds();
        const timeUnit = getTimeUnit(rangeSeconds);
        const now = new Date();
        const minTime = new Date(now.getTime() - rangeSeconds * 1000);

        // ── 1. Throughput Line Chart ──
        if (throughputChart) {
            throughputChart.data.datasets[0].borderColor = colors.down;
            throughputChart.data.datasets[0].backgroundColor = colors.downGlow;
            throughputChart.data.datasets[1].borderColor = colors.up;
            throughputChart.data.datasets[1].backgroundColor = colors.upGlow;

            throughputChart.data.datasets[0].data = realTimeHistory.map(pt => ({ x: pt.t, y: pt.down }));
            throughputChart.data.datasets[1].data = realTimeHistory.map(pt => ({ x: pt.t, y: pt.up }));

            throughputChart.options.scales.x.min = minTime;
            throughputChart.options.scales.x.max = now;
            throughputChart.options.scales.x.time.unit = timeUnit;
            throughputChart.update("none");
        }

        // ── 2. Payload Bar Chart ──
        if (payloadChart) {
            const serviceHist = historicalData.filter(d => d.service === target);

            payloadChart.data.datasets[0].backgroundColor = colors.down;
            payloadChart.data.datasets[1].backgroundColor = colors.up;

            payloadChart.data.datasets[0].data = serviceHist.map(d => ({ x: new Date(d.t * 1000), y: d.down / (1024 * 1024) }));
            payloadChart.data.datasets[1].data = serviceHist.map(d => ({ x: new Date(d.t * 1000), y: d.up / (1024 * 1024) }));

            // Accumulated payload card
            let totalDown = 0, totalUp = 0;
            serviceHist.forEach(d => { totalDown += d.down; totalUp += d.up; });
            const payloadEl = document.getElementById("serviceTotalPayload");
            if (payloadEl) payloadEl.innerText = formatBytes(totalDown + totalUp);

            payloadChart.options.scales.x.time.unit = timeUnit;
            payloadChart.update();
        }
    }

    // ── Fetch Historical Data from DB ────────────────────────────────────────

    async function loadHistoricalMetrics() {
        const gen = ++fetchGeneration;
        const targetAtStart = getActiveQueryTarget();
        const rangeSeconds = getRangeSeconds();

        // Build API URL
        const sel = document.getElementById("serviceRangeSelect");
        const rangeVal = sel ? sel.value : "1h";
        let url;
        if (rangeVal === "custom") {
            url = "/api/services/metrics?seconds=" + Math.round(rangeSeconds);
        } else {
            url = "/api/services/metrics?range=" + rangeVal;
        }

        try {
            const res = await fetch(url);
            if (!res.ok) throw new Error("API error fetching service metrics");

            // Race-condition guard: if user switched tabs during the fetch, discard
            if (fetchGeneration !== gen) return;

            historicalData = await res.json();

            if (getActiveQueryTarget() !== targetAtStart) return;

            // Build realTimeHistory (throughput) from ALL historical points for this target
            const serviceHist = historicalData.filter(d => d.service === targetAtStart);

            let interval = 10; // default raw resolution
            if (serviceHist.length > 1) {
                const diff = serviceHist[serviceHist.length - 1].t - serviceHist[0].t;
                interval = Math.max(1, Math.round(diff / (serviceHist.length - 1)));
            }

            realTimeHistory = serviceHist.map(d => ({
                t: new Date(d.t * 1000),
                down: (d.down * 8) / (interval * 1024 * 1024),
                up:   (d.up   * 8) / (interval * 1024 * 1024)
            }));

            updateChartsForTarget();
        } catch (err) {
            console.error("Error loading service historical metrics:", err);
        }
    }

    // ── Real-Time Polling ────────────────────────────────────────────────────

    function startPolling() {
        if (speedPollId) clearInterval(speedPollId);
        if (chartPollId) clearInterval(chartPollId);

        // 1-second poll for speed cards + live line-chart append
        async function pollSpeeds() {
            try {
                const res = await fetch("/api/services/current");
                if (!res.ok) return;
                cachedCurrentSpeeds = await res.json();

                // Update speed cards instantly
                updateSpeedCards();

                // Append live point to throughput chart
                const target = getActiveQueryTarget();
                const s = cachedCurrentSpeeds[target] || { down_mbps: 0, up_mbps: 0 };
                const now = new Date();
                realTimeHistory.push({ t: now, down: s.down_mbps, up: s.up_mbps });

                // Trim to the selected range
                const rangeSeconds = getRangeSeconds();
                const cutoff = new Date(now.getTime() - rangeSeconds * 1000);
                realTimeHistory = realTimeHistory.filter(pt => pt.t >= cutoff);

                // Update throughput chart in-place (fast, no animation)
                if (throughputChart) {
                    throughputChart.data.datasets[0].data = realTimeHistory.map(pt => ({ x: pt.t, y: pt.down }));
                    throughputChart.data.datasets[1].data = realTimeHistory.map(pt => ({ x: pt.t, y: pt.up }));
                    const minTime = new Date(now.getTime() - rangeSeconds * 1000);
                    throughputChart.options.scales.x.min = minTime;
                    throughputChart.options.scales.x.max = now;
                    throughputChart.options.scales.x.time.unit = getTimeUnit(rangeSeconds);
                    throughputChart.update("none");
                }
            } catch (err) {
                console.error("Error polling service current speeds:", err);
            }
        }

        pollSpeeds(); // Initial poll immediately
        speedPollId = setInterval(pollSpeeds, 1000);

        // 20-second poll for full chart data from DB
        chartPollId = setInterval(() => { loadHistoricalMetrics(); }, 20000);
    }

    // ── Tab & Control Listeners ──────────────────────────────────────────────

    function onTabSwitch() {
        // 1. Immediately update speed cards from cache
        updateSpeedCards();

        // 2. Immediately re-filter existing historicalData for the new target and re-render charts
        const target = getActiveQueryTarget();
        const serviceHist = historicalData.filter(d => d.service === target);
        let interval = 10;
        if (serviceHist.length > 1) {
            const diff = serviceHist[serviceHist.length - 1].t - serviceHist[0].t;
            interval = Math.max(1, Math.round(diff / (serviceHist.length - 1)));
        }
        realTimeHistory = serviceHist.map(d => ({
            t: new Date(d.t * 1000),
            down: (d.down * 8) / (interval * 1024 * 1024),
            up:   (d.up   * 8) / (interval * 1024 * 1024)
        }));
        updateChartsForTarget();

        // 3. Fetch fresh data in the background (non-blocking)
        loadHistoricalMetrics();
    }

    function setupTabListeners() {
        const tabs = document.querySelectorAll(".service-tab");
        const subTabsContainer = document.getElementById("googleSubTabs");

        tabs.forEach(tab => {
            tab.addEventListener("click", function () {
                tabs.forEach(t => t.classList.remove("active"));
                this.classList.add("active");
                activeService = this.getAttribute("data-service");
                subTabsContainer.style.display = activeService === "google" ? "flex" : "none";
                onTabSwitch();
            });
        });

        const subTabs = document.querySelectorAll(".sub-service-tab");
        subTabs.forEach(tab => {
            tab.addEventListener("click", function () {
                subTabs.forEach(t => t.classList.remove("active"));
                this.classList.add("active");
                activeSubService = this.getAttribute("data-sub");
                onTabSwitch();
            });
        });

        // Range dropdown
        const rangeSelect = document.getElementById("serviceRangeSelect");
        const customContainer = document.getElementById("serviceCustomRangeContainer");
        if (rangeSelect) {
            rangeSelect.addEventListener("change", function () {
                if (customContainer) {
                    customContainer.style.display = this.value === "custom" ? "flex" : "none";
                }
                loadHistoricalMetrics();
            });
        }

        // Custom range apply button
        const applyBtn = document.getElementById("serviceCustomRangeApply");
        if (applyBtn) {
            applyBtn.addEventListener("click", () => { loadHistoricalMetrics(); });
        }

        // Theme swap observer
        const observer = new MutationObserver(() => {
            if (throughputChart && payloadChart) {
                const colors = getThemeColors();
                throughputChart.options.scales.x.ticks.color = colors.muted;
                throughputChart.options.scales.y.ticks.color = colors.muted;
                throughputChart.options.scales.y.grid.color = colors.grid;
                throughputChart.options.plugins.legend.labels.color = colors.text;
                throughputChart.data.datasets[0].borderColor = colors.down;
                throughputChart.data.datasets[0].backgroundColor = colors.downGlow;
                throughputChart.data.datasets[1].borderColor = colors.up;
                throughputChart.data.datasets[1].backgroundColor = colors.upGlow;

                payloadChart.options.scales.x.ticks.color = colors.muted;
                payloadChart.options.scales.y.ticks.color = colors.muted;
                payloadChart.options.scales.y.grid.color = colors.grid;
                payloadChart.options.plugins.legend.labels.color = colors.text;
                payloadChart.data.datasets[0].backgroundColor = colors.down;
                payloadChart.data.datasets[1].backgroundColor = colors.up;

                updateChartsForTarget();
            }
        });
        observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    }

    // ── Init ─────────────────────────────────────────────────────────────────

    function init() {
        initCharts();
        setupTabListeners();
        loadHistoricalMetrics();
        startPolling();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
