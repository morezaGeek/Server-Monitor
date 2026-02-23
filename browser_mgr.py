import asyncio
import subprocess
import json
import os
import shlex
import socket

BROWSER_PORT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".browser_port")

def _find_free_port(start=3000, end=3020):
    """Find a free port on localhost, trying start first."""
    for port in range(start, end + 1):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    return None

def _read_saved_port():
    """Read the saved port from file."""
    try:
        if os.path.isfile(BROWSER_PORT_FILE):
            return int(open(BROWSER_PORT_FILE).read().strip())
    except:
        pass
    return 3000

def get_browser_port():
    """Get the port the browser container is mapped to."""
    return _read_saved_port()

class BrowserManager:
    def __init__(self):
        self.container_name = "server_monitor_browser"
        self.container_port = _read_saved_port()
        self.log_queues = []
        self.log_history = []
        
    def _add_log(self, message: str):
        self.log_history.append(message)
        if len(self.log_history) > 200:
            self.log_history = self.log_history[-200:]
            
        for q in self.log_queues:
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                pass

    async def run_command(self, cmd: str):
        self._add_log(f"> {cmd}")
        process = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )
        
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            line_str = line.decode().strip()
            if line_str:
                self._add_log(line_str)
                
        await process.wait()
        return process.returncode

    async def get_status(self):
        # Check if container exists
        proc = await asyncio.create_subprocess_shell(
            f"docker inspect -f '{{{{.State.Status}}}}' {self.container_name}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL
        )
        stdout, _ = await proc.communicate()
        status_str = stdout.decode().strip()
        
        if not status_str:
            return {"state": "not_installed"}
        elif status_str == "running":
            return {"state": "running"}
        else:
            return {"state": "stopped"}

    async def install(self, config: dict):
        self._add_log("Starting Installation Process...")
        
        # 1. Check if Docker is installed
        proc = await asyncio.create_subprocess_shell("command -v docker", stdout=asyncio.subprocess.PIPE)
        await proc.wait()
        if proc.returncode != 0:
            self._add_log("Docker not found. Attempting universal Docker installation...")
            rc = await self.run_command("curl -fsSL https://get.docker.com | bash")
            if rc == 0:
                await self.run_command("systemctl enable --now docker")
            else:
                self._add_log("Failed to install Docker. Please install Docker manually.")
        else:
            self._add_log("Docker is already installed.")
            
        # 2. Nginx configuration is NOT needed.
        # All /browser/ traffic is proxied by FastAPI in app.py (HTTP + WebSocket).
        # Adding a location /browser/ block to Nginx causes path conflicts.
        # 3. Pull image
        self._add_log("Pulling lscr.io/linuxserver/chromium image... (This may take a few minutes)")
        await self.run_command("docker pull lscr.io/linuxserver/chromium:latest")

        # 4. Remove existing if any
        await self.run_command(f"docker rm -f {self.container_name} || true")
        
        # 5. Find a free port
        port = _find_free_port()
        if port is None:
            self._add_log("❌ No free port found in range 3000-3020. Cannot start container.")
            return
        if port != 3000:
            self._add_log(f"⚠ Port 3000 is in use. Using port {port} instead.")
        
        # Save the chosen port
        with open(BROWSER_PORT_FILE, "w") as f:
            f.write(str(port))
        self.container_port = port
        
        # 6. Run new container
        user = shlex.quote(config.get("user", "admin"))
        password = shlex.quote(config.get("pass", "admin"))
        res = config.get("res", "2560x1440")
        
        cmd = (f"docker run -d --name {self.container_name} "
               f"-e CUSTOM_USER={user} "
               f"-e PASSWORD={password} "
               f"-e SUBFOLDER=/browser/ "
               f"-e TITLE='Virtual Browser' "
               f"-e KASMVNC_INTERFACE=0.0.0.0 "
               f"-e KASM_INTERFACE=0.0.0.0 "
               f"-e DISABLE_IPV6=true "
               f"-p 127.0.0.1:{port}:3000 "
               f"--shm-size='1gb' "
               f"--restart unless-stopped "
               f"lscr.io/linuxserver/chromium:latest")
        
        rc = await self.run_command(cmd)
        if rc == 0:
            self._add_log("✅ Installation complete. Container is starting. \n🌐 You can now click OPEN BROWSER.")
        else:
            self._add_log(f"❌ Installation failed with code {rc}")

    async def uninstall(self):
        self._add_log("Uninstalling Virtual Browser...")
        self._add_log("Removing container server_monitor_browser...")
        await self.run_command(f"docker rm -f {self.container_name} || true")
        self._add_log("Cleaning up unused Docker networks and containers...")
        await self.run_command("docker system prune -f")
        self._add_log("Uninstallation complete. You can now reinstall.")

    async def start(self):
        self._add_log(f"Starting {self.container_name}...")
        await self.run_command(f"docker start {self.container_name}")

    async def stop(self):
        self._add_log(f"Stopping {self.container_name}...")
        await self.run_command(f"docker stop {self.container_name}")

    async def clear_cache(self):
        self._add_log("Clearing Virtual Browser Cache...")
        await self.run_command(f"docker rm -f {self.container_name} || true")
        self._add_log("Removing Chromium Docker image (this may take a moment)...")
        await self.run_command("docker rmi -f lscr.io/linuxserver/chromium:latest || true")
        self._add_log("Running system prune...")
        await self.run_command("docker system prune -f")
        self._add_log("Cache memory cleared completely.")

browser_mgr = BrowserManager()
