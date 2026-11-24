#!/bin/sh
PATH=/data/adb/ksu/bin:/data/data/com.termux/files/usr/bin:$PATH
KSU_BIN=/data/adb/ksu/bin/ksud
DEST_BIN_DIR=/data/adb/ksu/bin

if [ -z "$KSU" ] ; then
	abort '[!] SuSFS is for KernelSU only.'
fi

if [ ! -d ${DEST_BIN_DIR} ]; then
    ui_print "'${DEST_BIN_DIR}' not existed, installation aborted."
    rm -rf ${MODPATH}
    exit 1
fi

unzip -qq ${ZIPFILE} -d ${TMPDIR}/susfs

susfs4ksu_config_check() {
  ui_print " "
  ui_print "****************************************"
  ui_print "     SUSFS4KSU Config Folder exists     "
  ui_print "****************************************"
  ui_print "     Do you want to reset settings?     "
  ui_print "****************************************"
  ui_print "  Volume Up (+): Reset to default"
  ui_print "  Volume Down (-): Keep current settings"
  ui_print " "
  ui_print "  Keep current settings in 10 seconds"
  ui_print "****************************************"
  local timeout=10
  local start_time=$(date +%s)
  while true; do
    local current_time=$(date +%s)
    if [ $((current_time - start_time)) -ge $timeout ]; then
      ui_print "[-] Timeout: Selected keep current settings"
      break
    fi
    local key_event=$(timeout 0.5 getevent -l 2>/dev/null)
    if echo "$key_event" | grep -q "KEY_VOLUMEUP"; then
      ui_print "[-] Key Detected: Selected Yes, reset to default"
	  ui_print "[-] Resetting susfs4ksu settings to default..."
	  rm -rf /data/adb/susfs4ksu
      break
    elif echo "$key_event" | grep -q "KEY_VOLUMEDOWN"; then
      ui_print "[-] Key Detected: Selected No, keep current settings"
      break
    fi
  done
}

# Pinned local adaptation: no network-selected root executable.
ksuver=$(${KSU_BIN} debug version | awk '{print $3}')
[ "$ksuver" = "40900" ] || abort "[!] This local package requires driver 40900."
PINNED_TOOL="${TMPDIR}/susfs/tools/ksu_susfs_arm64"
PINNED_TOOL_SHA=@KSU_SUSFS_SHA256@
actual_sha=$(sha256sum "$PINNED_TOOL" | awk '{print $1}')
[ "$actual_sha" = "$PINNED_TOOL_SHA" ] || abort "[!] Pinned SUSFS helper checksum mismatch."
chmod 755 "$PINNED_TOOL" || abort "[!] Cannot make pinned helper executable."
SUSFS_VERSION_RAW=$("$PINNED_TOOL" show version) || abort "[!] Cannot query SUSFS."
[ "$SUSFS_VERSION_RAW" = "v2.3.0" ] || abort "[!] This local package requires SUSFS v2.3.0."
ui_print "[-] Verified driver $ksuver and SUSFS $SUSFS_VERSION_RAW"
cp -f "$PINNED_TOOL" "$DEST_BIN_DIR/ksu_susfs" || abort "[!] Cannot install pinned helper."
chmod 755 "$DEST_BIN_DIR/ksu_susfs" || abort "[!] Cannot set helper permissions."
installed_sha=$(sha256sum "$DEST_BIN_DIR/ksu_susfs" | awk '{print $1}')
[ "$installed_sha" = "$PINNED_TOOL_SHA" ] || abort "[!] Installed helper checksum mismatch."
ui_print "[-] Installed the helper built from the kernel's SUSFS source; online binary replacement disabled."

# cleanup
rm -f ${MODPATH}/ksu_susfs_remote > /dev/null 2>&1

# set permissions
chmod 644 ${MODPATH}/post-fs-data.sh ${MODPATH}/post-mount.sh ${MODPATH}/service.sh ${MODPATH}/boot-completed.sh ${MODPATH}/action.sh ${MODPATH}/uninstall.sh ${MODPATH}/susfs-bin-update.sh ${MODPATH}/susfs_reset.sh

# Check if config folder exists
if [ -d /data/adb/susfs4ksu ]; then
susfs4ksu_config_check
fi

prop_value=$(getprop ro.boot.vbmeta.digest)
HASH_DIR=/data/adb/VerifiedBootHash
if ${KSU_BIN} module list | grep -qE "vbmeta-fixer|TA_utl"; then
	ui_print "****************************************"
	ui_print "! vbmeta-fixer or Tricky Addon module detected"
	ui_print "! skipping VerifiedBootHash creation"
	ui_print "****************************************"
else
	if [ -z "$prop_value" ]; then
		ui_print "[!] Property ro.boot.vbmeta.digest is empty, generate VerifiedBootHash directory"
		if [ ! -d "$HASH_DIR" ]; then
		ui_print "[-] Creating VerifiedBootHash directory"
		mkdir -p "$HASH_DIR"
		[ ! -f "$HASH_DIR/VerifiedBootHash.txt" ] && touch "$HASH_DIR/VerifiedBootHash.txt"
		fi
		ui_print "****************************************"
		ui_print "! Please copy your VerifiedBootHash in Key Attestation demo"
		ui_print "! And Paste it to /data/adb/VerifiedBootHash/VerifiedBootHash.txt"
		ui_print "****************************************"
	else
		ui_print "****************************************"
		ui_print "! Property ro.boot.vbmeta.digest has a value"
		ui_print "! skipping VerifiedBootHash creation"
		ui_print "****************************************"
	fi
fi

ui_print "[-] Preparing susfs4ksu persistent directory"
PERSISTENT_DIR=/data/adb/susfs4ksu
[ ! -d /data/adb/susfs4ksu ] && mkdir -p $PERSISTENT_DIR
files="sus_mount.txt try_umount.txt sus_path.txt sus_path_loop.txt sus_maps.txt sus_open_redirect.txt legit_mounts.txt sus_kstat_statically.json config.sh"
for i in $files ; do
    if [ ! -f $PERSISTENT_DIR/$i ] ; then
        # If file doesn't exist, create it
        cat $MODPATH/$i > $PERSISTENT_DIR/$i
    elif [ "$i" = "config.sh" ]; then
        # Ensure file ends with newline
        [ -s "$PERSISTENT_DIR/$i" ] && [ "$(tail -c1 "$PERSISTENT_DIR/$i" | xxd -p)" != "0a" ] && echo "" >> "$PERSISTENT_DIR/$i"
        # For config.sh, append only new keys
        while IFS= read -r line; do
            # Extract key name before = sign
            key=$(echo "$line" | cut -d'=' -f1)
            # Only append if key doesn't exist
            grep -q "^${key}=" "$PERSISTENT_DIR/$i" || echo "$line" >> "$PERSISTENT_DIR/$i"
        done < "$MODPATH/$i"
    fi
    rm $MODPATH/$i
done

# Random ro.boot.vbmeta.size value
vbmeta_size=$(( 5504 + (RANDOM % 14 + 1)*1024 ))  # 5504~16384

# Data persistence
if grep -q "^vbmeta_size=" /data/adb/susfs4ksu/config.sh; then
    sed -i "s/^vbmeta_size=.*/vbmeta_size=$vbmeta_size/" /data/adb/susfs4ksu/config.sh
else
    echo "vbmeta_size=$vbmeta_size" >> /data/adb/susfs4ksu/config.sh
fi

rm -rf ${MODPATH}/tools
rm ${MODPATH}/customize.sh

# EOF
