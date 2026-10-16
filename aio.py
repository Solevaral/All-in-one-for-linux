#!/usr/bin/env python3
"""All in One for Linux: ставит, обновляет и запускает модули из каталога All in One.

Каталог общий с Windows-версией (Solevaral/All-in-one-modules): у модуля, который есть
под Linux, в записи каталога есть поле "linux" — откуда качать и как ставить.
Только стандартная библиотека Python 3.8+: программа одна и та же в AppImage,
в пакете и запущенная просто как скрипт.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

VERSION = "0.1.2"
REPO = "Solevaral/All-in-one-for-linux"
CATALOG_URL = "https://raw.githubusercontent.com/Solevaral/All-in-one-modules/main/catalog.json"
USER_AGENT = f"AllInOne-Linux/{VERSION}"

HOME = Path.home()
DATA = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share") / "allinone"
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config")
MODULES = DATA / "modules"
STATE_FILE = DATA / "state.json"
CATALOG_CACHE = DATA / "catalog.json"
TIMER_FILE = DATA / "timer.json"
BIN_DIR = HOME / ".local/bin"
APPS_DIR = HOME / ".local/share/applications"
ICONS_DIR = HOME / ".local/share/icons"
AUTOSTART_DIR = CONFIG_HOME / "autostart"
USER_UNITS = CONFIG_HOME / "systemd/user"
APPIMAGE_HOME = HOME / "Applications"

# zapret работает от root (nftables + nfqws), поэтому живёт в каталоге root: иначе любой
# процесс пользователя мог бы подменить бинарник, который запускает системная служба.
ZAPRET_DIR = Path("/opt/allinone/zapret")
ZAPRET_BIN = ZAPRET_DIR / "zapret-rust"
ZAPRET_UNIT = "zapret-rust.service"

TIMER_UNIT = "allinone-timer"
UPDATE_UNIT = "allinone-update"

# Запасной каталог: если GitHub недоступен и кэша ещё нет. Записи из сети его перекрывают.
BUILTIN_CATALOG = [
    {
        "id": "zapret", "name": "zapret", "folder": "zapret", "category": "Сеть",
        "description": "Обход блокировок Discord и YouTube.",
        "linux": {
            "kind": "zapret", "author": "Sergeydigl3",
            "description": "Обход блокировок Discord и YouTube (zapret-rust): стратегии, Game Filter, служба systemd.",
            "homepage": "https://github.com/Sergeydigl3/zapret-discord-youtube-rust",
            "source": {"type": "github", "repo": "Sergeydigl3/zapret-discord-youtube-rust",
                       "asset": "zapret-rust-linux-x86_64"},
        },
    },
    {
        "id": "tg-ws-proxy", "name": "TG WS Proxy", "folder": "TgWsProxy", "category": "Сеть",
        "description": "Локальный MTProto-прокси для Telegram через WebSocket (Flowseal).",
        "linux": {
            "kind": "app", "desktop": True, "autostart": True, "icon": "telegram",
            "source": {"type": "github", "repo": "Flowseal/tg-ws-proxy",
                       "asset": "TgWsProxy_linux_amd64", "saveAs": "TgWsProxy"},
        },
    },
    {
        "id": "trytocatchme", "name": "TryToCatchMe", "folder": "TryToCatchMe", "category": "Сеть",
        "description": "VPN-клиент на sing-box: VLESS, VMess, Shadowsocks, Trojan, маршрутизация по сервисам, TUN.",
        "linux": {
            "kind": "app", "desktop": True, "categories": "Network;",
            "source": {"type": "github", "repo": "Solevaral/TryToCatchMe-client",
                       "asset": "TryToCatchMe_*_amd64.AppImage", "saveAs": "TryToCatchMe.AppImage"},
        },
    },
    {"id": "fdimmer", "name": "fDimmer", "category": "Экран",
     "description": "Затемнение экрана вместе с системным интерфейсом."},
    {"id": "magnif", "name": "magniF", "category": "Экран",
     "description": "Лупа по удержанию клавиши."},
    {
        "id": "shutdown-timer", "name": "Таймер выключения", "category": "Система",
        "description": "Выключение, перезагрузка или сон в заданное время или через интервал.",
        "linux": {"kind": "builtin:shutdown-timer"},
    },
]


class AioError(Exception):
    """Ошибка, которую показываем пользователю как есть, без трассировки."""


# --------------------------------------------------------------------------- вывод

# Оформление (цвета, значки, рамки, очистка экрана) — только когда поток идёт в терминал.
# В pipe, лог и журнал systemd вывод остаётся простым текстом прежнего вида, без ANSI.

def _tty(stream) -> bool:
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


_DUMB = os.environ.get("TERM") == "dumb"
_NO_COLOR = os.environ.get("NO_COLOR") is not None
SCREEN = _tty(sys.stdout) and not _DUMB              # можно очищать экран и рисовать прогресс
COLOR = SCREEN and not _NO_COLOR
ERR_COLOR = _tty(sys.stderr) and not _DUMB and not _NO_COLOR
FANCY = SCREEN and "utf" in (sys.stdout.encoding or "").lower()   # рамки и значки Unicode
QUIET = False

ANSI_RE = re.compile(r"\033\[[0-9;?]*[A-Za-z]")

# Состояние модуля → (значок, запасной ASCII-значок, цвет).
TONES = {
    "run": ("●", "*", "32"),     # работает
    "idle": ("○", "o", "36"),    # установлен, не запущен
    "warn": ("▲", "!", "33"),    # нужна настройка / таймер взведён
    "bad": ("✗", "x", "31"),     # служба упала
    "off": ("·", ".", "2"),      # не установлен
    "na": ("–", "-", "2"),       # нет версии для Linux
    "builtin": ("◆", "+", "35"),  # встроенный
}
LEGEND = {"run": "работает", "idle": "установлен", "warn": "внимание", "bad": "ошибка",
          "off": "не установлен", "na": "нет для Linux", "builtin": "встроен"}


def _c(code: str, text: str, on: bool | None = None) -> str:
    if on is None:
        on = COLOR
    return f"\033[{code}m{text}\033[0m" if on and text else text


def bold(t: str) -> str:
    return _c("1", t)


def dim(t: str) -> str:
    return _c("2", t)


def accent(t: str) -> str:
    return _c("1;36", t)


def glyph(tone: str) -> str:
    fancy, plain, color = TONES[tone]
    return _c(color, fancy if FANCY else plain)


def text_width(s: str) -> int:
    """Ширина строки в колонках терминала: без ANSI, с учётом широких символов."""
    import unicodedata
    w = 0
    for ch in ANSI_RE.sub("", s):
        if unicodedata.combining(ch):
            continue
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def pad(s: str, width: int) -> str:
    return s + " " * max(0, width - text_width(s))


def clip(s: str, width: int) -> str:
    """Обрезает простой (без ANSI) текст до ширины, ставя многоточие."""
    if text_width(s) <= width:
        return s
    out = ""
    for ch in s:
        if text_width(out + ch) > width - 1:
            break
        out += ch
    return out + ("…" if FANCY else "~")


def term_width() -> int:
    return max(56, min(shutil.get_terminal_size((80, 24)).columns, 96))


def wrap(text: str, width: int) -> list[str]:
    import textwrap
    return textwrap.wrap(text, max(20, width), break_on_hyphens=False) or [""]


def clear_screen() -> None:
    # Альтернативный экран, как у полноэкранных утилит: меню не копится в прокрутке,
    # а после выхода в терминале остаётся то, что было до запуска aio.
    # Повторяем ?1049h каждый раз: TUI zapret-rust при выходе сам покидает этот экран.
    if SCREEN:
        sys.stdout.write("\033[?1049h\033[H\033[2J")
        sys.stdout.flush()


def leave_screen() -> None:
    if SCREEN:
        sys.stdout.write("\033[?1049l")
        sys.stdout.flush()


def rule(width: int | None = None) -> str:
    return dim(("─" if FANCY else "-") * (width or term_width()))


def box(title: str, lines: list[str], right: str = "") -> None:
    """Рамка с заголовком в верхней границе: ╭─ Заголовок ───── справа ─╮."""
    w = term_width()
    h, v = ("─", "│") if FANCY else ("-", "|")
    tl, tr, bl, br = ("╭", "╮", "╰", "╯") if FANCY else ("+", "+", "+", "+")
    head = f"{h} {title} "
    tail = f" {right} {h}" if right else h
    fill = max(1, w - 2 - text_width(head) - text_width(tail))
    print(_c("36", tl + h) + " " + bold(title) + " " + _c("36", h * fill)
          + (" " + dim(right) + " " + _c("36", h) if right else "") + _c("36", tr))
    for line in lines:
        print(_c("36", v) + "  " + pad(line, w - 5) + " " + _c("36", v))
    print(_c("36", bl + h * (w - 2) + br))


def keys_hint(pairs: list[tuple[str, str]]) -> None:
    """Подсказка по клавишам: [1-6] модуль   [u] обновить всё …, с переносом по ширине."""
    w = term_width()
    items = [accent(f"[{k}]") + " " + label for k, label in pairs]
    line = " "
    for it in items:
        if text_width(line) + text_width(it) + 3 > w and line.strip():
            print(line)
            line = " "
        line += "  " + it
    if line.strip():
        print(line)


def prompt(label: str = "") -> str:
    """Ввод с приглашением ❯. Приглашение печатаем сами: input() без readline
    неверно считает ширину строки с ANSI-кодами."""
    arrow = accent("❯" if FANCY else ">")
    sys.stdout.write(f" {arrow} " + (f"{label}: " if label else ""))
    sys.stdout.flush()
    return input().strip()


def pause() -> None:
    if not sys.stdin.isatty():
        return
    print()
    sys.stdout.write(dim("  Enter — назад") + " ")
    sys.stdout.flush()
    try:
        input()
    except EOFError:
        pass


def info(msg: str) -> None:
    if not QUIET:
        print(msg)


def done(msg: str) -> None:
    """Успешный итог действия: в терминале — с зелёной галочкой, иначе как info."""
    if not QUIET:
        print(_c("32", "✓" if FANCY else "+") + " " + msg if COLOR else msg)


def step(msg: str) -> None:
    if not QUIET:
        print(accent("▸ ") + bold(msg) if COLOR else "==> " + msg)


def _report(msg: str, icon: str, color: str) -> None:
    if not ERR_COLOR:
        print("[!] " + msg, file=sys.stderr)
        return
    w = term_width() - 4
    lines = wrap(msg, w)
    print(_c(color, icon, True) + " " + lines[0], file=sys.stderr)
    for line in lines[1:]:
        print("  " + line, file=sys.stderr)


def warn(msg: str) -> None:
    _report(msg, "!", "1;33")


def error(msg: str) -> None:
    _report(msg, "✗" if FANCY else "x", "1;31")


def ask(question: str, default: bool = True) -> bool:
    if not sys.stdin.isatty():
        return default
    hint = "[Д/н]" if default else "[д/Н]"
    try:
        if COLOR:
            sys.stdout.write(_c("1;33", "? ") + question + " " + dim(hint) + " ")
            sys.stdout.flush()
            ans = input().strip().lower()
        else:
            ans = input(f"{question} {hint} ").strip().lower()
    except EOFError:
        return default
    if not ans:
        return default
    return ans[0] in "дyj"


def notify(title: str, body: str) -> None:
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-a", "All in One", "-i", "allinone", title, body],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


# --------------------------------------------------------------------------- файлы

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_state() -> dict:
    return load_json(STATE_FILE, {})


def save_state(state: dict) -> None:
    save_json(STATE_FILE, state)


def write_text(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)


# --------------------------------------------------------------------------- root

def root_cmd(args: list[str]) -> list[str]:
    """Команда от root: в терминале — sudo, без терминала (из меню/автообновления) — pkexec."""
    if os.geteuid() == 0:
        return args
    if sys.stdin.isatty() and shutil.which("sudo"):
        return ["sudo", *args]
    if shutil.which("pkexec"):
        return ["pkexec", *args]
    raise AioError("нужны права root, но нет ни sudo в терминале, ни pkexec")


def run_root(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    res = subprocess.run(root_cmd(args), check=False)
    if check and res.returncode != 0:
        raise AioError(f"команда не выполнилась: {' '.join(args)}")
    return res


# --------------------------------------------------------------------------- GitHub

def http_get(url: str, accept: str = "application/json") -> bytes:
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def latest_release(repo: str) -> dict:
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        return json.loads(http_get(url, "application/vnd.github+json"))
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            raise AioError(
                f"GitHub ограничил число запросов без входа. Подожди час или скачай вручную: "
                f"https://github.com/{repo}/releases/latest (или задай GITHUB_TOKEN)") from e
        if e.code == 404:
            raise AioError(f"у {repo} нет релизов") from e
        raise AioError(f"GitHub ответил {e.code} для {repo}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise AioError(f"нет связи с GitHub ({e})") from e


def pick_asset(release: dict, pattern: str) -> dict:
    for a in release.get("assets", []):
        if fnmatch.fnmatchcase(a["name"], pattern):
            return a
    names = ", ".join(a["name"] for a in release.get("assets", [])) or "нет файлов"
    raise AioError(f"в релизе {release.get('tag_name')} нет файла {pattern} ({names})")


def download(url: str, dest: Path, label: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    show = SCREEN and not QUIET
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, open(part, "wb") as f:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            last = 0.0
            while chunk := resp.read(1 << 16):
                f.write(chunk)
                done += len(chunk)
                now = time.monotonic()
                if show and now - last > 0.1:
                    last = now
                    progress_line(label, done, total, done / max(now - started, 0.001))
        if show:
            secs = time.monotonic() - started
            sys.stdout.write("\r\033[K")
            done_line = f"{label}  {_mb(done)} МБ за {secs:.0f} с"
            print("   " + (_c("32", "✓" if FANCY else "+") + " " + done_line if COLOR else done_line))
        if total and done != total:
            raise AioError(f"{label}: скачано {done} из {total} байт")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        if show:
            sys.stdout.write("\r\033[K")
        part.unlink(missing_ok=True)
        raise AioError(f"не удалось скачать {label}: {e}") from e
    except BaseException:
        if show:
            sys.stdout.write("\r\033[K")
        part.unlink(missing_ok=True)
        raise
    os.replace(part, dest)


def _mb(n: float) -> str:
    return f"{n / 1048576:.1f}"


def progress_line(label: str, done: int, total: int, speed: float) -> None:
    """   ███████████░░░░░░░  58%  15.9/27.3 МБ  4.2 МБ/с  имя-файла"""
    w = term_width()
    full, empty = ("█", "░") if FANCY else ("#", ".")
    if total:
        bar_w = max(10, min(28, w - 52))
        filled = min(bar_w, done * bar_w // total)
        bar = _c("36", full * filled) + dim(empty * (bar_w - filled))
        stats = f" {done * 100 // total:3d}%  {_mb(done)}/{_mb(total)} МБ  {_mb(speed)} МБ/с  "
        head = "   " + bar + stats
    else:
        head = f"   {_mb(done)} МБ  {_mb(speed)} МБ/с  "
    name = clip(label, max(8, w - text_width(head) - 1))
    sys.stdout.write("\r\033[K" + head + dim(name))
    sys.stdout.flush()


# --------------------------------------------------------------------------- каталог

def fetch_catalog() -> list[dict]:
    """Каталог из сети (с кэшем), объединённый со встроенным, как в Windows-версии."""
    remote = None
    try:
        data = json.loads(http_get(CATALOG_URL))
        remote = data.get("modules") or []
        save_json(CATALOG_CACHE, data)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        remote = (load_json(CATALOG_CACHE, {}) or {}).get("modules")
    merged = {m["id"]: m for m in BUILTIN_CATALOG}
    order = [m["id"] for m in BUILTIN_CATALOG]
    for m in remote or []:
        if m.get("id") not in merged:
            order.append(m["id"])
        merged[m["id"]] = m
    return [merged[i] for i in order]


_catalog: list[dict] | None = None


def catalog() -> list[dict]:
    global _catalog
    if _catalog is None:
        _catalog = fetch_catalog()
    return _catalog


def find_module(mid: str) -> dict:
    for m in catalog():
        if m["id"] == mid or m.get("name", "").lower() == mid.lower():
            return m
    ids = ", ".join(m["id"] for m in catalog())
    raise AioError(f"нет модуля {mid}. Есть: {ids}")


def linux_spec(m: dict) -> dict | None:
    return m.get("linux")


def description(m: dict) -> str:
    return (linux_spec(m) or {}).get("description") or m.get("description", "")


def source_of(m: dict) -> dict:
    spec = linux_spec(m) or {}
    src = dict(spec.get("source") or {})
    src.setdefault("repo", (m.get("source") or {}).get("repo"))
    if not src.get("repo") or not src.get("asset"):
        raise AioError(f"у модуля {m['name']} в каталоге не указан файл для Linux")
    return src


def module_dir(m: dict) -> Path:
    return MODULES / (m.get("folder") or m["id"])


def module_file(m: dict) -> Path:
    if (linux_spec(m) or {}).get("kind") == "zapret":
        return ZAPRET_BIN
    src = source_of(m)
    return module_dir(m) / (src.get("saveAs") or src["asset"].replace("*", "latest"))


# --------------------------------------------------------------------------- процессы

def pids_of(path: Path) -> list[int]:
    """Процессы, запущенные из этого файла: сам бинарник или AppImage (по $APPIMAGE)."""
    target = str(path)
    found = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit() or int(d.name) == os.getpid():
            continue
        try:
            if os.readlink(d / "exe") == target:
                found.append(int(d.name))
                continue
        except OSError:
            pass
        if target.endswith(".AppImage"):
            try:
                env = (d / "environ").read_bytes().split(b"\0")
            except OSError:
                continue
            if f"APPIMAGE={target}".encode() in env:
                found.append(int(d.name))
    return found


def stop_pids(pids: list[int], timeout: float = 8.0) -> None:
    for p in pids:
        try:
            os.kill(p, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(Path(f"/proc/{p}").exists() for p in pids):
            return
        time.sleep(0.2)
    for p in pids:
        try:
            os.kill(p, signal.SIGKILL)
        except ProcessLookupError:
            pass


def launch(path: Path, args: list[str] | None = None) -> None:
    subprocess.Popen([str(path), *(args or [])], cwd=path.parent, start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def systemctl_state(unit: str, user: bool = False) -> str:
    cmd = ["systemctl", *(["--user"] if user else []), "is-active", unit]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=False).stdout.strip()
    except OSError:
        return "unknown"


def unit_exists(unit: str) -> bool:
    return any(Path(d, unit).exists() for d in ("/etc/systemd/system", "/usr/lib/systemd/system"))


# --------------------------------------------------------------------------- ярлыки

def self_command() -> str:
    """Чем запускать All in One из ярлыков и таймеров."""
    link = BIN_DIR / "aio"
    if link.exists():
        return str(link)
    if os.environ.get("APPIMAGE"):
        return os.environ["APPIMAGE"]
    if installed := shutil.which("aio"):
        return installed
    return f"{sys.executable} {Path(__file__).resolve()}"


def desktop_entry(name: str, exe: str, icon: str, comment: str = "", categories: str = "Utility;",
                  terminal: bool = False) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={name}\n"
        f"Comment={comment}\n"
        f"Exec={exe}\n"
        f"Icon={icon}\n"
        f"Categories={categories}\n"
        f"Terminal={'true' if terminal else 'false'}\n"
        "X-AllInOne=true\n"
    )


def extract_appimage_icon(appimage: Path, dest: Path) -> bool:
    """Иконку достаём из самой AppImage: --appimage-extract не требует FUSE."""
    with tempfile.TemporaryDirectory() as tmp:
        try:
            subprocess.run([str(appimage), "--appimage-extract", "*.png"], cwd=tmp, timeout=60,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            subprocess.run([str(appimage), "--appimage-extract", ".DirIcon"], cwd=tmp, timeout=60,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return False
        root = Path(tmp, "squashfs-root")
        candidates = sorted(root.glob("*.png")) + [root / ".DirIcon"]
        for c in candidates:
            if c.is_file() and not c.is_symlink():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(c, dest)
                return True
            if c.is_symlink():
                real = (c.parent / os.readlink(c))
                if real.is_file():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(real, dest)
                    return True
    return False


def refresh_menu() -> None:
    for tool in ("kbuildsycoca6", "kbuildsycoca5", "update-desktop-database"):
        if shutil.which(tool):
            args = [tool] if tool.startswith("kbuild") else [tool, str(APPS_DIR)]
            subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def app_launchers(m: dict, path: Path) -> None:
    spec = linux_spec(m) or {}
    mid = m["id"]
    icon = spec.get("icon") or "application-x-executable"
    if path.name.endswith(".AppImage"):
        png = ICONS_DIR / f"allinone-{mid}.png"
        if extract_appimage_icon(path, png):
            icon = str(png)
    entry = desktop_entry(m["name"], f'"{path}"', icon, description(m), spec.get("categories", "Network;"))
    if spec.get("desktop"):
        write_text(APPS_DIR / f"allinone-{mid}.desktop", entry)
    if spec.get("autostart") and load_state().get(mid, {}).get("autostart", True):
        write_text(AUTOSTART_DIR / f"allinone-{mid}.desktop", entry)


def remove_launchers(mid: str) -> None:
    for p in (APPS_DIR / f"allinone-{mid}.desktop", AUTOSTART_DIR / f"allinone-{mid}.desktop",
              ICONS_DIR / f"allinone-{mid}.png"):
        p.unlink(missing_ok=True)


# --------------------------------------------------------------------------- модули

def is_installed(m: dict) -> bool:
    return m["id"] in load_state() and module_file(m).exists()


def install(m: dict, zapret_config: Path | None = None, force: bool = False) -> bool:
    """Ставит или обновляет модуль. True — что-то поменялось."""
    spec = linux_spec(m)
    if spec is None:
        raise AioError(f"{m['name']}: нет версии для Linux")
    kind = spec.get("kind")
    if kind == "builtin:shutdown-timer":
        info(f"{m['name']} встроен: aio timer")
        return False

    state = load_state()
    src = source_of(m)
    rel = latest_release(src["repo"])
    tag = rel.get("tag_name", "?")
    current = state.get(m["id"], {}).get("version")
    if current == tag and module_file(m).exists() and not force:
        done(f"{m['name']}: уже последняя версия {tag}")
        if kind == "zapret" and zapret_config:
            zapret_import_config(zapret_config)
        return False

    asset = pick_asset(rel, src["asset"])
    step(f"{m['name']} {tag}" + (f" (было {current})" if current else ""))
    if kind == "zapret":
        zapret_install(asset, zapret_config)
    else:
        path = module_file(m)
        running = pids_of(path)
        tmp = path.with_name(path.name + ".new")
        download(asset["browser_download_url"], tmp, asset["name"])
        tmp.chmod(0o755)
        if running:
            info(f"   останавливаю запущенный {m['name']}")
            stop_pids(running)
        os.replace(tmp, path)
        app_launchers(m, path)
        refresh_menu()
        if running or (not current and spec.get("autostart")):
            launch(path, spec.get("args"))
            info(f"   {m['name']} запущен")

    entry = state.get(m["id"], {})
    entry.update({"version": tag, "asset": asset["name"], "updated": datetime.now().isoformat(timespec="seconds")})
    state[m["id"]] = entry
    save_state(state)
    return True


def remove(m: dict) -> None:
    spec = linux_spec(m) or {}
    if spec.get("kind") == "zapret":
        zapret_remove()
    else:
        path = module_file(m)
        pids = pids_of(path)
        if pids:
            stop_pids(pids)
        shutil.rmtree(module_dir(m), ignore_errors=True)
        remove_launchers(m["id"])
        refresh_menu()
    state = load_state()
    state.pop(m["id"], None)
    save_state(state)
    done(f"{m['name']} удалён")


def update_all(include_self: bool = True) -> list[str]:
    changed = []
    errors = []
    for m in catalog():
        spec = linux_spec(m)
        if not spec or spec.get("kind", "").startswith("builtin:") or not is_installed(m):
            continue
        try:
            if install(m):
                changed.append(f"{m['name']} {load_state()[m['id']]['version']}")
        except AioError as e:
            errors.append(f"{m['name']}: {e}")
            warn(f"{m['name']}: {e}")
    if include_self:
        try:
            if self_update():
                changed.append("All in One")
        except AioError as e:
            errors.append(f"All in One: {e}")
            warn(f"All in One: {e}")
    if not changed and not errors:
        done("Всё уже обновлено.")
    if errors:
        raise AioError("не всё обновилось")
    return changed


# --------------------------------------------------------------------------- zapret

def zapret_import_config(conf: Path) -> None:
    target = ZAPRET_DIR / "conf.env"
    if target.exists():
        info(f"   {target} уже есть — оставляю как есть")
        return
    run_root(["install", "-m", "644", "-o", "root", "-g", "root", str(conf), str(target)])
    info(f"   настройки взяты из {conf}")


def zapret_install(asset: dict, conf: Path | None) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        new = Path(tmp, "zapret-rust")
        download(asset["browser_download_url"], new, asset["name"])
        was_active = systemctl_state(ZAPRET_UNIT) == "active"
        if was_active:
            info("   останавливаю службу zapret-rust")
            run_root(["systemctl", "stop", ZAPRET_UNIT])
        run_root(["install", "-d", "-m", "755", str(ZAPRET_DIR)])
        run_root(["install", "-m", "755", "-o", "root", "-g", "root", str(new), str(ZAPRET_BIN)])
        if conf:
            zapret_import_config(conf)
        if was_active:
            run_root(["systemctl", "start", ZAPRET_UNIT])
            info("   служба zapret-rust снова запущена")
    write_text(APPS_DIR / "allinone-zapret.desktop",
               desktop_entry("zapret", f"{self_command()} zapret", "network-wired",
                             "Настройка zapret: стратегия, Game Filter, служба", "Network;", terminal=True))
    refresh_menu()
    if not zapret_ready():
        info("   Осталось настроить: aio zapret → «Загрузчик → Скачать всё», стратегия, «Служба → Установить».")


def zapret_ready() -> bool:
    """Зависимости zapret скачаны (nfqws и стратегии лежат в каталоге кэша)."""
    return (ZAPRET_DIR / "bin").is_dir() and any(ZAPRET_DIR.glob("zapret-discord-youtube-*"))


def zapret_tui() -> None:
    if not ZAPRET_BIN.exists():
        raise AioError("zapret не установлен: aio install zapret")
    old = Path(f"/etc/systemd/system/{ZAPRET_UNIT}")
    if old.exists() and str(ZAPRET_BIN) not in old.read_text(errors="replace"):
        warn(f"служба {ZAPRET_UNIT} ссылается на другой zapret-rust — переустанови её в меню «Служба»")
    info("Открываю zapret-rust. Первый раз: «Загрузчик → Скачать всё», выбрать интерфейс и стратегию,")
    info("потом «Служба → Установить» — служба запустится и будет стартовать вместе с системой.")
    subprocess.run(root_cmd([str(ZAPRET_BIN), "--cache-dir", str(ZAPRET_DIR)]), cwd=ZAPRET_DIR, check=False)


def zapret_remove() -> None:
    if unit_exists(ZAPRET_UNIT):
        run_root(["systemctl", "disable", "--now", ZAPRET_UNIT], check=False)
        run_root(["rm", "-f", f"/etc/systemd/system/{ZAPRET_UNIT}"], check=False)
        run_root(["systemctl", "daemon-reload"], check=False)
    run_root(["rm", "-rf", str(ZAPRET_DIR)])
    (APPS_DIR / "allinone-zapret.desktop").unlink(missing_ok=True)


def zapret_status() -> str:
    if not ZAPRET_BIN.exists():
        return ""
    if not zapret_ready():
        return "нужна настройка: aio zapret"
    if not unit_exists(ZAPRET_UNIT):
        return "служба не установлена: aio zapret"
    st = systemctl_state(ZAPRET_UNIT)
    return {"active": "служба работает", "inactive": "служба остановлена",
            "failed": "служба упала: journalctl -u zapret-rust"}.get(st, f"служба: {st}")


# --------------------------------------------------------------------------- таймер

DURATION_RE = re.compile(r"^(?:(\d+)\s*[hч])?\s*(?:(\d+)\s*[mм]?)?$", re.IGNORECASE)
ACTIONS = {"poweroff": "выключение", "reboot": "перезагрузка", "suspend": "сон"}
ACTION_ALIASES = {"off": "poweroff", "shutdown": "poweroff", "выкл": "poweroff", "выключение": "poweroff",
                  "restart": "reboot", "перезагрузка": "reboot", "sleep": "suspend", "сон": "suspend"}


def parse_when(when: str) -> datetime:
    """"23:30" — ближайшее такое время; "90", "45m", "1h30m", "1ч30м" — через интервал."""
    now = datetime.now()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", when.strip())
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if h > 23 or mi > 59:
            raise AioError(f"нет такого времени: {when}")
        at = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        return at if at > now else at + timedelta(days=1)
    m = DURATION_RE.fullmatch(when.strip())
    if not m or not (m.group(1) or m.group(2)):
        raise AioError(f"не понял время «{when}». Примеры: 23:30, 45m, 1h30m, 90")
    minutes = int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
    if minutes <= 0:
        raise AioError("интервал должен быть больше нуля")
    return now + timedelta(minutes=minutes)


def timer_set(when: str, action: str) -> None:
    action = ACTION_ALIASES.get(action, action)
    if action not in ACTIONS:
        raise AioError(f"действие: {', '.join(ACTIONS)}")
    at = parse_when(when)
    timer_cancel(quiet=True)
    secs = max(1, int((at - datetime.now()).total_seconds()))
    res = subprocess.run(
        ["systemd-run", "--user", f"--unit={TIMER_UNIT}", f"--on-active={secs}s",
         f"--description=All in One: {ACTIONS[action]}", "systemctl", action],
        capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise AioError(f"не удалось поставить таймер: {res.stderr.strip()}")
    save_json(TIMER_FILE, {"at": at.isoformat(timespec="seconds"), "action": action})
    done(f"{ACTIONS[action].capitalize()} в {at:%H:%M}" + (f" {at:%d.%m}" if at.date() != datetime.now().date() else "")
         + ". Отмена: aio timer cancel")


def timer_cancel(quiet: bool = False) -> None:
    # reset-failed: иначе от прошлого срабатывания остаётся юнит, и systemd-run не создаст новый.
    for args in (["stop", f"{TIMER_UNIT}.timer"], ["reset-failed", f"{TIMER_UNIT}.timer", f"{TIMER_UNIT}.service"]):
        subprocess.run(["systemctl", "--user", *args],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    TIMER_FILE.unlink(missing_ok=True)
    if not quiet:
        done("Таймер снят")


def timer_status() -> str:
    t = load_json(TIMER_FILE, None)
    if not t or systemctl_state(f"{TIMER_UNIT}.timer", user=True) != "active":
        return "не запущен"
    at = datetime.fromisoformat(t["at"])
    left = at - datetime.now()
    mins = max(0, int(left.total_seconds() // 60))
    return f"{ACTIONS.get(t['action'], t['action'])} в {at:%H:%M} (через {mins // 60} ч {mins % 60} мин)"


# --------------------------------------------------------------------------- сам All in One

def appimage_path() -> Path | None:
    p = os.environ.get("APPIMAGE")
    return Path(p) if p else None


def self_update() -> bool:
    app = appimage_path()
    if app is None:
        info("All in One запущен не из AppImage — обновляется тем, чем установлен (пакетом или git).")
        return False
    rel = latest_release(REPO)
    tag = rel.get("tag_name", "")
    if tag.lstrip("v") == VERSION:
        done(f"All in One: уже последняя версия {VERSION}")
        return False
    asset = pick_asset(rel, "AllInOne-*-x86_64.AppImage")
    step(f"All in One {VERSION} → {tag.lstrip('v')}")
    tmp = app.with_name(app.name + ".new")
    download(asset["browser_download_url"], tmp, asset["name"])
    tmp.chmod(0o755)
    os.replace(tmp, app)
    info("   готово, новая версия — со следующего запуска")
    return True


def self_install() -> None:
    """Кладёт AppImage в ~/Applications, делает команду aio, ярлык в меню и иконку."""
    app = appimage_path()
    if app is None:
        raise AioError("self-install — только для AppImage. Из пакета All in One уже установлен.")
    APPIMAGE_HOME.mkdir(parents=True, exist_ok=True)
    target = APPIMAGE_HOME / "AllInOne.AppImage"
    if app.resolve() != target.resolve():
        shutil.copyfile(app, target)
        target.chmod(0o755)
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    link = BIN_DIR / "aio"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target)
    icon = ICONS_DIR / "allinone.png"
    appdir = os.environ.get("APPDIR")
    if appdir and Path(appdir, "allinone.png").exists():
        ICONS_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(appdir, "allinone.png"), icon)
    write_text(APPS_DIR / "allinone.desktop",
               desktop_entry("All in One", f'"{target}"',str(icon) if icon.exists() else "system-software-install",
                             "Модули: zapret, TG WS Proxy, TryToCatchMe, таймер выключения",
                             "System;Network;", terminal=True))
    refresh_menu()
    done(f"All in One установлен: {target}, команда aio, ярлык в меню.")
    if str(BIN_DIR) not in os.environ.get("PATH", "").split(":"):
        warn(f"{BIN_DIR} нет в PATH — после перезахода в систему команда aio заработает (или запускай {link})")


def autoupdate(enable: bool) -> None:
    service = USER_UNITS / f"{UPDATE_UNIT}.service"
    timer = USER_UNITS / f"{UPDATE_UNIT}.timer"
    if enable:
        write_text(service, "[Unit]\nDescription=All in One: обновление модулей\n"
                            "After=network-online.target\n\n[Service]\nType=oneshot\n"
                            f"ExecStart={self_command()} update --quiet --notify\n")
        write_text(timer, "[Unit]\nDescription=All in One: ежедневная проверка обновлений\n\n"
                          "[Timer]\nOnBootSec=5min\nOnUnitActiveSec=1d\nPersistent=true\n\n"
                          "[Install]\nWantedBy=timers.target\n")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        res = subprocess.run(["systemctl", "--user", "enable", "--now", f"{UPDATE_UNIT}.timer"], check=False)
        if res.returncode != 0:
            raise AioError("systemd не включил таймер автообновления (systemctl --user недоступен?)")
        done("Автообновление включено: раз в день, при обновлении — уведомление.")
    else:
        subprocess.run(["systemctl", "--user", "disable", "--now", f"{UPDATE_UNIT}.timer"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        service.unlink(missing_ok=True)
        timer.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        done("Автообновление выключено.")


def autoupdate_enabled() -> bool:
    return (USER_UNITS / f"{UPDATE_UNIT}.timer").exists()


# --------------------------------------------------------------------------- состояние

def module_status(m: dict) -> str:
    spec = linux_spec(m)
    if spec is None:
        return "нет версии для Linux"
    kind = spec.get("kind")
    if kind == "builtin:shutdown-timer":
        return "встроен · " + timer_status()
    st = load_state().get(m["id"])
    if not st or not module_file(m).exists():
        return "не установлен"
    parts = [f"установлен {st.get('version', '?')}"]
    if kind == "zapret":
        if s := zapret_status():
            parts.append(s)
    else:
        if pids_of(module_file(m)):
            parts.append("запущен")
        if spec.get("autostart"):
            parts.append("автозапуск" if st.get("autostart", True) else "без автозапуска")
    return " · ".join(parts)


def module_row(m: dict) -> tuple[str, str, str]:
    """Строка таблицы модулей: (тон значка, версия, состояние)."""
    spec = linux_spec(m)
    if spec is None:
        return "na", "", "нет версии для Linux"
    kind = spec.get("kind")
    if kind == "builtin:shutdown-timer":
        t = timer_status()
        return ("warn" if t != "не запущен" else "builtin"), "встроен", f"таймер: {t}"
    st = load_state().get(m["id"])
    if not st or not module_file(m).exists():
        return "off", "", "не установлен"
    version = st.get("version", "?")
    if kind == "zapret":
        s = zapret_status()
        if not s:
            return "idle", version, "установлен"
        tone = {"служба работает": "run"}.get(s, "bad" if s.startswith("служба упала") else "warn")
        return tone, version, s
    running = bool(pids_of(module_file(m)))
    parts = ["запущен" if running else "не запущен"]
    if spec.get("autostart"):
        parts.append("автозапуск" if st.get("autostart", True) else "без автозапуска")
    return ("run" if running else "idle"), version, " · ".join(parts)


def module_table(mods: list[dict], numbered: bool = True) -> None:
    """Таблица модулей по категориям: значок, имя, версия, состояние — в ровных колонках."""
    w = term_width()
    rows = [(i, m, *module_row(m)) for i, m in enumerate(mods, 1)]
    name_w = max(text_width(m["name"]) for m in mods)
    ver_w = min(22, max([text_width(r[3]) for r in rows] + [6]))
    num_w = len(str(len(mods))) + 2 if numbered else 0
    lead = 2 + num_w + 2                          # отступ, номер, значок
    state_w = max(12, w - lead - name_w - ver_w - 4)
    print("  " + " " * num_w + "  " + dim(pad("Модуль", name_w) + "  " + pad("Версия", ver_w) + "  Состояние"))
    category = None
    used = set()
    for i, m, tone, ver, state in rows:
        cat = m.get("category") or "Прочее"
        if cat != category:
            category = cat
            label = f" {cat} "
            print("  " + _c("1;34", cat.upper()) + " " + rule(max(4, w - 3 - text_width(label))))
        used.add(tone)
        num = accent(f"{i:>{num_w - 2}}") + "  " if numbered else ""
        name = pad(m["name"], name_w)
        version = pad(clip(ver, ver_w) if ver else ("—" if FANCY else "-"), ver_w)
        state_txt = clip(state, state_w)
        colored = {"run": _c("32", state_txt), "warn": _c("33", state_txt), "bad": _c("31", state_txt),
                   "off": dim(state_txt), "na": dim(state_txt)}.get(tone, state_txt)
        if tone in ("na", "off"):
            name, version = dim(name), dim(version)
        else:
            name = bold(name)
        print(f"  {num}{glyph(tone)} {name}  {version}  {colored}")
    legend = [f"{glyph(t)} {LEGEND[t]}" for t in LEGEND if t in used]
    if legend:
        print()
        print("  " + dim("   ").join(legend))


def print_status() -> None:
    if not COLOR:
        width = max(len(m["name"]) for m in catalog())
        for m in catalog():
            print(f"  {m['name']:<{width}}  {module_status(m)}")
        return
    box("All in One for Linux", [dim("Модули из каталога All in One")], f"v{VERSION}")
    print()
    module_table(catalog(), numbered=False)


# --------------------------------------------------------------------------- меню

CANCELLED = object()   # действие отменено пользователем — не нужно ждать Enter


def run_action(fn, wait: bool = True) -> None:
    """Выполняет пункт меню: ошибки показываем аккуратно, а не трассировкой."""
    print()
    try:
        if fn() is CANCELLED:
            return
    except AioError as e:
        error(str(e))
    except KeyboardInterrupt:
        print()
        warn("прервано")
    else:
        if not wait:
            return
    pause()


def main_screen(mods: list[dict], flash: str) -> None:
    clear_screen()
    auto = autoupdate_enabled()
    box("All in One for Linux", [
        "Модули из каталога All in One: установка, обновление, запуск.",
        dim("Автообновление: ") + (_c("32", "включено, раз в день") if auto else _c("33", "выключено")),
    ], f"v{VERSION}")
    print()
    module_table(mods)
    print()
    print(" " + rule(term_width() - 2))
    keys_hint([(f"1-{len(mods)}", "модуль"), ("u", "обновить всё"),
               ("a", "выключить автообновление" if auto else "включить автообновление"), ("q", "выход")])
    if flash:
        print()
        print("  " + flash)
    print()


def menu() -> None:
    try:
        if SCREEN:
            clear_screen()
            print(dim("Загружаю каталог модулей…"))
        _menu_loop()
    finally:
        leave_screen()


def _menu_loop() -> None:
    flash = ""
    while True:
        mods = catalog()
        main_screen(mods, flash)
        flash = ""
        try:
            choice = prompt().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if choice in ("q", "й", "exit", ""):
            return
        if choice in ("u", "г"):
            run_action(update_all)
        elif choice in ("a", "ф"):
            run_action(lambda: autoupdate(not autoupdate_enabled()), wait=False)
        elif choice.isdigit() and 1 <= int(choice) <= len(mods):
            try:
                module_menu(mods[int(choice) - 1])
            except (EOFError, KeyboardInterrupt):
                pass
        else:
            flash = _c("33", f"Нет такого пункта: {choice}. Введи номер модуля или букву из подсказки.")


def module_screen(m: dict, flash: str = "") -> None:
    clear_screen()
    spec = linux_spec(m) or {}
    w = term_width() - 6
    lines = wrap(description(m), w)
    src = spec.get("homepage") or (f"https://github.com/{spec['source']['repo']}"
                                   if (spec.get("source") or {}).get("repo") else "")
    if src:
        lines.append(dim(clip(src, w)))
    box(m["name"], lines, m.get("category", ""))
    print()
    tone, ver, state = module_row(m)
    print(f"  {dim(pad('Состояние', 10))} {glyph(tone)} {state}")
    if ver and tone not in ("na", "off"):
        print(f"  {dim(pad('Версия', 10))}   {ver}")
    if spec and tone not in ("na", "off") and spec.get("kind") != "builtin:shutdown-timer":
        path = str(module_file(m)).replace(str(HOME), "~", 1)
        print(f"  {dim(pad('Файл', 10))}   {dim(clip(path, w - 10))}")
    print()
    if flash:
        print("  " + flash)
        print()


def module_menu(m: dict) -> None:
    spec = linux_spec(m)
    if spec is None:
        module_screen(m)
        print("  " + _c("33", "Под Linux этого модуля нет."))
        pause()
        return
    if spec.get("kind") == "builtin:shutdown-timer":
        timer_menu(m)
        return

    kind = spec.get("kind")
    flash = ""
    while True:
        installed = is_installed(m)
        # (подпись, действие, ждать Enter после вывода)
        opts = []
        if not installed:
            opts.append(("Установить", lambda: install(m), True))
        else:
            opts.append(("Проверить обновление", lambda: install(m), True))
            if kind == "zapret":
                opts.append(("Настроить (стратегия, служба)", zapret_tui, False))
            else:
                path = module_file(m)
                if pids_of(path):
                    opts.append(("Остановить", lambda: stop_pids(pids_of(path)), False))
                else:
                    opts.append(("Запустить", lambda: launch(path, spec.get("args")), False))
                if spec.get("autostart"):
                    on = load_state().get(m["id"], {}).get("autostart", True)
                    opts.append(("Выключить автозапуск" if on else "Включить автозапуск",
                                 lambda: set_autostart(m, not on), False))
            opts.append(("Удалить", lambda: remove(m) if ask(f"Удалить {m['name']}?", False) else CANCELLED,
                         True))

        module_screen(m, flash)
        flash = ""
        for i, (label, _, _) in enumerate(opts, 1):
            print(f"   {accent(f'[{i}]')} {_c('31', label) if label == 'Удалить' else label}")
        print(f"   {accent('[0]')} {dim('Назад')}")
        print()
        choice = prompt()
        if choice in ("", "0", "q", "й"):
            return
        if not (choice.isdigit() and 1 <= int(choice) <= len(opts)):
            flash = _c("33", f"Нет такого пункта: {choice}")
            continue
        label, fn, wait = opts[int(choice) - 1]
        if wait:
            run_action(fn)
        else:
            try:
                fn()
                if label in ("Запустить", "Остановить"):
                    flash = _c("32", f"{m['name']}: {'запущен' if label == 'Запустить' else 'остановлен'}")
            except AioError as e:
                print()
                error(str(e))
                pause()
        if label == "Удалить" and not is_installed(m):
            return


def timer_menu(m: dict) -> None:
    flash = ""
    while True:
        module_screen(m, flash)
        flash = ""
        active = timer_status() != "не запущен"
        print("  " + dim("Когда сработать:"))
        examples = [(["23:30"], "в это время (сегодня или завтра)"),
                    (["45m", "1h30m", "90"], "через интервал (минуты без буквы)")]
        for ex, what in examples:
            print("     " + pad("  ".join(accent(e) for e in ex), 18) + dim(what))
        print()
        keys_hint(([("x", "снять таймер")] if active else []) + [("Enter", "назад")])
        print()
        when = prompt("когда")
        if not when:
            return
        if when.lower() in ("x", "ч", "cancel", "отмена"):
            timer_cancel(quiet=True)
            flash = _c("32", "Таймер снят")
            continue
        try:
            at = parse_when(when)
        except AioError as e:
            flash = _c("33", str(e))
            continue
        print()
        print("  " + dim(f"Что сделать в {at:%H:%M}:"))
        actions = list(ACTIONS)
        for i, a in enumerate(actions, 1):
            print(f"     {accent(f'[{i}]')} {ACTIONS[a]}" + (dim("  (Enter)") if i == 1 else ""))
        print()
        action = prompt("действие")
        if action.isdigit() and 1 <= int(action) <= len(actions):
            action = actions[int(action) - 1]
        run_action(lambda: timer_set(when, action or "poweroff"))


def set_autostart(m: dict, on: bool) -> None:
    state = load_state()
    state.setdefault(m["id"], {})["autostart"] = on
    save_state(state)
    if on:
        app_launchers(m, module_file(m))
    else:
        (AUTOSTART_DIR / f"allinone-{m['id']}.desktop").unlink(missing_ok=True)
    done(f"Автозапуск {m['name']} {'включён' if on else 'выключен'}")


# --------------------------------------------------------------------------- командная строка

def main(argv: list[str] | None = None) -> int:
    global QUIET
    p = argparse.ArgumentParser(prog="aio", description="All in One for Linux — модули из каталога All in One.")
    p.add_argument("--version", action="version", version=f"All in One for Linux {VERSION}")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("list", help="модули и их состояние")
    sub.add_parser("status", help="то же, что list")
    pi = sub.add_parser("install", help="установить модули")
    pi.add_argument("ids", nargs="+")
    pi.add_argument("--zapret-config", type=Path, help="готовый conf.env для zapret")
    pi.add_argument("--force", action="store_true", help="переустановить, даже если версия та же")
    pu = sub.add_parser("update", help="обновить установленные модули и сам All in One")
    pu.add_argument("ids", nargs="*")
    pu.add_argument("--quiet", action="store_true")
    pu.add_argument("--notify", action="store_true", help="уведомление на рабочем столе")
    pu.add_argument("--no-self", action="store_true", help="не обновлять сам All in One")
    pr = sub.add_parser("remove", help="удалить модуль")
    pr.add_argument("id")
    pr.add_argument("-y", "--yes", action="store_true")
    sub.add_parser("zapret", help="настройка zapret (TUI zapret-rust)")
    pt = sub.add_parser("timer", help="таймер выключения: aio timer 23:30 | 45m | 1h30m [reboot|suspend] | cancel")
    pt.add_argument("when", nargs="?")
    pt.add_argument("action", nargs="?", default="poweroff")
    pa = sub.add_parser("autoupdate", help="ежедневное автообновление: on | off")
    pa.add_argument("state", choices=["on", "off"])
    sub.add_parser("self-install", help="установить AppImage: ~/Applications, команда aio, ярлык")
    args = p.parse_args(argv)

    try:
        if args.cmd is None:
            if not sys.stdin.isatty():
                print_status()
                return 0
            menu()
        elif args.cmd in ("list", "status"):
            print_status()
        elif args.cmd == "install":
            failed = []
            for mid in args.ids:
                try:
                    install(find_module(mid), zapret_config=args.zapret_config, force=args.force)
                except AioError as e:
                    warn(f"{mid}: {e}")
                    failed.append(mid)
            if failed:
                return 1
        elif args.cmd == "update":
            QUIET = args.quiet
            if args.ids:
                for mid in args.ids:
                    install(find_module(mid))
            else:
                try:
                    changed = update_all(include_self=not args.no_self)
                except AioError as e:
                    if args.notify:
                        notify("Обновление не прошло", "Подробности: aio update")
                    raise e
                if args.notify and changed:
                    notify("Обновлено", ", ".join(changed))
        elif args.cmd == "remove":
            m = find_module(args.id)
            if args.yes or ask(f"Удалить {m['name']}?", False):
                remove(m)
        elif args.cmd == "zapret":
            zapret_tui()
        elif args.cmd == "timer":
            if args.when in (None, "status"):
                print(timer_status())
            elif args.when in ("cancel", "off", "отмена"):
                timer_cancel()
            else:
                timer_set(args.when, args.action)
        elif args.cmd == "autoupdate":
            autoupdate(args.state == "on")
        elif args.cmd == "self-install":
            self_install()
    except AioError as e:
        warn(str(e))
        return 1
    except KeyboardInterrupt:
        print()
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
