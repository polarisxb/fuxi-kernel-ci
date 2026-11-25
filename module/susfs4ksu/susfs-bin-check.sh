#!/system/bin/sh
expected=@KSU_SUSFS_SHA256@
actual=$(sha256sum /data/adb/ksu/bin/ksu_susfs 2>/dev/null | awk '{print $1}')
if [ "$actual" = "$expected" ]; then echo match; else echo mismatch; exit 1; fi
