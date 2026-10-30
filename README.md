# 双设备边缘计算、中央任务调度与运维工作台

第二版将 ESP32-C3 和粉色 ColorEasyDuino / Arduino Uno 作为计算节点：网页提交任务，Linux C 中央排队派发，两块板实际执行滤波与 RMS 并回传结果。Windows 网关只双向转发，不代算；大模型继续保持只读。原来的“设备上传、Linux 计算”演示与历史证据保留。

## 第二版：中央派发、板上计算

先烧录新的 `ComputeWorker` / `ComputeWiFi` 固件。ESP32 尚未联网时，可以先启动网页再从 ESP32 卡片的“连接 Wi-Fi”入口配网：

```powershell
python -m tools.dispatch_start --web-port 8765 --output D:/Codex/esp32-linux-edge-scheduler/outputs/my-wifi-run
```

页面区分 Wi-Fi 与中央接入状态，支持 USB 配网、只读板端状态查询和已有 IP 接入。成功配网后自动更新网关地址；尚无设备时任务会明确显示等待设备接入。Uno 可用 `--uno-port <实际COM口>` 接入。旧 ESP 固件可继续配网，新增 `STATUS` 查询需要更新本版固件。

Wi-Fi 入口改进已通过普通及 Sanitizer 各196项回归、浏览器无设备流程与固件编译；本次未连接板卡，尚未完成新版配网的实物验收，详见[Wi-Fi 入口验证记录](docs/evidence/wifi-onboarding/README.md)。

如果已知道 ESP32 实际地址，在项目根目录运行：

```powershell
python -m tools.dispatch_start --esp-host <ESP32实际IPv4> --uno-port <实际COM口> --web-port 8765 --output D:/Codex/esp32-linux-edge-scheduler/outputs/my-dispatch-run
```

打开启动器返回的 `http://127.0.0.1:8765/#dispatch`，提交普通或紧急任务，观察两块设备、队列和结果。保持启动终端运行，输入 `STOP` 回车或按 Ctrl+C 停止并归档；输出目录必须为新目录。若已有旧工作台占用端口，先停止旧实例，或用 `--web-port 8766`。

每块板最多一个在途任务，中央容量 64，紧急优先且同级 FIFO；断线／超时保留“结果未知”，不会自动重发。任务是明确标注的合成样本，不是实测传感器数据。构建、协议、数值精度、故障与恢复见[中央派发说明](docs/central-dispatch.md)。

中央派发初版已完成双板实物验收：93 个已接收任务中，92 个真实计算成功，1 个为主动断线测试保留的“结果未知”，另验证 1 次队列满拒绝。最大绝对误差约 `4.69e-6`，两设备同时在途上限为 2，审计记录 3 次紧急任务优先选择。当时普通及 ASan/UBSan 构建均通过 176 项 Python 测试；新旧固件分别编译。详见[实物记录、原始日志与网页截图](docs/evidence/central-dispatch/README.md)。

## 第一版：设备上传、Linux 计算

本项目支持两种已实物接入的设备：ESP32-C3 通过 Wi-Fi/TCP 上传合成数据；粉色立创 ColorEasyDuino（ATmega328P / Arduino Uno）通过 USB 串口上传。两者经 Windows Python 转发接入 WSL Ubuntu 的 Linux C 调度器，并由只读助手解释结果。原有 ESP-IDF 固件也保留为可选实现。原始规划保存在 [docs/original-plan.md](docs/original-plan.md)。当前实现及验收证据见 [docs/implementation.md](docs/implementation.md)。

历史版本已完成 Linux、ESP32 固件构建和集成验证，实测报告及图表见 [验收记录](docs/validation.md) 与 [证据快照](docs/evidence/2026-10-23/README.md)。原 ESP-IDF 固件尚未进行本次实物烧录；另增的 ESP32-C3 Arduino 固件已真实无线接入，见后文。真实无线掉线恢复仍待另行验收。

2026-10-25 已在本机 WSL Ubuntu 补齐此前 Windows 跳过的 9 项：普通与 ASan/UBSan 构建下均为 42 项测试全部通过，另保存六次调度对照实验和离线助手结果。见 [WSL 本机补验记录](docs/evidence/2026-10-25-wsl/README.md)。

粉色板已真实烧录并接入：COM10 → Windows 串口转发 → Ubuntu C 服务，20 个任务全部完成、0 拒绝，逐帧数值参考验证通过。完整自动测试为 59 项通过；见 [粉色板接入说明](docs/coloreasyduino.md) 与 [实物证据](docs/evidence/coloreasyduino-usb/README.md)。

这里的优先级是**应用队列选择顺序**：一个工作线程，已开始的任务不被抢占；不修改内核调度器，不提供硬实时保证。

## 一键运行项目

准备好两块板与同一2.4GHz局域网后，可一条命令完成Linux启动、双设备接入、完成量等待、正常停服、数据归档、数值验证和调度审计。

```powershell
python -m tools.project_demo --esp-host <ESP32实际IPv4> --uno-port COM10 --output D:/Codex/esp32-linux-edge-scheduler/outputs/my-project-demo
```

该一键流程已经用两块实物重跑通过：40任务全部完成、0拒绝，数值与调度审计通过，服务正常自动收尾。见 [一键验收证据](docs/evidence/project-demo/README.md)。

完整准备、重启配置、结果判断和代码学习路径见 [快速开始](docs/quickstart.md)。默认不需要API Key，所有设备样本都是synthetic数据。

## 目录

| 路径 | 内容 |
|---|---|
| `linux/` | 原版 C 计算服务、新版中央派发器、严格协议、有界队列与日志 |
| `firmware/coloreasyduino/` | Uno 数据源与计算节点两套固件 |
| `firmware/main/` | 原 ESP-IDF 设备端，作为可选实现保留 |
| `firmware/esp32c3-arduino/` | ESP32-C3 Wi-Fi 数据源与计算节点两套固件 |
| `firmware/compute-worker/` | 两板共享的单精度计算内核、协议和会话去重 |
| `tools/` | 单向／双向网关、统一启动、验收、回放、统计与只读查询 |
| `web/` | 实时任务派发、只读问答与历史数据查看，无前端构建依赖 |
| `tests/` | Python 单测与真实服务黑盒集成测试 |
| `scripts/vmware*`、`deploy/vmware/` | 独立 Ubuntu VMware 环境的创建与连接 |
| `.local/vmware/` | 本机镜像、虚拟磁盘和私钥，全部忽略提交 |
| `docs/` | 协议、实现取舍、硬件接入及验证记录 |

## 粉色板启动入口

默认接入流程、Arduino 编译/上传、Windows 串口转发和 WSL 服务命令见 [docs/coloreasyduino.md](docs/coloreasyduino.md)。串口不能同时被 IDE Serial Monitor 和桥接程序占用。13 口是约 1 秒心跳；实际计算结果以 Linux CSV 为准。

## 在 Linux 上构建与验证

需要 GCC、Make、Python 3.10+。Ubuntu 可执行 `sudo apt-get install build-essential python3`。
在项目根目录运行：

```bash
make -C linux
make -C linux test
EDGE_SERVER="$PWD/linux/build/edge_scheduler" python3 -m unittest discover -s tests -v
```

安装 CMake 后，`make test` 还会编译固件的可移植编码器，并执行固件最大帧到 Linux 的跨端测试。`make sanitize` 运行 C 核心的 ASan/UBSan 检查。

使用全新输出目录启动，已有 `tasks.csv` 时程序拒绝覆盖：

```bash
mkdir -p results
./linux/build/edge_scheduler --bind 127.0.0.1 --port 9000 \
  --policy priority --capacity 32 --window 8 --passes 1 --output results/live
```

另开终端回放：

```bash
python3 -m tools.replay generate results/workload.jsonl
python3 -m tools.replay send results/workload.jsonl --port 9000
python3 -m tools.agent status --status results/live/status.json
```

Ctrl+C 或 SIGTERM 停止接收并排空已入队任务。`status.json` 是原子更新的文件快照，异常断电/强杀后可能陈旧；它不替代进程存活检查。`tasks.csv` 每条记录刷新，正常退出时同步到磁盘。

## 可重复的策略对照

```bash
python3 -m tools.replay generate results/workload.jsonl
python3 -m tools.experiment --workload results/workload.jsonl \
  --output results/comparison --repeats 3 --capacity 64 --window 8 --passes 2000
python3 -m tools.agent explain --report results/comparison/report.json
```

可选图表：安装 Matplotlib 后执行 `python3 -m tools.plot results/comparison/report.json`，输出 PNG 和 SVG。图表展示汇总 P95、每次运行的 P95 点、完成量及拒绝量；核心服务和统计工具不依赖 Matplotlib。

实验顺序交替，每种策略三次。相同输入文件、间隔、队列、计算参数、线程数和可执行文件；记录文件 SHA256、CPU affinity、系统版本、实际发送迟到和接收时间。可用 `--cpu N` 将回放及子进程固定到允许的一个 CPU。先用 `python3 -c 'import os; print(os.sched_getaffinity(0))'` 查看允许的 CPU。

`passes` 每次重新计算同一输入，增加**实际计算工作**且不改变结果；计算函数没有 sleep。回放中的 sleep 仅实现任务到达间隔。需要观察积压时调大 passes 或增加批量，日常演示使用 1。主机负载和虚拟化仍会带来噪声，不能预先保证哪种策略快多少。

每次实验输出 CSV、状态、服务日志、元数据；最终生成 `report.json` 和 `report.md`，分别给出普通/紧急任务完成量、拒绝量、平均值和 P95。P95 使用 nearest-rank，拒绝任务不混入完成延迟。保留每次结果，避免只看汇总。

## Windows / VMware

见 [VMware 操作说明](deploy/vmware/README.md)。VM、镜像和项目都在当前目录，不修改现有虚拟机。Linux 编译与实验在 guest 内进行。

## 双设备任务调度

ESP32-C3与Arduino已同时接入同一Linux队列：实物40任务全部完成、0拒绝，观察到8次紧急任务优先处理；同设备数据的FIFO/priority六轮对照均完成且数值验证通过。启动及日志审计方法见 [双设备调度说明](docs/dual-device-scheduling.md)。所有样本为synthetic数据，公平回放采用另设的合成到达间隔。

## ESP32-C3 实物无线接入

新增 Arduino ESP32-C3 TCP 设备端和 Windows→WSL 转发入口，USB只用于配置和诊断；启动步骤、NAT下的实际数据路径与验收方式见 [ESP32-C3 无线接入说明](docs/esp32c3-wifi.md)。这与现有粉色板串口方案分别保留。

## ESP32 可选实现

固件及接线/配置/构建步骤见 [docs/firmware.md](docs/firmware.md)。默认是合成数据，不需要传感器。设备参数由 `idf.py menuconfig` 设置，Wi-Fi 密码只保存在忽略提交的本地配置中。

电脑到 NAT 虚拟机的回放可以直接使用 guest 地址；真实 ESP32 通常不能直接访问 VMware NAT 私网。接板时将**本项目 VM**切换到可达的桥接网络，确认开发板与 VM 的 IPv4 连通，再将服务绑定到 VM 的局域网地址。具体防火墙/路由需要按实际网络配置。没有应用层 ACK，发送成功仅表示 TCP 接受数据，不表示服务已入队或完成；最终结果以 Linux CSV 为准。

## 真实大模型只读解释

已接入用户选择的 `coding-glm-5.3-flash-free` 并完成真实两轮工具调用验证：模型读取 `query_status`、`read_report` 后解释实验数字，两次HTTP200、最终stop，未回退。入口 `python -m tools.agent_live --output <新目录>` 会隐藏输入Key；配置、输出完整性检查与实测证据见 [真实模型接入](docs/live-model.md)。模型仍只解释证据，不控制实时调度。

### 本地网页工作台

在项目根目录运行 `python -m tools.web_agent`，打开 <http://127.0.0.1:8765>。EdgeScope 提供“示例数据 / 本机实验”两个入口、聊天、实际工具记录、FIFO/priority 指标和离线规则模式；Key 仅保存在本地后端进程内存中。示例为作者历史结果，本机入口只读取 `--status` / `--report` 配置，未运行时显示“暂无实验记录”，不借用示例数据。每条问题独立分析，文件快照不代表设备当前在线。无需新增依赖，启动与数据路径配置见 [网页工作台说明](docs/web-workbench.md)。

## 只读助手与范围

`python3 -m tools.agent status|report|explain` 保留原有的离线查询和规则解释。新增 `ask` 自然语言入口：默认仍完全离线，显式选择 `--backend llm` 并配置模型后，模型可以调用同样两个只读工具，读取状态或实验报告，再生成解释。关闭它不影响 C 核心。

无需 API Key 的例子：

```bash
python3 -m tools.agent ask "为什么紧急任务快了，普通任务却更慢？" \
  --report docs/evidence/2026-10-23/comparison/report.json
```

接入支持函数工具调用的 Chat Completions 兼容模型，见 [只读 Agent 使用说明](docs/agent.md)。每次最多 5 次模型请求，禁止模型指定文件路径、执行 shell 或控制服务；API 失败、重复读同一工具或达到次数上限时，立即停止模型循环并回退到本地解释。程序独立显示从同一份证据快照计算的摘要，模型文字仍需与之核对。测试使用模拟模型响应，不调用真实付费 API。

问答扩展及关闭状态修复的本机测试范围见 [2026-10-25 验证记录](docs/validation-2026-10-25.md)；它与前面的 VMware 历史实测分开记录。

通信协议是受限的合成数据测试协议，见 [docs/protocol.md](docs/protocol.md)。当前适用于本机/可信实验网络；第一版没有鉴权、TLS、持久任务队列、ACK、去重或公平性老化。持续紧急流可能使普通任务饥饿。ESP32-C3 已验证两批共25任务无线接入及计算正确；真实断线恢复、长时间无线稳定性和演示录像尚未验收，不能由回放测试替代。
