# 通用环境检测与 Linux 服务端

本功能属于独立 GitHub 更新器。升级此仓库不会自动修改任何 Minecraft 服务器、面板、生产配置或已部署更新源。

## 服主第一次使用

准备 Python 3.10 或更新版本，以及一份包含 `mods` 或 `config` 的**客户端实例**。不要拿服务端目录直接生成玩家更新源。Linux 无须 PowerShell、Java 或 Minecraft 服务端插件。

将客户端放在仓库的 `main-client` 目录，或者用 `--source-client` 指定现有路径。不需要复制到 Minecraft 生产目录。

Linux：

```sh
sh tools/portable-server.sh doctor --source-client /srv/client-pack
sh tools/portable-server.sh init --source-client /srv/client-pack --host update.example.com --bind 0.0.0.0
# 编辑 tools/portable-pack.json：packId、packName、版本、更新说明和实际下载地址
sh tools/portable-server.sh publish
sh tools/portable-server.sh serve
```

Windows，在 PowerShell 中：

```powershell
.\tools\portable-server.bat doctor --source-client 'D:\Client Pack'
.\tools\portable-server.bat init --source-client 'D:\Client Pack' --host update.example.com --bind 0.0.0.0
# 编辑 tools/portable-pack.json 后：
.\tools\portable-server.bat publish
.\tools\portable-server.bat serve
```

两个入口都只寻找并实际检查可用的 Python，不自动安装软件。也可直接运行 `python3 tools/portable_server.py`（Windows 用 `python` 或 `py -3`）。所有相对配置路径以仓库根目录为基准，与终端当前目录无关。

`doctor` 只检查并显示有效路径、操作系统、Python、识别出的客户端版本和配置覆盖来源。`init` 只创建新配置，已有配置不会被覆盖。默认地址和监听都是 `127.0.0.1`，检测结果会提示它们只能供本机使用。公网域名、端口映射和反向代理不能从网卡可靠推断，需要明确配置；脚本不修改防火墙或 NAT。

`serve` 在前台运行，Ctrl+C 正常结束；可由自己的 systemd、Docker 或面板进程管理器托管。此通用版本没有面板专属 API 或 Docker 专用变量。示例域名必须换成真实地址。

## 自动检测与环境变量

优先级：**命令行参数 → 下表环境变量 → 配置文件 → 示例默认值**。只读取下表变量，不收集或打印整套服务器环境变量。

| 变量 | 配置 / 用途 |
|---|---|
| `PORTABLE_CONFIG` | 配置 JSON 路径；显式指定但不存在会报错 |
| `PORTABLE_SOURCE_CLIENT` | 客户端实例目录；`auto` 启用限定目录检测 |
| `PORTABLE_PUBLISH_DIR` | 仓库内专用发布目录 |
| `PORTABLE_PACK_ID` | 整合包固定身份 |
| `PORTABLE_PACK_NAME` | 整合包显示名称 |
| `PORTABLE_VERSION` | `X.Y.Z` 发布版本 |
| `PORTABLE_UPDATE_HOST` | 玩家可访问的域名或 IP，不含协议、端口或路径 |
| `PORTABLE_UPDATE_PORT` | 下载端口，1–65535 |
| `PORTABLE_UPDATE_BIND` | 本机监听地址；例如 `127.0.0.1`、`0.0.0.0`、`::` |
| `PORTABLE_UPDATE_SCHEME` | `http` / `https` |

`--source-client`、`--host`、`--port`、`--bind`、`--version` 可覆盖对应值。环境变量覆盖不会暗中回写现有配置；`init` 创建时会保存本次有效配置。后续发布和启动应使用相同环境。

自动查找当前仓库根目录、`main-client` / `client` / `客户端`、`.minecraft`，以及 `.minecraft/versions/*`、`versions/*`、`instances/*/{.minecraft,minecraft}` 等常见布局。找到多份时服主入口会列出候选并停止，要求明确指定；不扫描整块硬盘、不读取启动器账户。存在 `server.properties` 或 `eula.txt` 的目录不会当作客户端。

客户端版本 JSON 或 `mmc-pack.json` 存在时，可识别其中的 Minecraft、NeoForge、Forge、Fabric、Quilt 元数据；缺少元数据就保留未知。已有明确版本配置不会被识别结果覆盖。

## 玩家把 BAT 单独放在启动器旁

分发目录中的 `更新mod-Windows端.bat` 是独立入口。玩家可**只复制这一个 BAT** 到 PCL、HMCL 或 Prism/MultiMC 启动器所在文件夹，`_updater` 和 `UPDATE-URL.txt` 仍留在原客户端实例内，无须一并搬走。

入口会在自己的位置附近寻找已配备更新器的客户端：当前目录、`.minecraft`、`.minecraft/versions/*`、`versions/*`、`instances/*`、`instances/*/.minecraft`、`instances/*/minecraft`。找到一份就更新，找到多份就显示选择列表；取消时不修改任何实例。路径支持中文、空格、`&` 和 `!`。

该功能不按启动器进程或账户猜测实例。自定义目录或启动器把实例保存在其他磁盘时，设置玩家端环境变量 `PORTABLE_INSTANCE_DIR` 指向目标实例，或把 BAT 放回实例内即可。完全没有 `_updater` 与更新地址的原版客户端不能只靠一个 BAT 猜出整合包来源。

同步成功后沿用现有 Windows 启动器启动和后台预下载流程。仅同步时可设置 `PORTABLE_SYNC_ONLY=1`，成功后不启动启动器/后台预下载。Linux/macOS 使用实例内对应入口，需要 Python；新的通用发布器生成的 Unix 入口只执行同步，玩家自行打开启动器。

## 发布保护和回滚

通用发布器先在同一父目录准备完整候选，最后切换发布目录。已有发布目录会保留为同级 `.portable-backup-*`；切换失败会恢复原目录。没有自动删除历史备份，服主应自行管理保留期限。切换瞬间可能出现短暂下载失败，客户端会重试并校验内容哈希，不能把它当作零中断发布。

会拒绝：源目录与发布目录重叠、仓库外发布目录、覆盖工具源码目录、不明非空目录、不同 packId、路径穿越、软链接/junction、Windows 不支持的路径、大小写冲突、无效端口和把监听通配地址当玩家下载地址。目录锁防止两个通用发布器同时运行；异常断电留下锁时，先核查 `.portable-publish-lock/owner.json` 的主机/PID，再人工处理，脚本不会杀进程或盲目解锁。

保留已有玩家自定义配置、主动删除、资源包追加等清单策略；发行内容仍由 `includeRoots/includeFiles/excludeGlobs` 决定。通用发布器使用自建源，不做 Modrinth 查询，也不捆绑启动器；`launchers.include=true` 会明确报错。原 PowerShell 发布器保留官方源查询和启动器打包能力，两种发布器**不要同时对同一目录运行**。通用发布器可接收旧格式 2 清单并保留原目录备份。

直接 HTTPS 在配置中填写 `update.certFile/keyFile`。已有反向代理终止 TLS 时设置 `update.reverseProxyTls=true`、`scheme=https`，并让反代保持 URL 路径；这里的 `port` 同时用于本机服务和公开 URL，端口不同的反代部署须先对齐端口设计。随机路径令牌保存在私有文件中，不打印在检测报告或服务命令行里；Linux 新建令牌文件权限为 `0600`。
