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

VERSION = "0.1.0"
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

COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
QUIET = False


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLOR else text


def info(msg: str) -> None:
    if not QUIET:
        print(msg)


def step(msg: str) -> None:
    if not QUIET:
        print(_c("1;36", "==> ") + msg)


def warn(msg: str) -> None:
    print(_c("1;33", "[!] ") + msg, file=sys.stderr)


def ask(question: str, default: bool = True) -> bool:
    if not sys.stdin.isatty():
        return default
    hint = "[Д/н]" if default else "[д/Н]"
    try:
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
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, open(part, "wb") as f:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            last = 0.0
            while chunk := resp.read(1 << 16):
                f.write(chunk)
                done += len(chunk)
                now = time.monotonic()
                if total and sys.stdout.isatty() and not QUIET and now - last > 0.2:
                    last = now
                    print(f"\r   {label}: {done * 100 // total}% ({done >> 20} из {total >> 20} МБ)",
                          end="", flush=True)
        if total and sys.stdout.isatty() and not QUIET:
            print(f"\r   {label}: 100% ({total >> 20} МБ)" + " " * 20)
        if total and done != total:
            raise AioError(f"{label}: скачано {done} из {total} байт")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        part.unlink(missing_ok=True)
        raise AioError(f"не удалось скачать {label}: {e}") from e
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, dest)


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
        info(f"{m['name']}: уже последняя версия {tag}")
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
    info(f"{m['name']} удалён")


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
        info("Всё уже обновлено.")
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
    info(f"{ACTIONS[action].capitalize()} в {at:%H:%M}" + (f" {at:%d.%m}" if at.date() != datetime.now().date() else "")
         + ". Отмена: aio timer cancel")


def timer_cancel(quiet: bool = False) -> None:
    # reset-failed: иначе от прошлого срабатывания остаётся юнит, и systemd-run не создаст новый.
    for args in (["stop", f"{TIMER_UNIT}.timer"], ["reset-failed", f"{TIMER_UNIT}.timer", f"{TIMER_UNIT}.service"]):
        subprocess.run(["systemctl", "--user", *args],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    TIMER_FILE.unlink(missing_ok=True)
    if not quiet:
        info("Таймер снят")


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
        info(f"All in One: уже последняя версия {VERSION}")
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
    info(f"All in One установлен: {target}, команда aio, ярлык в меню.")
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
        subprocess.run(["systemctl", "--user", "enable", "--now", f"{UPDATE_UNIT}.timer"], check=False)
        info("Автообновление включено: раз в день, при обновлении — уведомление.")
    else:
        subprocess.run(["systemctl", "--user", "disable", "--now", f"{UPDATE_UNIT}.timer"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        service.unlink(missing_ok=True)
        timer.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        info("Автообновление выключено.")


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


def print_status() -> None:
    width = max(len(m["name"]) for m in catalog())
    for m in catalog():
        print(f"  {m['name']:<{width}}  {module_status(m)}")


# --------------------------------------------------------------------------- меню

def menu() -> None:
    while True:
        mods = catalog()
        print()
        print(_c("1", f"All in One for Linux {VERSION}"))
        width = max(len(m["name"]) for m in mods)
        for i, m in enumerate(mods, 1):
            st = module_status(m)
            dim = linux_spec(m) is None
            line = f"  {i}. {m['name']:<{width}}  {st}"
            print(_c("2", line) if dim else line)
        print()
        print("  номер — модуль · u — обновить всё · a — автообновление "
              f"({'вкл' if autoupdate_enabled() else 'выкл'}) · q — выход")
        try:
            choice = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        try:
            if choice in ("q", "й", "exit", ""):
                return
            if choice in ("u", "г"):
                update_all()
            elif choice in ("a", "ф"):
                autoupdate(not autoupdate_enabled())
            elif choice.isdigit() and 1 <= int(choice) <= len(mods):
                module_menu(mods[int(choice) - 1])
        except AioError as e:
            warn(str(e))
        except KeyboardInterrupt:
            print()


def module_menu(m: dict) -> None:
    spec = linux_spec(m)
    print()
    print(_c("1", m["name"]) + f" — {description(m)}")
    if spec is None:
        print("  Под Linux этого модуля нет.")
        return
    kind = spec.get("kind")
    if kind == "builtin:shutdown-timer":
        print(f"  Сейчас: {timer_status()}")
        when = input("  Когда (23:30, 45m, 1h30m; пусто — отмена таймера): ").strip()
        if not when:
            timer_cancel()
            return
        action = input("  Что сделать (poweroff/reboot/suspend) [poweroff]: ").strip() or "poweroff"
        timer_set(when, action)
        return

    installed = is_installed(m)
    opts = []
    if not installed:
        opts.append(("Установить", lambda: install(m)))
    else:
        opts.append(("Проверить обновление", lambda: install(m)))
        if kind == "zapret":
            opts.append(("Настроить (стратегия, служба)", zapret_tui))
        else:
            path = module_file(m)
            if pids_of(path):
                opts.append(("Остановить", lambda: stop_pids(pids_of(path))))
            else:
                opts.append(("Запустить", lambda: launch(path, spec.get("args"))))
            if spec.get("autostart"):
                on = load_state().get(m["id"], {}).get("autostart", True)
                opts.append(("Выключить автозапуск" if on else "Включить автозапуск",
                             lambda: set_autostart(m, not on)))
        opts.append(("Удалить", lambda: remove(m) if ask(f"Удалить {m['name']}?", False) else None))
    for i, (label, _) in enumerate(opts, 1):
        print(f"  {i}. {label}")
    choice = input("> ").strip()
    if choice.isdigit() and 1 <= int(choice) <= len(opts):
        opts[int(choice) - 1][1]()


def set_autostart(m: dict, on: bool) -> None:
    state = load_state()
    state.setdefault(m["id"], {})["autostart"] = on
    save_state(state)
    if on:
        app_launchers(m, module_file(m))
    else:
        (AUTOSTART_DIR / f"allinone-{m['id']}.desktop").unlink(missing_ok=True)
    info(f"Автозапуск {m['name']} {'включён' if on else 'выключен'}")


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
