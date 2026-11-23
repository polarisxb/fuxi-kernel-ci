/* SPDX-License-Identifier: GPL-2.0 */
#ifndef __KSU_RELEASE_SYMBOL_LOOKUP_H
#define __KSU_RELEASE_SYMBOL_LOOKUP_H

#include <linux/kallsyms.h>

/* The official release spoof handlers resolve data symbols only. This
 * built-in kernel can use kallsyms directly; no dynamic syscall/LSM hooks or
 * function-pointer resolver are imported from the LKM-capable branch.
 */
static inline unsigned long ksu_release_lookup_data_symbol(const char *name)
{
    return kallsyms_lookup_name(name);
}

#endif
