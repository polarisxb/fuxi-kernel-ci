/* SPDX-License-Identifier: GPL-2.0 */
#ifndef _LINUX_SUKISU_HOOKS_H
#define _LINUX_SUKISU_HOOKS_H

#include <linux/compiler_types.h>
#include <linux/types.h>

/* Shared by patched common and SukiSU: SUSFS resolves getname() before hooks. */
struct filename;
extern bool ksu_su_compat_enabled;
int ksu_handle_faccessat(int *dfd, struct filename **filename, int *mode,
                        int *flags);
int ksu_handle_stat(int *dfd, struct filename **filename, int *flags);
int ksu_handle_sys_read(unsigned int fd, char __user **buf_ptr, size_t *count_ptr);

#endif
