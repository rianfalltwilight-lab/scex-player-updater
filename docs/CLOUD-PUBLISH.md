# S3 云端发布

本实现按现有便携更新器协议独立编写。参考 MC-ModSync 的内容寻址、
先文件后清单和缓存分层设计；没有复制其实现。现有清单 SHA-1、玩家文件
保护和官方源策略继续有效，新增每文件 `downloadPath=blobs/<sha256>`。

## 本轮能力与边界

- 只读取已发布清单明确列出的文件，不递归上传生产服目录。
- 上传前检查清单 SHA-1 和大小；云端以 SHA-256 内容路径存储，重复内容复用。
- 更新器四个同步/预下载引擎使用本仓库版本，自动加入云端清单。
- 全部对象经公开下载回读校验后，保存不可变版本清单，最后切换稳定入口。
- 供应商 HEAD 若省略自定义哈希元数据，仍须通过大小和完整公开回读 SHA-256
  检查才可复用；已返回但不匹配的哈希会中止发布。
- 稳定清单和自刷新辅助文件禁止缓存；内容对象及清单快照长期缓存。
- 云端入口使用条件写入阻止竞争发布。供应商若不支持条件写入，停止切换；
  不能取消条件重试。各次发布仍应由同一管理机器串行执行。
- 不删除远端旧对象或清单；回滚所需对象保持可用。暂未加入自动回滚命令。
- 原有直目录 HTTP 源仍能使用，四个新引擎同时支持两种布局。
- 没有实现清单签名或文件断点续传。SHA-256 内容寻址不等于数字签名。

## 配置与计划

管理员安装官方 Python SDK：`python -m pip install boto3`。
从 `tools/cloud.example.json` 复制到 `tools/cloud.local.json`，填写实际桶参数。
`publicBaseUrl` 必须是**包含 prefix 的完整公开目录**，如
`https://example.cdn.provider.com/scex-updates`；不能只填写域名。
配置模板中的地址和区域是占位值，必须按自己服务商的控制台及实测填写。
AK/SK 通过 `SCEX_CLOUD_ACCESS_KEY`、`SCEX_CLOUD_SECRET_KEY` 环境变量提供，
不放入玩家文件或仓库。子账号只授权专用更新目录。

只读准备（不连接云、不要求密钥）：

```sh
python tools/cloud_publish.py --source /path/to/prepared/portable --config tools/cloud.local.json
```

实际上传和发布：

```sh
python tools/cloud_publish.py --source /path/to/prepared/portable --config tools/cloud.local.json --apply --receipt /path/to/private/cloud-receipt.json
```

程序不会重启或修改 Minecraft 服务端。`--source` 指向已经准备和验证的
玩家发布目录，包含 `server-manifest.json`；不是服务器根目录。
目录本身不会被修改，云端 URL 文件和四个引擎在发布数据中替换。
失败不输出 SDK 原始异常，以免签名 URL 或凭据出现在日志中。
若在最后一步回读失败，清单可能已经生效，应先查远端真实状态再重试。

## 先上传候选，再切换版本

把大文件上传与版本切换分开执行，可在发布窗口前完成上传和完整回读。下面三个命令使用同一份已准备目录、配置和候选回执：

```sh
# 上传不可变文件和清单快照；稳定清单、自刷新入口和旧更新源不变。
python tools/cloud_publish.py --source /path/to/prepared/portable --config tools/cloud.local.json --apply --stage-only --receipt /path/to/private/staged.json

# 只读核对候选、存储目标、对象身份及当前在线清单。
python tools/cloud_publish.py --source /path/to/prepared/portable --config tools/cloud.local.json --check-staged /path/to/private/staged.json

# 核对通过后更新自刷新文件，最后有条件地切换稳定清单。
python tools/cloud_publish.py --source /path/to/prepared/portable --config tools/cloud.local.json --apply --activate-staged /path/to/private/staged.json --receipt /path/to/private/activated.json
```

候选回执绑定文件内容、版本、下载目录与玩家保护策略。文件对象被改动或删除、在线清单被其他发布者切换、候选策略变化，都会中止激活。生成时间和单文件下载提示可刷新；最终清单仍重新保存并回读。普通 `--apply` 继续一次完成上传与激活。

已有离线发布计划时，可给 `--stage-only` 或 `--check-staged` 增加 `--release-plan /path/to/plan`。目录须含 `release-plan.json` 和 `manifest.proposed.json`：前者记录基线根目录/清单 SHA-256、目标版本、移除文件及 SHA-256、候选路径/来源/SHA-256/大小；后者列出替换后的完整清单。工具核对基线与每个差异，在内存中覆盖候选，不复制整包也不写基线。最终激活必须使用已准备完成的实际目录，不能直接激活离线计划。此仓库不提供生产运维计划生成器。

Windows 包装脚本对应参数为 `-StageOnly -ReceiptPath ...`、`-CheckStaged ...`、`-ActivateStaged ...`，离线计划用 `-ReleasePlanDirectory ...`。候选回执应保存在私有位置，并保留到完成切换或决定放弃候选。

`cloud.local.json` 可额外设置 `mirrorManifestUrls` 数组，最多写入三个不同的 HTTP(S) 清单地址。生成的地址文件先写主入口，再写镜像；镜像应提供同一版本、内容和玩家保护策略。整合包身份、版本或文件清单不一致的镜像不会用来拼接一次更新；玩家保护策略始终取主清单。

## 迁移旧玩家

云端清单含新下载字段，旧引擎不能直接处理。必须先通过原更新源的
`_updater` 自刷新下发新版引擎并核对 Windows/Python 入口，然后在一次
受控发布中修改玩家更新地址。云端对应 `_updater/*` 文件也保持可直接下载。
不能只替换地址就宣称迁移完成。保留原地址和其完整目录布局作回退。

`--bridge-source /path/to/old/portable --backup-root /path/to/backups` 在云端完整
回读之后，备份并原子替换旧源的辅助文件、地址和清单（清单最后写入）。旧源清单
保留直接路径，旧客户端可以先获得新引擎和云地址。期间若原源发生变化则拒绝覆盖。
失败会恢复已经替换的文件；备份目录包含 `rollback.json` 和原始文件。

Windows 管理端可使用 `tools/publish-cloud.ps1`。`cloud.local.json` 额外配置
`pythonExe`、`credentialPath`（当前用户 DPAPI 的 PSCredential CLIXML）、
`backupRoot`。它加载凭据到子进程环境，发布后恢复原环境。
生产的 `portable-publish.ps1` 可在释放原发布锁后调用此脚本；云发布会重新
获取同一把锁。只有本地私有配置存在时启用，缺省行为不变。

上线前仍需实际存储桶验证：S3 PUT/HEAD、条件写入、匿名 HTTPS 回读、
Cache-Control、稳定入口覆盖、首次同步与第二次增量同步。
公共桶只存玩家允许下载的文件；供应商能否禁止匿名列举需要单独实测。

## 回滚与费用

版本快照位于 `manifests/<清单SHA256>.json`，含该版本的完整策略和内容映射。
回滚时先验证旧快照引用的内容仍可下载，再以条件写覆盖稳定入口。
保留最近可回滚版本和正在下载玩家可能使用的对象，不立刻清理旧内容。
当前实现为所有引用对象做公开回读，因此发布校验本身也会产生下载流量。

## 验证

```sh
python -m unittest discover -s tests -v
```

`test_cloud_publish.py` 和 `test_cloud_staging.py` 使用内存对象存储模拟故障、竞争、分阶段激活与候选变化，并以真实本地 HTTP
测试 Windows/Python 玩家同步、中文路径、资源包和个人文件保护。
本地通过不代表真实存储桶实测通过。
