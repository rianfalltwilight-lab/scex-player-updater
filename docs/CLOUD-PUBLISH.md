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
配置模板中的七彩云参数是参考值，必须按自己的控制台及实测核对。
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

`test_cloud_publish.py` 使用内存对象存储模拟故障与竞争，并以真实本地 HTTP
测试 Windows/Python 玩家同步、中文路径、资源包和个人文件保护。
本地通过不代表七彩云实测通过。
