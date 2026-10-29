# 第二版：中央任务派发与板端计算

## 数据路径与角色

```text
本机浏览器 → Python 网页后端 → WSL Linux C edge_dispatcher
                                      ↕
                         Windows 双向网关（仅转发）
                           ↙                    ↘
                    Uno USB 串口          ESP32-C3 Wi-Fi/TCP
                           ↘                    ↙
                              接受确认与计算结果
```

中央使用非阻塞 socket 和 poll 事件循环管理两个执行节点，不调用原 `edge_compute()` 代替板卡执行。板端预装同一计算内核，收到参数后才生成合成样本并计算；这不是远程下载任意代码或部署大模型。Python 参考计算只在独立验收工具中使用，网关没有计算回退。

本版固定设备为 `uno-01`、`esp32c3-01`。Uno 使用115200 USB串口；ESP使用TCP9001，USB只用于Wi-Fi配置和诊断。Windows网关主动连接两端，沿用已验证的Windows→WSL本机端口转发，不新增防火墙规则。中央TCP适用于可信实验网络，没有公网鉴权或TLS。

## 固件与启动

新的 Uno 工程为 `firmware/coloreasyduino/ComputeWorker`，ESP32-C3 工程为 `firmware/esp32c3-arduino/ComputeWiFi`，共用本地库 `firmware/compute-worker`。必须随代码保留该库，并在 Arduino CLI 编译时增加 `--library <库目录绝对路径>`。详细命令与恢复方法见[板端库说明](../firmware/compute-worker/README.md)。

Uno仍选Arduino Uno，ESP32-C3仍用Arduino core3.3.12和DIO/40MHz/8MB。新固件会替换板上程序，但原 `EdgeDevice` / `EdgeWiFi` 工程未修改，可重新烧录恢复。不得同时用旧桥接工具、IDE串口监视器和新网关占用同一串口。

ESP烧录后，通过原配置入口连接电脑可达的2.4GHz局域网：

```powershell
python -m tools.wifi_configure --port <ESP串口> --ssid <热点名> --output D:/Codex/esp32-linux-edge-scheduler/outputs/my-wifi-config
```

密码隐藏输入，不写源码或GitHub。以返回的实际IPv4为准，不照抄历史IP。随后启动：

```powershell
python -m tools.dispatch_start --esp-host <实际IPv4> --uno-port <Uno实际COM口> --output D:/Codex/esp32-linux-edge-scheduler/outputs/my-dispatch-run --policy priority --web-port 8765
```

需要Python3.10+与`requirements-device.txt`中的pyserial；Linux需要GCC、Make。启动器自动构建新的C程序，选择未占用的中央端口，启动WSL子服务、双网关和回环网页，返回带`#dispatch`的页面地址。已有网页占用端口时明确失败，不停止其他程序。可用`--status`、`--report`保留原历史分析页的本机文件配置；它们不改变实时派发状态，不能将第二版的不同格式文件直接作为第一版分析证据。

终端保持运行；输入`STOP`回车、Ctrl+C或启动器stdin关闭后，仅清理自己拥有的服务。C实时文件在WSL原生文件系统中生成，停止后复制到指定D盘输出目录的`service/`。不会恢复以前的队列。重启使用新的输出目录；旧目录拒绝覆盖。

## 计算和调度语义

| 参数 | 范围与默认 |
|---|---|
| seed | uint32，默认0；批量按任务顺序加1并回绕 |
| count | 1～256，普通默认256，紧急默认16 |
| window | 1～16，默认8 |
| passes | 1～1000，默认100，每轮从空窗口真实重算 |
| batch_size | 网页一次1～32个，默认1 |

样本为`((i + seed % 201) % 201) - 100`。计算因果滑动平均，再求平均值和RMS；仅使用16项环形窗口，不保存整个样本数组。使用单精度浮点，结果与独立参考比较的容差为`1e-4 + 1e-4 * abs(reference)`，不沿用第一版double精度的1e-8容差。

中央等待容量默认64，不含最多两个在途任务。每设备一个任务，没有设备端队列；默认先派最早的紧急任务，再派最早的普通任务。同级FIFO，已派发任务不抢占；两设备同时空闲时轮流选取。也可用`--policy fifo`作为对照，但网页不在线改变策略。当前不按设备速度预测分配，不保证紧急任务总比普通任务更早完成，也没有饥饿老化。

单次运行最多保留512个已接收任务，达到上限明确返回`record_limit`，需归档重启。固件用64字节会话位图查重，允许优先级调度导致任务编号乱序；只拒绝已执行过的同会话编号。队列满返回`queue_full`，批量部分接收／拒绝逐项显示，不把部分成功当成全部成功。

## 状态、异常与接口

任务状态为`queued → dispatching → running → succeeded`；设备拒绝为`failed`，派发后断线、确认超时或结果超时为`unknown`。无在线设备时任务保留在有界中央队列，不在主机代算。3秒未接受或30秒未完成即关闭该会话；忙时不要求空闲心跳。空闲节点每秒心跳，5秒无响应后移除。

成功结果必须匹配设备、会话和当前任务，且已经收到接受确认。旧会话、错误节点、重复或迟到结果不能覆盖终态。未知不等于设备未计算；页面的手动重试创建新编号并关联`retry_of`，原记录保留。提交／重试绑定网页显示的`run_id`，防止中央重启后编号重用导致误操作。网络提交不确定时不重发，先刷新任务列表核对。

网页独立入口：

- `GET /api/dispatch/state`：实时查询配置好的中央服务，返回运行、设备及任务；不可达返回503，不拿历史记录冒充在线。
- `POST /api/dispatch/tasks`：用户显式提交，参数表字段加当前`run_id`。返回逐项接收与拒绝回执。
- `POST /api/dispatch/tasks/{id}/retry`：请求体只包含当前`run_id`，仅对失败／未知任务重试。

网页沿用本机Host、Origin、CSRF与JSON大小保护。中央地址只能通过启动配置指定，不能由网页或大模型改变。大模型仍只有原有`query_status`、`read_report`两个只读工具；历史分析的“示例／本机”入口不会提交任务，也不会自动读取本版实时任务结果。

中央`wait_ms`为入队至派发，`roundtrip_ms`为派发至结果／异常，包含转发和网络；`compute_us`为设备本机测得的计算耗时，可能受板端时钟分辨率影响。不能相减MCU与Linux时间戳，也不能把在途区间重叠说成同步CPU指令轨迹。

## 验证与实物证据

`make test`继续验证原有功能，并构建新的中央程序和共享板端测试。C黑盒覆盖FIFO／优先级、会话／编号、拆包粘包、队列、超时、失败与重试；网关和网页自动测试使用注入接口／模拟节点，不替代实物验收。CI同时编译新旧两套Uno及ESP32-C3固件。

只使用真实板卡的完整验收：

```powershell
python -m tools.dispatch_hardware_check --esp-host <实际IPv4> --uno-port <Uno实际COM口> --output D:/Codex/esp32-linux-edge-scheduler/outputs/my-hardware-check --timeout 300
```

验收依次隔离两块板跑同一固定输入集、验证双板在途与优先级竞争、断开自有Uno网关制造unknown并显式重试、验证无节点排队及64容量满后拒绝，最后重新连接真实设备排空。停止服务归档后才调用Python参考。完整验收故意保留一个unknown，不能要求`all_tasks_succeeded=true`或把它改成成功。

已归档结果可单独核对：`python -m tools.dispatch_verify --service <service目录>`。新旧模式的时间范围与负载不同，不直接据此宣称吞吐提升、硬实时或生产可靠性。

本次最终实物验收接收93个任务：92成功、1个主动断线产生的unknown，另有1次queue_full拒绝。最大绝对误差约4.69e-6；同时在途上限为2，每设备为1，优先级审计无违规并记录3次紧急任务越过普通任务的选择。unknown任务28关联到成功重试29，原记录保留。归档后的状态文件显示服务已停止，不能据此显示设备当前在线。

普通和ASan/UBSan构建均通过176项Python测试，共享固件3项CTest通过。网页另完成6项真实任务的表单提交与结果核对，以及离线禁用、筛选和窄屏检查；该6任务记录不用于证明紧急任务插队。原始记录、检查摘要、固件与源码SHA256以及截图见[第二版验收证据](evidence/central-dispatch/README.md)。
