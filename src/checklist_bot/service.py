"""Фоновый запуск и автозапуск на Windows и macOS (на Linux - только фоновый запуск)."""

from __future__ import annotations

import os
import plistlib
import signal
import subprocess
import sys
from pathlib import Path

LABEL = "com.checklistbot"
WINDOWS_SHORTCUT = "CheckListBot.lnk"


def is_windows() -> bool:
    return os.name == "nt"


def is_macos() -> bool:
    return sys.platform == "darwin"


# --- блокировка: не больше одного экземпляра бота на папку ---


class InstanceLock:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "bot.lock"
        self.pid_path = data_dir / "bot.pid"
        self._file = None

    def acquire(self) -> bool:
        self._file = open(self.path, "a+")  # noqa: SIM115 - держим открытым всё время работы
        try:
            if is_windows():
                import msvcrt

                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._file.close()
            self._file = None
            return False
        self.pid_path.write_text(str(os.getpid()), encoding="utf-8")
        return True

    def release(self) -> None:
        if self._file:
            self._file.close()
            self._file = None


def is_running(data_dir: Path) -> bool:
    lock = InstanceLock(data_dir)
    if lock.acquire():
        lock.release()
        return False
    return True


# --- команды запуска ---


def python_for_background() -> str:
    """На Windows pythonw.exe, чтобы не висело чёрное окно консоли."""
    exe = Path(sys.executable)
    if is_windows():
        pythonw = exe.with_name("pythonw.exe")
        if pythonw.exists():
            return str(pythonw)
    return str(exe)


def run_args(home: Path) -> list[str]:
    return [python_for_background(), "-m", "checklist_bot", "run", "--home", str(home)]


def launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def windows_startup_dir() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def install_autostart(home: Path) -> str:
    """Регистрирует запуск бота при входе в систему. Возвращает описание для пользователя."""
    if is_windows():
        args = run_args(home)
        shortcut = windows_startup_dir() / WINDOWS_SHORTCUT
        arguments = " ".join(f'"{arg}"' if " " in arg else arg for arg in args[1:])
        script = (
            "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:CLB_LNK); "
            "$s.TargetPath = $env:CLB_TARGET; $s.Arguments = $env:CLB_ARGS; "
            "$s.WorkingDirectory = $env:CLB_HOME; $s.Save()"
        )
        env = dict(os.environ, CLB_LNK=str(shortcut), CLB_TARGET=args[0], CLB_ARGS=arguments, CLB_HOME=str(home))
        subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True, env=env)
        return f"ярлык в автозагрузке Windows: {shortcut}"

    if is_macos():
        path = launch_agent_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        logs = home / "data" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        plist = {
            "Label": LABEL,
            # caffeinate -s не даёт Mac уснуть, пока он на зарядке и бот работает.
            "ProgramArguments": ["/usr/bin/caffeinate", "-s", *run_args(home)],
            "WorkingDirectory": str(home),
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 30,
            "StandardOutPath": str(logs / "launchd.out.log"),
            "StandardErrorPath": str(logs / "launchd.err.log"),
        }
        with open(path, "wb") as fh:
            plistlib.dump(plist, fh)
        return f"служба launchd: {path}"

    return "автозапуск на этой ОС не настраивается, запускайте командой `run`"


def remove_autostart() -> None:
    if is_windows():
        (windows_startup_dir() / WINDOWS_SHORTCUT).unlink(missing_ok=True)
    elif is_macos():
        stop_launchd()
        launch_agent_path().unlink(missing_ok=True)


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def stop_launchd() -> None:
    _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")


def start(home: Path) -> None:
    if is_macos() and launch_agent_path().exists():
        stop_launchd()
        result = _launchctl("bootstrap", f"gui/{os.getuid()}", str(launch_agent_path()))
        if result.returncode != 0:
            raise RuntimeError(f"launchctl bootstrap: {result.stderr.strip()}")
        return
    spawn(home)


def spawn(home: Path) -> None:
    """Запускает бота отдельным фоновым процессом, не привязанным к текущему окну."""
    kwargs: dict = {
        "cwd": str(home),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if is_windows():
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(run_args(home), **kwargs)  # noqa: S603


def stop(home: Path) -> bool:
    """Останавливает запущенного бота. Возвращает True, если он работал."""
    data_dir = home / "data"
    was_running = is_running(data_dir)
    if is_macos() and launch_agent_path().exists():
        stop_launchd()
    if is_running(data_dir):
        pid_path = data_dir / "bot.pid"
        if pid_path.exists():
            try:
                os.kill(int(pid_path.read_text(encoding="utf-8").strip()), signal.SIGTERM)
            except (OSError, ValueError):
                pass
    return was_running
