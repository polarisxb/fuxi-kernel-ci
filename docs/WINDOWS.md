# Windows 开发

Windows 只需要 Git for Windows 和 Python 3。编译、Linux 工具链、Full LTO 和
签名均由 GitHub Actions 完成，不需要在电脑上安装 Ubuntu 或 WSL。

```powershell
git clone https://github.com/polarisxb/fuxi-kernel-ci.git
cd fuxi-kernel-ci
git config core.autocrlf false
```

## 编辑的文件

- `src/common/`：已经适配的 common 内核实际源码覆盖层。
- `src/KernelSU/`：实际 builtin SukiSU 驱动源码。
- `src/SukiSU/`：Manager、userspace 与相应 UAPI 源码。
- `module/susfs4ksu/`：SUSFS local2 模块源码。
- `configs/`、`workspace/scripts/`、`scripts/`：固定输入和构建/验收流程。

仓库不要求 Windows 检出整个 Linux 内核树。完整上游存在大小写重名和 `aux.h`
等 Windows 保留名；CI 会按固定提交拉取完整源码，再应用这里的实际文件。
没有放入覆盖层的文件可以直接读取：

```powershell
python scripts/source.py read common/include/linux/fs.h
python scripts/source.py read common/fs/namei.c --upstream
```

`read` 默认优先读取你本地修改的覆盖层，`--upstream` 明确读取锁定上游版本。
需要修改尚未包含的文件时导入实际源文件：

```powershell
python scripts/source.py import common/mm/mmap.c
# 然后编辑 src/common/mm/mmap.c
```

已有文件不会被默认导入覆盖；如需恢复为锁定上游版本，明确使用 `--force`。
Windows 保留名及大小写冲突路径会拒绝导入，仍可用 `read` 阅读，不要关闭
Git 的 NTFS 保护来强行检出。符号链接由 `configs/source-links.json` 声明，CI
重建；例如 `common/drivers/kernelsu/...` 会映射到实际 KernelSU 源码。

## 提交和编译

按仓库的 `AGENTS.md` 写决策说明，保存文本为 UTF-8/LF，再提交：

```powershell
git add src module configs workspace scripts .github docs
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
