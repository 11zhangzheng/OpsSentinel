# Linux 主机代理

首次接入测试服务器，请先按 [实机接入与验收流程](first-server-onboarding.md) 操作，使用默认关闭恢复动作的配置模板。

主机代理把少量 Docker Compose 服务的观察与恢复接口提供给控制器。它是独立进程，不需要模型 Key，只接收固定的服务 ID、动作名称与幂等操作 ID。

本版真实主机代理实现受控重启、指定镜像回滚、已知配置恢复和专用日志轮转，并根据配置与探测生成动作线索。回滚、配置恢复默认关闭，必须明确配置其恢复依据并加入允许列表。已在 Ubuntu 22.04 的专用 Docker Compose 测试服务上验收原生 Agent 的监测、审批重启、自动重启和进程恢复，见 [实机部署记录](cloud-server-deployment.md)。其他恢复动作仍不能视为已通过真实业务验收。

## 部署前提

- Linux、Python 3.11+、Docker CLI 与 Docker Compose 插件。
- 已存在的 Compose 项目和服务；每个登记服务应对应单个容器。
- 运行代理的账户能够读取项目配置、执行所需 Docker 操作并写入代理状态目录。
- 控制器能够访问代理的认证接口。默认绑定回环地址，可通过 SSH 隧道或可信 TLS 反向代理连接。

代理使用 Docker CLI，拥有相应 Docker 访问权限的账户可以操作该 Docker daemon。不要把控制器与高权限主机代理当成同一个信任边界。

## 原生安装与配置

在已复制到 Linux 的项目根目录执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
cp configs/agent.example.yaml configs/agent.local.yaml
```

编辑 `configs/agent.local.yaml`，把示例路径替换成实际的绝对路径。初次只接入观察时，使用 `allowed_actions: []`。

```yaml
state_dir: /srv/opssentinel/state
services:
  web:
    compose_file: /srv/myapp/compose.yaml
    compose_service: web
    health_url: http://127.0.0.1:8080/health
    business_url: http://127.0.0.1:8080/ready
    allowed_actions: []
```

`web` 是代理 API 的固定服务 ID。`compose_service` 必须等于 Compose 文件中的服务名。`compose_file` 必须在代理启动时存在；代理不会创建项目或猜测位置。状态目录需要可写，内部保存操作幂等数据库及回滚 override 证据。

设置独立的随机 Token，至少 24 个字符，然后前台启动：

```bash
export OPS_AGENT_TOKEN='replace-with-at-least-24-random-characters'
.venv/bin/python -m opssentinel.host_agent --config configs/agent.local.yaml --host 127.0.0.1 --port 9876
```

示例字符串不是适合部署的凭据。实际 Token 在控制器添加服务时输入，或通过控制器配置的 `agent_token_env` 引用。直接运行 Python 不会自动读取 `.env` 文件。

本版启动命令不自动安装系统服务；若需要常驻，使用团队已有的 systemd 或容器管理方式，设置明确的工作目录、环境变量和重启策略。

## 接入控制器

在看板添加代理服务，填写：

| 字段 | 内容 |
| --- | --- |
| 连接器 | `agent` |
| 目标地址 | 从控制器可访问的代理根地址，例如 `http://127.0.0.1:9876` |
| 代理服务 ID | 本地 YAML 中的 `web` |
| 代理 Token | 代理进程的 `OPS_AGENT_TOKEN` |
| 自动动作 | 初始保留为空；确认后逐项开启 |

当控制器与代理分别运行在不同主机或容器时，两个 `127.0.0.1` 不代表同一网络空间。应填写隧道或网络配置中真正可达的地址。

控制器的自动动作策略与代理的 `allowed_actions` 都必须允许，动作才可能自动执行。控制器上的人工审批不能绕过代理允许列表。代理配置加载于进程启动；修改后需要重启代理生效。

## 真实动作的精确范围

| 动作 | 前提与效果 |
| --- | --- |
| `restart_service` | Compose 中能明确定位一个已有容器，且容器或健康/业务探测异常。仅重启配置的服务，带 `--no-deps`；容器不存在时拒绝，不自动创建 |
| `rollback_release` | 当前镜像精确匹配 `expected_current_image`；它与 `previous_image` 都是不同的固定摘要；容器 `Created` 位于回滚窗口内、业务探针失败、声明数据兼容且上一镜像本机已存在。写入独立 override，以 `--no-deps --no-build --pull never` 更新指定服务；不修改原 Compose 文件 |
| `rotate_logs` | 明确的专用 `managed_log_path`，日志超过阈值且不超过 50 MiB。只复制并截断该文件，保留配置数量的归档；拒绝符号链接和非法文件类型 |
| `restore_config` | 受管文件与已知备份均在本地配置中指定，备份 SHA-256 匹配且两文件内容不同，容器身份已确认，新鲜健康/业务探针失败。归档原文件、原子替换、校验 Compose，再仅重建指定服务 |

重启或回滚命令成功返回后，控制器仍需多次健康与业务探测才能判定恢复。回滚是故障缓解，不是源代码修复。仅有 HTTP 失败不一定足以推断应该回滚，规则可能选择等待进一步处理；模型建议也必须通过上述前提。

代理按明确证据生成配置恢复、回滚、日志轮转或重启线索；控制器依据服务策略自动执行或请求一次审批。模型只能消费这些线索，不能凭日志文字自行扩展动作权限。回滚与配置差异是受配置约束的缓解依据，仍不证明故障因果关系。

启用回滚时，将以下片段合并到具体服务条目内部，例如 `services.web`；它不是完整代理配置。摘要必须换成已验证的实际值：

```yaml
allowed_actions:
  - restart_service
  - rollback_release
expected_current_image: registry.example.com/myapp@sha256:<64 hexadecimal characters>
previous_image: registry.example.com/myapp@sha256:<different 64 hexadecimal characters>
rollback_data_compatible: true
rollback_window_seconds: 600
business_url: http://127.0.0.1:8080/ready
```

这个兼容标记是服务负责人作出的声明，系统不会证明数据库或持久化数据可回滚。原 Compose 文件仍可能声明新版本，后续常规发布应先统一期望版本，避免再次覆盖缓解结果。

回滚窗口默认 600 秒，允许配置 30–3600 秒，从容器 `Created` 时间计算，不是从 Git 提交时间计算。执行前会重新检查部署身份与时间窗口，不能对已变化的现场继续使用旧方案。

## 已知配置恢复

准备经过审核的可用备份，登记完整 SHA-256，再将以下片段合并到具体服务条目内部，例如 `services.web`。如果同时使用其他动作，应合并 `allowed_actions` 列表：

```yaml
allowed_actions:
  - restore_config
managed_config_path: /srv/myapp/config/application.yaml
known_good_config_path: /srv/myapp/backups/application.known-good.yaml
known_good_config_sha256: <64 hexadecimal characters from the reviewed backup>
```

两个路径必须不同，均为绝对路径，路径各级不能包含符号链接，文件必须是只有一个硬链接的普通文件且大小不超过 1 MiB。备份摘要必须与配置吻合。代理在替换前重新读取文件和容器身份，拒绝已经变化的现场。

原文件以 `0600` 权限存档到代理状态目录；代理在原文件所在目录写入临时文件，保留原权限和所有者后原子替换。随后执行固定的 `docker compose ... config --quiet` 校验，再执行 `up -d --no-deps --no-build --pull never --force-recreate <配置的服务>`。Compose 校验只检查 Compose 配置，不验证应用配置的语义；可用备份必须先由服务负责人审核。

失败时代理尽力还原原配置文件。如果服务重建已经开始，即使文件还原成功，运行中的容器状态也可能已经变化，结果仍记录为不确定，并阻止后续自动写操作。系统不会把“文件还原”宣称为“旧服务运行态已恢复”。

## 专用日志轮转

启用日志轮转时，只使用为此用途准备的应用日志，不使用 Docker 内部 JSON 日志。`max_log_bytes` 为 1 KiB–50 MiB，`retained_log_count` 为 1–10。轮转采用 copy-truncate；复制期间发现大小变化会保留源文件并拒绝截断，但检查与截断之间仍可能存在写入竞争。这不是无损日志归档方案，不适合必须零丢失的审计日志。

## 接口与幂等

| 接口 | 认证 | 作用 |
| --- | --- | --- |
| `GET /health` | 无 | 仅检查代理进程是否在线 |
| `GET /v1/services/{id}/observe` | Bearer | 获取指定服务的容器状态、HTTP/业务检查、有限日志与指标 |
| `POST /v1/services/{id}/actions` | Bearer | 请求 `{"action":"restart_service","operation_id":"unique-operation-id"}` |

动作 API 不接受 Shell、路径或部署镜像参数，目标由代理本地配置决定。同一 `operation_id` 完成后返回已保存结果；执行已开始但结果未知时拒绝重放。数据库同时约束每服务最多一个 `started` 操作，覆盖共享状态库的代理实例。中断或不确定结果会保留这个占用，因此更换操作 ID 仍无法自动写该服务。应先检查服务与持久化记录，本版不提供自动解锁流程。

`/health` 成功只说明代理自身在线，服务健康要看 `observe`。CPU、内存和磁盘指标反映代理所处运行环境；在容器中部署时，不能一概当成完整宿主机指标。

## 代理容器

[Dockerfile.agent](../Dockerfile.agent) 提供可构建模板，包含 Docker CLI 与 Compose 插件。使用时需要显式挂载配置、状态目录、所需 Compose 项目路径以及 Docker socket。Compose 引用的相对路径必须在容器内仍然正确，且供 Docker daemon 使用的宿主机路径需一致。

本仓库的 [compose.yaml](../compose.yaml) 只启动控制器，没有替用户授予代理 Docker 权限。代理镜像构建与真实 Docker 动作是否已通过验证，以 [验收记录](validation.md) 为准。
