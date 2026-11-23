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
| `ci-layout.json` | 所有上游的固定 commit（kernel/common、SUSFS、SukiSU 驱动与 Manager、NDK）和版本，唯一来源 |
| `patches/common/` | 对内核的改动，在官方 SUSFS 补丁之后按 `series` 顺序应用 |
| `patches/driver/` | 对 SukiSU builtin 驱动的改动 |
| `patches/manager/` | 对 SukiSU Manager / ksud 的改动（Zygisk Next 状态检测） |
| `module/susfs4ksu/` | SUSFS local2 模块源码 |
| `workspace/scripts/` | 构建、来源绑定、接口/模块/交付验证 |
| `configs/`、`reference/` | 构建配置、公开校验输入引用与链接声明 |
| `.github/workflows/` | 自动构建和公开产物发布 |

仓库不存上游源码。CI 按"固定上游 → 官方 SUSFS 补丁 → 补丁队列"现场组装完整
源码；上游更新时改 pin，再看哪个补丁打不上，只修那一个。每个补丁头写明原因、
上游状态和删除条件，已被上游合并的修复在升级时直接删掉。不要求 Windows 安装
Ubuntu、WSL 或 Android 编译工具链。

## 开发与下载

```powershell
git clone https://github.com/polarisxb/fuxi-kernel-ci.git
cd fuxi-kernel-ci
git switch -c my-change
python scripts/source.py edit common 0003   # 或 new 新建补丁
# 编辑 .work/edit/ 下的文件，然后：
python scripts/source.py refresh
git add patches
git commit
git push -u origin my-change
```

Windows 查看/编辑源码、CI 状态和 agent 下载的完整命令见
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
