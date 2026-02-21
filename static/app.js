/* ═══════════════════════════════════════════════════════════════════════════
   Server Monitor Dashboard — App Logic
   ═══════════════════════════════════════════════════════════════════════════ */

(() => {
    "use strict";

    // ─── Config ──────────────────────────────────────────────────────────────
    const STATS_INTERVAL = 1_000;   // live stats every 1 second
    const CHART_INTERVAL = 30_000;  // charts every 30 seconds
    const CIRCUMFERENCE = 2 * Math.PI * 52; // gauge circle circumference

    const RANGE_MS = {
        "1h": 3600 * 1000,
        "2h": 7200 * 1000,
        "6h": 21600 * 1000,
        "12h": 43200 * 1000,
        "1d": 86400 * 1000,
        "2d": 172800 * 1000,
        "1w": 604800 * 1000,
        "1m": 2592000 * 1000
    };

    let currentRange = "1h";
    let statsTimer = null;
    let chartTimer = null;
    let selectedNic = null;  // will be set from /api/interfaces

    // Returns the user-defined max Mbps from the input (default 400)
    function getNetMax() {
        const el = document.getElementById("netMaxMbps");
        return el ? (parseFloat(el.value) || 400) : 400;
    }

    // ─── Color Definitions ───────────────────────────────────────────────────
    const COLORS = {
        cpu: { start: "#6366f1", end: "#06b6d4", bg: "rgba(99,102,241,0.08)" },
        ram: { start: "#a855f7", end: "#ec4899", bg: "rgba(168,85,247,0.08)" },
        disk: { start: "#f97316", end: "#eab308", bg: "rgba(249,115,22,0.08)" },
        netSent: { start: "#10b981", end: "#06b6d4", bg: "rgba(16,185,129,0.08)" },
        netRecv: { start: "#06b6d4", end: "#3b82f6", bg: "rgba(6,182,212,0.08)" },
        tcp: { start: "#f59e0b", end: "#d97706", bg: "rgba(245,158,11,0.08)" },
        udp: { start: "#ef4444", end: "#dc2626", bg: "rgba(239,68,68,0.08)" }
    };

    // ─── SVG Gradient Definitions ────────────────────────────────────────────
    function injectSVGGradients() {
        const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("width", "0");
        svg.setAttribute("height", "0");
        svg.style.position = "absolute";
        svg.innerHTML = `
            <defs>
                <linearGradient id="cpuGrad" x1="0%" y1="0%" x2="100%" y2="0%">
                    <stop offset="0%" stop-color="${COLORS.cpu.start}"/>
                    <stop offset="100%" stop-color="${COLORS.cpu.end}"/>
                </linearGradient>
                <linearGradient id="ramGrad" x1="0%" y1="0%" x2="100%" y2="0%">
                    <stop offset="0%" stop-color="${COLORS.ram.start}"/>
                    <stop offset="100%" stop-color="${COLORS.ram.end}"/>
                </linearGradient>
                <linearGradient id="diskGrad" x1="0%" y1="0%" x2="100%" y2="0%">
                    <stop offset="0%" stop-color="${COLORS.disk.start}"/>
                    <stop offset="100%" stop-color="${COLORS.disk.end}"/>
                </linearGradient>
            </defs>
        `;
        document.body.prepend(svg);
    }

    // ─── Chart Setup ─────────────────────────────────────────────────────────

    const chartOptions = (yLabel, isPercent = true, forceLegend = false) => ({
        responsive: true,
        maintainAspectRatio: false,
        interaction: {
            mode: "index",
            intersect: false
        },
        plugins: {
            legend: {
                display: forceLegend || !isPercent,
                position: "top",
                labels: {
                    color: "#475569",
                    font: { family: "'Inter', sans-serif", size: 11 },
                    boxWidth: 12,
                    padding: 12
                }
            },
            tooltip: {
                backgroundColor: "rgba(255, 255, 255, 0.98)",
                titleColor: "#1e293b",
                bodyColor: "#475569",
                borderColor: "rgba(0,0,0,0.08)",
                borderWidth: 1,
                cornerRadius: 8,
                padding: 10,
                titleFont: { family: "'Inter', sans-serif", weight: "600", size: 12 },
                bodyFont: { family: "'Inter', sans-serif", size: 11 },
                callbacks: {
                    title: (items) => {
                        if (items.length > 0) {
                            const d = new Date(items[0].parsed.x);
                            return d.toLocaleString();
                        }
                        return "";
                    },
                    label: (ctx) => {
                        const val = ctx.parsed.y;
                        if (isPercent) return ` ${ctx.dataset.label}: ${val.toFixed(1)}%`;
                        return ` ${ctx.dataset.label}: ${val.toFixed(2)} Mbps`;
                    }
                }
            }
        },
        scales: {
            x: {
                type: "time",
                time: {
                    tooltipFormat: "PPpp"
                },
                grid: {
                    color: "rgba(0,0,0,0.04)",
                    drawBorder: false
                },
                ticks: {
                    color: "#64748b",
                    font: { family: "'Inter', sans-serif", size: 10 },
                    maxRotation: 0,
                    autoSkipPadding: 20,
                    maxTicksLimit: 8
                }
            },
            y: {
                beginAtZero: true,
                max: isPercent ? 100 : undefined,
                grid: {
                    color: "rgba(0,0,0,0.04)",
                    drawBorder: false
                },
                ticks: {
                    color: "#64748b",
                    font: { family: "'Inter', sans-serif", size: 10 },
                    callback: (val) => isPercent ? val + "%" : val.toFixed(1) + " Mbps",
                    maxTicksLimit: 6
                }
            }
        },
        elements: {
            point: { radius: 0, hoverRadius: 4, hitRadius: 20 },
            line: { tension: 0.4, borderWidth: 2 }
        },
        animation: {
            duration: 800,
            easing: "easeInOutQuart"
        }
    });

    function createGradient(ctx, startColor, endColor, bgAlpha = 0.15) {
        const gradient = ctx.createLinearGradient(0, 0, 0, ctx.canvas.height);
        gradient.addColorStop(0, startColor + hexAlpha(bgAlpha));
        gradient.addColorStop(1, startColor + "00");
        return gradient;
    }

    function hexAlpha(a) {
        return Math.round(a * 255).toString(16).padStart(2, "0");
    }

    let cpuChart, ramChart, diskChart, netChart, connChart;
    let lastConnData = [];  // store last fetched data for re-filtering

    function initCharts() {
        const cpuCtx = document.getElementById("cpuChart").getContext("2d");
        cpuChart = new Chart(cpuCtx, {
            type: "line",
            data: {
                datasets: [{
                    label: "CPU Avg",
                    data: [],
                    borderColor: COLORS.cpu.start,
                    backgroundColor: createGradient(cpuCtx, COLORS.cpu.start, COLORS.cpu.end),
                    fill: true,
                    borderWidth: 2
                }]
            },
            options: chartOptions("CPU %", true, true)
        });

        // Apply saved max CPU from localStorage
        const savedCpuMax = localStorage.getItem("cpuMaxPercent");
        if (savedCpuMax) {
            const el = document.getElementById("cpuDynamicMax");
            if (el) el.value = savedCpuMax;
            cpuChart.options.scales.y.max = parseInt(savedCpuMax, 10);
        }

        // Listen for CPU max input changes (fires instantly on typing)
        const cpuMaxInput = document.getElementById("cpuDynamicMax");
        if (cpuMaxInput) {
            cpuMaxInput.addEventListener("input", (e) => {
                let max = parseInt(e.target.value, 10);
                if (isNaN(max) || max <= 0) {
                    localStorage.removeItem("cpuMaxPercent");
                    cpuChart.options.scales.y.max = 100;
                } else {
                    if (max > 100) max = 100;
                    localStorage.setItem("cpuMaxPercent", max);
                    cpuChart.options.scales.y.max = max;
                }
                cpuChart.update("none");
            });
        }


        const ramCtx = document.getElementById("ramChart").getContext("2d");
        ramChart = new Chart(ramCtx, {
            type: "line",
            data: {
                datasets: [{
                    label: "Memory",
                    data: [],
                    borderColor: COLORS.ram.start,
                    backgroundColor: createGradient(ramCtx, COLORS.ram.start, COLORS.ram.end),
                    fill: true
                }]
            },
            options: chartOptions("Memory %", true, true)
        });

        const diskCtx = document.getElementById("diskChart").getContext("2d");
        diskChart = new Chart(diskCtx, {
            type: "line",
            data: {
                datasets: [{
                    label: "Disk",
                    data: [],
                    borderColor: COLORS.disk.start,
                    backgroundColor: createGradient(diskCtx, COLORS.disk.start, COLORS.disk.end),
                    fill: true
                }]
            },
            options: chartOptions("Disk %")
        });

        const netCtx = document.getElementById("netChart").getContext("2d");
        netChart = new Chart(netCtx, {
            type: "line",
            data: {
                datasets: [
                    {
                        label: "Upload",
                        data: [],
                        borderColor: COLORS.netSent.start,
                        backgroundColor: createGradient(netCtx, COLORS.netSent.start, COLORS.netSent.end, 0.1),
                        fill: true
                    },
                    {
                        label: "Download",
                        data: [],
                        borderColor: COLORS.netRecv.start,
                        backgroundColor: createGradient(netCtx, COLORS.netRecv.start, COLORS.netRecv.end, 0.1),
                        fill: true
                    }
                ]
            },
            options: chartOptions("Network", false)
        });

        // Apply saved max from localStorage
        const savedMax = localStorage.getItem("netMaxMbps");
        if (savedMax) {
            const el = document.getElementById("netMaxMbps");
            if (el) el.value = savedMax;
        }
        applyNetMax();

        // Listen for max input changes (fires instantly on typing)
        document.getElementById("netMaxMbps").addEventListener("input", () => {
            const max = getNetMax();
            localStorage.setItem("netMaxMbps", max);
            applyNetMax();
        });

        // ─── Connections chart ────────────────────────────────────────────
        const connCtx = document.getElementById("connChart").getContext("2d");
        connChart = new Chart(connCtx, {
            type: "line",
            data: {
                datasets: [
                    {
                        label: "TCP",
                        data: [],
                        borderColor: COLORS.tcp.start,
                        backgroundColor: createGradient(connCtx, COLORS.tcp.start, COLORS.tcp.end, 0.12),
                        fill: true
                    },
                    {
                        label: "UDP",
                        data: [],
                        borderColor: COLORS.udp.start,
                        backgroundColor: createGradient(connCtx, COLORS.udp.start, COLORS.udp.end, 0.12),
                        fill: true
                    }
                ]
            },
            options: connChartOptions()
        });

        // Listen for interface selector changes
        document.getElementById("connIfaceSelect").addEventListener("change", () => {
            updateConnChart(lastConnData);
        });
    }

    function connChartOptions() {
        return {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: "index", intersect: false },
            plugins: {
                legend: {
                    display: true,
                    position: "top",
                    labels: {
                        color: "#475569",
                        font: { family: "'Inter', sans-serif", size: 11 },
                        boxWidth: 12,
                        padding: 12
                    }
                },
                tooltip: {
                    backgroundColor: "rgba(255, 255, 255, 0.98)",
                    titleColor: "#1e293b",
                    bodyColor: "#475569",
                    borderColor: "rgba(0,0,0,0.08)",
                    borderWidth: 1,
                    cornerRadius: 8,
                    padding: 10,
                    titleFont: { family: "'Inter', sans-serif", weight: "600", size: 12 },
                    bodyFont: { family: "'Inter', sans-serif", size: 11 },
                    callbacks: {
                        title: (items) => {
                            if (items.length > 0) {
                                const d = new Date(items[0].parsed.x);
                                return d.toLocaleString();
                            }
                            return "";
                        },
                        label: (ctx) => ` ${ctx.dataset.label}: ${ctx.parsed.y}`
                    }
                }
            },
            scales: {
                x: {
                    type: "time",
                    time: { tooltipFormat: "PPpp" },
                    grid: { color: "rgba(0,0,0,0.04)", drawBorder: false },
                    ticks: {
                        color: "#64748b",
                        font: { family: "'Inter', sans-serif", size: 10 },
                        maxRotation: 0,
                        autoSkipPadding: 20,
                        maxTicksLimit: 8
                    }
                },
                y: {
                    beginAtZero: true,
                    grid: { color: "rgba(0,0,0,0.04)", drawBorder: false },
                    ticks: {
                        color: "#64748b",
                        font: { family: "'Inter', sans-serif", size: 10 },
                        maxTicksLimit: 6
                    }
                }
            },
            elements: {
                point: { radius: 0, hoverRadius: 4, hitRadius: 20 },
                line: { tension: 0.4, borderWidth: 2 }
            },
            animation: { duration: 800, easing: "easeInOutQuart" }
        };
    }

    function applyNetMax() {
        const max = getNetMax();
        netChart.options.scales.y.max = max;
        netChart.options.scales.y.ticks.stepSize = max > 200 ? 50 : 10;
        netChart.update();
    }

    // ─── Data Fetching ───────────────────────────────────────────────────────

    async function fetchCurrent() {
        try {
            const res = await fetch("/api/current");
            const data = await res.json();
            updateCurrentStats(data);
        } catch (err) {
            console.error("Failed to fetch current stats:", err);
            document.getElementById("headerSubtitle").textContent = "Connection error...";
        }
    }

    async function fetchMetrics() {
        try {
            const res = await fetch(`/api/metrics?range=${currentRange}`);
            const json = await res.json();
            window.lastMetricsJson = json; // store globally for the updateCharts function
            updateCharts(json.data);
            if (typeof updatePayloadDisplay === 'function') updatePayloadDisplay();
        } catch (err) {
            console.error("Failed to fetch metrics:", err);
        }
    }

    // ─── Update Current Stats ────────────────────────────────────────────────

    function updateCurrentStats(data) {
        // Subtitle
        document.getElementById("headerSubtitle").textContent =
            `Last updated: ${new Date().toLocaleTimeString()}`;

        // CPU Gauge
        setGauge("cpuGaugeFill", data.cpu.percent);
        animateNumber("cpuPercent", data.cpu.percent);
        document.getElementById("cpuCores").textContent = data.cpu.cores + " cores";
        document.getElementById("cpuFreq").textContent = data.cpu.freq_mhz + " MHz";

        const modelEl = document.getElementById("cpuModelName");
        if (modelEl && data.cpu.model) {
            // Clean up long strings like "AMD EPYC 7002-core Processor"
            let niceName = data.cpu.model.replace(" Processor", "").replace("(R)", "").replace("(TM)", "").replace(" CPU", "");
            modelEl.textContent = niceName.trim();
        }

        // Per-Core CPU Cylinders
        if (data.cpu.per_core) {
            renderCpuCores(data.cpu.per_core);
        }

        // RAM Gauge
        setGauge("ramGaugeFill", data.ram.percent);
        animateNumber("ramPercent", data.ram.percent);
        document.getElementById("ramTotal").textContent = data.ram.total_gb + " GB";
        document.getElementById("ramUsed").textContent = data.ram.used_gb + " GB";
        document.getElementById("ramFree").textContent = data.ram.free_gb + " GB";
        document.getElementById("ramShared").textContent = data.ram.shared_gb + " GB";
        document.getElementById("ramBuffCache").textContent = data.ram.buff_cache_gb + " GB";
        document.getElementById("ramAvailable").textContent = data.ram.available_gb + " GB";
        if (data.swap) {
            document.getElementById("ramSwap").textContent = `${data.swap.used_gb} GB / ${data.swap.total_gb} GB`;
        }

        // Disk Gauge
        setGauge("diskGaugeFill", data.disk.percent);
        animateNumber("diskPercent", data.disk.percent);
        document.getElementById("diskUsed").textContent = data.disk.used_gb + " GB";
        document.getElementById("diskFree").textContent = data.disk.free_gb + " GB";

        // Disk I/O
        const formatDiskSpeed = (bps) => bps > 1_048_576 ? (bps / 1_048_576).toFixed(1) + ' MB/s' : (bps / 1024).toFixed(0) + ' KB/s';
        document.getElementById("diskSpeed").textContent = `${formatDiskSpeed(data.disk.read_bps)} / ${formatDiskSpeed(data.disk.write_bps)}`;
        document.getElementById("diskIops").textContent = `${data.disk.read_iops} / ${data.disk.write_iops}`;

        // Network — use selected NIC data from per_nic, fallback to default
        const nicKey = selectedNic || (data.network && data.network.default_nic) || Object.keys(data.per_nic || {})[0] || '';
        let nicData = data.network || {};  // default fallback
        if (nicKey && data.per_nic && data.per_nic[nicKey]) {
            nicData = data.per_nic[nicKey];
        }

        const sentBps = nicData.sent_bps || 0;
        const recvBps = nicData.recv_bps || 0;
        document.getElementById("netSent").textContent = formatBps(sentBps);
        document.getElementById("netRecv").textContent = formatBps(recvBps);

        // Live Packets/sec
        document.getElementById("livePps").innerHTML = `<span style="color:#059669">↑${(nicData.sent_pps || 0).toLocaleString()}</span> &nbsp; <span style="color:#0891b2">↓${(nicData.recv_pps || 0).toLocaleString()}</span>`;

        // Save Boot Totals for the selected NIC
        window.lastBootTotals = {
            sent_gb: nicData.sent_gb || nicData.sent_gb || 0,
            recv_gb: nicData.recv_gb || nicData.recv_gb || 0
        };
        if (typeof updatePayloadDisplay === 'function') {
            const sel = document.getElementById("payloadRange");
            if (sel && sel.value === "boot") {
                updatePayloadDisplay();
            }
        }

        // Live TCP/UDP counts (selected NIC)
        if (data.connections && data.connections[nicKey]) {
            document.getElementById("liveTcpUdp").textContent =
                `${data.connections[nicKey].tcp.toLocaleString()} / ${data.connections[nicKey].udp.toLocaleString()}`;
        } else if (data.connections && data.connections.Total) {
            document.getElementById("liveTcpUdp").textContent =
                `${data.connections.Total.tcp.toLocaleString()} / ${data.connections.Total.udp.toLocaleString()}`;
        }

        // Load
        document.getElementById("load1").textContent = data.system.load_avg_1m;
        document.getElementById("load5").textContent = data.system.load_avg_5m;
        const load15El = document.getElementById("load15");
        if (load15El) load15El.textContent = data.system.load_avg_15m;

        // Uptime
        document.getElementById("uptimeText").textContent = formatUptime(data.system.uptime_seconds);
    }

    function setGauge(elementId, percent) {
        const el = document.getElementById(elementId);
        if (!el) return;
        const offset = CIRCUMFERENCE - (percent / 100) * CIRCUMFERENCE;
        el.style.strokeDashoffset = offset;
    }

    function animateNumber(elementId, targetValue) {
        const el = document.getElementById(elementId);
        if (!el) return;
        // Direct update for 1s refresh — no animation lag
        el.textContent = Math.round(targetValue);
    }

    // ─── Update Charts ───────────────────────────────────────────────────────

    function updateCharts(data) {
        if (!data || data.length === 0) {
            // Show empty state
            document.getElementById("cpuAvgBadge").textContent = "No data yet";
            document.getElementById("ramAvgBadge").textContent = "No data yet";
            document.getElementById("diskAvgBadge").textContent = "No data yet";
            document.getElementById("netAvgBadge").textContent = "No data yet";
            return;
        }

        const timestamps = data.map(d => new Date(d.t * 1000));

        // Calculate explicit min/max bounds based on the selected range
        const maxMs = Date.now();
        const minMs = maxMs - (RANGE_MS[currentRange] || 3600000);

        // CPU Max configuration
        const savedCpuMax = localStorage.getItem("cpuMaxPercent");
        if (savedCpuMax && !isNaN(parseInt(savedCpuMax, 10))) {
            cpuChart.options.scales.y.max = parseInt(savedCpuMax, 10);
        } else {
            cpuChart.options.scales.y.max = 100;
        }

        // CPU
        cpuChart.options.scales.x.min = minMs;
        cpuChart.options.scales.x.max = maxMs;
        const cpuAvgData = data.map((d, i) => ({ x: timestamps[i], y: d.cpu }));
        cpuChart.data.datasets[0].data = cpuAvgData;

        // Process individual CPU cores if extra exists
        const sampleWithCores = data.find(d => d.extra && d.extra.cpu_cores && d.extra.cpu_cores.length > 0);
        if (sampleWithCores) {
            const numCores = sampleWithCores.extra.cpu_cores.length;
            const corePalette = ["#ef4444", "#f97316", "#eab308", "#22c55e", "#14b8a6", "#0ea5e9", "#8b5cf6", "#d946ef", "#f43f5e", "#ff8a65", "#ba68c8"];

            // Ensure cpuChart has enough datasets
            while (cpuChart.data.datasets.length <= numCores) {
                const coreIndex = cpuChart.data.datasets.length - 1;
                const color = corePalette[coreIndex % corePalette.length];
                cpuChart.data.datasets.push({
                    label: `Core ${coreIndex}`,
                    data: [],
                    borderColor: color,
                    backgroundColor: 'transparent',
                    fill: false,
                    borderWidth: 1.5,
                    pointRadius: 0
                });
            }

            // Populate core data
            for (let j = 0; j < numCores; j++) {
                const coreDataPoints = [];
                for (let i = 0; i < data.length; i++) {
                    const d = data[i];
                    if (d.extra && d.extra.cpu_cores && d.extra.cpu_cores.length > j) {
                        coreDataPoints.push({ x: timestamps[i], y: d.extra.cpu_cores[j] });
                    } else {
                        coreDataPoints.push({ x: timestamps[i], y: null });
                    }
                }
                cpuChart.data.datasets[j + 1].data = coreDataPoints;
            }
        }
        cpuChart.update("none");
        const cpuAvg = (data.reduce((s, d) => s + d.cpu, 0) / data.length).toFixed(1);
        document.getElementById("cpuAvgBadge").textContent = `Avg: ${cpuAvg}%`;

        // RAM
        ramChart.options.scales.x.min = minMs;
        ramChart.options.scales.x.max = maxMs;
        const ramData = data.map((d, i) => ({ x: timestamps[i], y: d.ram }));
        ramChart.data.datasets[0].data = ramData;

        // Process advanced RAM metrics if extra exists
        if (data.some(d => d.extra && d.extra.ram_free_gb !== undefined)) {
            const ramMetrics = [
                { label: "Free %", key: "ram_free_gb", color: "#22c55e" },
                { label: "Shared %", key: "ram_shared_gb", color: "#eab308" },
                { label: "Buff/Cache %", key: "ram_buff_cache_gb", color: "#0ea5e9" },
                { label: "Available %", key: "ram_available_gb", color: "#14b8a6" },
                { label: "Swap %", key: "swap_used_gb", color: "#d946ef", forceTotalKey: "swap_total_gb" } // using swap percent if possible
            ];

            while (ramChart.data.datasets.length <= ramMetrics.length) {
                const metricIndex = ramChart.data.datasets.length - 1;
                const metricDef = ramMetrics[metricIndex];
                ramChart.data.datasets.push({
                    label: metricDef.label,
                    data: [],
                    borderColor: metricDef.color,
                    backgroundColor: 'transparent',
                    fill: false,
                    borderWidth: 1.5,
                    pointRadius: 0
                });
            }

            for (let j = 0; j < ramMetrics.length; j++) {
                const metricDef = ramMetrics[j];
                const dataPoints = [];
                for (let i = 0; i < data.length; i++) {
                    const d = data[i];
                    if (d.extra && d.extra[metricDef.key] !== undefined) {
                        let pct = 0;
                        if (metricDef.key === "swap_used_gb") {
                            const swapTotal = d.extra.swap_total_gb || 0;
                            if (swapTotal > 0) {
                                pct = (d.extra[metricDef.key] / swapTotal) * 100;
                            }
                        } else {
                            if (d.ram_total > 0) {
                                pct = (d.extra[metricDef.key] / d.ram_total) * 100;
                            }
                        }
                        dataPoints.push({ x: timestamps[i], y: pct.toFixed(1) });
                    } else {
                        dataPoints.push({ x: timestamps[i], y: null });
                    }
                }
                ramChart.data.datasets[j + 1].data = dataPoints;
            }
        }

        ramChart.update("none");
        const ramAvg = (data.reduce((s, d) => s + d.ram, 0) / data.length).toFixed(1);
        document.getElementById("ramAvgBadge").textContent = `Avg: ${ramAvg}%`;

        // Disk
        diskChart.options.scales.x.min = minMs;
        diskChart.options.scales.x.max = maxMs;
        const diskData = data.map((d, i) => ({ x: timestamps[i], y: d.disk }));
        diskChart.data.datasets[0].data = diskData;
        diskChart.update("none");
        const diskAvg = (data.reduce((s, d) => s + d.disk, 0) / data.length).toFixed(1);
        document.getElementById("diskAvgBadge").textContent = `Avg: ${diskAvg}%`;

        // Network — convert bytes/sec to Mbps, clamped to user max
        const maxMbps = getNetMax();
        const toMbps = (bytesSec) => {
            const mbps = (bytesSec * 8) / 1_000_000;
            return Math.min(mbps, maxMbps);  // clamp to max
        };
        netChart.options.scales.x.min = minMs;
        netChart.options.scales.x.max = maxMs;
        const netSentData = data.map((d, i) => ({ x: timestamps[i], y: toMbps(d.net_sent) }));
        const netRecvData = data.map((d, i) => ({ x: timestamps[i], y: toMbps(d.net_recv) }));
        netChart.data.datasets[0].data = netSentData;
        netChart.data.datasets[1].data = netRecvData;
        applyNetMax();
        const avgSent = (data.reduce((s, d) => s + d.net_sent, 0) / data.length * 8) / 1_000_000;
        const avgRecv = (data.reduce((s, d) => s + d.net_recv, 0) / data.length * 8) / 1_000_000;
        document.getElementById("netAvgBadge").textContent =
            `↑${avgSent.toFixed(1)} Mbps  ↓${avgRecv.toFixed(1)} Mbps`;

        // (Range Total logic removed from header, now handled in payload dropdown)

        // Connections — store and update
        lastConnData = data;
        updateConnChart(data);
    }

    function updateConnChart(data) {
        if (!data || data.length === 0) return;
        const timestamps = data.map(d => new Date(d.t * 1000));
        const iface = document.getElementById("connIfaceSelect").value || "Total";

        // Populate interface dropdown from data (discover available interfaces)
        const allIfaces = new Set();
        data.forEach(d => {
            if (d.conns) Object.keys(d.conns).forEach(k => allIfaces.add(k));
        });
        const select = document.getElementById("connIfaceSelect");
        const currentOptions = new Set([...select.options].map(o => o.value));
        allIfaces.forEach(name => {
            if (!currentOptions.has(name)) {
                const opt = document.createElement("option");
                opt.value = name;
                opt.textContent = name;
                select.appendChild(opt);
            }
        });
        // Restore selection
        select.value = iface;

        // Extract TCP/UDP data for the selected interface
        const tcpData = data.map((d, i) => {
            const c = d.conns && d.conns[iface];
            return { x: timestamps[i], y: c ? c.tcp : 0 };
        });
        const udpData = data.map((d, i) => {
            const c = d.conns && d.conns[iface];
            return { x: timestamps[i], y: c ? c.udp : 0 };
        });

        // Apply explicit min/max bounds
        const maxMs = Date.now();
        const minMs = maxMs - (RANGE_MS[currentRange] || 3600000);
        connChart.options.scales.x.min = minMs;
        connChart.options.scales.x.max = maxMs;

        connChart.data.datasets[0].data = tcpData;
        connChart.data.datasets[1].data = udpData;
        connChart.update("none");

        // Badge
        const lastConns = data[data.length - 1].conns;
        const lastIface = lastConns && lastConns[iface];
        if (lastIface) {
            document.getElementById("connBadge").textContent =
                `TCP: ${lastIface.tcp}  UDP: ${lastIface.udp}`;
        }
    }

    // ─── Helpers ─────────────────────────────────────────────────────────────

    function formatBytes(bytes) {
        if (bytes === 0) return "0 B";
        const k = 1024;
        const sizes = ["B", "KB", "MB", "GB", "TB"];
        const i = Math.floor(Math.log(Math.abs(bytes) || 1) / Math.log(k));
        const idx = Math.min(i, sizes.length - 1);
        return (bytes / Math.pow(k, idx)).toFixed(1) + " " + sizes[idx];
    }

    function formatBps(bitsPerSec) {
        if (bitsPerSec == null || isNaN(bitsPerSec)) return "0 bps";
        if (bitsPerSec < 1000) return bitsPerSec.toFixed(0) + " bps";
        if (bitsPerSec < 1_000_000) return (bitsPerSec / 1_000).toFixed(1) + " Kbps";
        if (bitsPerSec < 1_000_000_000) return (bitsPerSec / 1_000_000).toFixed(2) + " Mbps";
        return (bitsPerSec / 1_000_000_000).toFixed(2) + " Gbps";
    }

    function formatUptime(seconds) {
        const days = Math.floor(seconds / 86400);
        const hours = Math.floor((seconds % 86400) / 3600);
        const mins = Math.floor((seconds % 3600) / 60);

        if (days > 0) return `${days}d ${hours}h ${mins}m`;
        if (hours > 0) return `${hours}h ${mins}m`;
        return `${mins}m`;
    }

    // ─── Range Buttons ───────────────────────────────────────────────────────

    function setupRangeButtons() {
        const buttons = document.querySelectorAll(".range-btn");
        buttons.forEach(btn => {
            btn.addEventListener("click", () => {
                buttons.forEach(b => b.classList.remove("active"));
                btn.classList.add("active");
                currentRange = btn.dataset.range;
                fetchMetrics();
            });
        });
    }

    // ─── Fullscreen Buttons ──────────────────────────────────────────────────

    function setupFullscreenButtons() {
        const buttons = document.querySelectorAll(".btn-fullscreen");
        buttons.forEach(btn => {
            btn.addEventListener("click", () => {
                const card = btn.closest(".chart-card");
                if (!card) return;

                card.classList.toggle("fullscreen");

                const iconExpand = btn.querySelector(".icon-expand");
                const iconCompress = btn.querySelector(".icon-compress");

                if (card.classList.contains("fullscreen")) {
                    if (iconExpand) iconExpand.style.display = "none";
                    if (iconCompress) iconCompress.style.display = "block";
                    // Prevent body scroll when a chart is fullscreen
                    document.body.style.overflow = "hidden";
                } else {
                    if (iconExpand) iconExpand.style.display = "block";
                    if (iconCompress) iconCompress.style.display = "none";
                    // Restore body scroll (assuming no other fullscreen cards)
                    const otherFullscreen = document.querySelector(".chart-card.fullscreen");
                    if (!otherFullscreen) {
                        document.body.style.overflow = "";
                    }
                }

                // Give the browser a moment to apply CSS before triggering resize
                setTimeout(() => window.dispatchEvent(new Event("resize")), 50);
            });
        });
    }

    // ─── Payload Dropdown Handler ────────────────────────────────────────────

    async function updatePayloadDisplay() {
        const val = document.getElementById("payloadRange").value;
        const displayEl = document.getElementById("livePayload");
        if (!displayEl) return;

        if (val === "boot") {
            if (window.lastBootTotals) {
                displayEl.innerHTML = `<span style="color:#059669">↑${window.lastBootTotals.sent_gb.toFixed(2)} GB</span> &nbsp; <span style="color:#0891b2">↓${window.lastBootTotals.recv_gb.toFixed(2)} GB</span>`;
            }
        } else {
            // Check if matches main chart range, save an API call
            if (val === currentRange && window.lastMetricsJson && window.lastMetricsJson.totals) {
                const t = window.lastMetricsJson.totals;
                displayEl.innerHTML = `<span style="color:#059669">↑${t.sent_gb.toFixed(2)} GB</span> &nbsp; <span style="color:#0891b2">↓${t.recv_gb.toFixed(2)} GB</span>`;
                return;
            }

            try {
                const res = await fetch(`/api/metrics?range=${val}`);
                const json = await res.json();
                if (json.totals) {
                    displayEl.innerHTML = `<span style="color:#059669">↑${json.totals.sent_gb.toFixed(2)} GB</span> &nbsp; <span style="color:#0891b2">↓${json.totals.recv_gb.toFixed(2)} GB</span>`;
                }
            } catch (err) { }
        }
    }

    function setupPayloadDropdown() {
        const payloadSel = document.getElementById("payloadRange");
        if (payloadSel) {
            payloadSel.addEventListener("change", updatePayloadDisplay);
        }
    }

    // ─── Auto-Refresh ────────────────────────────────────────────────────────

    function startAutoRefresh() {
        // Live stats every 1 second
        statsTimer = setInterval(fetchCurrent, STATS_INTERVAL);
        // Charts every 30 seconds
        chartTimer = setInterval(fetchMetrics, CHART_INTERVAL);
    }

    // ─── Initialize ──────────────────────────────────────────────────────────

    // ─── Dark Mode Toggle ────────────────────────────────────────────────────

    function isDarkMode() {
        return document.documentElement.getAttribute("data-theme") === "dark";
    }

    function updateChartColors() {
        const gridColor = isDarkMode() ? "rgba(255,255,255,0.07)" : "rgba(0,0,0,0.06)";
        const tickColor = isDarkMode() ? "#94a3b8" : "#64748b";

        // Update all existing chart instances
        Chart.helpers.each(Chart.instances, (chart) => {
            if (!chart.options.scales) return;
            Object.values(chart.options.scales).forEach(scale => {
                if (scale.grid) scale.grid.color = gridColor;
                if (scale.ticks) scale.ticks.color = tickColor;
            });
            chart.update("none");
        });
    }

    function setupThemeToggle() {
        const btn = document.getElementById("themeToggle");
        if (!btn) return;

        // Restore saved preference
        const saved = localStorage.getItem("sm-theme");
        if (saved === "dark") {
            document.documentElement.setAttribute("data-theme", "dark");
            btn.textContent = "☀️";
        }

        btn.addEventListener("click", () => {
            const dark = !isDarkMode();
            document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
            btn.textContent = dark ? "☀️" : "🌙";
            localStorage.setItem("sm-theme", dark ? "dark" : "light");
            updateChartColors();
        });
    }

    // ─── Per-Core CPU Cylinder Rendering ──────────────────────────────────────

    function renderCpuCores(perCoreArray) {
        const grid = document.getElementById("cpuCoresGrid");
        if (!grid) return;

        const TOTAL_LINES = 50;

        // Build or update DOM
        if (grid.children.length !== perCoreArray.length) {
            // Rebuild
            grid.innerHTML = '';
            perCoreArray.forEach((_, idx) => {
                const item = document.createElement('div');
                item.className = 'cpu-core-item';

                const label = document.createElement('div');
                label.className = 'cpu-core-label';
                label.textContent = `C${idx}`;

                const cylinder = document.createElement('div');
                cylinder.className = 'cpu-cylinder';
                cylinder.id = `cpuCyl${idx}`;

                // Create 50 lines (bottom-up: index 0 = bottom, 49 = top)
                for (let i = 0; i < TOTAL_LINES; i++) {
                    const line = document.createElement('div');
                    line.className = 'cpu-cylinder-line empty';
                    cylinder.appendChild(line);
                    // Add tiny spacer between lines
                    if (i < TOTAL_LINES - 1) {
                        const sp = document.createElement('div');
                        sp.className = 'cpu-cylinder-line spacer';
                        cylinder.appendChild(sp);
                    }
                }

                const pct = document.createElement('div');
                pct.className = 'cpu-core-percent';
                pct.id = `cpuCorePct${idx}`;
                pct.textContent = '0%';

                item.appendChild(label);
                item.appendChild(cylinder);
                item.appendChild(pct);
                grid.appendChild(item);
            });
        }

        // Update values
        perCoreArray.forEach((percent, idx) => {
            const cylinder = document.getElementById(`cpuCyl${idx}`);
            const pctEl = document.getElementById(`cpuCorePct${idx}`);
            if (!cylinder || !pctEl) return;

            pctEl.textContent = Math.round(percent) + '%';

            const filledCount = Math.round(percent / 2); // 50 lines, each = 2%
            const lines = cylinder.querySelectorAll('.cpu-cylinder-line:not(.spacer)');

            lines.forEach((line, lineIdx) => {
                if (lineIdx < filledCount) {
                    // Determine color based on line position (lineIdx is 0=bottom)
                    const linePercent = (lineIdx + 1) * 2;
                    let colorClass;
                    if (linePercent <= 60) {
                        colorClass = 'line-green';
                    } else if (linePercent <= 80) {
                        colorClass = 'line-yellow';
                    } else {
                        colorClass = 'line-red';
                    }
                    line.className = `cpu-cylinder-line filled ${colorClass}`;
                } else {
                    line.className = 'cpu-cylinder-line empty';
                }
            });
        });
    }

    // ─── NIC Selection ───────────────────────────────────────────────────────

    async function fetchInterfaces() {
        try {
            const res = await fetch('/api/interfaces');
            const json = await res.json();

            const nicSelect = document.getElementById('nicSelect');
            const connSelect = document.getElementById('connIfaceSelect');
            if (!nicSelect) return;

            nicSelect.innerHTML = '';
            // Add discovered interfaces to both dropdowns
            const ifaces = json.interfaces || [];
            const defaultNic = json.default || '';

            ifaces.forEach(iface => {
                const opt = document.createElement('option');
                opt.value = iface.name;
                opt.textContent = iface.name;
                if (iface.name === defaultNic) opt.selected = true;
                nicSelect.appendChild(opt);
            });

            // Set connSelect default to the detected NIC
            if (connSelect) {
                // Clear existing and rebuild
                const currentVal = connSelect.value;
                connSelect.innerHTML = '';
                ifaces.forEach(iface => {
                    const opt = document.createElement('option');
                    opt.value = iface.name;
                    opt.textContent = iface.name;
                    if (iface.name === defaultNic) opt.selected = true;
                    connSelect.appendChild(opt);
                });
                // Add Total option
                const totalOpt = document.createElement('option');
                totalOpt.value = 'Total';
                totalOpt.textContent = 'Total';
                connSelect.appendChild(totalOpt);
            }

            selectedNic = defaultNic;

            // Listen for NIC change
            nicSelect.addEventListener('change', () => {
                selectedNic = nicSelect.value;
            });
        } catch (err) {
            console.error('Failed to fetch interfaces:', err);
        }
    }

    // ─── CPU Benchmark ───────────────────────────────────────────────────────
    function setupBenchmark() {
        const btn = document.getElementById("btnBenchmarkCpu");
        const btnStop = document.getElementById("btnStopBenchmark");
        const loader = btn ? btn.querySelector(".btn-loader") : null;
        const resultDiv = document.getElementById("benchmarkResult");
        const scoreSpan = document.getElementById("benchmarkScore");

        if (!btn || !btnStop || !loader || !resultDiv || !scoreSpan) return;

        btn.addEventListener("click", async () => {
            // UI Loading State
            btn.disabled = true;
            btn.querySelector('.btn-text').textContent = "Running...";
            loader.classList.remove("hidden");
            btnStop.classList.remove("hidden");
            resultDiv.classList.add("hidden");
            scoreSpan.textContent = "—";

            try {
                const res = await fetch("/api/benchmark/cpu");
                const data = await res.json();

                if (data.error) {
                    scoreSpan.textContent = data.error;
                } else if (data.status === "stopped" || (data.score === 0 && !data.error)) {
                    scoreSpan.textContent = "Stopped";
                } else {
                    scoreSpan.textContent = data.score.toLocaleString();
                }
                resultDiv.classList.remove("hidden");
            } catch (err) {
                console.error(err);
                scoreSpan.textContent = "Error";
                resultDiv.classList.remove("hidden");
            } finally {
                // Restore UI
                btn.disabled = false;
                btn.querySelector('.btn-text').textContent = "Run Benchmark";
                loader.classList.add("hidden");
                btnStop.classList.add("hidden");
            }
        });

        btnStop.addEventListener("click", async () => {
            try {
                // Disable stop button while stopping
                btnStop.disabled = true;
                btnStop.querySelector('.btn-text').textContent = "Stopping...";
                await fetch("/api/benchmark/cpu/stop");
            } catch (err) {
                console.error("Failed to stop benchmark:", err);
            } finally {
                btnStop.disabled = false;
                btnStop.querySelector('.btn-text').textContent = "Stop";
            }
        });
    }

    function setupSettings() {
        const btnOpen = document.getElementById('btnSettingsOpen');
        if (!btnOpen) return; // Wait for correct ID if missing

        const overlay = document.getElementById('settingsOverlay');
        const btnClose = document.getElementById('closeSettings');

        // Open/Close logic
        btnOpen.addEventListener('click', () => {
            overlay.classList.remove('hidden');
        });

        btnClose.addEventListener('click', () => {
            overlay.classList.add('hidden');
        });

        // Close on outside click
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay) {
                overlay.classList.add('hidden');
            }
        });

        // 1. SSL Form Handling
        const sslForm = document.getElementById('sslForm');
        sslForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            const btn = document.getElementById('btnSaveSSL');
            const loader = btn.querySelector('.btn-loader');
            const status = document.getElementById('sslStatus');
            const keyContent = document.getElementById('sslKey').value;
            const certContent = document.getElementById('sslCert').value;

            btn.disabled = true;
            loader.classList.remove('hidden');
            status.textContent = "Saving and scheduling restart...";
            status.style.color = "var(--text-primary)";

            try {
                const res = await fetch("/api/settings", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        certificate_pem: certContent,
                        private_key_pem: keyContent
                    })
                });

                if (res.ok) {
                    status.textContent = "Saved successfully! Panel is restarting in 2 seconds...";
                    status.style.color = "#10b981";
                    setTimeout(() => {
                        window.location.reload();
                    }, 4000);
                } else {
                    const data = await res.json();
                    throw new Error(data.detail || "Server error");
                }
            } catch (err) {
                status.textContent = "Error: " + err.message;
                status.style.color = "#ef4444";
                btn.disabled = false;
                loader.classList.add('hidden');
            }
        });

        // 2. DB Backup Download
        document.getElementById('btnBackupDb').addEventListener('click', () => {
            window.location.href = "/api/backup";
        });

        // 3. DB Restore Upload
        const fileInput = document.getElementById('dbFileInput');
        fileInput.addEventListener('change', async (e) => {
            const file = e.target.files[0];
            if (!file) return;

            const btn = document.getElementById('btnRestoreDb');
            const loader = btn.querySelector('.btn-loader');
            const span = btn.querySelector('.btn-text');

            if (!confirm(`Warning: This will completely replace your current metrics history. The panel will restart immediately.\n\nAre you sure you want to restore ${file.name}?`)) {
                fileInput.value = "";
                return;
            }

            btn.disabled = true;
            loader.classList.remove('hidden');
            span.textContent = "Uploading...";

            const formData = new FormData();
            formData.append("file", file);

            try {
                const res = await fetch("/api/restore", {
                    method: "POST",
                    body: formData
                });

                if (res.ok) {
                    span.textContent = "Restarting Server...";
                    setTimeout(() => {
                        window.location.reload();
                    }, 3000);
                } else {
                    const data = await res.json();
                    throw new Error(data.detail || "Failed to restore");
                }
            } catch (err) {
                alert("Restore failed: " + err.message);
                btn.disabled = false;
                loader.classList.add('hidden');
                span.textContent = "Restore & Restart";
                fileInput.value = "";
            }
        });
    }

    function init() {
        injectSVGGradients();
        setupThemeToggle();
        initCharts();
        setupRangeButtons();
        setupFullscreenButtons();
        setupPayloadDropdown();
        setupBenchmark();
        setupSettings();

        // Fetch interfaces first, then initial data + start timers
        fetchInterfaces().then(() => {
            fetchCurrent();
            fetchMetrics();

            // Apply chart colors for current theme
            setTimeout(updateChartColors, 100);

            // Start auto-refresh AFTER interfaces are loaded
            startAutoRefresh();
        }).catch(() => {
            // Even if interfaces fail, still start the dashboard
            fetchCurrent();
            fetchMetrics();
            setTimeout(updateChartColors, 100);
            startAutoRefresh();
        });
    }

    // Wait for DOM and Chart.js
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
