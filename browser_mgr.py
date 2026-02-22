import asyncio
import subprocess
import json
import os
import shlex

class BrowserManager:
    def __init__(self):
        self.container_name = "server_monitor_browser"
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
            
        # 2. Add Nginx Configuration
        self._add_log("Configuring Nginx reverse proxy...")
        injector_script = """
import os
import re
import glob

nginx_conf = '''
location /browser/ {
    proxy_pass http://127.0.0.1:3000/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    rewrite ^/browser/(.*) /$1 break;
}'''

files_to_check = []
for pattern in ["/etc/nginx/sites-available/*", "/etc/nginx/conf.d/*.conf", "/etc/nginx/nginx.conf"]:
    files_to_check.extend(glob.glob(pattern))

updated = False
for conf_path in set(files_to_check):
    if not os.path.isfile(conf_path): continue
    try:
        with open(conf_path, "r") as f:
            content = f.read()
            
        old_content = content
        
        # Iteratively remove existing location /browser/ {...} blocks
        while True:
            # We use a pattern to match the location block. Since {} can be nested, regex is tricky.
            # But Nginx location blocks usually don't have nested {} except maybe ifs.
            # We'll do a simple regex that matches until the first }
            content_new = re.sub(r'\\n\\s*location\s+/browser/\s*\{[^}]*\}', '', content)
            if content_new == content:
                break
            content = content_new

        if re.search(r"server\\s*\\{", content) and "listen" in content:
            matches = list(re.finditer(r"server\\s*\\{", content))
            if matches:
                for match in reversed(matches):
                    idx = match.end()
                    content = content[:idx] + "\\n" + nginx_conf + "\\n" + content[idx:]
                
        if content != old_content:
            with open(conf_path, "w") as f:
                f.write(content)
            print("Updated: " + conf_path)
            updated = True
    except Exception as e:
        print(f"Error checking {conf_path}: {e}")

if not updated:
    print("Checked Nginx configs. No changes made.")
"""
        await self.run_command(f"python3 -c {shlex.quote(injector_script)}")
        await self.run_command("systemctl reload nginx")

        # 3. Pull image
        self._add_log("Pulling lscr.io/linuxserver/chromium image... (This may take a few minutes)")
        await self.run_command("docker pull lscr.io/linuxserver/chromium:latest")

        # 4. Remove existing if any
        await self.run_command(f"docker rm -f {self.container_name} || true")
        
        # 5. Run new container
        user = shlex.quote(config.get("user", "admin"))
        password = shlex.quote(config.get("pass", "admin"))
        res = config.get("res", "2560x1440")
        
        # For KasmVNC chromium, we map 3000 to internal 3000
        cmd = (f"docker run -d --name {self.container_name} "
               f"-e CUSTOM_USER={user} "
               f"-e PASSWORD={password} "
               f"-e SUBFOLDER=/browser/ "
               f"-e TITLE='Virtual Browser' "
               f"-e KASMVNC_INTERFACE=0.0.0.0 "
               f"-e KASM_INTERFACE=0.0.0.0 "
               f"-e DISABLE_IPV6=true "
               f"-p 127.0.0.1:3000:3000 "
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
