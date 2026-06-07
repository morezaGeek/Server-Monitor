(function () {
    let throughputChart = null;
    let payloadChart = null;
    let activeService = "google"; // Default active service
    let activeSubService = "google"; // Default sub-service (Total)
    let historicalData = [];
    let realTimeHistory = []; // Local cache of last 60 points for line chart
    let pollIntervalId = null;

    // Helper to get CSS variables for styling charts
    function getThemeColors() {
        const bodyStyle = getComputedStyle(document.body);
        const theme = document.documentElement.getAttribute("data-theme") || "dark";
        const lightThemes = ["light", "catppuccin-latte", "solarized-light", "gruvbox-light", "material-light", "rose-pine-dawn"];
        const isDark = !lightThemes.includes(theme);
        
        // Use browser computed text color for 100% accurate contrast
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

    // Format speed value dynamically
    function formatSpeed(mbps) {
        if (mbps === undefined || mbps === null) return "0.00 Mbps";
        if (mbps < 0.1) {
            // Show Kbps
            return (mbps * 1024).toFixed(0) + " Kbps";
        }
        return mbps.toFixed(2) + " Mbps";
    }

    // Format bytes value dynamically
    function formatBytes(bytes) {
        if (!bytes) return "0.00 MB";
        const mb = bytes / (1024 * 1024);
        if (mb >= 1024) {
            return (mb / 1024).toFixed(2) + " GB";
        }
        return mb.toFixed(1) + " MB";
    }

    // Initialize the line and bar charts
    function initCharts() {
        const colors = getThemeColors();

        // 1. Throughput Line Chart
        const ctxThroughput = document.getElementById("serviceThroughputChart");
        if (ctxThroughput) {
            throughputChart = new Chart(ctxThroughput, {
                type: "line",
                data: {
                    datasets: [
                        {
                            label: "Download (Speed)",
                            borderColor: colors.down,
                            backgroundColor: colors.downGlow,
                            borderWidth: 2,
                            pointRadius: 0,
                            pointHoverRadius: 5,
                            fill: true,
                            tension: 0.4,
                            data: []
                        },
                        {
                            label: "Upload (Speed)",
                            borderColor: colors.up,
                            backgroundColor: colors.upGlow,
                            borderWidth: 2,
                            pointRadius: 0,
                            pointHoverRadius: 5,
                            fill: true,
                            tension: 0.4,
                            data: []
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: {
                            display: true,
                            labels: { color: colors.text, font: { family: "Outfit, Inter, sans-serif", size: 11 } }
                        },
                        tooltip: {
                            mode: "index",
                            intersect: false,
                            backgroundColor: "rgba(15, 23, 42, 0.9)",
                            titleColor: "#f8fafc",
                            bodyColor: "#cbd5e1",
                            borderColor: "rgba(99, 102, 241, 0.3)",
                            borderWidth: 1,
                            callbacks: {
                                label: function (context) {
                                    return context.dataset.label.split(" ")[0] + ": " + formatSpeed(context.parsed.y);
                                }
                            }
                        }
                    },
                    scales: {
                        x: {
                            type: "time",
                            time: {
                                displayFormats: {
                                    second: "HH:mm:ss",
                                    minute: "HH:mm",
                                    hour: "HH:mm",
                                    day: "MMM d",
                                    month: "MMM yyyy"
                                }
                            },
                            grid: { display: false },
                            ticks: { color: colors.muted, maxRotation: 0, autoSkip: true, maxTicksLimit: 10 }
                        },
                        y: {
                            grid: { color: colors.grid },
                            border: { dash: [4, 4] },
                            ticks: {
                                color: colors.muted,
                                callback: function (value) {
                                    return formatSpeed(value);
                                }
                            },
                            min: 0
                        }
                    }
                }
            });
        }

        // 2. Payload Bar Chart
        const ctxPayload = document.getElementById("servicePayloadChart");
        if (ctxPayload) {
            payloadChart = new Chart(ctxPayload, {
                type: "bar",
                data: {
                    datasets: [
                        {
                            label: "Download",
                            backgroundColor: colors.down,
                            borderRadius: 4,
                            data: []
                        },
                        {
                            label: "Upload",
                            backgroundColor: colors.up,
                            borderRadius: 4,
                            data: []
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: {
                            display: true,
                            labels: { color: colors.text, font: { family: "Outfit, Inter, sans-serif", size: 11 } }
                        },
                        tooltip: {
                            mode: "index",
                            intersect: false,
                            backgroundColor: "rgba(15, 23, 42, 0.9)",
                            callbacks: {
                                label: function (context) {
                                    const val = context.raw.y * 1024 * 1024; // Convert MB back to bytes for formatter
                                    return context.dataset.label + ": " + formatBytes(val);
                                }
                            }
                        }
                    },
                    scales: {
                        x: {
                            type: "time",
                            time: {
                                displayFormats: {
                                    second: "HH:mm:ss",
                                    minute: "HH:mm",
                                    hour: "HH:mm",
                                    day: "MMM d",
                                    month: "MMM yyyy"
                                }
                            },
                            grid: { display: false },
                            ticks: { color: colors.muted, maxRotation: 0, autoSkip: true, maxTicksLimit: 10 },
                            stacked: true
                        },
                        y: {
                            grid: { color: colors.grid },
                            border: { dash: [4, 4] },
                            ticks: {
                                color: colors.muted,
                                callback: function (value) {
                                    // value is in MB
                                    if (value >= 1024) {
                                        return (value / 1024).toFixed(1) + " GB";
                                    }
                                    return value.toFixed(0) + " MB";
                                }
                            },
                            stacked: true,
                            min: 0
                        }
                    }
                }
            });
        }
    }

    // Fetch and redraw historical charts
    async function loadHistoricalMetrics() {
        const range = document.getElementById("serviceRangeSelect").value;
        try {
            const res = await fetch(`/api/services/metrics?range=${range}`);
            if (!res.ok) throw new Error("API error fetching service metrics");
            
            historicalData = await res.json();
            
            // Build the initial real-time history from the tail of the historical data
            // Filter historical data for the active target service
            const target = getActiveQueryTarget();
            const serviceHist = historicalData.filter(d => d.service === target);
            
            // Use last 60 points (10 minutes of history at 10s intervals)
            const tail = serviceHist.slice(-60);
            
            // Calculate a reasonable interval duration between points
            let interval = 10; // default raw database resolution is 10s
            if (tail.length > 1) {
                const diff = tail[tail.length - 1].t - tail[0].t;
                interval = Math.max(5, Math.round(diff / (tail.length - 1)));
            }

            realTimeHistory = tail.map(d => ({
                t: new Date(d.t * 1000),
                down: (d.down * 8) / (interval * 1024 * 1024), // to Mbps
                up: (d.up * 8) / (interval * 1024 * 1024)    // to Mbps
            }));

            updateCharts();
        } catch (err) {
            console.error("Error loading service historical metrics:", err);
        }
    }

    // Get current query target based on active selections
    function getActiveQueryTarget() {
        if (activeService === "google") {
            return activeSubService;
        }
        return activeService;
    }

    // Update active charts with loaded data
    function updateCharts() {
        const target = getActiveQueryTarget();
        const colors = getThemeColors();

        // 1. Redraw Line Chart from realTimeHistory
        if (throughputChart) {
            throughputChart.data.datasets[0].borderColor = colors.down;
            throughputChart.data.datasets[0].backgroundColor = colors.downGlow;
            throughputChart.data.datasets[1].borderColor = colors.up;
            throughputChart.data.datasets[1].backgroundColor = colors.upGlow;

            throughputChart.data.datasets[0].data = realTimeHistory.map(pt => ({ x: pt.t, y: pt.down }));
            throughputChart.data.datasets[1].data = realTimeHistory.map(pt => ({ x: pt.t, y: pt.up }));
            
            if (realTimeHistory.length > 0) {
                const now = new Date();
                const minTime = new Date(now.getTime() - 10 * 60 * 1000);
                throughputChart.options.scales.x.min = minTime;
                throughputChart.options.scales.x.max = now;
                throughputChart.options.scales.x.time.unit = "second";
            }
            throughputChart.update("none");
        }

        // 2. Redraw Bar Chart from historicalData
        if (payloadChart) {
            const serviceHist = historicalData.filter(d => d.service === target);
            
            payloadChart.data.datasets[0].backgroundColor = colors.down;
            payloadChart.data.datasets[1].backgroundColor = colors.up;

            // map data into Chart.js coordinate objects (value in MB)
            payloadChart.data.datasets[0].data = serviceHist.map(d => ({ x: new Date(d.t * 1000), y: d.down / (1024 * 1024) }));
            payloadChart.data.datasets[1].data = serviceHist.map(d => ({ x: new Date(d.t * 1000), y: d.up / (1024 * 1024) }));

            // Accumulate total payload for display
            let totalDown = 0;
            let totalUp = 0;
            serviceHist.forEach(d => {
                totalDown += d.down;
                totalUp += d.up;
            });
            
            document.getElementById("serviceTotalPayload").innerText = formatBytes(totalDown + totalUp);

            // Set X-axis time settings depending on selected range
            const range = document.getElementById("serviceRangeSelect").value;
            if (range === "1h" || range === "2h") {
                payloadChart.options.scales.x.time.unit = "minute";
            } else if (range === "6h" || range === "12h" || range === "1d") {
                payloadChart.options.scales.x.time.unit = "hour";
            } else {
                payloadChart.options.scales.x.time.unit = "day";
            }
            payloadChart.update();
        }
    }

    // Periodically query current throughput speeds
    async function startRealTimePoll() {
        if (pollIntervalId) clearInterval(pollIntervalId);

        async function poll() {
            try {
                const res = await fetch("/api/services/current");
                if (!res.ok) throw new Error("API error polling current speed");
                const currentSpeeds = await res.json();

                const target = getActiveQueryTarget();
                const speedObj = currentSpeeds[target] || { down_mbps: 0, up_mbps: 0 };

                // Update text stats
                document.getElementById("serviceDownSpeed").innerText = formatSpeed(speedObj.down_mbps);
                document.getElementById("serviceUpSpeed").innerText = formatSpeed(speedObj.up_mbps);

                // Add to realTimeHistory
                const now = new Date();
                realTimeHistory.push({
                    t: now,
                    down: speedObj.down_mbps,
                    up: speedObj.up_mbps
                });

                // Keep only points from the last 10 minutes
                const cutoff = new Date(now.getTime() - 10 * 60 * 1000);
                realTimeHistory = realTimeHistory.filter(pt => pt.t >= cutoff);

                // Update line chart datasets directly (snappy rendering)
                if (throughputChart) {
                    throughputChart.data.datasets[0].data = realTimeHistory.map(pt => ({ x: pt.t, y: pt.down }));
                    throughputChart.data.datasets[1].data = realTimeHistory.map(pt => ({ x: pt.t, y: pt.up }));
                    
                    const minTime = new Date(now.getTime() - 10 * 60 * 1000);
                    throughputChart.options.scales.x.min = minTime;
                    throughputChart.options.scales.x.max = now;
                    throughputChart.options.scales.x.time.unit = "second";
                    throughputChart.update("none");
                }
            } catch (err) {
                console.error("Error polling service current speeds:", err);
            }
        }

        await poll(); // initial poll
        pollIntervalId = setInterval(poll, 10000); // Poll every 10 seconds
    }

    // Set active tab and toggle sub-tabs visibility
    function setupTabListeners() {
        const tabs = document.querySelectorAll(".service-tab");
        const subTabsContainer = document.getElementById("googleSubTabs");

        tabs.forEach(tab => {
            tab.addEventListener("click", function () {
                tabs.forEach(t => t.classList.remove("active"));
                this.classList.add("active");

                activeService = this.getAttribute("data-service");

                if (activeService === "google") {
                    subTabsContainer.style.display = "flex";
                } else {
                    subTabsContainer.style.display = "none";
                }

                // Reload the charts for the newly selected service
                loadHistoricalMetrics();
            });
        });

        const subTabs = document.querySelectorAll(".sub-service-tab");
        subTabs.forEach(tab => {
            tab.addEventListener("click", function () {
                subTabs.forEach(t => t.classList.remove("active"));
                this.classList.add("active");

                activeSubService = this.getAttribute("data-sub");
                loadHistoricalMetrics();
            });
        });

        // Range change listener
        document.getElementById("serviceRangeSelect").addEventListener("change", function () {
            loadHistoricalMetrics();
        });

        // Detect theme swap events (listening on data-theme change on html tag)
        // If app.js changes theme data-theme on html, we redetect and update chart colors
        const observer = new MutationObserver(() => {
            if (throughputChart && payloadChart) {
                const colors = getThemeColors();
                
                // Update throughput chart options
                throughputChart.options.scales.x.ticks.color = colors.muted;
                throughputChart.options.scales.y.ticks.color = colors.muted;
                throughputChart.options.scales.y.grid.color = colors.grid;
                throughputChart.options.plugins.legend.labels.color = colors.text;

                // Update throughput datasets colors dynamically
                throughputChart.data.datasets[0].borderColor = colors.down;
                throughputChart.data.datasets[0].backgroundColor = colors.downGlow;
                throughputChart.data.datasets[1].borderColor = colors.up;
                throughputChart.data.datasets[1].backgroundColor = colors.upGlow;

                // Update payload chart options
                payloadChart.options.scales.x.ticks.color = colors.muted;
                payloadChart.options.scales.y.ticks.color = colors.muted;
                payloadChart.options.scales.y.grid.color = colors.grid;
                payloadChart.options.plugins.legend.labels.color = colors.text;

                // Update payload datasets colors dynamically
                payloadChart.data.datasets[0].backgroundColor = colors.down;
                payloadChart.data.datasets[1].backgroundColor = colors.up;

                updateCharts();
            }
        });

        observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    }

    // Initialize module on page load
    function init() {
        initCharts();
        setupTabListeners();
        loadHistoricalMetrics();
        startRealTimePoll();
    }

    // Run when DOM is ready
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
