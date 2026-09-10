# 云服务器首次实机部署与验收

验收日期：2026-09-09。服务器 `123.60.71.37`（Ubuntu 22.04.5 LTS，约 1 GB 内存）。SSH 主机指纹已与用户提供的 `SHA256:my60maPD1k1sAD2EkNfPi9JVdIn/HaKVaBfmYBeL7WY` 核对一致。

## 当前可用入口

打开本机看板 **http://127.0.0.1:8765**，查看 **云服务器 · 测试 API**。

服务 ID：`a6e0c69cfaab463ca2373145cd8e7c49`。当前为健康、新鲜观测；监测容器、健康和就绪接口、CPU、内存及磁盘。检查间隔 15 秒，连续失败 2 次建单，连续恢复 2 次确认恢复。

代理和控制器均仅允许对 `cloud_test_api` 执行 `restart_service`。回滚版本、恢复配置和日志轮转没有为该目标开放。

## 部署结构

| 组件 | 实际位置 / 状态 |
| --- | --- |
| 本机控制器与看板 | `D:\Desktop\OpsSentinel`，`127.0.0.1:8765` |
| SSH 隧道 | 本机 `127.0.0.1:19876` → 云服务器 `127.0.0.1:9876`，当前后台运行 |
| 服务器 Agent 源码与虚拟环境 | `/opt/opssentinel`，OpsSentinel 0.2.0 |
| 独立 Python | `/opt/opssentinel-python`，Python 3.12.14；系统 Python 3.10 保留 |
| Agent 配置 | `/etc/opssentinel/agent.yaml` |
| Agent 凭据 | `/etc/opssentinel/agent.env`，权限 0600；未写入本文或源码 |
| Agent 状态与操作数据库 | `/var/lib/opssentinel-agent` |
| systemd 服务 | `opssentinel-agent.service`，已启用开机启动与失败重启 |
| 测试 API | `/srv/opssentinel-lab/compose.yaml`，服务 `api`，容器 `opssentinel-lab-api-1` |
| 应用地址 | 服务器本机 `http://127.0.0.1:18080/health` 和 `/ready` |
| Docker Compose | 新增 Ubuntu 软件源插件 2.40.3，保留现有 Docker 29.1.3 |

Agent 原生运行在宿主机。磁盘指标反映 `/var/lib/opssentinel-agent` 所在文件系统；Agent 的观测耗时包括容器和日志检查，不是纯业务响应时延。

测试 API 是无数据库依赖的专用无状态服务。它的 `/ready` 仅用于验证本次流程，不能当成真实业务的数据库或缓存连通性测试。

镜像从 AWS Public ECR 的 Docker 官方镜像命名空间获取并固定为：

```text
public.ecr.aws/docker/library/python@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
```

测试容器限制为 64 MB 内存、0.25 CPU，使用非 root 用户、只读根文件系统和有限日志轮转。Agent 以 root 运行并具备 Docker 访问能力；工具动作由固定服务配置及允许列表限制。

## 实际验收结果

| 场景 | 结果 |
| --- | --- |
| 只观察接入 | 连续多次真实容器、HTTP 与主机资源观测通过 |
| 单次审批恢复 | 停止专用测试容器 → 等待审批且未执行动作 → 批准一次重启 → 连续新探测通过；57.4 秒 |
| 自动恢复 | 再次停止同一测试容器 → 无审批自动重启 → 连续新探测通过；58.5 秒 |
| 动作次数与容器身份 | 两轮各只执行一次重启；各轮前后容器 ID 一致，未重建容器 |
| Agent 进程故障 | 在无进行中动作时终止 Agent 主进程，systemd 自动拉起，主 PID 改变 |
| 持久化 | Agent 重启前后，两条已完成动作记录保持一致；没有重放 |
| 最终状态 | 测试容器 healthy，Agent active/enabled，看板 healthy/fresh，两条事故均 resolved |
| 监听范围 | 9876、18080 均仅绑定 127.0.0.1 |

上述耗时为本次测试的实际值，不构成恢复时限承诺。没有执行服务器整机重启，也没有在真实业务上测试版本回退、配置恢复或日志轮转。

## 之后如何使用

日常在本机看板查看“云服务器 · 测试 API”的历史、资源预警和事故时间线。当前 CPU 超过 90%、内存超过 90%、磁盘超过 85%，持续 3 次观测才触发资源预警；恢复阈值分别为 80%、85%、80%。资源预警本身不触发重启。

**目前控制器和调度仍在本机电脑上。电脑休眠、控制器停止或 SSH 隧道断开后，云端 Agent 虽继续运行，但不会自行发现事故并发起恢复。** 这次已验证实机接入与恢复闭环；要实现不依赖电脑的 24 小时值守，还需把控制器迁移到常驻环境。

隧道已在后台启动。若以后断开，在 PowerShell 中执行：

```powershell
cd D:\Desktop\OpsSentinel
powershell -ExecutionPolicy Bypass -File .\scripts\connect-cloud.ps1 -Server root@123.60.71.37
```

该脚本前台运行，保持终端开启；Ctrl+C 停止隧道。若提示端口已被监听，先检查现有隧道，避免重复启动。首次连接偶发 SSH 握手超时，重试成功；这不影响云端 Agent 和测试容器运行。

专用 SSH 文件存放在 `D:\Desktop\OpsSentinel\.opssentinel\cloud-ssh`，目录被 Git 忽略，私钥限制为用户本人可读。控制器原有演练、HTTP 监测项及其历史保留。

服务器端常用只读检查：

```bash
systemctl status opssentinel-agent --no-pager
docker compose -f /srv/opssentinel-lab/compose.yaml ps
curl --fail http://127.0.0.1:18080/ready
```

已验证的部署样例保存在正式项目 `examples/cloud-lab`。样例 Agent 配置仍默认 `allowed_actions: []`；本台测试服务器经过上述验证后单独开放了重启权限。
