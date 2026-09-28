#!/usr/bin/env bash
# contrib-модули для встроенного PostgreSQL из пакета pgserver (в его сборке нет contrib): btree_gist (I-12),
# pg_stat_statements и auto_explain (фаза 9 §3), amcheck (проверка восстановленного бэкапа §4).
#
#   scripts/build_pg_contrib.sh build <pg-source-dir> [module ...]   # компилирует в .pgext/<module>/ (zig cc из PyPI, без root)
#   scripts/build_pg_contrib.sh install [module ...]                 # копирует из .pgext/ в site-packages/pgserver (после каждого uv sync)
#
# Исходники: https://ftp.postgresql.org/pub/source/v16.2/ (та же минорная версия, что у pgserver — pg_config --version).
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
EXT="$HERE/.pgext"
PY="$HERE/.venv/bin/python"
PGCONFIG="$("$PY" -c 'import pgserver, os; print(os.path.join(os.path.dirname(pgserver.__file__), "pginstall", "bin", "pg_config"))')"
ALL="btree_gist pg_stat_statements auto_explain amcheck"
cmd="${1:-}"; shift || true
case "$cmd" in
  build)
    SRC="${1:?pg source dir}"; shift || true
    mods="${*:-$ALL}"
    for m in $mods; do
      out="$EXT/$m"; mkdir -p "$out"
      uvx --from ziglang python -m ziglang cc -shared -fPIC -O2 -fno-strict-aliasing -fwrapv -fvisibility=hidden \
        -I"$("$PGCONFIG" --includedir-server)" -I"$("$PGCONFIG" --includedir)" \
        "$SRC"/contrib/"$m"/*.c -o "$out/$m.so"
      cp "$SRC"/contrib/"$m"/*.control "$out/" 2>/dev/null || true
      cp "$SRC"/contrib/"$m"/*.sql "$out/" 2>/dev/null || true
      echo "built $m: $(ls "$out" | wc -l) files"
    done
    ;;
  install)
    mods="${*:-$ALL}"
    for m in $mods; do
      [ -f "$EXT/$m/$m.so" ] || { echo "нет $EXT/$m/$m.so — сначала: $0 build <pg-src> $m"; exit 1; }
      install -m 755 "$EXT/$m/$m.so" "$("$PGCONFIG" --pkglibdir)/$m.so"
      mkdir -p "$("$PGCONFIG" --sharedir)/extension"
      if ls "$EXT/$m"/*.control >/dev/null 2>&1; then
        install -m 644 "$EXT/$m"/*.control "$EXT/$m"/*.sql "$("$PGCONFIG" --sharedir)/extension/"
      fi
      echo "installed $m"
    done
    ;;
  *) sed -n '2,8p' "$0"; exit 1;;
esac
