---
name: newsagent-windows-streaming-setup
description: 为 NewsAgent 实时数仓实验准备 Windows 台式机：检查资源，安装或复用 WSL2、Ubuntu 与 Docker Desktop，验证容器和镜像网络，输出可供后续部署使用的环境报告。用于用户要求安装实验基础环境时；不默认部署完整流式作业或执行性能压测。
---

# Windows 实时数仓实验环境准备

## 目标与执行方式

用户有一台 Windows 台式机，希望交给本机 AI 安装基础环境，随后搭建并压测：

```text
Python 合成行为事件 → Kafka → Flink → PostgreSQL 版本化聚合快照
→ 消费账本 → Temporal → NewsAgent 热点排行与分析
```

本 Skill 的交付是“基础环境可用、依赖可获取、资源与后续缺口明确”。默认范围为
WSL2、Ubuntu、Docker Desktop、必要命令行工具和小型容器验收；完整 Kafka/Flink/Agent
部署、长时间压测和性能优化属于下一阶段，不将基础安装成功描述为流式链路已完成。

本文件可单独传给任何具备终端或桌面操作能力的 AI，无需安装其他 Skill，也不要求本机
已经有 NewsAgent 仓库。用户明确要求“按此 Skill 安装”后，直接推进范围内的常规安装、
检查和修复，不反复索要相同许可。只有发现缺失信息、需要用户操作的 UAC/固件界面、
许可选择或未获授权的停机操作时，才提出具体问题，并继续不受影响的工作。

先确认操作的是 Windows 目标主机。若当前 AI 只连接其他机器，应先取得目标主机的访问方式，
不要在 Mac 或其他服务器安装本流程。以下 Windows 命令在 PowerShell 执行；Linux 命令在
所选 Ubuntu 的普通用户终端执行。不要把安装命令全部拼成一次不可恢复的脚本。

## 1. 只读盘点并选择预算

检查 Windows 版本与架构、CPU、内存、硬件虚拟化、各 SSD 分区剩余空间，已有 WSL、
Docker 与运行容器。示例命令按实际权限选用；命令不存在是待安装状态，不是整体失败：

```powershell
Get-CimInstance Win32_OperatingSystem | Select-Object Caption, Version, BuildNumber, OSArchitecture
Get-CimInstance Win32_Processor | Select-Object Name, NumberOfCores, NumberOfLogicalProcessors, VirtualizationFirmwareEnabled
Get-CimInstance Win32_ComputerSystem | Select-Object TotalPhysicalMemory, HypervisorPresent
Get-Volume | Where-Object DriveType -eq 'Fixed' | Select-Object DriveLetter, FileSystem, Size, SizeRemaining
wsl --version
wsl --status
wsl -l -v
Get-Command docker, winget -ErrorAction SilentlyContinue
```

若已有 Docker，读取 `docker version`、`docker compose version`、`docker ps`、
`docker stats --no-stream`、`docker system df`。不要输出完整容器 inspect、环境变量或代理凭证。
不能只因某个虚拟化字段为 false 就下结论；结合当前 hypervisor 和实际 WSL 启动结果判断。

工程起始预算如下，不代表实测吞吐或官方最低要求：

| 实验档位 | 实验可用 CPU / 内存 | 可用 SSD | 用途 |
| --- | --- | --- | --- |
| 轻量 | 4 个逻辑 CPU / 8 GiB | 50 GiB | 功能验证、短时低速实验 |
| 推荐 | 6–8 个逻辑 CPU / 10–12 GiB | 80 GiB | 后续完整链路与多轮对照 |

资源指留给实验的部分，Windows 和其他应用另留余量。16 GiB 物理内存优先轻量档；
32 GiB 及以上可从推荐档起步，仍以实际负载为准。无需 GPU。不要因未达推荐预算阻止
不依赖容量的检查；分别报告“基础环境可用”和“推荐压测预算是否满足”。

推荐 80 GiB 的预算为：镜像/构建缓存 15、Kafka 日志 15、PostgreSQL 数据及 WAL 10、
Flink 检查点 10、日志/报告 5、余量 25 GiB。只核对容量，不在本阶段分配或写满这些空间。

重点核实 Docker 数据文件和 Ubuntu 虚拟磁盘实际位于哪个分区。D 盘有空间不代表仍存于
C 盘的数据能够增长；WSL 内 `df` 的虚拟容量也不能替代 Windows 所在盘的真实余量。

## 2. 安装或复用 WSL2 与 Ubuntu

先查已有发行版并复用合适的 Ubuntu，不重装、注销或覆盖已有环境。新安装优先 Ubuntu 24.04 LTS。
确认 Windows 满足当前 Docker Desktop 要求；WSL 能安装不等于该 Windows 版本仍受 Docker 支持。

尚无合适 Ubuntu 时，在需要的管理员 PowerShell 中安装：

```powershell
wsl --list --online
wsl --install -d Ubuntu-24.04
```

确认在线列表中有该名称再执行安装。按需要使用 `wsl --update`；发行版版本由 `wsl -l -v`
确认，应为 2。已有 WSL1 发行版转换为 WSL2 前说明对该发行版的影响，尊重现有运行任务。

首次启动 Ubuntu，由用户完成需要其掌握的账户/密码步骤。密码不写入脚本或报告。
如果要求 Windows 重启，保存当前进度和恢复命令后提示用户重启；仅在用户已明确允许时
代为重启。遇到未开启固件虚拟化时给出已确认的原因和用户操作要求，不循环重装 WSL。

安装后验证 Linux 可实际执行命令，确认发行版、CPU 架构、内存和磁盘：

```bash
cat /etc/os-release
uname -m
free -h
df -h ~
```

## 3. 安装或复用 Docker Desktop

优先复用健康且受支持的现有 Docker Desktop。缺失时从 Docker 官方网站下载安装包，
或使用已验证的软件包管理条目。架构要匹配目标 Windows；不能默认所有台式机都是 AMD64。
可先执行 `winget show --id Docker.DockerDesktop --exact --source winget` 核实包，再按宿主
支持的授权机制安装。需要人工许可选择时交给用户，不代为购买订阅。

确保 Docker Desktop 已实际启动并满足：

- 使用 WSL2 后端和 Linux containers。
- `Settings → Resources → WSL Integration` 已启用所选 Ubuntu。
- Docker 数据位置所在 SSD 有预算要求的可用空间。
- Windows 登录账户与所用 WSL 发行版所属账户一致。

此路线不在 Ubuntu 内再安装第二套 Docker Engine。如果已经存在独立 Engine，先确认
哪个 daemon 持有现有容器和数据，诊断冲突，不自动卸载或清除它。

初次安装可选择空间充足的数据位置；已有数据的位置迁移通过 Docker Desktop 支持的设置处理，
在执行前说明停机影响并确认已有授权。不要手工移动运行中的 VHDX，不用 prune、reset、
`wsl --unregister` 或删除卷作为释放空间的快捷方式。

检查已有 `%USERPROFILE%\.wslconfig` 的相关资源设置，保留其他配置。仅在确有需要时
按盘点结果合并调整内存/CPU，不覆盖文件。该配置影响所有 WSL2 发行版；应用变更可能需要
退出相关应用并执行 `wsl --shutdown`，须先处理已有运行任务，不能在后台悄悄停止全部 WSL。
从 Linux `free -h`、`nproc` 和 `docker info` 核对实际生效值，而非只报告配置文件中的数字。

Ubuntu 中缺失的基础工具可安装 `git`、`curl`、`ca-certificates`。不要求宿主额外安装
Java、Maven、Python、Node、Kafka、Flink、PostgreSQL、Hadoop、ZooKeeper 或 Kubernetes；
后续运行依赖统一放容器并固定版本。

## 4. 分层验证容器与依赖网络

在选定 Ubuntu 终端执行，不能只验证 PowerShell 的 Docker 命令：

```bash
docker version
docker compose version
docker info --format '{{.OSType}} {{.Architecture}} {{.NCPU}} {{.MemTotal}}'
docker run --rm hello-world
```

要求 Server 可达、OSType 为 linux、Compose 可用、hello-world 成功退出。如果 hello-world
来自缓存，记录这一事实；它不能证明此次网络下载可用。

再只读检查未来镜像的 manifest，例如下面这组 2026-10-22 的候选：

```bash
docker buildx imagetools inspect apache/kafka:3.9.1
docker buildx imagetools inspect flink:1.20.5-java17
docker buildx imagetools inspect postgres:16-alpine
```

这些是待部署时再次核定的候选，不是永久最新版本。记录目标架构是否存在及摘要；
具体版本若已不可用或不再适合，从官方发布页及 Flink 连接器兼容矩阵选一致的替代组合，
说明变更，不默默退回无固定版本的镜像。Java 使用容器内匹配版本。
还需验证后续 Python 依赖源、Maven Central 的连接器/驱动获取路径；能访问网站首页不能
替代实际依赖 URL 的验证。此阶段不必下载全部 JAR 或大型镜像。

若用户同时要求预拉镜像，在磁盘预算足够后按核定版本执行并记录实际结果；否则预拉状态为
`not_run`。区分“manifest 可读”“镜像已缓存”“容器运行成功”，不能合并成一个下载成功结论。

网络失败时，分别检查 Windows 与 WSL 的 DNS、HTTPS 和 Docker 下载路径。HTTP 401
可能表示 registry 已可达，不应单凭该状态认定断网。记录脱敏错误；同一暂态请求最多补试一次，
随后转入诊断。不要擅自改系统 DNS、关闭防火墙/证书校验或配置未知镜像站。
离线方案仅导入来源明确、摘要和架构已核对的镜像包，并明确实际联网检查未通过。

## 5. 可选 SSH 与后续工作目录

用户在台式机本地让 AI 执行时，不需要 SSH，未安装 SSH 不阻止基础环境验收。
只有用户要求从 Mac/其他机器远程部署时，再配置 Windows OpenSSH Server、合适的
账户及局域网访问规则，验证 SSH 登录后能访问同一用户的 Ubuntu 和 Docker。
采用密钥认证时只交换公钥；不把密码、私钥、Token 放进报告或聊天。
不为远程操作把 Kafka、数据库或 Docker daemon 直接开放到公网。

后续代码工作目录建议放在 Ubuntu 的 `~/NewsAgent`；若目录已存在先识别，不覆盖。
Kafka、数据库和检查点用新实验专属卷。尚未收到仓库来源或实验代码时，不伪造克隆地址，
不从原 Mac 复制 `.env` 或企业数据；先交付环境报告即可。

## 6. 验收与交接

将简短中文报告与机器可读摘要保存到目标主机新建的、带时间戳的报告目录，返回绝对路径。
Ubuntu 已可用时可用 `~/newsagent-setup-reports/<时间戳>/`；未就绪时可先用 Windows 用户
目录下的独立报告文件夹。保存命令名称、时间、退出状态和必要脱敏输出，不能将未执行步骤填为通过。

报告至少包含：

- Windows/CPU/架构/物理内存；所选预算及磁盘实际位置、可用容量。
- WSL 版本、Ubuntu 名称/版本/用户上下文、有效 CPU/内存；重启待办。
- Docker Client/Server/Compose 版本、Linux daemon 与 Ubuntu 集成结果。
- hello-world 状态；各候选镜像的架构、摘要、manifest/预拉分别的状态。
- Python 与 Maven 依赖网络检查结果；无法核实时明确未验证。
- 做了什么修改、保留了什么现有服务、SSH 是否需要/是否可用。
- 后续代码目录、报告位置、已知阻碍、恢复后应执行的准确下一步。

各项使用 `passed / failed / blocked / not_run / not_applicable`，分别给出三层结论：

- **基础运行时可用**：WSL、Ubuntu 内的 Docker/Compose 和 hello-world 实际通过，基础工具齐备。
  这一结论不表示磁盘足够或镜像网络可用。
- **实验资源满足预算**：实际数据位置的可用 SSD、有效 CPU/内存及宿主余量满足所选档位。
  低于轻量档时记为 `blocked`，继续保留已通过的运行时结果；不能仅因“已核实”就算资源合格。
  用户另行明确批准更小的冒烟预算时记录该预算及限制，不将其视为推荐压测配置。
- **后续依赖可获取**：所需镜像架构/manifest 与实际依赖获取路径已验证；离线包只有在所需
  依赖齐备且校验通过时才可作为替代，并标注离线来源。

三层都满足才报告“实验准备就绪，可进入独立数仓部署”。网络失败或资源不足时说明具体阻碍，
不要用“安装成功”替代整体就绪。完整流式部署与性能压测按本阶段实际情况标为 `not_run`，
不得声称已达到任何吞吐或延迟指标。

后续实施待办：独立 Compose 和版本锁定；事件 ID/UV/迟到口径；持久消费位置与检查点；
幂等写入和原子快照发布；独立正确性核对器；分档负载与故障恢复实验；Agent 修订身份和补窗消费。
未来压力测试设置事件数、时长、磁盘余量和积压停止条件；本阶段不启动该负载。
新闻行为使用明确标记的合成数据，企业原始个人行为不在本地保存，外部模型调用预算为 0。

## 官方参考

- [Microsoft：安装 WSL](https://learn.microsoft.com/en-us/windows/wsl/install)
- [Docker Desktop：Windows 安装与系统要求](https://docs.docker.com/desktop/setup/install/windows-install/)
- [Docker Desktop：WSL2 后端、集成与数据位置](https://docs.docker.com/desktop/features/wsl/)
- [Microsoft：Windows OpenSSH Server](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_install_firstuse)
- [Apache Flink：发布版本与连接器兼容矩阵](https://flink.apache.org/downloads/)
- [Apache Kafka：3.9 官方 Docker 镜像说明](https://kafka.apache.org/39/getting-started/docker/)

遇到命令、系统要求或界面差异时以执行当日官方文档为准，并在交接报告记录实际版本。
