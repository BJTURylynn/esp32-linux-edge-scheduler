# 粉色板 USB 接入与调度验证

本目录保存 ATmega328P ColorEasyDuino（非ESP32、非GD32）实物接入证据。操作日期及源码哈希见 [manifest](manifest.json)，完整运行步骤见 [接入说明](../../coloreasyduino.md)。旧ESP32/VMware/WSL证据保留原样。

## 实物验证

- Uno 固件真实编译并通过 CH340 COM10 上传：Flash2660B、全局SRAM201B；[编译日志](uno-compile.log)、[上传日志](uno-upload.log)。
- COM10 → Windows Python串口桥接 → Ubuntu C服务，捕获并转发20帧；16帧普通（256样本）、4帧紧急（16样本），Linux完成20、拒绝0。
- 每帧校验样本生成公式，并用独立Python参考核对mean/RMS，20帧全部匹配。[逐帧数值核对](hardware-summary.json)、[Linux CSV](linux-service/tasks.csv)、[最终状态](linux-service/status.json)。
- [桥接统计](serial-bridge/stats.json) 显示转发20、invalid0、oversized0、dropped0、uncertain0；[捕获帧](serial-bridge/frames.jsonl) 是验证并重新编码后的成功sendall数据，不是原始串口抓包。
- 本轮未观察到传输错误，不代表协议保证无损。没有应用层ACK、去重、持久化或设备反压；串口重开会复位Uno，序号可能重复。

## 自动验证

[普通测试](linux-tests.log) 与 [ASan/UBSan测试](linux-sanitize-tests.log) 均为59项通过、0跳过；另有C核心与可移植固件编码器测试。[Sanitizer构建](linux-sanitize-build.log)。新增桥接测试覆盖有界帧、分包/粘包、CRLF、错误消息、断开残帧隔离、TCP失败不重放、运行预算/退出码及转发到真实C服务的数值一致性。

## 两种调度策略的公平回放

为重复相同输入，将实物捕获的20帧编成 [回放输入](captured-replay.jsonl)，每帧合成到达间隔2ms；FIFO/priority各三次、相同CPU绑定、容量64、window8、passes20000。每种策略完成60个任务且拒绝0。配置、哈希和逐次CSV保存在 comparison/run-*。

| 类型 | FIFO等待P95(ms) | priority等待P95(ms) |
| --- | ---: | ---: |
| urgent | 85.274 | 7.219 |
| normal | 74.158 | 73.549 |

[报告](comparison/report.md)、[结构化报告](comparison/report.json)、[PNG](comparison/comparison.png)、[SVG](comparison/comparison.svg)。本次普通任务的合并P95没有上升；不能为迎合预期改写实测结论，也不能据此保证任何负载都提高性能。

这些是受控回放的Linux排队指标，**不等于真实USB到计算完成的端到端延迟**。原生Linux文件系统中完成对照；初次在Windows挂载目录实验遇到状态文件读取OSError61后未纳入本证据。`total_ms`从Linux收到完整帧起算，不含UART及桥接时间。

[离线Agent输出](agent-offline.json) 使用真实最终状态及新报告，model_calls=0，未访问付费模型。状态属于已结束服务快照；验证后服务与桥接均已关闭，板子仍持续输出合成数据。

## 验收边界

已完成物理USB串口传输、C计算及离线解释；没有实测USB拔插恢复或断电重启，没有传感器测量或硬实时保证。13口1000ms灯光为心跳，不能证明任务入队或完成。此前独立BlinkSerial测试已被本项目固件替换。
