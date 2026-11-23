# 小米 13：SukiSU 40900 + SUSFS 2.3.0 r2 修复候选

本候选从 2026-11-10 保存的 r1 基线修复已审查缺陷，内核后缀为
`suki40900-susfs230-r2`。原 r1 成品与只读基线保留，r2 不覆盖其发布目录。
重点修复重定向对象生命周期、RCU 查找、查询分配失败、规则键匹配及
readlink 长度/释放处理。对应的 SUSFS 模块 local2 单独打包，修复 WebUI
回调、日志开关和 uname 保存；它不包含在内核 AnyKernel ZIP 中。

这是新编译的 5.15.194 内核，使用官方 builtin v4.2.0 源码
e2912817f4e1b194e582a06e0b5eacf6a3fb7083，与官方 Manager 40900 配套，
并整合 SUSFS bca0d2333c1a7d717e7278b019d7af7ba1d16005 / v2.3.0。
完整本地版本保留 builtin 源码身份，不把它冒充为原生 Tracepoint 分支。
驱动 40900 使用该 builtin 分支上游的 main 发布计数规则固定，接口及实际处理器
单独检查；没有只给旧 37974 驱动改数字或关闭 Manager 警告。

common 基线为 f4321180a3973d19b626b3eef51871199e4e1fac；保留 Full LTO、
CFI、MODVERSIONS、MODULE_SIG_PROTECT 和严格 KMI。没有引入 LKM、Magisk 或 KPM。
编译使用 8 个任务，锁定 Full LTO 单分区与默认 2 个链接线程；这是 r1
成功构建实际使用的配置。锁定的 Clang 14 / CFI / BTF 组合拒绝多分区，
避免 BTF 内部符号与 CFI 跳表在分区间失配。
SUSFS 原有路径、挂载、kstat、uname、符号隐藏、启动参数、重定向和映射功能
均要求配置启用。tools/ksu_susfs 是与此 v2.3.0 源码配套的 ARM64 工具。

候选包通过编译与离线检查后才发布。构建期间冻结候选输入，并把源码、
配置与生成的二进制摘要绑定；验收拒绝无构建记录或源码/镜像混配。
检查报告在 reports/offline/、
reports/uapi/ 和 reports/delivery-verification.json。RELEASE.json 记录实际文件
大小及哈希，SHA256SUMS 覆盖所有交付文件（校验和文件自身除外）。

在交付根目录运行：

    sha256sum -c SHA256SUMS
    python3 scripts/verify-delivery.py --art-dir .

第二条命令检查完整内容和源码重放，需要 Linux x86_64、Python 3.12+、git 和
openssl；随包包含审核时使用的 AVB/objcopy 工具。公开证书仅验证本次配套模块，
不声称重现 OEM 私钥或任意模块信任。

AnyKernel3 ZIP 只接受 fuxi，目标为活动槽 boot；不会替换 init_boot、vbmeta
或厂商分区。独立 boot.img 使用原厂 boot v4 头、空 ramdisk 和原有 AVB 属性，
哈希算法元数据为 NONE，不声称具有 OEM 签名。此包不会自动刷写或重启手机。

## 完成等级和 SUSFS 配套

这份交付的运行时兼容性尚未由此离线流程证明。必须在另行授权的临时启动测试中
核对 40900/40900 无告警、Root、SUSFS v2.3.0 版本及功能查询，并实际验证应用
状态标记、挂载隔离和所需硬件路径。仅看到配置为 y、CRC 相同或模块签名有效
不足以证明全部运行功能。此前无 SUSFS 对照内核的实测不算本包的实机验证。

本次保持 SUSFS 2.3.0，不变更 Manager/驱动 40900 或已锁定的控制接口。
内核包不自动替换模块脚本；local2 模块继续使用经核验的 R28 ARM64 工具，
与随内核源码编译的交付工具是两份不同二进制。只读验收命令：

    ksu_susfs show version
    ksu_susfs show enabled_features
    ksu_susfs show variant

这些命令需要已经授权的 Root。

## 源码重建记录

source/common-base.tar.gz 是锁定 common 提交的原始源码，
source/builtin-e2912817.tar.gz、source/susfs-bca0d233.tar.gz 和
source/manager-904c60d1.tar.gz 是独立锁定归档。将 common 解包至 kernel/common，
将 builtin 归档根目录放至 kernel/KernelSU，分别实际应用
source-diffs/common.diff、source-diffs/SukiSU.diff。common 补丁必须创建
drivers/kernelsu 指向 ../../KernelSU/kernel 的链接；SUSFS 内核实现已包含在最终
common 补丁中。验证脚本会在干净临时目录实际执行上述重建，并与完整集成源码
文件清单逐项对比。

工具链与其他构建输入见锁定 manifest、configs 及 reports。构建私钥是单独安全提供
的外部输入，不在交付内；不得为复建方便把它加入公开源码或压缩包。

单元回归使用电脑上的 VFS/内核 API 模拟与 JavaScript bridge mock，并配合
实际源码提取、ASan/UBSan、ARM64 编译和完整内核构建。它们不能替代手机上的
授权、应用、相机、通信、休眠和稳定性验收；本轮候选没有刷入手机。
