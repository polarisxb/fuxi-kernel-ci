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
`susfs4ksu/`），CI 默认从固定的公开 URL 获取。`--native-only` 仅准备固定 904c60d1
Manager 原生程序源码和 NDK，保留真实 Git 历史及 40900 版本计数。

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
   `50_add_susfs_in_gki-android13-5.15.patch`。
3. `.scmversion`：由 `kernel_release` 生成，并校验它以本树 Makefile 的版本开头、
   包含 common commit 前 12 位。
4. `patches/common/series`：本仓库对内核的改动，按顺序严格应用（无 fuzz）。
5. `kernel/KernelSU` 取自固定 driver 提交的 Git 归档。在应用 `patches/driver/series`
   前，逐项核对新树与旧输入包中的 builtin 树，包括内容、可执行位与符号链接目标。
6. 原生构建：固定 Manager checkout 上应用 `patches/manager/series`。

三个固定提交的源码归档由 `git archive` 生成在 `upstream/archives/`。捕捉驱动差异与
打包交付使用同一份 builtin 归档。使用 `--mirror-root` 时，`KernelSU/` 镜像需要
包含 builtin 和 Manager 两个固定提交。

组装时从 manifest 生成 `configs/locked-workspace-inputs.json`，按项目顺序记录
路径、提交和只读属性。校对内核 Makefile 后将版本和文件名写入 `configs/build-identity.json`；
两个文件都随构建配置冻结，并随离线交付包分发。

任何补丁打不上都会以补丁文件名报错停止，不会带着旧改动继续构建。组装后仍从
实际 Git checkout 生成 `common-final.diff` / `sukisu-final.diff`，与发布中的
干净源码重放保持一致。构建与真实 ELF/CRC/UAPI/签名验收由便携工作区脚本负责。

phase 1 迁移时，13 个补丁在原 r2 的上游基础上严格重放，与原 `src/` 覆盖层
（内核 29 个文件、驱动 81 个文件、ksud 改动）逐字节一致；`.scmversion` 改为
由锁文件生成，字节也相同。
