/* Host fixtures for the actual SET_SPOOF_CPU handler, inserted by the runner. */
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define CONFIG_ARM64 1
#define HZ 250
#define TEST_CPUS 8
#define CHECK(condition) do { if (!(condition)) { \
    fprintf(stderr, "%s:%d: %s\n", __FILE__, __LINE__, #condition); exit(1); \
} } while (0)

typedef uint32_t __u32;
typedef uint64_t __u64;

/* SOURCE_STRUCTURES */

/* Same member type as arch/arm64/include/asm/cpu.h on the pinned branch. */
struct cpuinfo_arm64 { uint64_t reg_midr; };

/* Values the fuxi kernel reports before any spoofing: a 19.2 MHz arch timer
 * at HZ 250 gives loops_per_jiffy 76800 (BogoMIPS 38.40). The HWCAP word is
 * an arbitrary non-zero marker.
 */
#define REAL_MIDR UINT64_C(0x410fd461)
#define REAL_LPJ 76800UL
#define REAL_HWCAP ((unsigned long)UINT64_C(0x0000a3ff0fffffff))
#define SPOOFED_MIDR 0x412fd050U

static struct cpuinfo_arm64 cpu_data[TEST_CPUS];
static unsigned long loops_per_jiffy;
static unsigned long elf_hwcap;
static unsigned int nr_cpu_ids = TEST_CPUS;
static unsigned int possible_cpus = 0x7f; /* CPU 7 is below nr_cpu_ids but not possible. */
static int fail_lookup;
static const char *resolved[8];
static int resolved_count;

#define cpu_possible(cpu) (((possible_cpus >> (cpu)) & 1U) != 0)
#define per_cpu_ptr(ptr, cpu) (&(ptr)[(cpu)])
#define WRITE_ONCE(target, value) ((target) = (value))
#define pr_warn(...) ((void)0)
#define pr_info(...) ((void)0)

/* Resolves the kernel data an implementation might write, so a regression
 * that writes loops_per_jiffy or elf_hwcap again fails an assertion instead
 * of failing to link.
 */
static unsigned long ksu_release_lookup_data_symbol(const char *name)
{
    CHECK(resolved_count < (int)(sizeof(resolved) / sizeof(resolved[0])));
    resolved[resolved_count++] = name;
    if (fail_lookup)
        return 0;
    if (!strcmp(name, "cpu_data"))
        return (unsigned long)cpu_data;
    if (!strcmp(name, "loops_per_jiffy"))
        return (unsigned long)&loops_per_jiffy;
    if (!strcmp(name, "elf_hwcap"))
        return (unsigned long)&elf_hwcap;
    return 0;
}

int ksu_set_spoof_cpu(const struct ksu_set_spoof_cpu_cmd *cmd);

/* SOURCE_FUNCTIONS */

static void reset(void)
{
    for (int cpu = 0; cpu < TEST_CPUS; cpu++)
        cpu_data[cpu].reg_midr = REAL_MIDR;
    loops_per_jiffy = REAL_LPJ;
    elf_hwcap = REAL_HWCAP;
    fail_lookup = 0;
    resolved_count = 0;
}

/* changed_cpu < 0 means no CPU may report the spoofed MIDR. */
static void check_state(int changed_cpu)
{
    for (int cpu = 0; cpu < TEST_CPUS; cpu++)
        CHECK(cpu_data[cpu].reg_midr == (cpu == changed_cpu ? SPOOFED_MIDR : REAL_MIDR));
    CHECK(loops_per_jiffy == REAL_LPJ);
    CHECK(elf_hwcap == REAL_HWCAP);
    for (int index = 0; index < resolved_count; index++)
        CHECK(!strcmp(resolved[index], "cpu_data"));
}

static struct ksu_set_spoof_cpu_cmd request(unsigned int cpu, unsigned int bogomips,
                                            uint64_t hwcap, uint64_t hwcap2)
{
    struct ksu_set_spoof_cpu_cmd cmd;

    memset(&cmd, 0, sizeof(cmd));
    cmd.cpu_index = cpu;
    cmd.midr = SPOOFED_MIDR;
    cmd.bogomips = bogomips;
    cmd.hwcap = hwcap;
    cmd.hwcap2 = hwcap2;
    return cmd;
}

int main(int argc, char **argv)
{
    const char *name = argc == 2 ? argv[1] : "";
    struct ksu_set_spoof_cpu_cmd cmd;

    reset();
    if (!strcmp(name, "midr-only")) {
        /* ksud sends 0 when --bogomips/--hwcap/--hwcap2 are omitted. */
        cmd = request(0, 0, 0, 0);
        CHECK(ksu_set_spoof_cpu(&cmd) == 0);
        check_state(0);
    } else if (!strcmp(name, "secondary-cpu")) {
        cmd = request(3, 0, 0, 0);
        CHECK(ksu_set_spoof_cpu(&cmd) == 0);
        check_state(3);
    } else if (!strcmp(name, "manager-prefill")) {
        /* The Manager turns "BogoMIPS : 38.40" into 38; upstream wrote lpj 760. */
        cmd = request(0, 38, 0, 0);
        CHECK(ksu_set_spoof_cpu(&cmd) == -EOPNOTSUPP);
        check_state(-1);
        cmd = request(2, 3840, 0, 0);
        CHECK(ksu_set_spoof_cpu(&cmd) == -EOPNOTSUPP);
        check_state(-1);
    } else if (!strcmp(name, "hwcap")) {
        cmd = request(0, 0, REAL_HWCAP & 0xffffffffUL, 0);
        CHECK(ksu_set_spoof_cpu(&cmd) == -EOPNOTSUPP);
        check_state(-1);
        cmd = request(1, 0, 0, 1);
        CHECK(ksu_set_spoof_cpu(&cmd) == -EOPNOTSUPP);
        check_state(-1);
    } else if (!strcmp(name, "cpu-bounds")) {
        cmd = request(TEST_CPUS, 0, 0, 0);
        CHECK(ksu_set_spoof_cpu(&cmd) == -EINVAL);
        cmd = request(7, 0, 0, 0);
        CHECK(ksu_set_spoof_cpu(&cmd) == -EINVAL);
        CHECK(ksu_set_spoof_cpu(NULL) == -EINVAL);
        check_state(-1);
    } else if (!strcmp(name, "lookup-failure")) {
        fail_lookup = 1;
        cmd = request(0, 0, 0, 0);
        CHECK(ksu_set_spoof_cpu(&cmd) == -ENOENT);
        check_state(-1);
    } else {
        fprintf(stderr, "unknown case: %s\n", name);
        return 2;
    }
    return 0;
}
