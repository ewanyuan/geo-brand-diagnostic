#!/bin/sh
# /src is a source-only read-only mount, /packages and /reports are empty outputs.
set -eu
geo_work=$(mktemp -d /tmp/geo-linux-build-XXXXXX)
cp -a /src/. "$geo_work/"
cd "$geo_work"
python deploy/build_desktop.py --platform linux > /reports/linux-build.log 2>&1
geo_release=$(python -c 'from app_paths import VERSION;from deploy.build_desktop import native_target;print("linux-"+native_target()[1]+"-"+VERSION)')
geo_archive="dist/$geo_release/GEO-$geo_release.tar.gz"
cp "$geo_archive" "dist/$geo_release/package-manifest.json" "dist/$geo_release/SHA256.txt" /packages/
python tools/verify_desktop_package.py "$geo_archive" --output /reports/linux-package-check.json > /reports/linux-package-check.log 2>&1
python tools/verify_linux_visible.py --output /reports > /reports/linux-visible-check.log 2>&1
if [ "${GEO_SKIP_SOURCE_TESTS:-0}" != "1" ]; then
    python -m unittest discover -s tests -v > /reports/linux-unittest.log 2>&1
fi
