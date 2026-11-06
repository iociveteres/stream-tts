#!/bin/sh
# Заглушки библиотек CUDA для облегчённого torch ESpeech в Docker (docs/espeech.md, «Поставка»): по скрипту версий
# <папка>/**/<soname>.map (пишет packaging/pin-espeech.py) — библиотека <вывод>/**/<soname> с тем же SONAME, теми
# же символами и версиями. Все функции — stub_fail (код ошибки — fail=N в заголовке .map: вызов даст ошибку torch, а
# не тихий мусор), *String/*Name — stub_str (строка "stub"). Без libc и без зависимостей, по 14–20 КБ.
# usage: packaging/linux/build-stubs.sh <папка .map> <папка вывода>   (нужен gcc; CC — другой компилятор)
set -eu
src=${1:?папка .map} out=${2:?папка вывода}
cc=${CC:-gcc}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
find "$src" -name '*.map' | sort | while read -r map; do
    rel=${map#"$src"/}
    lib=$(basename "$map" .map)
    dir=$out/$(dirname "$rel")
    fail=$(sed -n 's/.*fail=\([0-9][0-9]*\).*/\1/p' "$map" | head -n 1)
    c=$tmp/$lib.c
    {
        echo 'static const char stub_msg[] = "stub";'
        echo "__attribute__((visibility(\"hidden\"))) long stub_fail(void) { return ${fail:-1}; }"
        echo '__attribute__((visibility("hidden"))) const char *stub_str(void) { return stub_msg; }'
        # символы — строки "    имя;" скрипта версий; имя может быть и C++ (_Z…), поэтому псевдоним — через as
        sed -n 's/^ *\([A-Za-z_][A-Za-z0-9_]*\);$/\1/p' "$map" | while read -r s; do
            case $s in *String | *Name) t=stub_str ;; *) t=stub_fail ;; esac
            printf '__asm__(".globl %s\\n.type %s,@function\\n.set %s,%s");\n' "$s" "$s" "$s" "$t"
        done
    } > "$c"
    mkdir -p "$dir"
    $cc -shared -fPIC -O1 -nostdlib -Wl,-soname,"$lib" -Wl,--version-script="$map" -Wl,-z,noexecstack \
        -o "$dir/$lib" "$c"
    echo "$rel -> $dir/$lib"
done
