# 固定的公开验证输入

`inputs.lock.json` 引用仓库的 `inputs-v1` Release，并锁定整个压缩包和各文件
SHA256。输入来自已核验的官方 OTA、固定 upstream 源码和本项目基线。

- 原厂 OTA：`https://cdnorg.d.miui.com/OS3.0.309.0.WMCCNXM/fuxi-ota_full-OS3.0.309.0.WMCCNXM-user-16.0-4729900895.zip`。
- 保留提取后的 boot、407 个唯一原厂模块 ELF，以及 CRC/namespace/import 的
  原始验证索引。213 个重复路径采用 TAR hardlink，保留全部 620 路径语义。
- 原厂模块许可证为 GPL 或 Dual BSD/GPL。CI 重新读取 ELF验证，不以旧成功
  JSON 代替新构建兼容检查。
- AnyKernel 模板只清空旧 Image，保留审核过的脚本/工具；新 Image 来自本次
  实际构建。
- 官方 Manager APK、builtin/SUSFS/Manager 的固定源码归档、配套工具与公开
  模块签名证书均带明确摘要。

当前构建使用固定提交生成 builtin、SUSFS 与 Manager 的 Git 归档。不可变输入包中的
旧 builtin 归档仅用于迁移期间的逐文件比较。旧 SUSFS 和 Manager 归档作为历史输入保留。
官方 Manager APK 继续来自经过摘要校验的输入包。

输入包没有手机备份、用户数据、授权记录、研究日志或私钥。私钥只能存 Actions
Secret，不能加入这些输入、Git、cache 或 Release。

`source-snapshot.json` 记录 r2 覆盖层（原 `src/`）各文件的摘要，即已成功刷入的
r2 基线源码。phase 1 迁移已证明 `patches/` 补丁队列在固定上游上严格重放后与它
逐字节一致；之后它只作为历史记录，不再参与检查。CI 会为每个新提交重新冻结并
验证来源和产物。
