<p align="center"><img src="packaging/allinone.png" width="128" alt="All in One"></p>

# All in One for Linux

Linux-версия [All in One](https://github.com/Solevaral/All-in-one): ставит, обновляет и запускает программы-модули из того же [каталога](https://github.com/Solevaral/All-in-one-modules). Меню в терминале и команда `aio`.

## Модули

| Модуль | Что делает | Под Linux |
|---|---|---|
| **zapret** | Обход блокировок Discord и YouTube | [zapret-rust](https://github.com/Sergeydigl3/zapret-discord-youtube-rust) от Sergeydigl3, служба systemd |
| **TG WS Proxy** | Локальный MTProto-прокси для Telegram | [Flowseal/tg-ws-proxy](https://github.com/Flowseal/tg-ws-proxy), автозапуск при входе |
| **TryToCatchMe** | VPN-клиент на sing-box | [AppImage](https://github.com/Solevaral/TryToCatchMe-client), ярлык в меню |
| **Таймер выключения** | Выключение, перезагрузка или сон по времени или через интервал | встроен, `aio timer` |
| fDimmer, magniF | — | только Windows |

## Установка

Нужен Python 3. В Arch и CachyOS это пакет `python`, он почти всегда уже стоит.

**AppImage.** Скачай `AllInOne-<версия>-x86_64.AppImage` из [Releases](https://github.com/Solevaral/All-in-one-for-linux/releases) и выполни:

```bash
chmod +x AllInOne-*-x86_64.AppImage
./AllInOne-*-x86_64.AppImage self-install
```

Программа копируется в `~/Applications/AllInOne.AppImage`, появляются команда `aio` (в `~/.local/bin`) и ярлык «All in One» в меню.

**Arch / CachyOS.** Скачай `PKGBUILD` из релиза и в его папке выполни `makepkg -si`. В этом варианте All in One обновляется как пакет.

## Использование

```bash
aio                       # меню: модули, обновления, автообновление
aio install zapret tg-ws-proxy trytocatchme
aio update                # все установленные модули и сам All in One
aio status
aio remove tg-ws-proxy
aio zapret                # настройка zapret: стратегия, Game Filter, служба
aio timer 23:30           # выключение в 23:30; ещё 45m, 1h30m, … reboot | suspend
aio timer cancel
aio autoupdate on         # проверка обновлений раз в день, с уведомлением
```

### zapret

zapret-rust работает от root, поэтому лежит в `/opt/allinone/zapret`, в каталоге, принадлежащем root: подменить бинарник, который запускает служба, может только root. После `aio install zapret` один раз запусти `aio zapret` и в его меню:

1. «Загрузчик → Скачать всё»: скачаются `nfqws` и стратегии;
2. выбери интерфейс и стратегию;
3. «Служба → Установить»: служба `zapret-rust` запустится и будет стартовать вместе с системой.

Готовый `conf.env` можно подложить при установке: `aio install zapret --zapret-config conf.env`. Существующий файл не перезаписывается. При обновлении служба останавливается и запускается снова.

## Где что лежит

```
~/.local/share/allinone/modules/<модуль>/   программы
~/.local/share/allinone/state.json          установленные версии
/opt/allinone/zapret/                       zapret-rust, conf.env, стратегии (root)
~/.local/share/applications/allinone-*.desktop, ~/.config/autostart/allinone-*.desktop
```

Версии модулей берутся из GitHub Releases. Без входа GitHub даёт 60 запросов в час. Если лимит кончился, подожди или задай переменную окружения `GITHUB_TOKEN`.

## Каталог

Модуль с полем `linux` в записи [catalog.json](https://github.com/Solevaral/All-in-one-modules) появляется здесь сам, без нового релиза. Формат поля описан в README каталога. Если каталог недоступен, используется встроенная копия.

## Сборка

```bash
python3 -m unittest discover -s tests
bash build-appimage.sh        # dist/AllInOne-<версия>-x86_64.AppImage
```

Релиз собирается по тегу `v*` (`.github/workflows/release.yml`). Версия тега должна совпадать с `VERSION` в `aio.py`.

## Лицензия

MIT
