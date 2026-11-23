# CI 工作区组装

实施计划：固定上游 Git revision，验证公共输入包，再在新的 Linux 工作区覆盖仓库
实际源码文件、恢复声明的链接并捕捉完整补丁。缓存只保存公共下载和 Git 对象，
不保存构建输出或签名密钥。Windows 不需要检出完整内核树。

```sh
python3 scripts/bootstrap.py --workspace "$RUNNER_TEMP/fuxi-project"
bash scripts/build-ksud.sh --workspace "$RUNNER_TEMP/fuxi-native" --dist dist/native
```

`FUXI_INPUTS_CACHE` 或 `--inputs-cache` 控制公共缓存目录。工作区必须新建；脚本
拒绝覆盖未知的非空目录。`--mirror-root` 只用于本机只读 Git 镜像的验收，CI
默认从 manifest 中固定的公开 URL 获取。`--native-only` 仅准备固定 904c60d1
Manager 原生程序源码和 NDK，保留真实 Git 历史及 40900 版本计数。

原生构建要求开发仓库已有 commit 且工作区干净；`CI_SOURCE_SHA` 必须等于
当前真实 HEAD。组装时记录开发源码与实际 Manager 树中的文件哈希、执行位和
链接，Cargo 运行前再次记录并在编译后逐项比较。上游保持 904c60d1、完整历史
3715 个 commit、真实 `40000 - 2815 + 3715 = 40900`，NDK 保持固定 r23 revision，
不修改 `Cargo.lock`。Cargo 输出中的原始版本、ARM64 ELF、Android API 26 与
Rust 1.91.1 生成的 144 项 Rust/C UAPI 比较都通过后才导出。

`native-build-info.json` 的外部记录将 `libksud.so` 与完成的原生 ZIP 分别绑定到
字节数和 SHA256，并保留 Cargo 前后的源码快照。ZIP 内部的同名记录只绑定
原生程序，避免 ZIP 哈希的循环引用；ZIP 完成后才写外部记录和 `SHA256SUMS`。

输入包由 `reference/inputs.lock.json` 的 URL 与 SHA256 锁定。解包只允许
`inputs/`、`downloads/`、`tools/`，拒绝路径穿越、特殊节点、符号链接与私钥；
原厂模块的合法 tar hardlink 保留。源码中的必要链接只按
`configs/source-links.json` 恢复，并验证目标仍在当前工作区内。

根目录 `src/common` 是可编辑覆盖层；删除上游文件使用
`configs/source-deletions.json`。`src/KernelSU` 是完整 builtin 驱动。
补丁在组装后从实际 Git checkout 生成，包含新增和删除文件，避免源码已改却
继续使用旧补丁。构建与真实 ELF/CRC/UAPI/签名验收由便携工作区脚本负责。
