document.addEventListener('DOMContentLoaded', () => {
    const termContainer = document.getElementById('terminal-container');
    const loginOverlay = document.getElementById('login-overlay');
    const sshForm = document.getElementById('ssh-form');
    const loginError = document.getElementById('login-error');
    const connectBtn = document.getElementById('connect-btn');
    const spinnerOverlay = document.getElementById('loading-spinner');
    const spinnerText = document.getElementById('spinner-text');

    const term = new Terminal({
        cursorBlink: true,
        theme: {
            background: '#000000',
            foreground: '#ffffff',
            cursor: '#ffffff',
            selectionBackground: 'rgba(255, 255, 255, 0.3)',
        },
        fontFamily: 'Consolas, "Courier New", monospace',
        fontSize: 14,
    });

    const fitAddon = new FitAddon.FitAddon();
    term.loadAddon(fitAddon);
    term.open(termContainer);

    // Auto-focus host input on load
    document.getElementById('ssh-host').focus();

    let ws = null;
    let authSent = false;

    function showSpinner(text) {
        spinnerText.textContent = text;
        spinnerOverlay.style.display = 'flex';
        loginOverlay.style.display = 'none';
        connectBtn.disabled = true;
    }

    function showError(msg) {
        loginError.textContent = msg;
        spinnerOverlay.style.display = 'none';
        loginOverlay.style.display = 'flex';
        connectBtn.disabled = false;
    }

    sshForm.addEventListener('submit', (e) => {
        e.preventDefault();
        e.stopPropagation();
        loginError.textContent = '';

        const host = document.getElementById('ssh-host').value.trim();
        const portVal = document.getElementById('ssh-port').value;
        const port = portVal ? parseInt(portVal, 10) : 22;
        const username = document.getElementById('ssh-user').value.trim();
        const password = document.getElementById('ssh-pass').value;

        if (!host || !username) {
            showError("Host and Username are required.");
            return;
        }

        showSpinner("Connecting WebSocket...");
        connectWebSocket({ host, port, username, password });
        return false;
    });

    function connectWebSocket(credentials) {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/api/ssh`;

        try {
            ws = new WebSocket(wsUrl);
        } catch (e) {
            showError("Failed to initialize WebSocket.");
            return;
        }

        ws.onopen = () => {
            console.log('WebSocket connected. Sending Auth payload.');
            spinnerText.textContent = "Authenticating with SSH Server...";

            // Send auth chunk
            ws.send(JSON.stringify({
                type: 'auth',
                ...credentials
            }));
            authSent = true;
        };

        ws.onmessage = (event) => {
            // Check if backend rejected auth (assuming we haven't shown terminal yet)
            if (event.data.includes("Authentication failed") || event.data.includes("SSH Connection Error")) {
                ws.close();
                showError(event.data.trim());
                return;
            }

            // Hide spinner and show terminal on first real data
            if (spinnerOverlay.style.display !== 'none') {
                spinnerOverlay.style.display = 'none';
                termContainer.style.display = 'block';
                fitAddon.fit();

                // Send initial size
                ws.send(JSON.stringify({
                    type: 'resize',
                    cols: term.cols,
                    rows: term.rows
                }));
                term.focus();
            }

            term.write(event.data);
        };

        ws.onclose = () => {
            if (termContainer.style.display === 'block') {
                // If terminal was already open, show a generic disconnected screen via spinner overlay
                spinnerOverlay.style.display = 'flex';
                spinnerOverlay.style.background = 'rgba(0, 0, 0, 0.9)';
                spinnerOverlay.innerHTML = '<div style="color: #ef4444; font-size: 1.2rem; margin-bottom: 20px;">Connection Closed</div><button onclick="window.location.reload()" style="padding:10px 20px; background:linear-gradient(135deg, #4f46e5, #9333ea); border:none; color:white; border-radius:8px; cursor:pointer; font-weight: 600;">Reconnect</button>';
            } else if (authSent) {
                // Closed during auth phase and no specific error caught in onmessage
                showError("Connection closed by server. Check credentials or host.");
            }
            authSent = false;
        };

        ws.onerror = (error) => {
            console.error('WebSocket error:', error);
            if (!authSent) {
                showError("WebSocket error. Check connection to panel.");
            }
        };

        // Send keystrokes to the server
        term.onData(data => {
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({
                    type: 'data',
                    data: data
                }));
            }
        });
    }

    // Handle window resize
    window.addEventListener('resize', () => {
        if (termContainer.style.display === 'block') {
            fitAddon.fit();
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({
                    type: 'resize',
                    cols: term.cols,
                    rows: term.rows
                }));
            }
        }
    });
});
