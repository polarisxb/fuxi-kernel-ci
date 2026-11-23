/* Host API fixtures. Production functions/structures are inserted by the runner. */
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>

#define __user
#define GFP_KERNEL 0
#define SUSFS_MAX_LEN_PATHNAME 256
#define SUSFS_ENABLED_FEATURES_SIZE 8192
#define SUSFS_LOGI(...) ((void)0)
#define SUSFS_LOGE(...) ((void)0)
#define CONFIG_KSU_SUSFS_SUS_PATH 1
#define CONFIG_KSU_SUSFS_SUS_MOUNT 1
#define CONFIG_KSU_SUSFS_SUS_KSTAT 1
#define CONFIG_KSU_SUSFS_SPOOF_UNAME 1
#define CONFIG_KSU_SUSFS_ENABLE_LOG 1
#define CONFIG_KSU_SUSFS_HIDE_KSU_SUSFS_SYMBOLS 1
#define CONFIG_KSU_SUSFS_SPOOF_CMDLINE_OR_BOOTCONFIG 1
#define CONFIG_KSU_SUSFS_OPEN_REDIRECT 1
#define CONFIG_KSU_SUSFS_SUS_MAP 1
#define UID_NON_APP_PROC 0
#define UID_ROOT_PROC_EXCEPT_SU_PROC 1
#define UID_NON_SU_PROC 2
#define UID_UMOUNTED_APP_PROC 3
#define UID_UMOUNTED_PROC 4
#define AS_FLAGS_SUS_KSTAT 3
#define FUSE_SUPER_MAGIC 0x65735546
#define min_t(type, a, b) ((type)(a) < (type)(b) ? (type)(a) : (type)(b))
#define CHECK(condition) do { if (!(condition)) { \
    fprintf(stderr, "%s:%d: %s\n", __FILE__, __LINE__, #condition); exit(1); \
} } while (0)

struct hlist_node { int unused; };
struct address_space { unsigned long flags; };
struct super_block { unsigned long s_dev, s_magic; };
struct inode {
    unsigned long i_ino;
    struct super_block *i_sb;
    struct address_space *i_mapping;
};
struct fuse_inode { struct inode inode; };
struct kstatfs { unsigned long marker; };
struct kstat {
    unsigned long ino, dev, nlink, blksize;
    long long size, blocks;
    struct { long tv_sec; unsigned long tv_nsec; } atime, mtime, ctime;
};
struct filename { char *name; };
struct uid { unsigned int val; };

/* SOURCE_STRUCTURES */

static struct st_susfs_open_redirect_hlist OPEN_REDIRECT_HLIST[3];
static struct st_susfs_sus_kstat_hlist SUS_KSTAT_HLIST[3];
static const size_t OPEN_REDIRECT_HLIST_count = 3;
static const size_t SUS_KSTAT_HLIST_count = 3;
static unsigned int bucket(unsigned long key)
{
    return (key * UINT64_C(0x61C8864680B583EB)) >> 54;
}
#define hash_for_each_possible_rcu(table, entry, node, key) \
    for (size_t index = 0; index < table##_count; index++) \
        if ((entry = &table[index]) && bucket(entry->target_ino) == bucket(key))

static int rcu_depth, srcu_depth, susfs_srcu_open_redirect;
static int srcu_read_lock(int *lock)
{
    CHECK(lock == &susfs_srcu_open_redirect);
    srcu_depth++;
    return 17;
}
static void srcu_read_unlock(int *lock, int index)
{
    CHECK(lock == &susfs_srcu_open_redirect && index == 17 && srcu_depth == 1);
    srcu_depth--;
}
static void rcu_read_lock(void) { rcu_depth++; }
static void rcu_read_unlock(void) { CHECK(rcu_depth == 1); rcu_depth--; }
static unsigned int mock_uid = 10001;
static bool mock_ksu_domain, mock_umounted_app = true, mock_umounted = true;
static struct uid current_uid(void) { return (struct uid){mock_uid}; }
static bool susfs_is_current_ksu_domain(void) { return mock_ksu_domain; }
static bool susfs_is_current_proc_umounted_app(void) { return mock_umounted_app; }
static bool susfs_is_current_proc_umounted(void) { return mock_umounted; }
static struct fuse_inode *mock_fuse_inode;
static struct fuse_inode *get_fuse_inode(struct inode *inode)
{
    (void)inode;
    return mock_fuse_inode;
}
static bool test_bit(int bit, const unsigned long *flags)
{
    return (*flags & (1UL << bit)) != 0;
}
static bool allocation_failure, filename_failure, copyin_failure, copyout_failure;
static int allocations, releases, copyin_calls, copyout_calls;
static void *kzalloc(size_t size, int flags)
{
    (void)flags;
    if (allocation_failure) return NULL;
    allocations++;
    return calloc(1, size);
}
static void kfree(void *ptr)
{
    if (ptr) releases++;
    free(ptr);
}
static struct filename *getname_kernel(const char *name)
{
    if (filename_failure) return (struct filename *)(intptr_t)-ENOMEM;
    struct filename *filename = malloc(sizeof(*filename));
    CHECK(filename != NULL);
    filename->name = strdup(name);
    CHECK(filename->name != NULL);
    return filename;
}
static void putname(struct filename *filename)
{
    free(filename->name);
    free(filename);
}
static int copy_to_user(void *to, const void *from, size_t size)
{
    copyout_calls++;
    if (copyout_failure) return 1;
    if (size) memcpy(to, from, size);
    return 0;
}
static int copy_from_user(void *to, const void *from, size_t size)
{
    copyin_calls++;
    if (copyin_failure) return 1;
    if (size) memcpy(to, from, size);
    return 0;
}
static ssize_t strscpy(char *to, const char *from, size_t size)
{
    if (!size) return -E2BIG;
    size_t length = strlen(from);
    size_t copied = length < size ? length : size - 1;
    memcpy(to, from, copied);
    to[copied] = '\0';
    return length < size ? (ssize_t)length : -E2BIG;
}

/* SOURCE_FUNCTIONS */

static struct super_block sb = {.s_dev = 5};
static struct address_space mapping = {.flags = 1UL << AS_FLAGS_SUS_KSTAT};
static struct inode target = {.i_ino = 1, .i_sb = &sb, .i_mapping = &mapping};

static void redirect_fixture(bool reverse)
{
    CHECK(bucket(1) == bucket(988));
    for (int i = 0; i < 3; i++) {
        struct st_susfs_open_redirect_hlist *entry = &OPEN_REDIRECT_HLIST[i];
        entry->target_ino = i == 0 ? 988 : 1;
        entry->target_dev = i == 1 ? 6 : 5;
        entry->reversed_lookup_only = reverse;
        entry->redirected_ino = 700 + i;
        entry->redirected_dev = 800 + i;
        entry->spoofed_mnt_id = 900 + i;
        entry->spoofed_kstatfs.marker = 1000 + i;
        entry->info.uid_scheme = UID_UMOUNTED_APP_PROC;
        strcpy(entry->info.target_pathname, "/real-A");
        strcpy(entry->info.redirected_pathname, i == 2 ? "/fake-A" : "/fake-B");
    }
}

static void kstat_fixture(bool fuse)
{
    for (int i = 0; i < 3; i++) {
        struct st_susfs_sus_kstat_hlist *entry = &SUS_KSTAT_HLIST[i];
        entry->target_ino = i == 0 ? 988 : 1;
        entry->target_dev = i == 1 ? 6 : 5;
        entry->is_fuse = fuse;
        entry->info.flags = 0xfff;
        entry->info.spoofed_ino = 100 + i;
        entry->info.spoofed_dev = 200 + i;
        entry->info.spoofed_nlink = 300 + i;
        entry->info.spoofed_size = 400 + i;
        entry->info.spoofed_blocks = 500 + i;
        entry->info.spoofed_blksize = 600 + i;
        entry->info.spoofed_atime_tv_sec = 700 + i;
        entry->info.spoofed_atime_tv_nsec = 800 + i;
        entry->info.spoofed_mtime_tv_sec = 900 + i;
        entry->info.spoofed_mtime_tv_nsec = 1000 + i;
        entry->info.spoofed_ctime_tv_sec = 1100 + i;
        entry->info.spoofed_ctime_tv_nsec = 1200 + i;
    }
}

static void check_kstat(bool matches)
{
    struct kstat stat = {.ino = 42, .dev = 43};
    dev_t dev = 43;
    unsigned long ino = 42;
    susfs_sus_kstat_spoof_generic_fillattr(&target, &stat);
    susfs_sus_kstat_spoof_show_map_vma(&target, &dev, &ino);
    CHECK(stat.ino == (matches ? 102UL : 42UL));
    CHECK(stat.dev == (matches ? 202UL : 43UL));
    CHECK(ino == stat.ino && dev == stat.dev);
    if (matches) {
        CHECK(stat.nlink == 302 && stat.size == 402 && stat.blocks == 502);
        CHECK(stat.blksize == 602 && stat.atime.tv_sec == 702 && stat.atime.tv_nsec == 802);
        CHECK(stat.mtime.tv_sec == 902 && stat.mtime.tv_nsec == 1002);
        CHECK(stat.ctime.tv_sec == 1102 && stat.ctime.tv_nsec == 1202);
    }
}

static void check_open(bool matches)
{
    struct filename *filename = susfs_open_redirect_spoof_do_sys_openat(&target);
    if (!matches) { CHECK(filename == NULL); return; }
    CHECK(filename != NULL && (uintptr_t)filename < (uintptr_t)-4095);
    CHECK(!strcmp(filename->name, "/fake-A"));
    putname(filename);
}

static void check_reverse_metadata(bool matches)
{
    struct kstatfs stat = {.marker = 1};
    int mount = 2;
    unsigned long ino = 3;
    dev_t dev = 4;
    char *name = NULL;
    int status = susfs_open_redirect_spoof_vfs_statfs(&target, &stat);
    CHECK(status == (matches ? 0 : -EINVAL));
    CHECK(stat.marker == (matches ? 1002UL : 1UL));
    status = susfs_open_redirect_spoof_seq_show(&target, &mount, &ino);
    CHECK(status == (matches ? 0 : -EINVAL));
    CHECK(mount == (matches ? 902 : 2) && ino == (matches ? 702UL : 3UL));
    int index = srcu_read_lock(&susfs_srcu_open_redirect);
    status = susfs_open_redirect_spoof_show_map_vma_srcu(&target, &ino, &dev, &name);
    CHECK(status == (matches ? 0 : -EINVAL));
    CHECK(dev == (matches ? 802UL : 4UL));
    CHECK(matches ? name && !strcmp(name, "/fake-A") : name == NULL);
    srcu_read_unlock(&susfs_srcu_open_redirect, index);
}

int main(int argc, char **argv)
{
    CHECK(argc == 2);
    const char *mode = argv[1];
    if (!strncmp(mode, "features-", 9)) {
        struct st_susfs_enabled_features user = { .err = 37 };
        void *argument = &user;
        allocation_failure = strstr(mode, "nomem") != NULL;
        copyin_failure = strstr(mode, "copyin") != NULL;
        copyout_failure = strstr(mode, "copyout") != NULL;
        susfs_get_enabled_features(&argument);
        CHECK(allocations == releases);
        CHECK(allocations == (allocation_failure ? 0 : 1));
        CHECK(copyin_calls == (allocation_failure ? 0 : 1));
        CHECK(copyout_calls == 1);
        CHECK(user.err == (copyout_failure ? 37 : allocation_failure ? -ENOMEM : copyin_failure ? -EFAULT : 0));
        if (!allocation_failure && !copyin_failure && !copyout_failure) {
            CHECK(strstr(user.enabled_features, "CONFIG_KSU_SUSFS_SUS_PATH\n"));
            CHECK(strstr(user.enabled_features, "CONFIG_KSU_SUSFS_SUS_MAP\n"));
            CHECK(strstr(user.enabled_features, "CONFIG_KSU_SUSFS_OPEN_REDIRECT\n"));
        }
    } else if (!strncmp(mode, "open-", 5)) {
        redirect_fixture(false);
        if (!strcmp(mode, "open-collision")) check_open(true);
        else if (!strcmp(mode, "open-device")) {
            OPEN_REDIRECT_HLIST[0].target_ino = 2;
            check_open(true);
        } else if (!strcmp(mode, "open-no-match")) {
            OPEN_REDIRECT_HLIST[2].target_ino = 988;
            check_open(false);
        } else if (!strcmp(mode, "open-nomem")) {
            filename_failure = true;
            CHECK(susfs_open_redirect_spoof_do_sys_openat(&target) == (struct filename *)(intptr_t)-ENOMEM);
        } else if (!strcmp(mode, "open-uid")) {
            OPEN_REDIRECT_HLIST[0].target_ino = 2;
            for (int scheme = UID_NON_APP_PROC; scheme <= UID_UMOUNTED_PROC; scheme++) {
                OPEN_REDIRECT_HLIST[2].info.uid_scheme = scheme;
                mock_uid = 0; mock_ksu_domain = false; mock_umounted_app = true; mock_umounted = true;
                check_open(true);
                if (scheme == UID_NON_APP_PROC) mock_uid = 10001;
                else if (scheme == UID_ROOT_PROC_EXCEPT_SU_PROC) mock_uid = 2000;
                else if (scheme == UID_NON_SU_PROC) mock_ksu_domain = true;
                else if (scheme == UID_UMOUNTED_APP_PROC) mock_umounted_app = false;
                else mock_umounted = false;
                check_open(false);
            }
            OPEN_REDIRECT_HLIST[2].info.uid_scheme = UID_NON_APP_PROC;
            mock_uid = 110001; check_open(false);
            OPEN_REDIRECT_HLIST[2].info.uid_scheme = UID_ROOT_PROC_EXCEPT_SU_PROC;
            mock_uid = 0; mock_ksu_domain = true; check_open(false);
            OPEN_REDIRECT_HLIST[2].info.uid_scheme = -1; check_open(false);
            OPEN_REDIRECT_HLIST[2].info.uid_scheme = UID_UMOUNTED_PROC + 1; check_open(false);
        } else CHECK(false);
    } else if (!strncmp(mode, "readlink-", 9)) {
        redirect_fixture(true);
        char buffer[32];
        if (!strcmp(mode, "readlink-boundaries")) {
            memset(buffer, 'X', sizeof(buffer));
            CHECK(susfs_open_redirect_spoof_vfs_readlink(&target, buffer, -1) == -EINVAL);
            CHECK(buffer[0] == 'X');
            int sizes[] = {0, 1, 6, 7, 8, 32};
            for (size_t i = 0; i < sizeof(sizes)/sizeof(sizes[0]); i++) {
                memset(buffer, 'X', sizeof(buffer));
                int copied = sizes[i] < 7 ? sizes[i] : 7;
                CHECK(susfs_open_redirect_spoof_vfs_readlink(&target, buffer, sizes[i]) == copied);
                CHECK(!memcmp(buffer, "/fake-A", copied) && buffer[copied] == 'X');
            }
        } else if (!strcmp(mode, "readlink-copyout")) {
            copyout_failure = true;
            CHECK(susfs_open_redirect_spoof_vfs_readlink(&target, buffer, sizeof(buffer)) == -EFAULT);
        } else if (!strcmp(mode, "readlink-keys")) {
            CHECK(susfs_open_redirect_spoof_vfs_readlink(&target, buffer, sizeof(buffer)) == 7);
            CHECK(!memcmp(buffer, "/fake-A", 7));
            OPEN_REDIRECT_HLIST[2].target_ino = 988;
            CHECK(susfs_open_redirect_spoof_vfs_readlink(&target, buffer, sizeof(buffer)) == -ENOENT);
        } else CHECK(false);
    } else if (!strncmp(mode, "proc-", 5)) {
        redirect_fixture(true);
        char buffer[4096];
        if (!strcmp(mode, "proc-boundaries")) {
            int sizes[] = {0, 1, 7, 8, 256, 4096};
            for (size_t i = 0; i < sizeof(sizes)/sizeof(sizes[0]); i++) {
                memset(buffer, 'X', sizeof(buffer));
                int length = susfs_open_redirect_spoof_do_proc_readlink(&target, buffer, sizes[i]);
                CHECK(length == (sizes[i] <= 7 ? -ENAMETOOLONG : 7));
                CHECK(sizes[i] <= 7 ? buffer[0] == 'X' : !strcmp(buffer, "/fake-A") && buffer[8] == 'X');
            }
            OPEN_REDIRECT_HLIST[2].target_ino = 988;
            CHECK(susfs_open_redirect_spoof_do_proc_readlink(&target, buffer, sizeof(buffer)) == -ENOENT);
        } else if (!strcmp(mode, "proc-maximum")) {
            memset(OPEN_REDIRECT_HLIST[2].info.redirected_pathname, 'p', 255);
            OPEN_REDIRECT_HLIST[2].info.redirected_pathname[255] = '\0';
            CHECK(susfs_open_redirect_spoof_do_proc_readlink(&target, buffer, sizeof(buffer)) == 255);
            CHECK(strlen(buffer) == 255 && buffer[254] == 'p');
        } else CHECK(false);
    } else if (!strncmp(mode, "metadata-", 9)) {
        redirect_fixture(true);
        if (!strcmp(mode, "metadata-keys")) check_reverse_metadata(true);
        else if (!strcmp(mode, "metadata-no-match")) {
            OPEN_REDIRECT_HLIST[2].target_ino = 988;
            check_reverse_metadata(false);
            unsigned long ino = 3; dev_t dev = 4; char *name = "occupied";
            int index = srcu_read_lock(&susfs_srcu_open_redirect);
            CHECK(susfs_open_redirect_spoof_show_map_vma_srcu(&target, &ino, &dev, &name) == -EINVAL);
            CHECK(susfs_open_redirect_spoof_show_map_vma_srcu(&target, &ino, &dev, NULL) == -EINVAL);
            CHECK(ino == 3 && dev == 4);
            srcu_read_unlock(&susfs_srcu_open_redirect, index);
        } else CHECK(false);
    } else if (!strncmp(mode, "kstat-", 6)) {
        kstat_fixture(false);
        if (!strcmp(mode, "kstat-collision")) check_kstat(true);
        else if (!strcmp(mode, "kstat-device")) {
            SUS_KSTAT_HLIST[0].target_ino = 2;
            check_kstat(true);
        } else if (!strcmp(mode, "kstat-fuse")) {
            struct fuse_inode fuse = {.inode = target};
            sb.s_magic = FUSE_SUPER_MAGIC;
            mock_fuse_inode = &fuse;
            kstat_fixture(true);
            check_kstat(true);
            SUS_KSTAT_HLIST[2].is_fuse = false;
            check_kstat(false);
        } else if (!strcmp(mode, "kstat-guards")) {
            mock_umounted_app = false; check_kstat(false);
            mock_umounted_app = true; mapping.flags = 0; check_kstat(false);
            mapping.flags = 1UL << AS_FLAGS_SUS_KSTAT;
            target.i_mapping = NULL; check_kstat(false);
            target.i_mapping = &mapping;
            SUS_KSTAT_HLIST[2].target_ino = 988; check_kstat(false);
        } else CHECK(false);
    } else CHECK(false);
    CHECK(rcu_depth == 0 && srcu_depth == 0);
    printf("PASS %s\n", mode);
    return 0;
}
