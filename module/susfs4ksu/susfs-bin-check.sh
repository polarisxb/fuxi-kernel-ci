#!/system/bin/sh
expected=8b9fa0174ab9bc95910258f935628dfb9b551ae9b5074ba0eb1fa85e06590641
actual=$(sha256sum /data/adb/ksu/bin/ksu_susfs 2>/dev/null | awk '{print $1}')
if [ "$actual" = "$expected" ]; then echo match; else echo mismatch; exit 1; fi
