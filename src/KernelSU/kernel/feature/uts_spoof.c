/* Ported from SukiSU Ultra 904c60d18f026340ce38ca2d5a1da529aadcba1a. */

#include <linux/string.h>

#include "uts_spoof.h"
#include "infra/release_symbol_lookup.h"
#include "klog.h"

static void do_spoof_version(struct rw_semaphore *sem, struct uts_namespace *ns,
                             const char *release, const char *version)
{
    if (sem) {
        down_write(sem);
    } else {
        down_write(&uts_sem);
    }

    if (ns) {
        if (release && release[0] != '\0') {
            strscpy(ns->name.release, release, sizeof(ns->name.release));
        }
        if (version && version[0] != '\0') {
            strscpy(ns->name.version, version, sizeof(ns->name.version));
        }
    } else {
        if (release && release[0] != '\0') {
            strscpy(init_uts_ns.name.release, release, sizeof(init_uts_ns.name.release));
        }
        if (version && version[0] != '\0') {
            strscpy(init_uts_ns.name.version, version, sizeof(init_uts_ns.name.version));
        }
    }

    if (sem) {
        up_write(sem);
    } else {
        up_write(&uts_sem);
    }

    if (ns) {
        pr_info("ksu: spoofed version: %s, release: %s\n", ns->name.version, ns->name.release);
    } else {
        pr_info("ksu: spoofed version: %s, release: %s\n", init_uts_ns.name.version, init_uts_ns.name.release);
    }
}

void ksu_spoof_version(const char *spoof_release, const char *spoof_version)
{
    struct rw_semaphore *sem = (struct rw_semaphore *)ksu_release_lookup_data_symbol("uts_sem");
    struct uts_namespace *ns = (struct uts_namespace *)ksu_release_lookup_data_symbol("init_uts_ns");

    do_spoof_version(sem, ns, spoof_release, spoof_version);
}

int ksu_set_spoof_version(const char *release, const char *version)
{
    struct rw_semaphore *sem = (struct rw_semaphore *)ksu_release_lookup_data_symbol("uts_sem");
    struct uts_namespace *ns = (struct uts_namespace *)ksu_release_lookup_data_symbol("init_uts_ns");

    do_spoof_version(sem, ns, release, version);

    return 0;
}
