#!/usr/bin/env bash
# Сборка AppImage: bash build-appimage.sh [версия]
# Версия по умолчанию — VERSION из aio.py; если передана, должна с ней совпадать.
set -euo pipefail
cd "$(dirname "$0")"

ver=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' aio.py)
if [[ $# -ge 1 && "${1#v}" != "$ver" ]]; then
    echo "Версия ${1#v} не совпадает с VERSION в aio.py ($ver)" >&2
    exit 1
fi

tool=build/appimagetool
if [[ ! -x $tool ]]; then
    mkdir -p build
    curl -fL --retry 3 -o "$tool" \
        https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
    chmod +x "$tool"
fi

app=build/AppDir
rm -rf "$app"
install -Dm755 packaging/appimage/AppRun "$app/AppRun"
install -Dm755 aio.py "$app/usr/share/allinone/aio.py"
install -Dm644 packaging/allinone.desktop "$app/allinone.desktop"
install -Dm644 packaging/allinone.png "$app/allinone.png"
install -Dm644 packaging/allinone.png "$app/usr/share/icons/hicolor/512x512/apps/allinone.png"
ln -s allinone.png "$app/.DirIcon"

mkdir -p dist
out="dist/AllInOne-$ver-x86_64.AppImage"
# --appimage-extract-and-run: на сборочной машине может не быть FUSE.
ARCH=x86_64 "$tool" --appimage-extract-and-run --no-appstream "$app" "$out"
echo "$out"
