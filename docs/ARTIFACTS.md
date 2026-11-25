# CI 产物与 agent 下载

公开下载入口：

```text
https://github.com/polarisxb/fuxi-kernel-ci/releases/latest
https://api.github.com/repos/polarisxb/fuxi-kernel-ci/releases/latest
```

每个成功的当前 `main` 构建发布独立 tag：`ci-源码前12位-runId-runAttempt`。
先以 draft 上传全部文件，再公开并标记 latest；旧运行不会覆盖较新的提交。
agent 查询 latest 后应使用返回的 `browser_download_url`，它对应不可变 tag。

## 文件

| 文件 | 用途 |
| --- | --- |
| `Fuxi_5.15.194_SukiSU_40900_SUSFS_2.3.0_r3_AnyKernel3.zip` | fuxi 内核 AnyKernel3 包 |
| `SUSFS_R28_Fuxi230_40900_local3.zip` | 独立 SUSFS 模块包 |
| `Fuxi_SukiSU_40900_Zygisk_StatusFix.zip` | SukiSU 40900 的 ARM64 检测修复材料 |
| `libksud.so` | 对应的 ARM64 本地程序，不是官方签名 APK |
| `kernel-build-info.json` / `native-build-info.json` | 每个构建组件的验证记录 |
| `build-info.json` | 总源码 SHA、工作流 run 与所有产物 SHA/大小绑定 |
| `SHA256SUMS` | Release 文件的 SHA256 清单 |

AnyKernel 包和 SUSFS 模块包是两个独立产物。状态修复 ZIP 不能当作内核包、
官方 Manager APK 或自动可安装模块。CI 不连接或修改手机。

## 推荐 agent 命令

```powershell
python scripts/download-artifact.py --output downloads
python scripts/download-artifact.py --asset Fuxi_SukiSU_40900_Zygisk_StatusFix.zip --output downloads-native
```

下载工具检查 `build-info.json` 自身的 SHA、完整源码提交、仓库身份、目标 ZIP
SHA 与大小，然后原子写入最终文件，并保存包含 URL/提交/校验的下载收据。
需要精确重复某次构建时固定 `--tag`，并可用 `--commit` 进一步要求提交一致。

Actions 页面中的 artifact 适合分支排错，通常保留 14 天且下载需要登录；公开
Release 才是长期、无需登录的 agent 入口。输入 Release `inputs-v1` 是独立的
固定编译资料，不是可刷入的内核产物。

CI 验收证明源码、工具链、保护配置与产物绑定；它不替代每次修改后的实机
启动、Root、模块与硬件验证。安装前仍需核对设备、系统和当前活动槽并备份。

## 签名模式

`kernel-build-info.json` 和 `native-build-info.json` 的 `signing` 记录本次构建的发布资格。
`main` 的 push 运行 `kernel-release`，校验固定的公开证书。分支 push 以及所有手动运行均运行
`kernel-check`，在 runner 临时目录中生成一次性证书，核对实际编入内核的证书并验证模块签名。
`ephemeral` 产物仅供检查，请勿刷机。发布任务要求两个组件均标记为 `release`。

切换工作流前，由所有者创建 `release-signing` 环境，部署分支只允许 `main`，在环境中配置与基线证书
配对的 `KERNEL_SIGNING_KEY_PEM`。确认环境配置正确后再移除仓库级同名 secret；应与工作流合并协调操作，
以免仍使用旧配置的 main 构建取不到密钥。私钥只留在环境 secret 和 runner 临时文件中。
