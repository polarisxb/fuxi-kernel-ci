# Windows 开发

Windows 只需要 Git for Windows 和 Python 3。编译、Linux 工具链、Full LTO 和
签名均由 GitHub Actions 完成，不需要在电脑上安装 Ubuntu 或 WSL。

```powershell
git clone https://github.com/polarisxb/fuxi-kernel-ci.git
cd fuxi-kernel-ci
git config core.autocrlf false
```

## 源码在哪里

仓库不存上游源码，只存三样东西：

- `ci-layout.json` 的 `upstreams`：kernel/common、SUSFS、SukiSU builtin 驱动、
  SukiSU Manager 与 NDK 的固定 commit，是所有版本的唯一来源。
- `patches/common`、`patches/driver`、`patches/manager`：本项目自己的改动，
  每个目录一个按顺序应用的补丁队列（`series`）。每个补丁头写明原因
  （`Subject:`）、上游状态（`Upstream-Status:`）和何时可以删除（`Drop-when:`）。
- `module/susfs4ksu/`：SUSFS local2 模块源码（phase 3 再改为补丁队列）。

CI 每次按"上游 → 官方 SUSFS 补丁 → 补丁队列"现场组装完整源码。上游更新时
只需要改 pin，再看哪个补丁打不上；不需要重新导入整棵源码树。

## 查看源码

`read` 只下载需要的文件，输出 CI 实际编译的内容；`--upstream` 输出原始上游：

```powershell
python scripts/source.py read common/fs/namei.c
python scripts/source.py read common/fs/namei.c --upstream
python scripts/source.py read driver/kernel/feature/sucompat.c
python scripts/source.py read manager/userspace/ksud/src/module.rs
```

`common/drivers/kernelsu/...` 会映射到驱动源码，`KernelSU`、`SukiSU` 仍可作为
`driver`、`manager` 的别名使用。

## 修改源码

不直接改上游文件，而是改某个补丁。`edit` 把这个补丁涉及的文件放到
`.work/edit/<队列>/`（打好它之前的补丁并应用它本身），你像平常一样编辑这些
文件，再用 `refresh` 写回补丁：

```powershell
python scripts/source.py edit common 0003
# 编辑 .work/edit/common/fs/namei.c
python scripts/source.py refresh
```

- `edit common 0003 --add fs/stat.c`：把另一个文件也纳入这个补丁。
- `new common susfs-fix-statfs fs/statfs.c`：在队列末尾新建补丁，`refresh` 后
  记得补全补丁头里的 `Upstream-Status` 和 `Drop-when`。
- `refresh` 会检查后面的补丁是否仍能应用；不能时什么都不写并指出是哪个补丁
  （确实要先保留再去修后面的补丁时用 `--force`）。
- `status` 查看当前编辑，`discard` 放弃。

编辑的文件必须保存为 LF；含 CR 的改动会被拒绝。Windows 保留名及大小写冲突
路径只能 `read`，不能编辑，不要关闭 Git 的 NTFS 保护来强行检出。符号链接由
`configs/source-links.json` 声明，CI 重建。

## 提交和编译

按仓库的 `AGENTS.md` 写决策说明，保存文本为 UTF-8/LF，再提交：

```powershell
git add ci-layout.json patches module configs workspace scripts tests .github docs
git commit --file decision-message.txt
git push origin main
```

`push` 会触发构建。普通分支保留 Actions 调试产物；当前 `main` 成功后发布
公开 Release。PR 只进行不使用私钥的验证。查看 Actions 页：

```text
https://github.com/polarisxb/fuxi-kernel-ci/actions
```

## 下载结果

```powershell
python scripts/download-artifact.py --output downloads
```

下载完成前会验证 ZIP 长度、SHA256 和源码提交绑定，失败不会把未验证文件放到
最终路径。命令会输出完整源码提交、不可变 Release URL，并保存 `.download.json`
收据。指定历史 Release：

```powershell
python scripts/download-artifact.py --tag ci-SOURCEPREFIX-RUN-ATTEMPT --output downloads-pinned
```

`--commit` 是对选定 Release 的提交前缀约束；不会自动搜索所有历史 Release。
如果选中的 latest 不符合约束，会停止，届时使用准确的 `--tag`。

```powershell
python scripts/download-artifact.py --commit FULL_OR_PREFIX_SHA --output downloads
```

公开 Release 下载不要求登录 GitHub。API 未登录有速率限制；需要时可给进程
设置 `GH_TOKEN`，该令牌只发给 GitHub API，不会写入收据或发到产物下载地址。
