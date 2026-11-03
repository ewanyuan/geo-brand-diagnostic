#!/bin/sh
set -eu
geo_program_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
    printf '%s\n' 'GEO 登录与采集需要 Linux 桌面环境，请在桌面会话中启动。' >&2
    exit 1
fi
exec "$geo_program_dir/geo-diagnostic"
