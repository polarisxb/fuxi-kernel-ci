# CI 工作区组装

固定上游 Git revision，验证公共输入包，再在新的 Linux 工作区按固定顺序组装：
上游源码 → 官方 SUSFS 补丁 → `patches/` 补丁队列，恢复声明的链接并捕捉完整
补丁。缓存只保存公共下载和 Git 对象，不保存构建输出或签名密钥。Windows 不需要
检出完整内核树。

```sh
python3 scripts/bootstrap.py --workspace "$RUNNER_TEMP/fuxi-project"
bash scripts/build-ksud.sh --workspace "$RUNNER_TEMP/fuxi-native" --dist dist/native
```

`FUXI_INPUTS_CACHE` 或 `--inputs-cache` 控制公共缓存目录。工作区必须新建；脚本
拒绝覆盖未知的非空目录。`--mirror-root` 只用于本机只读 Git 镜像的验收（SUSFS 镜像放在
`susfs4ksu/`，SUSFS 模块镜像放在 `susfs4ksu-module/`），CI 默认从固定的公开 URL 获取。`--native-only` 仅准备固定 904c60d1
Manager 原生程序源码和 NDK，保留真实 Git 历史及 40900 版本计数。

`ci-layout.json` 的上游可以声明 `backup_urls`。只有固定 URL 拉取失败、或拉到的内容
里没有固定 commit 时，才依次改从备份按 commit 拉取，并在 Actions 日志留下警告。
备份只取这个 commit（Manager 取它的完整历史），不取备份仓库的 tag 和分支，所以
备份里保活用的 `fuxi-pin/*` tag 不会进入 checkout，`git describe` 不受影响。拿到的
commit 照样按完整 SHA、历史计数和 describe 校验，来源不同不改变构建结果。使用
`--mirror-root` 时不访问备份。SukiSU 驱动和 Manager 的备份是 `polarisxb/SukiSU-Ultra`
（上游改名为 SukiSU-Nova 之前建的 fork），三个固定 commit 由其中的 `fuxi-pin/*`
tag 保留；不要同步它的旧分支。

原生构建要求开发仓库已有 commit 且工作区干净；`CI_SOURCE_SHA` 必须等于
当前真实 HEAD。组装时记录开发源码与实际 Manager 树中的文件哈希、执行位和
链接，Cargo 运行前再次记录并在编译后逐项比较。上游保持 904c60d1、完整历史
3715 个 commit、真实 `40000 - 2815 + 3715 = 40900`，NDK 保持固定 r23 revision，
不修改 `Cargo.lock`。Cargo 输出中的原始版本、ARM64 ELF、Android API 26 与
Rust 1.91.1 生成的 144 项 Rust/C UAPI 比较都通过后才导出。

上游已删除历史 `v4.2.0` tag。CI 只在自己的 Manager checkout 中将它恢复为
固定 commit `85eb4a95b8a61d756ecf53b9c5785e48e1b15039`：必须验证该 commit
存在、是固定 HEAD `904c60d1` 的祖先、距离恰为 1，且实际完整历史计数为
3715。若已有同名 tag 指向其他 commit，则立即拒绝，不覆盖；构建开始后若
tag 消失也拒绝。恢复记录随原生 metadata 发布，`build.rs` 的 Git 版本计算
保持原样，不修改上游或远程 tag。

`native-build-info.json` 的外部记录将 `libksud.so` 与完成的原生 ZIP 分别绑定到
字节数和 SHA256，并保留 Cargo 前后的源码快照。ZIP 内部的同名记录只绑定
原生程序，避免 ZIP 哈希的循环引用；ZIP 完成后才写外部记录和 `SHA256SUMS`。

输入包由 `reference/inputs.lock.json` 的 URL 与 SHA256 锁定。解包只允许
`inputs/`、`downloads/`、`tools/`，拒绝路径穿越、特殊节点、符号链接与私钥；
原厂模块的合法 tar hardlink 保留。源码中的必要链接只按
`configs/source-links.json` 的 `kernel_links` 恢复，并验证目标仍在当前工作区内。

## 组装顺序

所有上游 pin 只写在 `ci-layout.json` 的 `upstreams`（`configs/manifest.locked.xml`
中 kernel/common 与 NDK 的 revision 必须与它一致，否则组装立即停止）。

1. `kernel/common`：manifest 固定的 AOSP checkout。
2. SUSFS：按 `upstreams.susfs` 的 commit 从 GitLab 获取，复制 `kernel_files`
   （`fs/susfs.c` 与两个头文件），再用 `git apply` 严格应用官方
   `50_add_susfs_in_gki-android13-5.15.patch`。同一个检出里的 `ksu_susfs/` 复制到
   `upstream/susfs-tool/src`，用 manifest 固定的 NDK r23 运行 `ndk-build` 编译；产物
   `upstream/susfs-tool/ksu_susfs` 必须是 AArch64 ELF64，`NEEDED` 只允许 Bionic 的
   `libc.so`、`libdl.so`、`libm.so`、`liblog.so`。它的 SHA-256、字节数和 NDK 提交记入构建身份的
   `ksu_susfs`。交付包的 `tools/ksu_susfs` 与模块 ZIP 的 `tools/ksu_susfs_arm64` 都是这个文件；
   打包时把它的 SHA-256 填进模块 `customize.sh` 与 `susfs-bin-check.sh` 的 `@KSU_SUSFS_SHA256@`，
   `kernel-build-info.json` 的 `ksu_susfs` 记录同一个摘要。
3. `.scmversion`：由 `kernel_release` 生成，并校验它以本树 Makefile 的版本开头、
   包含 common commit 前 12 位。
4. `patches/common/series`：本仓库对内核的改动，按顺序严格应用（无 fuzz）。
5. `kernel/KernelSU` 取自固定 driver 提交的 Git 归档。在应用 `patches/driver/series`
   前，逐项核对新树与旧输入包中的 builtin 树，包括内容、可执行位与符号链接目标。
6. SUSFS 模块：按 `upstreams.module` 的 commit 从 GitHub 获取 sidex15/susfs4ksu-module（R28），
   去掉 `exclude` 列出的路径（仓库自己的 `.github`、`.gitignore`、`LICENSE`、`README.md`，以及上游
   预编译的 `tools/ksu_susfs_arm64`、`tools/sus_su_arm64`），把 `crlf_to_lf` 列出的 8 个文件从 CRLF
   转成 LF，再应用 `patches/module/series`，结果在 `module/susfs4ksu`。去掉 4 项仓库自身的路径（共 8 个文件）后，上游树
   与 R28 发布包 `ksu_module_susfs_1.5.2+.zip`（SHA-256 `abcd6bf4…`）的 78 个文件逐字节相同；
   转换的正是旧版仓库副本里由 Git `autocrlf` 统一过换行的文件，CRLF 与 LF 混用的 4 个 HTML 保持原样。
   排除规则不再匹配任何路径，或者列出的文件不再是纯 CRLF，组装都会停下。组装结果的各文件
   SHA-256 记入 `configs/ci-source.json` 的 `module_files`，导出模块 ZIP 前逐项核对。
7. 原生构建：固定 Manager checkout 上应用 `patches/manager/series`。

三个固定提交的源码归档由 `git archive` 生成在 `upstream/archives/`。捕捉驱动差异与
打包交付使用同一份 builtin 归档。使用 `--mirror-root` 时，`KernelSU/` 镜像需要
包含 builtin 和 Manager 两个固定提交。

组装时从 manifest 生成 `configs/locked-workspace-inputs.json`，按项目顺序记录
路径、提交和只读属性。校对内核 Makefile 后将版本和文件名写入 `configs/build-identity.json`；
两个文件都随构建配置冻结，并随离线交付包分发。

kernel 作业复用 Manager 归档的 Git 缓存，从锁定提交稀疏检出 `uapi/` 到
`upstream/manager-uapi/`。各头文件的 SHA-256 记入构建身份的 `manager_uapi_sha256`，
交付包中的对应文件位于 `reports/uapi/manager-headers/`。

`verify-uapi.py` 比较 host 与 arm64 上共有接口的值、大小和成员偏移。
UAPI 版本和 profile 版本必须在双方存在并一致。单侧声明的 ioctl 和结构体按所属方列入
`reports/uapi/complete-uapi-alignment.json`，不导致失败。导出时将 Manager 独有的 ioctl
列表写入 `kernel-build-info.json` 的 `uapi.manager_only_ioctl_constants`。
驱动每个已实现命令的 handler 与权限仍按 `configs/driver-ioctl-permissions.json` 验证。

任何补丁打不上都会以补丁文件名报错停止，不会带着旧改动继续构建。组装后仍从
实际 Git checkout 生成 `common-final.diff` / `sukisu-final.diff`，与发布中的
干净源码重放保持一致。构建与真实 ELF/CRC/UAPI/签名验收由便携工作区脚本负责。

phase 1 迁移时，13 个补丁在原 r2 的上游基础上严格重放，与原 `src/` 覆盖层
（内核 29 个文件、驱动 81 个文件、ksud 改动）逐字节一致；`.scmversion` 改为
由锁文件生成，字节也相同。
