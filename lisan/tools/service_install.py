"""Cross-platform always-on service install (launchd / systemd --user).

One capability, many faces: the Telegram bot and the scheduler both need
"keep this Lisan process alive across logins." The OS layer holds no task
state — it only keeps a process running; everything it executes reads its
work from the vault and the jobs database.

The rendered definitions always embed a PATH: detached services get a
minimal environment that misses Homebrew/npm dirs, so provider binaries
(codex) would otherwise vanish — the installing shell's PATH demonstrably
resolves them.
"""
from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

_FALLBACK_PATH = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def service_path_env(path_env: str | None = None) -> str:
    return path_env or os.environ.get("PATH") or _FALLBACK_PATH


@dataclass
class ServiceSpec:
    label: str                 # launchd label, e.g. "com.lisan.scheduler"
    unit_name: str             # systemd unit file name, e.g. "lisan-scheduler.service"
    description: str
    program_args: list[str]
    environment: dict[str, str]
    working_directory: Path
    out_log: Path
    err_log: Path


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def launchd_plist_path(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def systemd_unit_path(unit_name: str) -> Path:
    return Path.home() / ".config" / "systemd" / "user" / unit_name


def render_launchd_plist(spec: ServiceSpec) -> str:
    args_xml = "\n".join(f"      <string>{_xml_escape(a)}</string>" for a in spec.program_args)
    env_xml = "\n".join(
        f"      <key>{_xml_escape(key)}</key>\n      <string>{_xml_escape(value)}</string>"
        for key, value in spec.environment.items()
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>{spec.label}</string>
    <key>ProgramArguments</key>
    <array>
{args_xml}
    </array>
    <key>EnvironmentVariables</key>
    <dict>
{env_xml}
    </dict>
    <key>WorkingDirectory</key>
    <string>{_xml_escape(str(spec.working_directory))}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{_xml_escape(str(spec.out_log))}</string>
    <key>StandardErrorPath</key>
    <string>{_xml_escape(str(spec.err_log))}</string>
  </dict>
</plist>
"""


def render_systemd_unit(spec: ServiceSpec) -> str:
    exec_start = shlex.join(spec.program_args)
    env_lines = "\n".join(f'Environment="{key}={value}"' for key, value in spec.environment.items())
    return f"""[Unit]
Description={spec.description}
After=network-online.target

[Service]
ExecStart={exec_start}
{env_lines}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""


def install_launchd(spec: ServiceSpec) -> int:
    uid = os.getuid()
    spec.out_log.parent.mkdir(parents=True, exist_ok=True)
    plist_path = launchd_plist_path(spec.label)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(render_launchd_plist(spec))
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{spec.label}"], capture_output=True)
    result = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)], capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"✗ Failed to load service: {result.stderr.strip() or result.stdout.strip()}")
        print(f"  plist written to {plist_path}")
        return 1
    print(f"✓ Installed and started {spec.label} — auto-starts on login.")
    print(f"  plist: {plist_path}")
    print(f"  logs:  {spec.err_log}")
    return 0


def install_systemd(spec: ServiceSpec) -> int:
    unit_path = systemd_unit_path(spec.unit_name)
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(render_systemd_unit(spec))
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    result = subprocess.run(
        ["systemctl", "--user", "enable", "--now", spec.unit_name], capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"✗ Failed to enable service: {result.stderr.strip() or result.stdout.strip()}")
        print(f"  unit written to {unit_path}")
        return 1
    print(f"✓ Installed and started {spec.unit_name} (systemd --user).")
    print("  To keep it running without an active login: sudo loginctl enable-linger $USER")
    print("  (WSL: enable systemd via /etc/wsl.conf [boot] systemd=true, or run this")
    print("  service from Windows Task Scheduler: wsl.exe -- <the ExecStart line>)")
    return 0


def install_service(spec: ServiceSpec) -> int:
    import platform

    system = platform.system()
    if system == "Darwin":
        return install_launchd(spec)
    if system == "Linux":
        return install_systemd(spec)
    print(f"✗ Automatic service install isn't supported on {system}.")
    print(f"  Run `{shlex.join(spec.program_args)}` under your own process manager.")
    return 1


def uninstall_service(*, label: str, unit_name: str) -> int:
    import platform

    system = platform.system()
    if system == "Darwin":
        uid = os.getuid()
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
        plist_path = launchd_plist_path(label)
        if plist_path.exists():
            plist_path.unlink()
        print(f"✓ Removed {label}.")
        return 0
    if system == "Linux":
        subprocess.run(["systemctl", "--user", "disable", "--now", unit_name], capture_output=True)
        unit_path = systemd_unit_path(unit_name)
        if unit_path.exists():
            unit_path.unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        print(f"✓ Removed {unit_name}.")
        return 0
    print(f"✗ Nothing to do on {system}.")
    return 1
