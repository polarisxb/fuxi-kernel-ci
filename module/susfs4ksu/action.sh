#!/system/bin/sh
echo 'SUSFS R28 / Fuxi 2.3.0 local adaptation'
sh /data/adb/modules/susfs4ksu/susfs-bin-check.sh || exit 1
/data/adb/ksu/bin/ksu_susfs show version
/data/adb/ksu/bin/ksu_susfs show enabled_features
