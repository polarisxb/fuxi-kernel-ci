/* Ported from SukiSU Ultra 904c60d18f026340ce38ca2d5a1da529aadcba1a. */
// SPDX-License-Identifier: GPL-2.0
#include <linux/kernel.h>
#include <linux/types.h>
#include <linux/cpumask.h>
#include <linux/percpu.h>
#include <linux/jiffies.h>
#include <linux/delay.h>
#include <linux/clocksource.h>
#include <linux/errno.h>
#include <linux/version.h>

#include "cpu_spoof.h"
#include "infra/release_symbol_lookup.h"
#include "klog.h"

#if defined(CONFIG_ARM64) || defined(__aarch64__)
#include <asm/cpu.h>
#include <asm/cputype.h>
#include <asm/cpufeature.h>
#include <vdso/datapage.h>
#include <vdso/clocksource.h>

#ifndef MIDR_PARTNUM_SHIFT
#define MIDR_PARTNUM_SHIFT 4
#endif

#ifndef MIDR_PARTNUM_MASK
#define MIDR_PARTNUM_MASK (0xfff << MIDR_PARTNUM_SHIFT)
#endif

#ifndef MIDR_PARTNUM
#define MIDR_PARTNUM(midr) (((midr) & MIDR_PARTNUM_MASK) >> MIDR_PARTNUM_SHIFT)
#endif

#ifndef ARM_CPU_PART_NEOVERSE_N1
#define ARM_CPU_PART_NEOVERSE_N1 0xd0c
#endif

#ifndef ARM_CPU_PART_CORTEX_A76
#define ARM_CPU_PART_CORTEX_A76 0xd0b
#endif

#ifndef QCOM_CPU_PART_KRYO_4XX_GOLD
#define QCOM_CPU_PART_KRYO_4XX_GOLD 0x804
#endif

#ifndef CS_HRES_COARSE
#define CS_HRES_COARSE 0
#endif

#ifndef CS_RAW
#define CS_RAW 1
#endif

#ifndef VDSO_CLOCKMODE_ARCHTIMER
#define VDSO_CLOCKMODE_ARCHTIMER 1
#endif

#ifndef ARM64_WORKAROUND_1418040
#define ARM64_WORKAROUND_1418040 37
#endif

#endif

int ksu_set_spoof_cpu(const struct ksu_set_spoof_cpu_cmd *cmd)
{
    if (!cmd)
        return -EINVAL;

    /* Boundary Enforcement to prevent out-of-bounds per-CPU page dereferencing */
    if (cmd->cpu_index >= nr_cpu_ids || !cpu_possible(cmd->cpu_index)) {
        pr_warn("ksu: set_spoof_cpu out-of-bounds cpu_index %u\n", cmd->cpu_index);
        return -EINVAL;
    }

#if defined(CONFIG_ARM64) || defined(__aarch64__)
    /* This pinned android13-5.15 branch stores HWCAP/HWCAP2 as the low/high
     * 32 bits of one elf_hwcap word (cpu_get_elf_hwcap{,2}()). Reject values
     * that cannot be represented before applying any change.
     */
    if ((cmd->hwcap >> 32) || (cmd->hwcap2 >> 32))
        return -ERANGE;

    /* Fail before any mutation if the required target data is unavailable.
     * The upstream handler merely logged failed lookups and returned success.
     */
    if (!ksu_release_lookup_data_symbol("cpu_data"))
        return -ENOENT;
    if (cmd->cpu_index == 0 &&
        (!ksu_release_lookup_data_symbol("loops_per_jiffy") ||
         !ksu_release_lookup_data_symbol("elf_hwcap")))
        return -ENOENT;

    /* 1. Resolve 'cpu_data' and overwrite target core MIDR (executed for each CPU) */
    {
        unsigned long cpu_data_base = ksu_release_lookup_data_symbol("cpu_data");
        if (cpu_data_base) {
            struct cpuinfo_arm64 *info = per_cpu_ptr((struct cpuinfo_arm64 *)cpu_data_base, cmd->cpu_index);
            if (info) {
                info->reg_midr = cmd->midr;
            }
        } else {
            pr_warn("ksu: set_spoof_cpu failed to resolve 'cpu_data'\n");
        }
    }

    /* 2. Global modifications (executed exactly once on CPU 0) */
    if (cmd->cpu_index == 0) {
        /* A. Override global dynamic timing metric */
        unsigned long *lpj = (unsigned long *)ksu_release_lookup_data_symbol("loops_per_jiffy");
        if (lpj) {
            pr_info("ksu: set_spoof_cpu current global loops_per_jiffy: %lu\n", *lpj);
            *lpj = ((unsigned long)cmd->bogomips * 500000) / (100 * HZ);
            pr_info("ksu: set_spoof_cpu updated global loops_per_jiffy: %lu\n", *lpj);
        } else {
            pr_warn("ksu: set_spoof_cpu failed to resolve 'loops_per_jiffy'\n");
        }

        /* B. Globally overwrite hardware capabilities */
        unsigned long *elf_hwcap_word = (unsigned long *)ksu_release_lookup_data_symbol("elf_hwcap");
        if (elf_hwcap_word) {
            /* Do not use upstream's [0]/[1] layout: on this branch the
             * second write would corrupt a neighboring kernel variable.
             */
            unsigned long packed = (unsigned long)cmd->hwcap |
                                   ((unsigned long)cmd->hwcap2 << 32);
            WRITE_ONCE(*elf_hwcap_word, packed);
            pr_info("ksu: set_spoof_cpu packed elf_hwcap: 0x%lx\n", packed);
        } else {
            pr_warn("ksu: set_spoof_cpu failed to resolve 'elf_hwcap'\n");
        }

        /* A reported CPU identity does not change the physical CPU.
         * Preserve the real clocksource/vDSO synchronization and errata
         * capabilities instead of overriding them from a spoofed MIDR.
         */

    }
#else
    return -EOPNOTSUPP;
#endif

    pr_info("KernelSU Stealth: CPU %u identity spoofed (MIDR: 0x%08x)\n", cmd->cpu_index, cmd->midr);
    return 0;
}
