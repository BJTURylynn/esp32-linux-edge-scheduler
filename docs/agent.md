# 两个只读工具的运维 Agent

沿用现有 `ReadOnlyAgent` 和 Python 标准库，在规则解释之外增加可选的模型问答。C 服务、ESP32 协议和原始实验数据不依赖模型；原有 `status`、`report`、`explain` 命令继续可用。

## 先运行离线版本

在仓库根目录执行：

```bash
python3 -m tools.agent explain --report docs/evidence/2026-10-23/comparison/report.json
python3 -m tools.agent ask "两种策略有什么取舍？" \
  --report docs/evidence/2026-10-23/comparison/report.json
```

这里读取的是合作者保存的 **2026-10-23 历史合成数据实验**。它不能证明当前服务或开发板正在运行。

读取自己的最新输出：

```bash
python3 -m tools.agent ask "现在接收任务吗，队列有多长？" \
  --status results/live/status.json --report results/comparison/report.json
```

默认 `--backend rules`，不访问网络、不读取 API Key。离线模式提供已观测值及固定调度机制说明，不尝试通用自然语言推理。`--json` 可输出结构化结果，包含模式、证据摘要、工具调用轨迹及模型请求次数。

## 配置可选的大模型

模型服务需要支持 Chat Completions 的 `tools` / `tool_calls` 函数工具调用格式。请使用自己账号有权限访问的实际模型 ID。以下都是占位配置，不是可直接调用的服务：

```bash
export EDGE_AGENT_BASE_URL="https://your-provider.example/v1"
export EDGE_AGENT_MODEL="your-tool-calling-model-id"
read -rs -p "API Key: " EDGE_AGENT_API_KEY
export EDGE_AGENT_API_KEY

python3 -m tools.agent ask "为什么紧急任务的 P95 下降？" \
  --backend llm --report results/comparison/report.json
```

Windows PowerShell 也可以运行；Python 和 C 服务不要求运行在同一台电脑，只要已把对应 JSON 结果复制过来：

```powershell
$env:EDGE_AGENT_BASE_URL = "https://your-provider.example/v1"
$env:EDGE_AGENT_MODEL = "your-tool-calling-model-id"
# 在自己的终端中设置 EDGE_AGENT_API_KEY；不要写入项目源码。
python -m tools.agent ask "解释两种策略的差异" --backend llm --report results/comparison/report.json
```

`--base-url` 和 `--model` 可以覆盖对应环境变量，Key 只从 `EDGE_AGENT_API_KEY` 读取。程序不会自动加载 `.env`。只有选择 `--backend llm` 后才会创建模型客户端；三项配置缺失时回退到离线解释。

base URL 末尾可以是 `/v1` 等服务前缀，程序追加 `/chat/completions`。远程地址必须是 HTTPS；仅本机 `localhost`、`127.0.0.1` 和 `::1` 允许 HTTP，便于测试本地兼容服务。客户端拒绝重定向，避免把授权头转发到另一地址。

请求使用 `model`、`messages`、`tools`、`tool_choice=auto`、`parallel_tool_calls=false` 和 `max_tokens=800`。部分供应商或模型不支持其中某些字段，需要选择与此接口兼容的模型；HTTP 错误会显示状态码并回退，不会反复重试。没有为某个供应商默认指定付费模型。

## 工具与执行边界

| 工具 | 来源 | 交给模型的内容 |
| --- | --- | --- |
| `query_status` | `--status` 固定的文件 | 策略、队列、任务计数、运行及接收标记等已校验字段 |
| `read_report` | `--report` 固定的文件 | 合成数据标记、配置可比性、分组完成量/拒绝量、等待及完成延迟统计 |

两个工具均无参数，模型只能传 `{}`。路径只能由启动命令指定；模型不能换路径，也不能获得 shell、改策略或重启服务等工具。工具读取的是普通 JSON 文件，单文件最多 8 MiB，拒绝无效 JSON、非有限数值或不完整的证据结构。

模型收到的是经过筛选的字段，不含主机完整文件路径、原始 metadata、命令行记录或任意说明文字。工具记录作为数据处理。最终输出另附程序生成的证据摘要，并重用模型实际读取的快照，不在回答后用变化了的文件替换原始观测。

## 循环与回退

1. 把用户问题与两个工具的声明发给模型。
2. 校验返回的工具名和空参数，再读取配置的文件。
3. 使用对应 `tool_call_id` 回传程序提供的结果。
4. 模型获得有效证据后，才接受其解释；否则要求先读取证据。
5. 出现 API/协议错误、重复读取同一工具或达到预算时，结束模型循环，输出本地规则解释。

默认最多 **5 次模型请求**，可用 `--max-turns 1..10` 调整。每个模型响应最多处理两个工具调用。每次 HTTP 操作默认超时 20 秒，`--timeout` 允许 0.1 到 120 秒；响应最多读取 256 KiB。没有自动重试，因此 403 不会被误当作“没有 Action”而循环请求。

模型生成的解释不能被当作已验证事实。程序摘要才是已记录数据的直接展示；仍需留意实验配置、拒绝量和每次运行的波动。状态文件不等于进程存活探测，合成数据也不等于板上验收。此版本不保证模型不会产生错误解释，也不接管实时调度。

## 验证

```bash
python3 -m unittest discover -s tests -p 'test_agent.py' -v
python3 -m unittest discover -s tests -p 'test_llm_transport.py' -v
make test
make sanitize
```

问答与传输测试使用可控的模拟响应，覆盖工具回传、原始证据筛选、禁止指定路径、空/缺失报告、非有限数值、API 403、超时、重复调用、预算耗尽、错误脱敏、响应大小以及重定向拒绝。没有使用真实模型凭证，也没有产生模型调用费用。

Windows 若默认临时目录受限，可在运行测试前把 `TEMP` 和 `TMP` 指向仓库 `.local/tmp`；这是本地测试目录，不应提交。Linux C 和固件跨端测试需要先编译对应程序，不能把跳过的测试计为已通过。

接口依据：[OpenAI 官方函数调用说明](https://developers.openai.com/api/docs/guides/function-calling?api-mode=chat)。这里实现的是兼容的 Chat Completions 工具循环，不绑定 OpenAI SDK 或特定模型。
