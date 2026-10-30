# SCEX 玩家更新器

从 SCEX 怀旧服（Legacy Genesis）当前使用版本中独立提取的 Minecraft 客户端更新工具。基于 [i0czf/minecraft-server-ops-kit](https://github.com/i0czf/minecraft-server-ops-kit)，本仓库是其 GitHub fork。

默认分支仅提供更新器：客户端增量同步、后台预下载、更新脚本刷新与修复、管理员发布清单、更新文件下载服务。上游的服务器启停、面板、RCON、QQ/Discord 通知、备份管理、整套工具包自更新均已移除。Git 历史保留上游记录。

`scex` 分支是 SCEX 正式部署使用的版本：在 main 的基础上保留单一公网地址规则、发布器 `-NoNotify` 与 Discord/QQ/RCON 通知、下载服务 `/kit/` 通道及默认 `0.0.0.0` 监听。正式发布物应从该分支的某个提交构建。

这是脚本工具，不是 Minecraft 模组，无须放进 `mods` 文件夹，也不需要服务器安装插件。仓库不包含整合包、模组 JAR、玩家存档、生产地址、令牌或私有配置。

## 功能

- 按清单 SHA-1 校验并增量下载；先校验临时文件，再在同一文件系统原子替换，旧文件覆盖或移除时保留本地备份。只保留最近一次同步的备份；没有替换或删除任何文件的同步不会清掉上一份。
- Windows PowerShell / Python 默认并行下载 4 个文件，清单可设置 1–8；官方源先直连，受阻后尝试已有代理，再回退更新服务。同一官方源主机连续 3 个文件因连接失败、超时、低速或繁忙下载失败时，本轮不再尝试该主机；404、哈希不符等单个文件问题不计数，一次成功即清零。
- Python 同步器与 Windows PowerShell 同步器，Windows / Linux / macOS 启动入口。
- 通用服主入口自动识别 Windows/Linux、可用 Python、指定环境变量和常见客户端布局，缺项/多实例明确提示。
- 玩家更新 BAT 可单独复制到启动器旁，自动查找已有更新器的实例；多实例由玩家选择。
- 游戏期间可后台预下载到 `.portable-staging`，下一次启动前应用并重新核对；同步成功后清空预下载缓存和“更新已就绪”提示。后台预下载沿用同步刚记录的文件校验结果，不在游戏启动时重读整个整合包。
- 更新器自刷新、Windows 入口修复、客户端自助检查及按需修复；NeoForge 实例可补齐已安装的 client-extra 资源库引用，保留自定义启动参数并备份原版本配置。
- 可配置保留玩家额外文件、本地配置改动与主动删除；也可显式指定强制同步、强制删除。
- 发布器生成清单和更新摘要，复用相同内容的文件；按文件 SHA-1 查询并缓存 Modrinth 官方源，可选接入 CurseForge 元数据。
- 下载服务只接受 GET / HEAD，通过随机 URL 路径限制访问，可配置 TLS；限制大文件并发缓冲并支持 PID 记录，默认监听本机。

## 管理员快速开始

**Windows/Linux 通用入口（推荐新部署）**：只需 Python 3.10+，支持检测、初始化、发布和启动下载服务。Linux 无须 PowerShell。完整环境变量、目录识别、可移动 BAT 和回滚说明见 [通用部署指南](docs/PORTABLE-SETUP.md)。

```sh
sh tools/portable-server.sh doctor --source-client /srv/client-pack
sh tools/portable-server.sh init --source-client /srv/client-pack --host update.example.com --bind 0.0.0.0
# 编辑 tools/portable-pack.json 的整合包身份、版本和更新说明后：
sh tools/portable-server.sh publish
sh tools/portable-server.sh serve
```

Windows 将 `sh tools/portable-server.sh` 换成 `.\tools\portable-server.bat`，路径换成自己的客户端目录。`update.example.com` 是占位符；自动检测不猜测公网转发地址。通用发布器不捆绑启动器，也不查询第三方下载源。

**已有 Windows PowerShell 发布流程**继续可用：发布器需要 Windows PowerShell 5.1 或 PowerShell 7；下载服务需要 Python 3.10+。玩家 Windows 入口优先使用 Python，未安装时使用系统 PowerShell；macOS 入口需要 `python3`。以下步骤对应原 PowerShell 流程：

1. 下载或克隆仓库，复制配置：

   ```powershell
   Copy-Item tools\portable-pack.example.json tools\portable-pack.json
   ```

2. 编辑 `tools/portable-pack.json`：设置 `packId`、`packName`、`sourceClient`、`version`；`sourceClient` 指向包含 `mods/config` 的客户端实例目录。相对路径以仓库根目录为基准。`publishDir` 必须是仓库内专用子目录，其中过期文件会被发布器清理。
3. `update.host` 改为玩家能访问的主机名或 IP，`update.port` 设置下载端口。示例的 `127.0.0.1` 仅用于本机试用。
4. 生成更新源：

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File tools\portable-publish.ps1
   ```

5. 在另一个终端启动下载服务：

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File tools\start-portable-update-server.ps1
   ```

   默认只监听本机。需要直接对局域网/公网提供下载时，按实际网络配置传入 `-Bind 0.0.0.0`（IPv4）或 `-Bind ::`（双栈）。脚本不修改防火墙、路由或公网转发。可用 `-Python C:\Python\python.exe` 指定解释器。

   直接提供 HTTPS 时，填写 `update.certFile`、`update.keyFile` 并设置 `update.scheme` 为 `https`；也可由反向代理终止 TLS。随机 URL 不是账户认证机制，拿到链接的人即可下载。不要把 `.update-server-token`、生成的地址文件或含令牌的日志提交到 Git。

6. 把生成目录中的 `_updater`、`更新mod-Windows端.bat`、`更新mod-Mac端.command`、`一键客户端自助修复.bat` 和 `UPDATE-URL.txt` 放入发给玩家的**客户端实例目录**。初始客户端的游戏本体、加载器、Java 等仍需自行准备；本工具同步的是清单文件。

后续修改客户端源目录，更新版本与 `releaseNotesVersion/releaseNotes`，再运行发布脚本即可。沿用怀旧服发布规则：换版本时使用 `X.Y.Z`，填写 `releaseLevel`（`major` / `minor` / `patch`）及 `releaseDecision`；例如 `1.0.0 → 1.0.1` 用 `patch`，`1.0.0 → 1.1.0` 用 `minor`。同版本允许重建。下载服务保持运行。此独立版发布脚本不发送群消息或游戏广播，也不提供 `-NoNotify` 参数。

## 玩家使用

关闭游戏后，双击实例内的 `更新mod-Windows端.bat`，也可只把这个 BAT 复制到启动器旁。它会寻找附近的已配置实例，多份时先选择；`_updater` 和更新地址文件留在实例内。Windows 入口会在更新完成后尝试启动已有启动器，并可启动后台预下载。Linux/macOS 在实例内用 `sh 更新mod-Linux端.sh` / `sh 更新mod-Mac端.command` 运行通用发布器生成的入口；原 PowerShell 发布器生成的旧 macOS 入口仍用 `bash` 运行。

只执行同步、不启动启动器或后台任务时：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File _updater\player-update-generic.ps1 -InstanceDir . -NoPause
```

```sh
python3 _updater/player-update-generic.py --instance-dir .
```

异常时先运行 `一键客户端自助修复.bat` 检查；确认需要修补后使用 `player-self-repair.ps1 -Fix`。同步状态位于 `.portable-sync-state.json`，被替换文件的备份位于 `.portable-sync-backups`，预下载位于 `.portable-staging`。Windows 上更新器会把这几个内部文件夹（以及 `.portable-sync-logs`、`.portable-manifest-cache`）设为隐藏；文件不设隐藏，因为隐藏文件会让部分写入方式失败。

## 配置与边界

- 示例默认保留配置的本地修改与删除，不分发 `options.txt`、`servers.dat` 或启动器，也不启用重复模组清理。需要统一覆盖某个文件时，在 `forceSyncGlobs` 填入精确路径。
- `forceDeleteGlobs` 会主动移除匹配文件并备份，优先使用精确旧文件名；不要用宽泛规则清理玩家自装内容。
- 常规同步会按上次清单管理的文件处理旧版本；额外模组不因“未出现在新清单”被全目录删除。`additiveDirs` 还可保留资源包、光影等目录的内容。
- SHA-1 用于文件一致性检查，清单未做数字签名；请只向玩家提供自己可信的更新源。
- macOS 启动器交互、实际游戏运行期间的后台预下载及公网 TLS/代理环境尚未在本次独立提取中实机验收。详见 [验证记录](docs/VALIDATION.md)。

## 下载源与并发配置

在 `tools/portable-pack.json` 设置 `downloadWorkers`（默认 `4`，有效范围 `1–8`），两种发布器都会写入玩家清单。`updaterRevision` 用于标记更新器脚本版本。

PowerShell 发布器有可用 Python 时调用 `official-source-index.py`，按精确 SHA-1 查询 Modrinth，并缓存已匹配来源；无 Python 时沿用原 Modrinth 查询。可在 `update.pythonExe` 指定解释器，设置 `officialSources.enabled=false` 则不做元数据查询。通用 Python 发布器仍只使用自建源。

可选的 CurseForge 元数据查询从发布进程的 `CURSEFORGE_API_KEY` 或 `officialSources.curseforgeKeyFile` 读取密钥。指纹只用于找候选，SHA-1 一致才会接受；客户端清单不含密钥。元数据匹配不保证 CDN 对玩家开放，下载仍须校验并回退，实际 CDN 鉴权需按自己的服务配置验证。

地址文件可写多行 URL。scex 分支的所有入口（Python/PowerShell 同步器、自刷新、两个后台预下载、macOS 入口）使用同一规则：忽略非 http(s) 行，只使用第一个公网地址，失败后按文件顺序尝试明确写入的局域网地址；规则与用例见 `tests/fixtures/url-order-cases.json`。地址文件里的地址都连不上时，两个同步引擎会再试本实例以前记录的地址（上次成功的地址、包内和缓存清单里的 `updateUrl`）；用它更新成功后，实例根目录里过期的地址文件会换成 `_updater/UPDATE-URL.txt` 的内容（原文件先备份）。只剩局域网地址时，失败提示会直接说明需要向管理员要最新的 `UPDATE-URL.txt`。客户端不会修改系统代理、路由或防火墙；开启 TUN 时，应用直连不等于流量绕过 TUN。

## 更新器维护工具（scex 分支）

- `python3 tools/update-adoption.py [--days 7]`：在下载服务器上只读汇总访问日志，按天列出玩家用的更新器版本和组件、每位访客最后一次的版本和状态，以及需要关注的访客（例如仍在用 2.27.24、或取清单失败）。访客编号每天更换，不记录 IP。
- `python tools/bump-updater-version.py X.Y.Z [--mac-note 说明]`：一次修改所有引擎副本里的更新器版本号，macOS 入口的 `@version` 同时加一并记一行历史；各文件的 BOM 和换行保持不变。`--check` 只检查各副本是否一致。
- `python tools/release-updater.py --config <主机配置> --release <release.json> <步骤>`：只发布更新器、不重发整合包。preflight 会核对提交里的引擎版本并显示玩家版本分布；deploy 前检查该提交的 GitHub CI 全部通过（`--skip-ci` 可跳过并记入回执）；finalize 后自动生成中文 `发布回执.txt`。主机配置和令牌放在仓库之外。

## 来源与许可

上游作者：**i0czf**，原仓库：[minecraft-server-ops-kit](https://github.com/i0czf/minecraft-server-ops-kit)。SCEX 怀旧服适配及此更新器独立提取由 **rianfalltwilight-lab / SCEX** 维护。本 fork 不代表上游官方发行版。

遵循上游 **PolyForm Noncommercial 1.0.0** 许可，保留原作者版权及 Required Notice；这是允许非商业用途的公开源码项目，商业使用须取得相应许可。详见 [LICENSE](LICENSE)。提取日期、固定上游提交与原始文件 SHA-256 见 [来源记录](docs/source-snapshot.json)。

## 回归测试

```powershell
python -m unittest discover -s tests -v
```

测试使用临时客户端与本机随机端口，不连接生产更新源；Windows 自动测试 PowerShell 同步器与发布器，可用环境变量 `UPDATER_TEST_POWERSHELL` 指定 `pwsh.exe`。测试结束关闭服务并清理临时数据。

GitHub CI 同时覆盖 Windows、Ubuntu 与 Python 3.10/3.12。通用发布、实际 HTTP 下载与增量同步、失败恢复、目录识别和 Windows 可移动 BAT 均使用合成数据验收；不启动 Minecraft 或操作生产服务器。
