# OpsSentinel：第一台测试服务器接入

状态：本地接入材料已准备；尚未指定、连接或部署真实服务器。以下地址和路径均为示例，不能作为已确认的部署参数。

目标：接入一台 Linux 测试服务器上的单容器、无状态 Docker Compose 服务，验证观察、事故发现、一次审批重启与后续恢复确认。

## 1. 确定目标

需要提供 SSH 地址或已配置的别名、Linux 系统类型、Compose 文件绝对路径、Compose 服务名和健康检查 URL。另需明确是否允许对该测试服务进行一次停止与恢复演练。不通过聊天发送密码、私钥或代理 Token。

本版本以 Compose 服务为接入单位，同时采集代理环境的 CPU、内存与磁盘指标。磁盘指标对应代理状态目录所在文件系统；原生部署更适合观察宿主机。尚不覆盖任意 systemd 服务或 Windows 服务的自动恢复。

## 2. 在目标服务器只读核查

以下示例假设 Compose 文件为 `/srv/myapp/compose.yaml`，服务名为 `api`。以将来运行 Agent 的账户执行：

```bash
python3 --version
docker version
docker compose version
docker compose -f /srv/myapp/compose.yaml config --quiet
docker compose -f /srv/myapp/compose.yaml ps -a -q api
curl --max-time 5 --silent --show-error --output /dev/null --write-out '%{http_code}\n' http://127.0.0.1:8080/health
curl --max-time 5 --silent --show-error --output /dev/null --write-out '%{http_code}\n' http://127.0.0.1:8080/ready
```

验收：Python 3.11+，Docker daemon 和 Compose 可用，配置有效，目标只返回一个已有容器，应用接口返回 2xx。若没有独立 `/ready`，删除代理配置中的 `business_url`，并记录尚未验证关键依赖。

必须核对实际 Compose 项目身份。若原部署使用额外的 `-p`、多份 `-f`、`--env-file` 或不同 Docker context，当前代理仅使用一份固定 Compose 文件，需先确认能定位到原容器；不能通过重新创建服务来掩盖身份不匹配。

## 3. 安装并启动观察代理

复制项目源码至服务器的部署目录，例如 `/srv/opssentinel`。传输源码时排除 Windows `.venv`、`.opssentinel`、`.backups`、`.env` 和所有本地凭据配置。

```bash
cd /srv/opssentinel
python3 -m venv .venv
.venv/bin/python -m pip install .
cp configs/agent.observe.example.yaml configs/agent.local.yaml
```

编辑 `configs/agent.local.yaml`，替换为已核查的实际路径、服务名和 URL；状态目录需对代理账户可写，保持 `allowed_actions: []`。该模板登记的代理服务 ID 为 `test_api`，它与 Compose 服务名 `api` 是两个字段。

在同一个 Linux 终端中生成令牌并启动：

```bash
export OPS_AGENT_TOKEN="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"
.venv/bin/python -m opssentinel.host_agent --config configs/agent.local.yaml --host 127.0.0.1 --port 9876
```

令牌不写入源码或验收记录。接入时通过自己的受信终端取用并输入看板，不粘贴到聊天。此方式用于首次前台验证，终端关闭或进程退出后不会自动恢复；常驻部署时再配置稳定的凭据存储和进程管理。

## 4. 连接本机看板

在 Windows 单独打开终端，将 `your-test-server` 替换为实际 SSH 别名或 `用户名@地址`：

```powershell
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -L 127.0.0.1:19876:127.0.0.1:9876 your-test-server
```

隧道需保持运行。确认本机 `http://127.0.0.1:19876/health` 可达后，在 `http://127.0.0.1:8765` 看板添加：

| 字段 | 首次接入值 |
| --- | --- |
| 连接器 | 主机 Agent / `agent` |
| 名称 | 测试服务器 · API |
| 代理根地址 | `http://127.0.0.1:19876` |
| 代理服务 ID | `test_api` |
| 代理 Token | 上一步生成的实际令牌 |
| 检查间隔 | 15 秒 |
| 连续失败 / 恢复阈值 | 各 2 次 |
| 自动动作 | 全部关闭 |

代理 `/health` 只证明代理在线。必须进一步确认看板中目标服务的容器、应用检查和资源数据正确，且连续取得至少 3 条新观测。代理采集耗时不是纯业务接口时延。

## 5. 审批恢复演练

仅在目标和一次中断演练范围已明确后进行。先将该服务的代理配置改为 `allowed_actions: [restart_service]` 并重启代理；控制器自动动作继续保持为空。

先开放代理重启能力，再制造故障，避免在只读阶段生成一个没有动作建议的旧事故。若已存在旧事故，先查清其状态，不将旧事故等同于这次演练。

1. 记录目标容器 ID 和健康基线。
2. 对已确认的单个测试服务执行 Compose `stop`，保留容器。不要执行 `down` 或删除容器；代理重启不会重建缺失容器。
3. 等待连续失败形成事故，确认动作建议为重启、状态为等待审批。
4. 在看板批准本次重启，检查动作记录。
5. 等待至少 2 次新的业务探测通过，再判定恢复。
6. 若自动流程未恢复，使用预先确认的人工恢复命令启动同一服务；保留失败证据，不能反复注入故障。

15 秒间隔不代表固定 30 秒内必定恢复，探测和动作也需要时间。演练通过后，再按已约定范围开启控制器自动重启并进行独立验证。

## 6. 记录验收结果

| 检查项 | 当前状态 |
| --- | --- |
| 目标服务器、服务身份和演练范围 | 待提供 |
| Linux / Docker 环境核查 | 未执行 |
| Agent 部署与隧道连接 | 未执行 |
| 容器、HTTP、资源的新观测 | 未执行 |
| 故障发现和一次审批重启 | 未执行 |
| 连续探测确认恢复 | 未执行 |
| 常驻与重启恢复 | 未执行 |

本次文档准备不构成 Linux/Docker 实机验收。首次实机验证后，再配置 systemd 等常驻运行方式。本机控制器或 SSH 隧道随电脑休眠而中断，不能当作 24 小时值守部署。
