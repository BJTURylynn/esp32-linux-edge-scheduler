# 双设备任务调度演示

本项目当前的调度发生在Linux应用层：ESP32-C3和Arduino是合成数据任务源，Linux是计算节点。不是将计算任务分配给两块板执行，也不涉及Linux内核调度器改造。

```text
ESP32-C3 ── Wi-Fi/TCP ─┐
                      ├─ Windows并发转发 ── WSL Ubuntu C统一队列 ── 工作线程
Arduino Uno ── USB ───┘                    FIFO / priority
```

## 已完成的实物验收

两块板同时连接，ESP32-C3为COM11、Arduino Uno为COM10。电脑和ESP32均连接Shino 2.4GHz信道6。各发送20个任务，共40个（32普通、8紧急），全部计算完成，0拒绝；两设备的device_id+seq、样本数、样本值和mean/RMS与独立参考逐项一致。

本次实时接入为观察排队，使用passes=1000000反复执行真实计算，capacity64、window8、priority、一个工作线程，没有通过sleep伪造计算负载。日志审计观察到8次紧急任务优先取出，共65对“urgent先于更早已等待normal”的关系，其中10对跨设备；未发现确定的优先级违规或工作线程重叠，同优先级保持FIFO。

例如Linux task21（ESP32 seq19，urgent）先于已入队的task19（Uno seq0，normal）开始。取任务的判断以“上一任务结束”作为保守边界，避免把出队后到started_ns之间入队的任务误当成违规。纳秒列用整数解析；单位为ns不代表纳秒测量精度。

## 启动步骤

先将ESP32配置到同一2.4GHz热点（见esp32c3-wifi.md），确认输出WIFI_READY；当前实物IP是172.20.10.14，可能由DHCP改变。Arduino已运行EdgeDevice，无需重复烧录。

第一终端启动服务，输出目录要全新；重复请改为dual-device-demo-v2等名称：

```powershell
wsl -d Ubuntu-24.04 --cd /mnt/d/Projects/esp32-linux-edge-scheduler -- bash -lc './linux/build/edge_scheduler --bind 0.0.0.0 --port 9000 --policy priority --capacity 64 --window 8 --passes 1000000 --output "$HOME/edge-results/dual-device-demo"'
```

第二终端在项目根目录运行：

```powershell
$python = 'D:\Codex\esp32-linux-edge-scheduler\tools\device-venv\Scripts\python.exe'
& $python -m tools.dual_demo --serial-port COM10 --device-host 172.20.10.14 --count 20 --output D:/Codex/esp32-linux-edge-scheduler/outputs/dual-device-demo
```

两个接入线程会同时运行。Arduino打开串口可能复位，ESP32序号跨TCP连接递增，所以必须以device_id+seq共同识别任务。forwarded只表示转发sendall成功，不能当Linux已计算完成。Arduino输出一直继续，停止桥接不代表停止其固件。

服务的status.json中completed达到40后，第一终端按Ctrl+C正常排空退出。将tasks.csv/status.json复制到D盘之后验证：

```powershell
& $python -m tools.dual_verify --capture <本次双设备捕获目录> --service <本次C结果目录>
& $python -m tools.schedule_audit --tasks <本次C结果目录>/tasks.csv --policy priority
```

日常功能演示可以将passes降为1；这时计算很快，未必出现排队，也就未必能观察优先级带来的差异。已经开始的计算不被抢占，优先级只决定工作线程接下来选谁。持续紧急任务可能造成普通任务饥饿。

## FIFO公平对照

直接分别实时运行两次，两块板到达时刻不完全相同，不适合据此计算策略提升百分比。应先保存两设备真实生成的合成帧，以同一份数据建立固定到达表，再回放两种策略。

本次按实时C服务的task_id顺序排列40帧，重新设置每帧2ms的合成到达时刻，passes20000、capacity64、window8、同CPU绑定，每种策略3次，次序交替。它是“真实设备数据的合成到达回放”，不是实物原始到达时间，也不是无线吞吐测试。

| 等待P95（合并三轮完成任务） | FIFO | priority |
|---|---:|---:|
| urgent | 170.845ms | 8.117ms |
| normal | 159.699ms | 163.057ms |

所有六轮都完成40、拒绝0，全部数值参考及策略审计通过。CPU、输入和服务二进制哈希、实际发送迟到等均记录在metadata.json；本次最大单帧发送迟到约1.54～5.86ms。相同计划不保证真实到达完全一样。

完整实物数据与对照证据：D:/Codex/esp32-linux-edge-scheduler/outputs/dual-device-scheduler。comparison/report.json、report.md、comparison.png、audit-summary.json分别保存统计、图及调度审计。agent-explanation.txt为规则模式的只读证据解释，未调用付费模型。

回放命令模板：

```bash
python3 -m tools.experiment --workload <相同捕获数据的到达表.jsonl> --output "$HOME/edge-results/dual-comparison-v2" --repeats 3 --capacity 64 --window 8 --passes 20000 --cpu <允许的CPU编号>
```

80项Python测试及C核心测试通过。尚未验收真实Wi-Fi拔插恢复、断电恢复、长期满负载和多个Linux节点调度。项目已能演示双设备接入、有界排队、优先级选择、FIFO对照、数值验算及只读解释，所有设备样本均为明确标注的synthetic数据。
