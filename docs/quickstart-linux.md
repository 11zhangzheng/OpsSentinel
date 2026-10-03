# Linux 常驻值守与故障演练

此入口部署一个原生 systemd 控制器、一个主机 Agent，以及专用的无状态 Docker Compose 测试 API。关闭浏览器或 SSH 隧道后，云端调度继续运行。整机停机时，这台机器上的控制器也会停止；整机监测需要外部节点。

## 前提

- 使用可中断测试服务的 Linux 主机，支持 systemd。
- 已安装可用的 Docker daemon 和 `docker compose`。
- 首次安装需要 Python3/pip、下载 Python 运行时、PyPI 依赖和官方容器镜像的网络访问。
- 使用完整源码目录；不需要模型 API Key。
- `/opt/opssentinel`、`/etc/opssentinel`、`/srv/opssentinel-lab` 是本安装配置保留的目录。

在源码目录执行：

```bash
sudo bash scripts/install-linux.sh --with-lab --auto-restart
```

安装器保留系统 Python，在 `/opt/opssentinel/.venv` 使用独立 Python 3.12。默认下载 uv 0.12.10；可通过 pip/uv 支持的索引环境变量选择团队认可的镜像。未满足 Docker 或 Compose 前提时会直接退出，不会自行更换系统的 Docker 安装。

`--auto-restart` 仅授权专用测试 API 的重启；不加该参数时新配置只观察。已有 Agent 配置会保留，不能通过再次运行安装器扩大它的允许列表。升级前需停止两个 OpsSentinel systemd 服务；状态数据库与凭据文件保留。

控制器使用独立的 `opssentinel` 系统账户，不加入 Docker 组、不挂载 Docker socket。Agent 以 root 运行，通过本地允许列表接受固定动作。两者都只监听服务器回环地址。

## 打开看板

在自己的电脑运行：

```bash
ssh -N -L 127.0.0.1:19865:127.0.0.1:8765 用户名@服务器
```

打开 `http://127.0.0.1:19865`。登录令牌为服务器 `/etc/opssentinel/controller.env` 中的 `OPS_API_TOKEN`，通过自己的受信终端取用；不放到 URL、Git、Issue 或公开演示中。

Windows 项目的专用密钥已经配置时，也可使用：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\connect-cloud.ps1 -Server root@服务器 -Dashboard
```

## 自己验证自动恢复

先等待看板中的测试服务健康，再在服务器上逐个执行：

```bash
sudo /opt/opssentinel/.venv/bin/python -m opssentinel.lab approval --allow-faults
sudo /opt/opssentinel/.venv/bin/python -m opssentinel.lab automatic --allow-faults
sudo /opt/opssentinel/.venv/bin/python -m opssentinel.lab unhealthy --allow-faults
sudo /opt/opssentinel/.venv/bin/python -m opssentinel.lab maintenance --allow-faults
```

| 场景 | 要验证的行为 |
| --- | --- |
| approval | 停止测试容器，先验证无动作，再由演练工具批准一次重启 |
| automatic | 停止测试容器，控制器自动重启并验证恢复 |
| unhealthy | 通过测试进程专用信号制造 HTTP 503，进程保持运行，验证容器健康检查和恢复 |
| maintenance | 维护中停止测试容器，至少 3 条失败观测不建单、不处置；结束维护后恢复 |

演练命令只接受固定的 `opssentinel-lab` 单服务配置，不接受任意服务器地址或 Shell。每次结果存入 `/var/lib/opssentinel-lab-results/run-*`，包含机器可读结果和事故 Markdown 报告。它会验证只有一次恢复动作、前后容器身份一致，以及后续业务探测通过。

失败演练不能算作已验证恢复。如果已有动作执行或结果不明，工具暂停该测试服务并保留现场；不会自动重复写操作。先检查事故与结果文件，再决定人工恢复。

## 将本机历史迁至云端

迁移是一项切换执行权的操作，必须在一个控制器停止调度该服务后再让另一个接管。

1. 在旧看板暂停目标 Agent 服务，并清空自动动作；等待正在执行的动作结束。
2. 使用 `python -m opssentinel.transfer --source 原数据库 --service-id 服务ID --output 新快照` 导出单服务。工具拒绝仍有执行权或动作正在进行的快照。
3. 通过受信 SSH 将快照传到服务器。快照包含该服务凭据，应按私密数据库保护。
4. 在全新的云端控制器数据目录安装：`sudo bash scripts/install-linux.sh --with-lab --auto-restart --import-db 快照路径 --service-id 原服务ID`。
5. 核对事故、动作和历史数量以及新鲜观测，再执行故障验证。旧控制器的该条目继续暂停。

导入保留事故证据，重写 Agent 连接地址为服务器本机地址，并重新累计新观测。不支持将快照合并进一个已有控制器数据库。若迁移失败，先停止云端控制器并确认不再有进行中的动作，再决定是否恢复旧控制器；不要让两个控制器同时启用自动处置。

## 阅读证据

事故详情中的“下载复盘报告”包含初始检查、动作前检查、持久化操作 ID/结果、恢复检查和完整事件时间线。它明确区分系统处置后的恢复、外部恢复与尚未恢复；原始日志和任意上下文不进入该摘要。

运行状态与日志：

```bash
systemctl status opssentinel-controller opssentinel-agent --no-pager
journalctl -u opssentinel-controller --since '15 minutes ago' --no-pager
```

首次体验目标是预装依赖后五分钟完成接入，但网络下载、依赖准备和新人安装成功率仍需独立实测，不把该目标当作已验证结果。
