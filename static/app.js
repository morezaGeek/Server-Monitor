/* ═══════════════════════════════════════════════════════════════════════════
   Server Monitor Dashboard — App Logic
   ═══════════════════════════════════════════════════════════════════════════ */

(() => {
    "use strict";

    // ─── Config ──────────────────────────────────────────────────────────────
    let STATS_INTERVAL = parseInt(localStorage.getItem('uiRefreshInterval')) * 1000 || 3000;
    const CHART_INTERVAL = 30_000;  // charts every 30 seconds
    const CIRCUMFERENCE = 2 * Math.PI * 52; // gauge circle circumference

    // ─── Legend Hover-Bold Helper ─────────────────────────────────────────────
    // Tracks which dataset index is hovered per chart (WeakMap, GC-friendly).
    const _legendHover = new WeakMap();

    /**
     * Returns a Chart.js legend config object identical to `base` but with
     * onHover / onLeave callbacks that bold the hovered item's label text.
     * Merges cleanly with any labels sub-config you pass in.
     */
    function makeLegend(base) {
        const bl = (base.labels || {});
        return {
            ...base,
            onHover(e, item, legend) {
                if (legend.chart.canvas) {
                    legend.chart.canvas.style.cursor = item ? 'pointer' : 'default';
                }
                const idx = (item && typeof item.datasetIndex !== 'undefined') ? item.datasetIndex : -1;
                if (_legendHover.get(legend.chart) !== idx) {
                    if (idx >= 0) {
                        _legendHover.set(legend.chart, idx);
                    } else {
                        _legendHover.delete(legend.chart);
                    }
                    legend.chart.update('none');
                }
            },
            onLeave(e, item, legend) {
                if (legend.chart.canvas) {
                    legend.chart.canvas.style.cursor = 'default';
                }
                if (_legendHover.has(legend.chart)) {
                    _legendHover.delete(legend.chart);
                    legend.chart.update('none');
                }
            },
            labels: {
                ...bl,
                generateLabels(chart) {
                    const hIdx = _legendHover.has(chart) ? _legendHover.get(chart) : -1;
                    const orig = Chart.defaults.plugins.legend.labels.generateLabels(chart);
                    return orig.map(lbl => {
                        const isHovered = (hIdx >= 0 && lbl.datasetIndex === hIdx);
                        lbl.font = {
                            size: bl.font?.size || 11,
                            family: bl.font?.family || "'Inter', sans-serif",
                            weight: isHovered ? 'bold' : 'normal'
                        };
                        return lbl;
                    });
                }
            }
        };
    }

    // ─── Tooltip Helpers ─────────────────────────────────────────────────────
    function formatTooltipLabel(ctx, text) {
        const isHovered = (_legendHover.has(ctx.chart) && _legendHover.get(ctx.chart) === ctx.datasetIndex);
        return isHovered ? `➔ ${text}` : `  ${text}`;
    }

    function getTooltipLabelColor(ctx) {
        const dark = isDarkMode();
        const hasHover = _legendHover.has(ctx.chart);
        if (!hasHover) {
            return dark ? "#94a3b8" : "#475569";
        }
        const isHovered = (_legendHover.get(ctx.chart) === ctx.datasetIndex);
        if (dark) {
            return isHovered ? "#ffffff" : "rgba(255, 255, 255, 0.4)";
        } else {
            return isHovered ? "#0f172a" : "#94a3b8";
        }
    }

    function getTooltipConfig(labelCallback) {
        return {
            backgroundColor: () => isDarkMode() ? "rgba(15, 23, 42, 0.95)" : "rgba(255, 255, 255, 0.98)",
            titleColor: () => isDarkMode() ? "#f8fafc" : "#1e293b",
            bodyColor: () => isDarkMode() ? "#94a3b8" : "#475569",
            borderColor: () => isDarkMode() ? "rgba(255, 255, 255, 0.1)" : "rgba(0, 0, 0, 0.08)",
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
                label: labelCallback,
                labelTextColor: (ctx) => getTooltipLabelColor(ctx)
            }
        };
    }

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
    let customSeconds = null; // null = use preset range, number = custom seconds
    let statsTimer = null;
    let chartTimer = null;
    let selectedNic = null;  // will be set from /api/interfaces

    // ─── Smoothing (Moving Average) ─────────────────────────────────────────
    function getSmoothWindow() {
        const el = document.getElementById("smoothLevel");
        return el ? (parseInt(el.value, 10) || 0) : 0;
    }

    /**
     * Apply a simple centered moving average to an array of {x, y} points.
     * Returns a new array with smoothed y values; x values are preserved.
     * window=0 means no smoothing (returns original data).
     */
    function movingAverage(data, window) {
        if (!window || window < 2 || data.length < window) return data;
        const half = Math.floor(window / 2);
        return data.map((point, i) => {
            if (point.y === null || point.y === undefined) return point;
            let sum = 0;
            let count = 0;
            for (let j = Math.max(0, i - half); j <= Math.min(data.length - 1, i + half); j++) {
                if (data[j].y !== null && data[j].y !== undefined) {
                    sum += parseFloat(data[j].y);
                    count++;
                }
            }
            return { x: point.x, y: count > 0 ? sum / count : point.y };
        });
    }

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
        onHover(event, activeElements, chart) {
            const nativeEvt = event.native || event;
            const nearest = chart.getElementsAtEventForMode(nativeEvt, 'nearest', { intersect: false }, true);
            const idx = (nearest && nearest[0]) ? nearest[0].datasetIndex : -1;
            
            chart.data.datasets.forEach((dataset, i) => {
                if (typeof dataset._origBorderWidth === 'undefined') {
                    dataset._origBorderWidth = dataset.borderWidth || 2;
                }
                const isHovered = (idx >= 0 && i === idx);
                dataset.borderWidth = isHovered ? (dataset._origBorderWidth + 1.5) : dataset._origBorderWidth;
            });
            
            if (_legendHover.get(chart) !== idx) {
                if (idx >= 0) {
                    _legendHover.set(chart, idx);
                } else {
                    _legendHover.delete(chart);
                }
            }
            
            if (!chart.canvas._hasMouseOutListener) {
                chart.canvas._hasMouseOutListener = true;
                chart.canvas.addEventListener('mouseout', () => {
                    chart.data.datasets.forEach((ds) => {
                        if (typeof ds._origBorderWidth !== 'undefined') {
                            ds.borderWidth = ds._origBorderWidth;
                        }
                    });
                    _legendHover.delete(chart);
                    chart.update('none');
                });
            }
            
            chart.update('none');
        },
        plugins: {
            legend: makeLegend({
                display: forceLegend || !isPercent,
                position: "top",
                labels: {
                    color: "#475569",
                    font: { family: "'Inter', sans-serif", size: 11 },
                    boxWidth: 12,
                    padding: 12
                }
            }),
            tooltip: getTooltipConfig((ctx) => {
                const val = ctx.parsed.y;
                const labelText = isPercent ? `${ctx.dataset.label}: ${val.toFixed(1)}%` : `${ctx.dataset.label}: ${val.toFixed(2)} Mbps`;
                return formatTooltipLabel(ctx, labelText);
            })
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

    let cpuChart, ramChart, diskChart, diskIopsChart, netChart, connChart;
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
                    label: "Used %",
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
                datasets: [
                    {
                        label: "Disk %",
                        data: [],
                        borderColor: COLORS.disk.start,
                        backgroundColor: "transparent",
                        fill: false,
                        yAxisID: "y"
                    },
                    {
                        label: "Read",
                        data: [],
                        borderColor: "#10b981", // Emerald
                        backgroundColor: "transparent",
                        fill: false,
                        yAxisID: "y1",
                        borderWidth: 1.5,
                        pointRadius: 0
                    },
                    {
                        label: "Write",
                        data: [],
                        borderColor: "#ef4444", // Red
                        backgroundColor: "transparent",
                        fill: false,
                        yAxisID: "y1",
                        borderWidth: 1.5,
                        pointRadius: 0
                    }
                ]
            },
            options: {
                ...chartOptions("Disk", false, true),
                interaction: {
                    mode: 'index',
                    intersect: false,
                },
                plugins: {
                    legend: makeLegend({
                        display: true,
                        position: 'top',
                        align: 'end',
                        labels: { boxWidth: 12, usePointStyle: true, font: { size: 10 } }
                    }),
                    tooltip: getTooltipConfig((ctx) => {
                        const val = ctx.parsed.y;
                        if (val === null) return null;
                        let labelText = "";
                        if (ctx.dataset.yAxisID === "y") {
                            labelText = `Usage: ${val.toFixed(1)}%`;
                        } else {
                            labelText = `${ctx.dataset.label}: ${val.toFixed(1)} MB/s`;
                        }
                        return formatTooltipLabel(ctx, labelText);
                    })
                },
                scales: {
                    x: {
                        type: "time",
                        time: { tooltipFormat: "PPpp" },
                        grid: { color: "rgba(0,0,0,0.04)", drawBorder: false },
                        ticks: { color: "#64748b", font: { family: "'Inter', sans-serif", size: 10 }, maxRotation: 0, autoSkipPadding: 20, maxTicksLimit: 8 }
                    },
                    y: {
                        type: 'linear',
                        display: true,
                        position: 'left',
                        beginAtZero: true,
                        max: 100,
                        title: { display: true, text: '% Usage', font: { size: 10 } },
                        grid: { color: "rgba(0,0,0,0.04)", drawBorder: false },
                        ticks: { color: "#64748b", font: { size: 10 }, callback: val => val + "%" }
                    },
                    y1: {
                        type: 'linear',
                        display: true,
                        position: 'right',
                        beginAtZero: true,
                        title: { display: true, text: 'Speed (MB/s)', font: { size: 10 } },
                        grid: { drawOnChartArea: false }, // Prevent gridline overlap
                        ticks: { color: "#64748b", font: { size: 10 }, callback: val => val.toFixed(1) + " MB" }
                    }
                }
            }
        });

        const diskIopsCtx = document.getElementById("diskIopsChart").getContext("2d");
        diskIopsChart = new Chart(diskIopsCtx, {
            type: "line",
            data: {
                datasets: [
                    {
                        label: "Read IOPS",
                        data: [],
                        borderColor: "#8b5cf6", // Violet
                        backgroundColor: "transparent",
                        fill: false,
                        yAxisID: "y",
                        borderWidth: 1.5,
                        pointRadius: 0
                    },
                    {
                        label: "Write IOPS",
                        data: [],
                        borderColor: "#f59e0b", // Amber
                        backgroundColor: "transparent",
                        fill: false,
                        yAxisID: "y",
                        borderWidth: 1.5,
                        pointRadius: 0
                    }
                ]
            },
            options: {
                ...chartOptions("IOPS", false, true),
                interaction: {
                    mode: 'index',
                    intersect: false,
                },
                plugins: {
                    legend: makeLegend({
                        display: true,
                        position: 'top',
                        align: 'end',
                        labels: { boxWidth: 12, usePointStyle: true, font: { size: 10 } }
                    }),
                    tooltip: getTooltipConfig((ctx) => {
                        const val = ctx.parsed.y;
                        if (val === null) return null;
                        const labelText = `${ctx.dataset.label}: ${Math.round(val)}`;
                        return formatTooltipLabel(ctx, labelText);
                    })
                },
                scales: {
                    x: {
                        type: "time",
                        time: { tooltipFormat: "PPpp" },
                        grid: { color: "rgba(0,0,0,0.04)", drawBorder: false },
                        ticks: { color: "#64748b", font: { family: "'Inter', sans-serif", size: 10 }, maxRotation: 0, autoSkipPadding: 20, maxTicksLimit: 8 }
                    },
                    y: {
                        type: 'linear',
                        display: true,
                        position: 'left',
                        beginAtZero: true,
                        suggestedMax: 10,
                        title: { display: true, text: 'Operations/sec', font: { size: 10 } },
                        grid: { color: "rgba(0,0,0,0.04)", drawBorder: false },
                        ticks: { color: "#64748b", font: { size: 10 }, callback: val => Math.round(val) }
                    }
                }
            }
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
            onHover(event, activeElements, chart) {
                const nativeEvt = event.native || event;
                const nearest = chart.getElementsAtEventForMode(nativeEvt, 'nearest', { intersect: false }, true);
                const idx = (nearest && nearest[0]) ? nearest[0].datasetIndex : -1;
                
                chart.data.datasets.forEach((dataset, i) => {
                    if (typeof dataset._origBorderWidth === 'undefined') {
                        dataset._origBorderWidth = dataset.borderWidth || 2;
                    }
                    const isHovered = (idx >= 0 && i === idx);
                    dataset.borderWidth = isHovered ? (dataset._origBorderWidth + 1.5) : dataset._origBorderWidth;
                });
                
                if (_legendHover.get(chart) !== idx) {
                    if (idx >= 0) {
                        _legendHover.set(chart, idx);
                    } else {
                        _legendHover.delete(chart);
                    }
                }
                
                if (!chart.canvas._hasMouseOutListener) {
                    chart.canvas._hasMouseOutListener = true;
                    chart.canvas.addEventListener('mouseout', () => {
                        chart.data.datasets.forEach((ds) => {
                            if (typeof ds._origBorderWidth !== 'undefined') {
                                ds.borderWidth = ds._origBorderWidth;
                            }
                        });
                        _legendHover.delete(chart);
                        chart.update('none');
                    });
                }
                
                chart.update('none');
            },
            plugins: {
                legend: makeLegend({
                    display: true,
                    position: "top",
                    labels: {
                        color: "#475569",
                        font: { family: "'Inter', sans-serif", size: 11 },
                        boxWidth: 12,
                        padding: 12
                    }
                }),
                tooltip: getTooltipConfig((ctx) => {
                    const val = ctx.parsed.y;
                    const labelText = `${ctx.dataset.label}: ${val !== null && val !== undefined ? Math.round(val) : 0}`;
                    return formatTooltipLabel(ctx, labelText);
                })
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
            let url;
            if (customSeconds !== null) {
                url = `/api/metrics?seconds=${customSeconds}`;
            } else {
                url = `/api/metrics?range=${currentRange}`;
            }
            const res = await fetch(url);
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
        // Hostname Badge
        if (data.system && data.system.hostname) {
            const hostEl = document.getElementById("serverHostname");
            if (hostEl) {
                hostEl.textContent = data.system.hostname;
                hostEl.style.display = "inline-block";
            }
        }

        // Check for updates
        if (data.system && data.system.version) {
            checkForUpdates(data.system.version);
        }

        // Subtitle
        document.getElementById("headerSubtitle").textContent =
            `Last updated: ${new Date().toLocaleTimeString()}`;

        // Panel Version
        if (data.system && data.system.version) {
            const verEl = document.getElementById("panelVersion");
            if (verEl) verEl.textContent = "v" + data.system.version;
        }

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

        // Public IPs (only update when value changes to avoid flicker)
        if (data.public_ips) {
            const ipv4El = document.getElementById("publicIpv4");
            const ipv6El = document.getElementById("publicIpv6");
            if (ipv4El && data.public_ips.ipv4 && ipv4El.textContent !== data.public_ips.ipv4) {
                ipv4El.textContent = data.public_ips.ipv4;
            }
            if (ipv6El && data.public_ips.ipv6 && ipv6El.textContent !== data.public_ips.ipv6) {
                ipv6El.textContent = data.public_ips.ipv6;
            }
        }
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
        const rangeMs = (customSeconds !== null) ? (customSeconds * 1000) : (RANGE_MS[currentRange] || 3600000);
        const minMs = maxMs - rangeMs;

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
        cpuChart.data.datasets[0].data = movingAverage(cpuAvgData, getSmoothWindow());

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
                cpuChart.data.datasets[j + 1].data = movingAverage(coreDataPoints, getSmoothWindow());
            }
        }
        cpuChart.update("none");
        const cpuAvg = (data.reduce((s, d) => s + d.cpu, 0) / data.length).toFixed(1);
        document.getElementById("cpuAvgBadge").textContent = `Avg: ${cpuAvg}%`;

        // RAM
        ramChart.options.scales.x.min = minMs;
        ramChart.options.scales.x.max = maxMs;
        const ramData = data.map((d, i) => ({ x: timestamps[i], y: d.ram }));
        ramChart.data.datasets[0].data = movingAverage(ramData, getSmoothWindow());

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
                ramChart.data.datasets[j + 1].data = movingAverage(dataPoints, getSmoothWindow());
            }
        }

        ramChart.update("none");
        const ramAvg = (data.reduce((s, d) => s + d.ram, 0) / data.length).toFixed(1);
        document.getElementById("ramAvgBadge").textContent = `Avg: ${ramAvg}%`;

        // Disk
        diskChart.options.scales.x.min = minMs;
        diskChart.options.scales.x.max = maxMs;
        const diskData = data.map((d, i) => ({ x: timestamps[i], y: d.disk }));

        // Extract and process Disk I/O metrics
        const diskReadData = [];
        const diskWriteData = [];
        const diskReadIopsData = [];
        const diskWriteIopsData = [];

        for (let i = 0; i < data.length; i++) {
            const d = data[i];
            if (d.extra && d.extra.disk_read_bps !== undefined) {
                // Convert bytes/sec to MB/sec
                const readMBps = d.extra.disk_read_bps / 1_048_576;
                const writeMBps = d.extra.disk_write_bps / 1_048_576;
                diskReadData.push({ x: timestamps[i], y: readMBps });
                diskWriteData.push({ x: timestamps[i], y: writeMBps });
                diskReadIopsData.push({ x: timestamps[i], y: d.extra.disk_read_iops || 0 });
                diskWriteIopsData.push({ x: timestamps[i], y: d.extra.disk_write_iops || 0 });
            } else {
                diskReadData.push({ x: timestamps[i], y: null });
                diskWriteData.push({ x: timestamps[i], y: null });
                diskReadIopsData.push({ x: timestamps[i], y: null });
                diskWriteIopsData.push({ x: timestamps[i], y: null });
            }
        }

        // Calculate dynamic max for disk speed (Y1) with 20% padding
        let maxDiskSpd = 1; // start with an absolute minimum scale
        for (let i = 0; i < diskReadData.length; i++) {
            if (diskReadData[i].y > maxDiskSpd) maxDiskSpd = diskReadData[i].y;
            if (diskWriteData[i].y > maxDiskSpd) maxDiskSpd = diskWriteData[i].y;
        }
        diskChart.options.scales.y1.max = maxDiskSpd * 1.2;

        const sw = getSmoothWindow();
        diskChart.data.datasets[0].data = movingAverage(diskData, sw);
        diskChart.data.datasets[1].data = movingAverage(diskReadData, sw);
        diskChart.data.datasets[2].data = movingAverage(diskWriteData, sw);
        diskChart.update("none");

        // Calculate dynamic max for disk IOPS with 20% padding
        let maxIops = 10;
        for (let i = 0; i < diskReadIopsData.length; i++) {
            if (diskReadIopsData[i].y > maxIops) maxIops = diskReadIopsData[i].y;
            if (diskWriteIopsData[i].y > maxIops) maxIops = diskWriteIopsData[i].y;
        }
        diskIopsChart.options.scales.y.max = maxIops * 1.2;

        diskIopsChart.options.scales.x.min = minMs;
        diskIopsChart.options.scales.x.max = maxMs;
        diskIopsChart.data.datasets[0].data = movingAverage(diskReadIopsData, getSmoothWindow());
        diskIopsChart.data.datasets[1].data = movingAverage(diskWriteIopsData, getSmoothWindow());
        diskIopsChart.update("none");

        const diskAvg = (data.reduce((s, d) => s + d.disk, 0) / data.length).toFixed(1);
        document.getElementById("diskAvgBadge").textContent = `Avg: ${diskAvg}%`;

        // Network — convert bytes/sec to Mbps (no clamping; chart y.max handles scale)
        const toMbps = (bytesSec) => (bytesSec * 8) / 1_000_000;
        netChart.options.scales.x.min = minMs;
        netChart.options.scales.x.max = maxMs;
        const netSentData = data.map((d, i) => ({ x: timestamps[i], y: toMbps(d.net_sent) }));
        const netRecvData = data.map((d, i) => ({ x: timestamps[i], y: toMbps(d.net_recv) }));
        const nsw = getSmoothWindow();
        netChart.data.datasets[0].data = movingAverage(netSentData, nsw);
        netChart.data.datasets[1].data = movingAverage(netRecvData, nsw);
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
        const rangeMs = (customSeconds !== null) ? (customSeconds * 1000) : (RANGE_MS[currentRange] || 3600000);
        const minMs = maxMs - rangeMs;
        connChart.options.scales.x.min = minMs;
        connChart.options.scales.x.max = maxMs;

        const csw = getSmoothWindow();
        connChart.data.datasets[0].data = movingAverage(tcpData, csw);
        connChart.data.datasets[1].data = movingAverage(udpData, csw);
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
        const allRangeBtns = document.querySelectorAll(".range-btn[data-range]");
        const customBtn = document.getElementById("btnCustomRange");
        const popover = document.getElementById("customRangePopover");
        const customInput = document.getElementById("customRangeValue");
        const customUnit = document.getElementById("customRangeUnit");
        const applyBtn = document.getElementById("btnApplyCustomRange");

        // Helper: format seconds to human-readable label for custom button
        function formatCustomLabel(secs) {
            if (secs >= 86400) {
                const d = Math.round(secs / 86400);
                return d + "d";
            }
            const h = Math.round(secs / 3600);
            return h + "h";
        }

        // Helper: clear all active states and set custom
        function activateCustom(seconds) {
            customSeconds = seconds;
            currentRange = null;
            allRangeBtns.forEach(b => b.classList.remove("active"));
            customBtn.classList.add("active");
            customBtn.querySelector("svg").nextSibling.textContent = " " + formatCustomLabel(seconds);
            popover.classList.remove("show");
            fetchMetrics();
        }

        // Preset range buttons
        allRangeBtns.forEach(btn => {
            btn.addEventListener("click", () => {
                customSeconds = null;
                allRangeBtns.forEach(b => b.classList.remove("active"));
                customBtn.classList.remove("active");
                // Reset custom button text
                const svgEl = customBtn.querySelector("svg");
                if (svgEl && svgEl.nextSibling) svgEl.nextSibling.textContent = " Custom";
                btn.classList.add("active");
                currentRange = btn.dataset.range;
                popover.classList.remove("show");
                fetchMetrics();
            });
        });

        // Toggle custom popover
        if (customBtn) {
            customBtn.addEventListener("click", (e) => {
                e.stopPropagation();
                popover.classList.toggle("show");
            });
        }

        // Apply button
        if (applyBtn) {
            applyBtn.addEventListener("click", () => {
                const val = parseInt(customInput.value, 10);
                const unit = parseInt(customUnit.value, 10);
                if (val > 0) {
                    const secs = Math.min(val * unit, 7776000); // cap 90 days
                    activateCustom(secs);
                }
            });
        }

        // Enter key on input
        if (customInput) {
            customInput.addEventListener("keydown", (e) => {
                if (e.key === "Enter") {
                    applyBtn.click();
                }
            });
        }

        // Quick presets
        document.querySelectorAll(".custom-preset").forEach(btn => {
            btn.addEventListener("click", () => {
                const hours = parseInt(btn.dataset.hours, 10);
                activateCustom(hours * 3600);
            });
        });

        // Close popover on click outside
        document.addEventListener("click", (e) => {
            if (popover && !popover.contains(e.target) && e.target !== customBtn && !customBtn.contains(e.target)) {
                popover.classList.remove("show");
            }
        });

        // Smoothing level change — re-render charts with last data
        const smoothSelect = document.getElementById("smoothLevel");
        if (smoothSelect) {
            // Restore saved value
            const saved = localStorage.getItem("smoothLevel");
            if (saved !== null) smoothSelect.value = saved;

            smoothSelect.addEventListener("change", () => {
                localStorage.setItem("smoothLevel", smoothSelect.value);
                // Re-render with existing data
                if (window.lastMetricsJson && window.lastMetricsJson.data) {
                    updateCharts(window.lastMetricsJson.data);
                }
            });
        }
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

    let topProcessesTimer = null;
    let topProcInterval = 1000;

    async function fetchTopProcesses() {
        try {
            const res = await fetch("/api/top_processes");
            const data = await res.json();

            const tbody = document.getElementById("topProcessesBody");
            if (!tbody) return;

            if (data.length === 0) {
                tbody.innerHTML = `<tr><td colspan="5" style="text-align: center; color: var(--text-muted); padding: 20px;">No processes found</td></tr>`;
                return;
            }

            // Build HTML
            const rowsHtml = data.map(p => {
                let cpuClass = "proc-cpu-low";
                if (p.cpu > 50) cpuClass = "proc-cpu-high";
                else if (p.cpu > 15) cpuClass = "proc-cpu-med";

                return `
                <tr>
                    <td class="proc-pid">#${p.pid}</td>
                    <td class="proc-name">${p.name}</td>
                    <td class="proc-user">${p.user}</td>
                    <td style="text-align: right;" class="proc-val">${p.mem}</td>
                    <td style="text-align: right;" class="proc-val ${cpuClass}">${p.cpu.toFixed(1)}%</td>
                </tr>
               `;
            }).join('');

            tbody.innerHTML = rowsHtml;
        } catch (err) {
            console.error("Failed to fetch top processes:", err);
        }
    }

    function setupTopProcInterval() {
        const select = document.getElementById("topProcRefresh");
        if (select) {
            select.addEventListener("change", (e) => {
                topProcInterval = parseInt(e.target.value, 10) || 1000;
                if (topProcessesTimer) {
                    clearInterval(topProcessesTimer);
                    fetchTopProcesses(); // fetch immediately
                    topProcessesTimer = setInterval(fetchTopProcesses, topProcInterval);
                }
            });
        }
    }

    function setupGlobalRefreshInterval() {
        const select = document.getElementById("globalRefreshSelect");
        if (select) {
            select.value = (STATS_INTERVAL / 1000).toString();
            
            // Sync with backend on startup
            fetch(`/api/settings/interval?seconds=${select.value}`, { method: 'POST' }).catch(e => console.error(e));

            select.addEventListener("change", (e) => {
                const val = parseInt(e.target.value, 10);
                if (val) {
                    localStorage.setItem('uiRefreshInterval', val);
                    STATS_INTERVAL = val * 1000;
                    
                    fetch(`/api/settings/interval?seconds=${val}`, { method: 'POST' }).catch(e => console.error(e));

                    if (statsTimer) {
                        clearInterval(statsTimer);
                        fetchCurrent();
                        statsTimer = setInterval(fetchCurrent, STATS_INTERVAL);
                    }
                    
                    window.dispatchEvent(new CustomEvent('globalRefreshChanged', { detail: STATS_INTERVAL }));
                }
            });
        }
    }

    function startAutoRefresh() {
        setupGlobalRefreshInterval();
        // Live stats every X seconds
        statsTimer = setInterval(fetchCurrent, STATS_INTERVAL);
        // Charts every 30 seconds
        chartTimer = setInterval(fetchMetrics, CHART_INTERVAL);

        // Top processes
        setupTopProcInterval();
        fetchTopProcesses();
        topProcessesTimer = setInterval(fetchTopProcesses, topProcInterval);
    }

    // ─── Initialize ──────────────────────────────────────────────────────────

    // ─── Dark Mode Toggle ────────────────────────────────────────────────────

    function isDarkMode() {
        const t = document.documentElement.getAttribute("data-theme") || "dark";
        const lightThemes = ["light", "catppuccin-latte", "solarized-light", "gruvbox-light", "material-light", "rose-pine-dawn"];
        return !lightThemes.includes(t);
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
        const btn = document.getElementById("themeDropdownBtn");
        const menu = document.getElementById("themeMenu");
        const nameDisplay = document.getElementById("currentThemeName");
        if (!btn || !menu || !nameDisplay) return;

        let currentTheme = localStorage.getItem("sm-theme") || "dark";
        document.documentElement.setAttribute("data-theme", currentTheme);

        const options = menu.querySelectorAll(".theme-option");

        function updateDisplay(themeVal) {
            options.forEach(opt => {
                if (opt.getAttribute("data-value") === themeVal) {
                    nameDisplay.innerHTML = opt.innerHTML;
                    opt.classList.add("selected");
                } else {
                    opt.classList.remove("selected");
                }
            });
        }

        updateDisplay(currentTheme);

        // Toggle menu
        btn.addEventListener("click", (e) => {
            e.stopPropagation();
            const expanded = btn.getAttribute("aria-expanded") === "true";
            btn.setAttribute("aria-expanded", !expanded);
            btn.classList.toggle("active");
            menu.classList.toggle("show");
        });

        // Close when clicking outside
        document.addEventListener("click", (e) => {
            if (!menu.contains(e.target) && !btn.contains(e.target)) {
                btn.setAttribute("aria-expanded", "false");
                btn.classList.remove("active");
                menu.classList.remove("show");
            }
        });

        // Handle selection
        options.forEach(opt => {
            opt.addEventListener("click", (e) => {
                e.stopPropagation();
                const selectedVal = opt.getAttribute("data-value");
                currentTheme = selectedVal;

                document.documentElement.setAttribute("data-theme", currentTheme);
                updateDisplay(currentTheme);
                localStorage.setItem("sm-theme", currentTheme);
                updateChartColors();

                btn.setAttribute("aria-expanded", "false");
                btn.classList.remove("active");
                menu.classList.remove("show");
            });

            // Live preview on hover
            opt.addEventListener("mouseenter", () => {
                const previewVal = opt.getAttribute("data-value");
                document.documentElement.setAttribute("data-theme", previewVal);
                updateChartColors();
            });

            // Revert preview on mouse leave
            opt.addEventListener("mouseleave", () => {
                document.documentElement.setAttribute("data-theme", currentTheme);
                updateChartColors();
            });
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
    // ─── CPU/System Benchmark ───────────────────────────────────────────────────
    function setupBenchmark() {
        const btn = document.getElementById("btnBenchmarkCpu");
        const btnStopMain = document.getElementById("btnStopBenchmark"); // main page stop
        const btnStopModal = document.getElementById("btnStopBenchmarkModal"); // modal stop
        
        const overlay = document.getElementById("benchmarkOverlay");
        const closeBtn = document.getElementById("closeBenchmark");
        
        const runningState = document.getElementById("benchmarkRunningState");
        const resultState = document.getElementById("benchmarkResultState");
        
        const progressBar = document.getElementById("benchmarkProgressBar");
        const progressPercent = document.getElementById("benchmarkProgressPercent");
        const progressText = document.getElementById("benchmarkProgressText");
        
        // Modal Results
        const modalOverallScore = document.getElementById("modalOverallScore");
        const scoreCpuSingle = document.getElementById("scoreCpuSingle");
        const metaCpuSingle = document.getElementById("metaCpuSingle");
        const scoreCpuMulti = document.getElementById("scoreCpuMulti");
        const metaCpuMulti = document.getElementById("metaCpuMulti");
        const scoreRam = document.getElementById("scoreRam");
        const metaRam = document.getElementById("metaRam");
        const scoreDisk = document.getElementById("scoreDisk");
        const metaDisk = document.getElementById("metaDisk");
        
        // Also main page results
        const mainResultDiv = document.getElementById("benchmarkResult");
        const mainScoreSpan = document.getElementById("benchmarkScore");

        if (!btn || !overlay || !closeBtn || !runningState || !resultState || !progressBar || !progressPercent || !progressText) return;

        let progressInterval = null;
        let activeProgress = 0;
        let isRunning = false;

        // Helper: Reset and open modal
        function openModal() {
            overlay.classList.remove("hidden");
            document.body.style.overflow = "hidden"; // disable scroll
            
            // Show running state, hide results
            runningState.classList.remove("hidden");
            resultState.classList.add("hidden");
            
            progressBar.style.width = "0%";
            progressPercent.textContent = "0%";
            progressText.textContent = "Initializing benchmark...";
            activeProgress = 0;
        }

        // Helper: Close modal
        function closeModal() {
            if (isRunning) return; // don't close while running unless stopped
            overlay.classList.add("hidden");
            document.body.style.overflow = ""; // restore scroll
        }

        closeBtn.addEventListener("click", closeModal);
        overlay.addEventListener("click", (e) => {
            if (e.target === overlay) closeModal();
        });

        // Helper: Smoothly animate overall score counter
        function animateScore(element, endVal) {
            let current = 0;
            if (endVal <= 0) {
                element.textContent = "0";
                return;
            }
            const duration = 1200; // 1.2s total duration
            const startTime = performance.now();

            function update(now) {
                const elapsed = now - startTime;
                const progress = Math.min(elapsed / duration, 1);
                
                // Ease out cubic
                const easeProgress = 1 - Math.pow(1 - progress, 3);
                current = Math.floor(easeProgress * endVal);
                element.textContent = current.toLocaleString();

                if (progress < 1) {
                    requestAnimationFrame(update);
                } else {
                    element.textContent = endVal.toLocaleString();
                }
            }
            requestAnimationFrame(update);
        }

        // Helper: Stop benchmark
        async function stopBenchmark() {
            try {
                if (btnStopMain) {
                    btnStopMain.disabled = true;
                    btnStopMain.querySelector('.btn-text').textContent = "Stopping...";
                }
                if (btnStopModal) {
                    btnStopModal.disabled = true;
                    btnStopModal.querySelector('.btn-text').textContent = "Stopping...";
                }
                
                await fetch("/api/benchmark/cpu/stop");
                
                // Clear interval
                if (progressInterval) clearInterval(progressInterval);
                progressText.textContent = "Benchmark stopped by user.";
                
                isRunning = false;
                setTimeout(() => {
                    overlay.classList.add("hidden");
                    document.body.style.overflow = "";
                    
                    // Reset buttons
                    if (btnStopMain) {
                        btnStopMain.disabled = false;
                        btnStopMain.querySelector('.btn-text').textContent = "Stop";
                        btnStopMain.classList.add("hidden");
                    }
                    if (btnStopModal) {
                        btnStopModal.disabled = false;
                        btnStopModal.querySelector('.btn-text').textContent = "Stop Benchmark";
                    }
                    btn.disabled = false;
                    btn.querySelector('.btn-text').textContent = "Run Benchmark";
                }, 1000);
            } catch (err) {
                console.error("Failed to stop benchmark:", err);
            }
        }

        btn.addEventListener("click", async () => {
            if (isRunning) return;
            isRunning = true;
            openModal();

            // Set main layout button state
            btn.disabled = true;
            btn.querySelector('.btn-text').textContent = "Running...";
            if (btnStopMain) btnStopMain.classList.remove("hidden");

            // Start smooth progress loading bar
            // Total benchmark takes ~6.5 seconds.
            // 6500ms / 100 steps = 65ms per 1% increment.
            progressInterval = setInterval(() => {
                if (activeProgress < 99) {
                    activeProgress += 1;
                    progressBar.style.width = activeProgress + "%";
                    progressPercent.textContent = activeProgress + "%";

                    // Dynamic stages messages based on progress
                    if (activeProgress < 30) {
                        progressText.textContent = `Running Single-Core CPU test... (${activeProgress}%)`;
                    } else if (activeProgress < 60) {
                        progressText.textContent = `Running Multi-Core CPU test... (${activeProgress}%)`;
                    } else if (activeProgress < 80) {
                        progressText.textContent = `Measuring Memory (RAM) speed... (${activeProgress}%)`;
                    } else {
                        progressText.textContent = `Measuring Disk Read/Write speed... (${activeProgress}%)`;
                    }
                }
            }, 65);

            try {
                const res = await fetch("/api/benchmark/cpu");
                const data = await res.json();

                if (progressInterval) clearInterval(progressInterval);

                if (data.error) {
                    progressText.textContent = data.error;
                    if (mainScoreSpan) mainScoreSpan.textContent = "Error";
                    isRunning = false;
                } else if (data.status === "stopped") {
                    progressText.textContent = "Benchmark stopped.";
                    if (mainScoreSpan) mainScoreSpan.textContent = "Stopped";
                    isRunning = false;
                } else {
                    // Set progress to 100%
                    progressBar.style.width = "100%";
                    progressPercent.textContent = "100%";
                    progressText.textContent = "Benchmark complete! Loading results...";

                    setTimeout(() => {
                        // Switch modal views
                        runningState.classList.add("hidden");
                        resultState.classList.remove("hidden");
                        
                        // Populate sub-scores and stats in overlay
                        scoreCpuSingle.textContent = `${data.cpu_single_score} pts`;
                        metaCpuSingle.textContent = `${data.cpu_single_val.toLocaleString()} real-world tasks/sec`;
                        
                        scoreCpuMulti.textContent = `${data.cpu_multi_score} pts`;
                        const labelCpuMulti = document.getElementById("labelCpuMulti");
                        if (data.cores === 1) {
                            if (labelCpuMulti) labelCpuMulti.textContent = "CPU Stress Test";
                            metaCpuMulti.textContent = `${data.cpu_multi_val.toLocaleString()} tasks/sec (Sustained)`;
                        } else {
                            if (labelCpuMulti) labelCpuMulti.textContent = "CPU Multi-Core";
                            const scaling = (data.cpu_single_val > 0) ? (data.cpu_multi_val / data.cpu_single_val).toFixed(1) : "0";
                            metaCpuMulti.textContent = `${data.cpu_multi_val.toLocaleString()} tasks/sec (${scaling}x speedup)`;
                        }
                        
                        scoreRam.textContent = `${data.ram_score} pts`;
                        metaRam.textContent = `${data.ram_val_gbps} GB/s Read-Write`;
                        
                        scoreDisk.textContent = `${data.disk_score} pts`;
                        metaDisk.textContent = `Write: ${data.disk_write_mbps} MB/s | Read: ${data.disk_read_mbps} MB/s`;

                        // Animate overall score circular counter
                        animateScore(modalOverallScore, data.score);

                        // Also update the main dashboard card score
                        if (mainScoreSpan) mainScoreSpan.textContent = data.score.toLocaleString();
                        if (mainResultDiv) mainResultDiv.classList.remove("hidden");

                        isRunning = false;
                    }, 500);
                }
            } catch (err) {
                console.error(err);
                if (progressInterval) clearInterval(progressInterval);
                progressText.textContent = "An error occurred during testing.";
                if (mainScoreSpan) mainScoreSpan.textContent = "Error";
                isRunning = false;
            } finally {
                // Restore main dashboard buttons
                btn.disabled = false;
                btn.querySelector('.btn-text').textContent = "Run Benchmark";
                if (btnStopMain) btnStopMain.classList.add("hidden");
            }
        });

        // Click listeners for stop
        if (btnStopMain) {
            btnStopMain.addEventListener("click", stopBenchmark);
        }
        if (btnStopModal) {
            btnStopModal.addEventListener("click", stopBenchmark);
        }
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

    function setupV2rayMonitor() {
        const btnOpen = document.getElementById('btnV2rayOpen');
        const overlay = document.getElementById('v2rayOverlay');
        const btnClose = document.getElementById('closeV2ray');
        const searchInput = document.getElementById('v2raySearch');
        const tableBody = document.getElementById('v2rayTableBody');
        const activeCountSpan = document.getElementById('v2rayActiveCount');
        const totalCountSpan = document.getElementById('v2rayTotalCount');

        if (!btnOpen || !overlay) return;

        let pollInterval = null;
        let countdownInterval = null;
        let lastUpdateSec = 0;
        let cachedUsers = [];

        // Open
        btnOpen.addEventListener('click', () => {
            overlay.classList.remove('hidden');
            fetchV2rayUsers();
            // Start polling every 2000ms
            if (!pollInterval) {
                pollInterval = setInterval(fetchV2rayUsers, 2000);
            }
            if (!countdownInterval) {
                countdownInterval = setInterval(tickCountdown, 1000);
            }
        });

        // Close
        function closeOverlay() {
            overlay.classList.add('hidden');
            if (pollInterval) {
                clearInterval(pollInterval);
                pollInterval = null;
            }
            if (countdownInterval) {
                clearInterval(countdownInterval);
                countdownInterval = null;
            }
            lastUpdateSec = 0;
            const timerSpan = document.getElementById('v2rayTimer');
            if (timerSpan) {
                timerSpan.textContent = '5s';
                timerSpan.style.color = '#6366f1';
            }
        }

        btnClose.addEventListener('click', closeOverlay);

        overlay.addEventListener('click', (e) => {
            if (e.target === overlay) {
                closeOverlay();
            }
        });

        // Search Filter
        searchInput.addEventListener('input', () => {
            renderTable();
        });

        function updateSyncTimer(backendLastUpdate) {
            if (backendLastUpdate > lastUpdateSec) {
                const timerBadge = document.getElementById('v2rayTimerContainer');
                if (timerBadge && lastUpdateSec > 0) { // Don't flash on first load
                    timerBadge.style.transition = 'none';
                    timerBadge.style.borderColor = '#10b981';
                    timerBadge.style.background = 'rgba(16, 185, 129, 0.1)';
                    setTimeout(() => {
                        timerBadge.style.transition = 'border-color 0.5s ease, background 0.5s ease';
                        timerBadge.style.borderColor = 'var(--border-color)';
                        timerBadge.style.background = 'var(--bg-card)';
                    }, 800);
                }
            }
            lastUpdateSec = backendLastUpdate;
            tickCountdown();
        }

        function tickCountdown() {
            if (!lastUpdateSec) return;
            const nowSec = Date.now() / 1000;
            const elapsed = Math.floor(nowSec - lastUpdateSec);
            const remaining = Math.max(0, 5 - elapsed);
            
            const timerSpan = document.getElementById('v2rayTimer');
            if (timerSpan) {
                if (remaining === 0) {
                    timerSpan.textContent = 'Syncing...';
                    timerSpan.style.color = '#ef4444';
                } else {
                    timerSpan.textContent = remaining + 's';
                    timerSpan.style.color = '#6366f1';
                }
            }
        }

        async function fetchV2rayUsers() {
            try {
                const res = await fetch("/api/v2ray/users");
                if (res.ok) {
                    const data = await res.json();
                    if (data.error) {
                        tableBody.innerHTML = `<tr><td colspan="7" style="text-align: center; color: #ef4444; padding: 20px;">${data.error}</td></tr>`;
                        return;
                    }
                    cachedUsers = data.users || [];
                    const backendLastUpdate = data.last_update || 0;
                    updateSyncTimer(backendLastUpdate);
                    renderTable();
                } else {
                    console.error("Failed to fetch V2ray users: " + res.statusText);
                }
            } catch (err) {
                console.error("Error fetching V2ray users:", err);
            }
        }

        function formatSpeed(mbps) {
            if (mbps >= 1.0) {
                return `${mbps.toFixed(2)} Mbps`;
            } else {
                return `${(mbps * 1024).toFixed(0)} Kbps`;
            }
        }

        function formatTraffic(gb) {
            if (gb >= 1.0) {
                return `${gb.toFixed(2)} GB`;
            } else {
                return `${(gb * 1024).toFixed(1)} MB`;
            }
        }

        function formatLastActive(lastOnline, isOnline) {
            if (isOnline) return '<span style="color: #10b981; font-weight: bold;">Online</span>';
            if (!lastOnline) return '<span style="color: var(--text-secondary);">Never</span>';
            const date = new Date(lastOnline);
            const now = new Date();
            const diffMs = now - date;
            const diffMins = Math.floor(diffMs / 60000);

            if (diffMins < 1) return 'Just now';
            if (diffMins < 60) return `${diffMins}m ago`;
            const diffHours = Math.floor(diffMins / 60);
            if (diffHours < 24) return `${diffHours}h ago`;
            return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
        }

        function renderTable() {
            const query = searchInput.value.toLowerCase().trim();
            
            // Filter users
            const filtered = cachedUsers.filter(u => {
                return u.email && u.email.toLowerCase().includes(query);
            });

            // Sort by download speed descending, then upload speed
            filtered.sort((a, b) => b.down_speed_mbps - a.down_speed_mbps || b.up_speed_mbps - a.up_speed_mbps);

            // Update stats
            const onlineCount = cachedUsers.filter(u => u.is_online).length;
            activeCountSpan.textContent = onlineCount;
            totalCountSpan.textContent = cachedUsers.length;

            if (filtered.length === 0) {
                tableBody.innerHTML = `<tr><td colspan="7" style="text-align: center; color: var(--text-secondary); padding: 30px;">No users found</td></tr>`;
                return;
            }

            tableBody.innerHTML = filtered.map(u => {
                const isOnline = u.is_online;
                const statusDot = isOnline 
                    ? '<span class="pulse-dot" style="display: inline-block; margin-right: 8px; vertical-align: middle;"></span>' 
                    : '<span style="display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #ef4444; margin-right: 8px; vertical-align: middle;"></span>';
                
                const speedDownText = isOnline ? formatSpeed(u.down_speed_mbps) : formatSpeed(0);
                const speedUpText = isOnline ? formatSpeed(u.up_speed_mbps) : formatSpeed(0);
                
                // Highlight active speeds
                const speedDownStyle = (isOnline && u.down_speed_mbps > 0.05) ? 'color: #10b981; font-weight: bold;' : '';
                const speedUpStyle = (isOnline && u.up_speed_mbps > 0.05) ? 'color: #3b82f6; font-weight: bold;' : '';

                // Limit display: if total_limit_gb is 0.0, it means unlimited
                const limitText = u.total_limit_gb > 0 ? ` / ${u.total_limit_gb} GB` : '';
                const totalDownText = formatTraffic(u.total_down_gb);
                const totalUpText = formatTraffic(u.total_up_gb);

                return `
                    <tr style="${!u.enable ? 'opacity: 0.5;' : ''}">
                        <td style="display: flex; align-items: center; font-weight: 500;">
                            ${statusDot}
                            <span>${u.email}</span>
                            ${!u.enable ? ' <span style="font-size: 0.75rem; color: #ef4444; background: rgba(239, 68, 68, 0.1); padding: 2px 6px; border-radius: 4px; margin-left: 8px;">Disabled</span>' : ''}
                        </td>
                        <td style="text-align: right; ${speedDownStyle}">${speedDownText}</td>
                        <td style="text-align: right; ${speedUpStyle}">${speedUpText}</td>
                        <td style="text-align: right;">${totalDownText}${limitText}</td>
                        <td style="text-align: right;">${totalUpText}</td>
                        <td style="text-align: center; font-weight: ${u.unique_ips > 1 ? 'bold' : 'normal'}; color: ${u.unique_ips > 2 ? '#f59e0b' : 'inherit'};">${u.unique_ips || 0}</td>
                        <td style="text-align: center; font-size: 0.85rem;">${formatLastActive(u.last_online, isOnline)}</td>
                    </tr>
                `;
            }).join('');
        }
    }

    function setupVirtualBrowser() {
        const btnManage = document.getElementById('btnManageBrowser');
        const overlay = document.getElementById('browserOverlay');
        const btnClose = document.getElementById('closeBrowser');

        if (!btnManage || !overlay) return;

        // Open/Close logic
        btnManage.addEventListener('click', () => {
            overlay.classList.remove('hidden');
            checkBrowserStatus();
        });

        btnClose.addEventListener('click', () => {
            overlay.classList.add('hidden');
        });

        overlay.addEventListener('click', (e) => {
            if (e.target === overlay) {
                overlay.classList.add('hidden');
            }
        });

        // Elements
        const badge = document.getElementById('browserStatusBadge');
        const btnInstall = document.getElementById('btnBrowserInstall');
        const btnUninstall = document.getElementById('btnBrowserUninstall');
        const btnClear = document.getElementById('btnBrowserClear');
        const btnStart = document.getElementById('btnBrowserStart');
        const btnStop = document.getElementById('btnBrowserStop');
        const btnOpen = document.getElementById('btnBrowserOpen');
        const logsPre = document.getElementById('browserLogs');
        const logsBox = document.getElementById('browserLogBox');
        let ws = null;

        function connectLogsWS() {
            if (ws && ws.readyState !== WebSocket.CLOSED) return;
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            ws = new WebSocket(`${protocol}//${window.location.host}/api/browser/ws`);
            ws.onmessage = (event) => {
                logsPre.textContent += event.data + "\n";
                // Max lines keep
                const lines = logsPre.textContent.split('\n');
                if (lines.length > 200) {
                    logsPre.textContent = lines.slice(-200).join('\n');
                }
                logsBox.scrollTop = logsBox.scrollHeight;
            };
            ws.onclose = () => { setTimeout(connectLogsWS, 3000); };
        }

        async function checkBrowserStatus() {
            try {
                const res = await fetch("/api/browser/status");
                if (res.ok) {
                    const data = await res.json();
                    updateBrowserUI(data);
                }
            } catch (err) {
                console.error("Failed to fetch browser status:", err);
            }
        }

        function updateBrowserUI(data) {
            // data.state = "not_installed", "running", "stopped"
            if (data.state === "running") {
                badge.textContent = "Running";
                badge.className = "badge badge-success";
                btnInstall.style.display = "none";
                btnStart.style.display = "none";
                btnStop.style.display = "flex";
                btnUninstall.style.display = "block";
                btnOpen.disabled = false;
            } else if (data.state === "stopped") {
                badge.textContent = "Stopped";
                badge.className = "badge badge-warning";
                btnInstall.style.display = "none";
                btnStart.style.display = "flex";
                btnStop.style.display = "none";
                btnUninstall.style.display = "block";
                btnOpen.disabled = true;
            } else {
                badge.textContent = "Not Installed";
                badge.className = "badge badge-error";
                btnInstall.style.display = "flex";
                btnStart.style.display = "none";
                btnStop.style.display = "none";
                btnUninstall.style.display = "none";
                btnOpen.disabled = true;
            }
        }

        async function performAction(action) {
            connectLogsWS();
            const config = {
                user: document.getElementById('browserUser').value || "admin",
                pass: document.getElementById('browserPass').value || "admin",
                res: document.getElementById('browserRes').value,
                quality: document.getElementById('browserQuality').value
            };

            const theBtn = action === 'install' ? btnInstall : (action === 'start' ? btnStart : (action === 'stop' ? btnStop : (action === 'clear_cache' ? btnClear : btnUninstall)));
            const originalText = theBtn.textContent;
            theBtn.disabled = true;

            // Handle element structure for button loaders
            if (theBtn.querySelector('.btn-text')) {
                theBtn.querySelector('.btn-text').textContent = "Working...";
            } else {
                theBtn.textContent = "Working...";
            }

            try {
                const res = await fetch("/api/browser/action", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ action, config })
                });
                if (res.ok) {
                    const data = await res.json();
                    updateBrowserUI(data);
                } else {
                    const err = await res.json();
                    alert("Error: " + (err.detail || "Action failed"));
                }
            } catch (err) {
                alert("Network error: " + err.message);
            } finally {
                theBtn.disabled = false;
                if (theBtn.querySelector('.btn-text')) {
                    theBtn.querySelector('.btn-text').textContent = action === 'install' ? "Install / Update" : "Working..."; // fallback
                } else {
                    theBtn.textContent = originalText;
                }
            }
        }

        btnInstall.addEventListener('click', () => performAction('install'));
        btnUninstall.addEventListener('click', () => {
            if (confirm("Uninstall Virtual Browser? This will remove the container.")) performAction('uninstall');
        });
        btnClear.addEventListener('click', () => {
            if (confirm("Clear Docker cache and completely remove Chromium images?")) performAction('clear_cache');
        });
        btnStart.addEventListener('click', () => performAction('start'));
        btnStop.addEventListener('click', () => performAction('stop'));

        btnOpen.addEventListener('click', () => {
            window.open("https://" + window.location.host + "/browser/", "_blank");
        });

        // Initial fetch
        checkBrowserStatus();
    }

    // ─── Detached Self Update & Reinstall ────────────────────────────────────
    function setupSelfUpdate() {
        const btnUpdate = document.getElementById("btnUpdatePanel");
        const btnReinstall = document.getElementById("btnReinstallPanel");
        const overlay = document.getElementById("updateOverlay");
        const statusText = document.getElementById("updateStatusText");
        const progressBar = document.getElementById("updateProgressBar");
        const progressPercent = document.getElementById("updateProgressPercent");

        if (!overlay || !statusText || !progressBar || !progressPercent) return;

        let updateInterval = null;
        let pollInterval = null;
        let currentProgress = 0;
        let isUpdating = false;

        async function checkServerOnline() {
            try {
                // Fetch simple lightweight endpoint without caching
                const res = await fetch("/api/current?t=" + Date.now());
                if (res.status === 200) {
                    return true;
                }
            } catch (err) {
                // Server is down
            }
            return false;
        }

        function startPolling() {
            let attempts = 0;
            const maxAttempts = 30; // 60 seconds max polling

            pollInterval = setInterval(async () => {
                attempts += 1;
                statusText.textContent = `Waiting for server to restart... Attempt ${attempts}/${maxAttempts}`;

                const online = await checkServerOnline();
                if (online) {
                    clearInterval(pollInterval);
                    if (updateInterval) clearInterval(updateInterval);
                    
                    progressBar.style.width = "100%";
                    progressPercent.textContent = "100%";
                    statusText.textContent = "Server is back online! Refreshing...";
                    
                    setTimeout(() => {
                        window.location.reload();
                    }, 1000);
                } else if (attempts >= maxAttempts) {
                    clearInterval(pollInterval);
                    if (updateInterval) clearInterval(updateInterval);
                    statusText.textContent = "Update timed out. Please refresh manually.";
                    // Enable close click
                    overlay.addEventListener("click", () => {
                        overlay.classList.add("hidden");
                        document.body.style.overflow = "";
                    });
                }
            }, 2000);
        }

        async function triggerAction(skipGit) {
            if (isUpdating) return;
            const actionText = skipGit ? "نصب مجدد (Reinstall)" : "به‌روزرسانی (Update)";
            const confirmed = confirm(`آیا مایل به ${actionText} مانیتور سرور هستید؟\nاین عملیات حدود ۲۰ تا ۳۰ ثانیه زمان می‌برد و پنل به طور خودکار ریستارت خواهد شد.`);
            if (!confirmed) return;

            isUpdating = true;
            overlay.classList.remove("hidden");
            document.body.style.overflow = "hidden";
            progressBar.style.width = "0%";
            progressPercent.textContent = "0%";
            currentProgress = 0;
            statusText.textContent = "Contacting backend and launching detached updater...";

            // Smooth fake progress bar for visual feedback (0 to 95% over 22 seconds)
            // 22000ms / 95 steps = ~230ms per 1% increment
            updateInterval = setInterval(() => {
                if (currentProgress < 95) {
                    currentProgress += 1;
                    progressBar.style.width = currentProgress + "%";
                    progressPercent.textContent = currentProgress + "%";
                    
                    if (currentProgress < 30) {
                        statusText.textContent = skipGit ? `Analyzing local files... (${currentProgress}%)` : `Fetching latest files from GitHub... (${currentProgress}%)`;
                    } else if (currentProgress < 60) {
                        statusText.textContent = `Installing Python dependencies... (${currentProgress}%)`;
                    } else {
                        statusText.textContent = `Rebuilding systemd service... (${currentProgress}%)`;
                    }
                }
            }, 230);

            try {
                await fetch(`/api/update?skip_git=${skipGit}`, {
                    method: "POST"
                });
                
                // If successful or if it fails due to network termination, start polling
                startPolling();
            } catch (err) {
                // Connection reset due to systemd restart
                console.log("Connection reset due to server restart. Starting offline polling...");
                startPolling();
            }
        }

        if (btnUpdate) {
            btnUpdate.addEventListener("click", () => triggerAction(false));
        }
        if (btnReinstall) {
            btnReinstall.addEventListener("click", () => triggerAction(true));
        }
    }

    let hasCheckedForUpdates = false;
    function checkForUpdates(currentVersion) {
        if (!currentVersion) return;
        if (hasCheckedForUpdates) return;
        hasCheckedForUpdates = true;

        fetch("https://api.github.com/repos/morezaGeek/Server-Monitor/releases/latest")
            .then(res => res.json())
            .then(release => {
                if (release && release.tag_name) {
                    const latest = release.tag_name.replace(/^v/, "");
                    const current = currentVersion.replace(/^v/, "");
                    
                    const parseVersion = (v) => v.split(".").map(Number);
                    const currParts = parseVersion(current);
                    const lateParts = parseVersion(latest);
                    
                    let hasNewUpdate = false;
                    for (let i = 0; i < Math.max(currParts.length, lateParts.length); i++) {
                        const c = currParts[i] || 0;
                        const l = lateParts[i] || 0;
                        if (l > c) {
                            hasNewUpdate = true;
                            break;
                        } else if (c > l) {
                            break;
                        }
                    }

                    if (hasNewUpdate) {
                        const btnUpdate = document.getElementById("btnUpdatePanel");
                        if (btnUpdate) {
                            let badge = document.getElementById("updateBadge");
                            if (!badge) {
                                badge = document.createElement("span");
                                badge.id = "updateBadge";
                                badge.className = "update-available-badge";
                                badge.textContent = `New: v${latest}`;
                                btnUpdate.style.position = "relative";
                                btnUpdate.appendChild(badge);
                            }
                        }
                    }
                }
            })
            .catch(err => console.log("Failed to check GitHub releases:", err));
    }

    function setupSpeedTest() {
        const btnSpeedTest = document.getElementById("btnSpeedTest");
        const serverSelect = document.getElementById("speedtestServer");
        const pingSpan = document.getElementById("speedtestPing");
        const downSpan = document.getElementById("speedtestDownload");
        const upSpan = document.getElementById("speedtestUpload");
        const btnText = document.getElementById("btnSpeedTestText");
        const btnLoader = document.getElementById("btnSpeedTestLoader");

        if (!btnSpeedTest || !serverSelect) return;

        let isRunning = false;

        // Fetch servers
        fetch("/api/speedtest/servers")
            .then(res => res.json())
            .then(data => {
                if (data.status === "success" && data.servers) {
                    serverSelect.innerHTML = '<option value="">Auto Select Best</option>';
                    data.servers.forEach(srv => {
                        const opt = document.createElement("option");
                        opt.value = srv.id;
                        opt.textContent = `${srv.sponsor} - ${srv.name} (${srv.country})`;
                        serverSelect.appendChild(opt);
                    });
                } else {
                    serverSelect.innerHTML = '<option value="">Failed to load servers</option>';
                }
            })
            .catch(err => {
                serverSelect.innerHTML = '<option value="">Error loading servers</option>';
            });

        btnSpeedTest.addEventListener("click", async () => {
            if (isRunning) return;
            isRunning = true;
            btnSpeedTest.disabled = true;
            btnText.textContent = "Testing...";
            btnLoader.classList.remove("hidden");

            pingSpan.textContent = "Testing...";
            downSpan.textContent = "Testing...";
            upSpan.textContent = "Testing...";

            const serverId = serverSelect.value;
            let url = "/api/speedtest/run";
            if (serverId) url += `?server_id=${encodeURIComponent(serverId)}`;

            try {
                const res = await fetch(url);
                const data = await res.json();
                if (data.status === "success" && data.result) {
                    pingSpan.textContent = `${data.result.ping.toFixed(1)} ms`;
                    downSpan.textContent = `${data.result.download.toFixed(2)} Mbps`;
                    upSpan.textContent = `${data.result.upload.toFixed(2)} Mbps`;
                } else {
                    pingSpan.textContent = "Error";
                    downSpan.textContent = "Error";
                    upSpan.textContent = "Error";
                    alert(data.error || "Speed test failed");
                }
            } catch (err) {
                console.error("Speed test fetch error:", err);
                pingSpan.textContent = "Error";
                downSpan.textContent = "Error";
                upSpan.textContent = "Error";
                alert("Network error: " + err.message);
            } finally {
                isRunning = false;
                btnSpeedTest.disabled = false;
                btnText.textContent = "Run Test";
                btnLoader.classList.add("hidden");
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
        setupVirtualBrowser();
        setupV2rayMonitor();
        setupSelfUpdate();
        setupSpeedTest();

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
