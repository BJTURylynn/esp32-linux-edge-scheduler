# 双设备调度实物证据

本目录保留ESP32-C3与Arduino Uno同时接入同一Linux C调度队列的原始验收记录。两板生成的都是synthetic合成数据，不是真实传感器采集。

- 真实数据路径：ESP32-C3 Wi-Fi/TCP与Uno USB串口 → Windows并发转发 → WSL2 Ubuntu C服务。
- 两设备各20任务：完成40、拒绝0；[数值验证](live-verification.json)逐项核对device_id+seq、样本与mean/RMS。
- [调度审计](live-schedule-audit.json)观察8次urgent优先取出、65对越过已有normal的关系，其中[10对跨设备](cross-device-priority-witnesses.json)。无确定优先级违规、同优先级保持FIFO。
- 实时用于制造计算竞争的参数：priority/capacity64/window8/passes1000000/worker1。每次pass执行真实计算，不用sleep模拟算力。
- [原始C记录](live/tasks.csv)、[最终状态](live/status.json)、[双设备捕获](live-capture/summary.json)均保留。

## 同一批设备数据的策略回放

将40帧按原实时接收task_id顺序排列，另设每帧2ms合成到达间隔。相同输入哈希、二进制哈希、CPU绑定、capacity64/window8/passes20000/worker1，每种策略各三次、次序交替。

| 等待P95（ms，合并三轮完成任务） | FIFO | priority |
|---|---:|---:|
| urgent | 170.845 | 8.117 |
| normal | 159.699 | 163.057 |

六轮240任务全部完成、0拒绝，数值参考与[六轮审计](comparison/audit-summary.json)全部通过。每轮CSV、metadata、调度审计和数值验证位于comparison/run-*。

[报告](comparison/report.md)、[JSON](comparison/report.json)、[图表PNG](comparison/comparison.png)、[图表SVG](comparison/comparison.svg)、[只读规则解释](comparison/agent-explanation.txt)。

这组回放的2ms不是真实无线到达时间；实时实物接入和回放的passes不同，不能跨两组直接比较速度。等待P95只反映Linux完整帧入队后的测量，不是端到端无线延迟，也不证明吞吐提升或硬实时。

[tests.log](tests.log)是该次验收的80项Python测试结果（另含C核心）；后续一键演示增量测试在CI及最新项目报告中独立记录，不改写历史测量。

不含Wi-Fi密码、校园网账号、API Key或SSH私钥。Windows TCP转发是明确的链路节点，没有把WSL NAT当作可直接从局域网访问的桥接VM。没有测试真实断线恢复/重启自动接入/长期稳定性。停止接收服务不代表两块板已经断电。
