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
    /* Spoof only the reported MIDR. On arm64, loops_per_jiffy is the
     * udelay()/ndelay() calibration (xloops_to_cycles()), and elf_hwcap feeds
     * AT_HWCAP/AT_HWCAP2 and the HWCAP check run when a CPU comes online, so
     * neither is a display value. Upstream rewrote both from this request:
     * the Manager's pre-filled BogoMIPS of 38 (kernel unit: 3840) made every
     * delay about 100x shorter, and the default hwcap of 0 cleared all
     * HWCAPs. Accept only 0 ("unchanged") and fail before any mutation.
     */
    if (cmd->bogomips || cmd->hwcap || cmd->hwcap2) {
        pr_warn("ksu: set_spoof_cpu only spoofs MIDR; bogomips/hwcap/hwcap2 must be 0\n");
        return -EOPNOTSUPP;
    }

    {
        /* Fail before any mutation if the required target data is unavailable.
         * The upstream handler merely logged failed lookups and returned success.
         */
        unsigned long cpu_data_base = ksu_release_lookup_data_symbol("cpu_data");
        struct cpuinfo_arm64 *info;

        if (!cpu_data_base)
            return -ENOENT;

        /* Only /proc/cpuinfo and sysfs midr_el1 report this field. Errata
         * detection reads MIDR_EL1 itself, and bringing a CPU online stores
         * the real value again.
         */
        info = per_cpu_ptr((struct cpuinfo_arm64 *)cpu_data_base, cmd->cpu_index);
        info->reg_midr = cmd->midr;
    }
#else
    return -EOPNOTSUPP;
#endif

    pr_info("KernelSU Stealth: CPU %u identity spoofed (MIDR: 0x%08x)\n", cmd->cpu_index, cmd->midr);
    return 0;
}
