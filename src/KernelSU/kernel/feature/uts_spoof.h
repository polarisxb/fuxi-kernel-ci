/* Ported from SukiSU Ultra 904c60d18f026340ce38ca2d5a1da529aadcba1a. */
#ifndef __KSU_H_UTS_SPOOF
#define __KSU_H_UTS_SPOOF

#include <linux/rwsem.h>
#include <linux/utsname.h>

void ksu_spoof_version(const char *spoof_release, const char *spoof_version);

int ksu_set_spoof_version(const char *release, const char *version);

#endif
