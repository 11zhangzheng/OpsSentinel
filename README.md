# OpsSentinel

让小团队的服务故障有证据、有边界地走到恢复结果。

OpsSentinel 是一个可本地运行的主动运维原型：持续检查服务，合并重复异常，给出诊断，在已授权的范围内执行恢复动作，再通过后续探测确认恢复。任务、证据和执行记录保存在 SQLite 中，浏览器看板展示当前状态与待审批事项。

**本版提供规则诊断、可选模型诊断建议和受控恢复。** 重启或回滚后恢复可用，表示故障得到缓解；它不证明代码根因已经修复。本版没有自动修代码、创建 PR 或完成生产部署的能力。

## 5 分钟体验

需要 Python 3.11 或更高版本。演示不需要 Docker、模型 API Key，也不连接真实服务器。

Windows PowerShell，在项目根目录执行：

```powershell
python -m venv .venv
& '.\.venv\Scripts\python.exe' -m pip install -e '.[dev]'
powershell -ExecutionPolicy Bypass -File '.\scripts\start-demo.ps1'
```

若系统只有 `py` 启动器，用 `py -3 -m venv .venv` 替换第一行。已有项目 `.venv` 时可直接从安装依赖开始。下文命令均从项目根目录执行，并显式使用 `.venv`，无需激活环境。

Linux / macOS：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
sh scripts/start-demo.sh
```

打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)。脚本在前台运行，按 `Ctrl+C` 停止。脚本会优先使用项目的 `.venv`，不依赖当前终端的工作目录；路径包含空格、中文也可以使用。

交付时已经在后台运行的本机预览，可用 `powershell -ExecutionPolicy Bypass -File '.\scripts\stop-preview.ps1'` 关闭；该脚本核对 PID 文件、项目路径和本机演练命令后，仅停止匹配的预览进程，普通 `start-demo.ps1` 仍使用 `Ctrl+C`。

在看板的演练区注入一次故障，观察以下过程：

1. 连续探测失败后建立一个事件，记录探测、日志和事实。
2. 根据证据提出动作，自动授权的动作直接执行，其余等待本事件审批。
3. 执行后继续探测，连续恢复达到阈值才标记已恢复。
4. 查看时间线里的动作、结果、验证证据和恢复类型。

演练会启动一个独立的本机 HTTP 子进程，包含真实 HTTP 探测和可恢复的演示状态。可注入错误版本、错误配置、进程退出和日志压力。**这是一套隔离演练，不是对真实 Docker 服务的恢复测试。**

默认演练数据位于项目的 `.opssentinel` 目录。保留该目录可保留事件记录；不要让两个控制器共用同一个数据目录。重新启动会保留原有启停和授权设置；需要时在看板重新启用演练服务。

## 当前能力

| 接入方式 | 观察 | 动作 |
| --- | --- | --- |
| 本机演练 `demo` | HTTP、业务检查、演示日志和状态 | 对专用演示子进程执行四类恢复动作 |
| HTTP `http` | 指定 HTTP 健康地址 | 只观察 |
| Linux 主机代理 `agent` | 代理允许列表中的 Docker Compose 服务 | 符合允许列表和动作前提的重启、指定镜像回滚、已知配置恢复、专用日志轮转 |

真实主机代理会依据新鲜探测和明确配置生成四类处置线索。回滚要求当前/上一镜像的固定摘要、回滚时间窗口、失败的业务探针及数据兼容声明；配置恢复要求已审核备份及匹配的 SHA-256。两者默认关闭，必须先配置并授权，执行前还会重新检查。真实 Linux/Docker 环境尚未验收；看板演练和自动化测试的结果单独记录在验收文档中。

诊断默认由透明规则完成。配置 OpenAI 兼容模型后，可以请求额外建议；模型失败仍可使用规则流程。模型不能提交任意 Shell 命令，动作必须属于固定枚举并通过控制器与主机代理的授权检查。

## 不开启演练运行

```powershell
& '.\.venv\Scripts\python.exe' -m opssentinel --host 127.0.0.1 --port 8765 --data-dir .opssentinel --no-demo
```

可在看板中添加 HTTP 或主机代理服务，也可加载 [控制器配置示例](configs/controller.example.yaml)：

```powershell
& '.\.venv\Scripts\python.exe' -m opssentinel --config configs/controller.example.yaml --no-demo
```

Linux / macOS 对应命令为 `.venv/bin/python -m opssentinel --config configs/controller.example.yaml --no-demo`。

配置中的示例服务默认关闭，先替换地址再开启。代理配置可以用 `agent_token_env: OPS_LINUX_AGENT_TOKEN` 引用控制器环境变量，避免将实际 Token 写进 YAML。HTTP 探测成功只说明该地址满足探测条件；应选择能反映核心业务的健康检查地址。

## 认证和模型配置

控制器默认仅监听 `127.0.0.1:8765`。监听非回环地址时必须设置至少 24 字符的 `OPS_API_TOKEN`，并把实际访问域名或 IP 加入 `OPS_ALLOWED_HOSTS`，多个值以逗号分隔、不带端口。设置 Token 后，看板会要求输入；浏览器仅在当前标签页会话的 `sessionStorage` 中保留它。请通过可信网络或 HTTPS 反向代理访问。

PowerShell 示例：

```powershell
$env:OPS_API_TOKEN = 'replace-with-a-long-random-controller-token'
$env:OPS_ALLOWED_HOSTS = 'ops.example.com,192.0.2.10' # 替换为实际访问的域名或 IP
& '.\.venv\Scripts\python.exe' -m opssentinel --host 0.0.0.0 --port 8765 --no-demo
```

可选模型环境变量：

```powershell
$env:OPS_MODEL_BASE_URL = 'https://your-provider.example/v1'
$env:OPS_MODEL_API_KEY = 'your-key'
$env:OPS_MODEL_NAME = 'your-model-name'
```

请把服务地址、日志摘要及诊断证据视为可能发送给已配置模型提供方的数据。运行前确认所使用的数据源允许这样处理。

[.env.example](.env.example) 用作变量说明和 Docker Compose 模板。**直接运行 Python 不会自动加载 `.env`**，请在终端中设置环境变量。

这些环境变量在进程启动时读取；修改后请重新启动控制器。

代理 Token 在解析后会随连接配置保存在本地 SQLite 中，本版不提供数据目录的静态加密；请保护数据目录及其备份的访问权限。`agent_token_env` 可以避免凭据进入 YAML，不能替代数据目录保护。

## 独立部署 Linux 主机代理

控制器与主机代理是两个进程。控制器不挂载 Docker socket，不会直接执行用户服务器上的命令。主机代理应部署在具有所需 Docker Compose 权限的 Linux 主机上，只注册明确允许管理的服务。

先按 [主机代理文档](docs/host-agent.md) 在 Linux 安装项目，并把示例复制为 `configs/agent.local.yaml`、填写实际路径，再启动代理：

```bash
export OPS_AGENT_TOKEN='replace-with-at-least-24-random-characters'
.venv/bin/python -m opssentinel.host_agent --config configs/agent.local.yaml --host 127.0.0.1 --port 9876
```

代理配置字段、动作要求与具体部署方式见 [主机代理文档](docs/host-agent.md)。代理 Token 与控制器 Token 是不同凭据。控制器通过代理地址、服务 ID 和代理 Token 接入一个已配置的服务；使用隧道、私网或 TLS 代理建立实际连接。

首次接入建议保留空的 `auto_actions`，确认探测和建议准确后，再对该服务开启允许自动执行的动作。即使在控制器点击审批，代理也不会执行其本地配置未允许的动作。

停止某事件后，定时与手动巡检仍会记录健康状态，**不会重新开启处置**。结果不明的动作也不会自动重放，需要操作员核对现场；手动审批只批准该事件的一次具体动作。

## 运行控制器容器

此 Compose 示例只运行控制器。默认不开启演练，端口仅发布到宿主机回环地址，数据存入命名卷。

```bash
cp .env.example .env
# 编辑 .env，设置 OPS_API_TOKEN 为长随机值。
docker compose up --build
```

打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)，输入控制器 Token。容器里的 `127.0.0.1` 指向容器自身；接入外部代理时，请填写从控制器容器可达的实际地址。此示例不部署主机代理，也不挂载 Docker socket。

## 验证与项目结构

Windows：

```powershell
powershell -ExecutionPolicy Bypass -File '.\scripts\run-tests.ps1'
```

Linux / macOS：

```bash
.venv/bin/python -m pytest -q
```

测试结果、已验证范围与未验证范围见 [验收记录](docs/validation.md)。测试通过不能替代对真实服务回滚、数据兼容性和部署权限的单独验收。

本次 Windows / Python 3.12.14 验证使用的独立环境版本快照保存在 [requirements.lock](requirements.lock)，包含开发测试依赖。若需要复现这些版本，可在已经创建的 `.venv` 中执行：

```powershell
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.lock
& '.\.venv\Scripts\python.exe' -m pip install -e . --no-deps
```

Linux / macOS 将解释器路径换成 `.venv/bin/python`。该文件固定包版本；Linux 主机代理与 Docker 部署仍需在目标环境单独验证。

```text
opssentinel/          控制器、持久化、连接器、主机代理、静态看板
configs/              控制器与主机代理配置示例
scripts/              前台演练与测试入口
tests/                自动化验证
docs/                 架构、代理部署、验收记录
Dockerfile            控制器镜像
Dockerfile.agent      主机代理镜像
compose.yaml          仅控制器的本地容器示例
```

实现边界与状态转换见 [架构说明](docs/architecture.md)。采用 [MIT License](LICENSE)。
