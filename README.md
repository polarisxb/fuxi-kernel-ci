# Xiaomi 13 / fuxi — Windows 开发，GitHub 编译

在 Windows 拉代码、编辑、提交并 push，GitHub Actions 自动完成 Linux 编译与
ZIP 打包。公开 Release 提供固定版本的下载 URL、源码提交号和 SHA256，agent
可以匿名下载并验证产物。

当前基线：5.15.194 / SukiSU 40900 / SUSFS 2.3.0，保留 Full LTO、CFI、BTF、
严格 KMI 与模块签名保护。当前 r2 基线曾在 fuxi 的 OS3.0.309.0.WMCCNXM 上通过
启动、Root、屏幕/触摸及模块验证；新 CI 提交的编译通过不替代其独立实机验收。

## 源码布局

| 路径 | 用途 |
| --- | --- |
| `src/common/` | 实际修改后的 Linux 源码，按路径覆盖固定上游 |
| `src/KernelSU/` | 完整当前 builtin 驱动 |
| `src/SukiSU/` | 配套 Manager / ksud 源码，包含 Zygisk 实时检测修复 |
| `module/susfs4ksu/` | SUSFS local2 模块源码 |
| `workspace/scripts/` | 构建、来源绑定、接口/模块/交付验证 |
| `configs/`、`reference/` | 锁定版本、公开校验输入引用与链接声明 |
| `.github/workflows/` | 自动构建和公开产物发布 |

完整 Linux 树有大小写冲突及 Windows 保留文件名，所以本仓库提供可直接编辑的
覆盖层。完整源码由 CI 从锁定上游组装；其他上游文件可以按需读取和导入。
不要求 Windows 安装 Ubuntu、WSL 或 Android 编译工具链。

## 开发与下载

```powershell
git clone https://github.com/polarisxb/fuxi-kernel-ci.git
cd fuxi-kernel-ci
git switch -c my-change
# 编辑 src/common、src/KernelSU、src/SukiSU 或 module 中的实际文件
git add .
git commit
git push -u origin my-change
```

Windows 源码读取/导入、CI 状态和 agent 下载的完整命令见
[Windows 开发说明](docs/WINDOWS.md) 与 [产物说明](docs/ARTIFACTS.md)。

分支 push 运行构建；当前 main 的成功构建发布公开 Release。下载入口：

- [GitHub Actions](https://github.com/polarisxb/fuxi-kernel-ci/actions)
- [最新 Release](https://github.com/polarisxb/fuxi-kernel-ci/releases/latest)
- [最新 Release 公共 API](https://api.github.com/repos/polarisxb/fuxi-kernel-ci/releases/latest)

下载后核对 `SHA256SUMS` 和 `build-info.json` 中的源码提交，不从“正在构建”或
失败运行获取旧 ZIP。安装包只针对 fuxi；刷入前应保存当前系统、活动槽与 boot。

## 构建输入

上游与工具固定在 `configs/manifest.locked.xml` 等文件。原厂参考 ELF、boot、
公开证书和固定源码归档保存在 `inputs-v1` Release，由
`reference/inputs.lock.json` 的 SHA 锁定；CI 不重复下载整包 ROM。

唯一私密输入为 `KERNEL_SIGNING_KEY_PEM` repository Actions Secret，与已记录
公开证书配对。私钥不进入仓库、缓存或 ZIP。输出只从验证通过的白名单 dist
发布，避免泄露构建目录中的密钥。

KernelSU、Linux、Manager 和模块保留各自源文件许可证；根目录 LICENSE 为
Linux GPL-2.0。本地 Zygisk 修复库不是官方重签 Manager APK，更新/重装 Manager
后需按实际版本重新核对其部署兼容性。
