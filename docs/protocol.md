# 线协议与计算语义

TCP 字节流，每条 UTF-8 JSON 后追加一个 LF (`\n`)。协议内容限制为 ASCII，兼容 CRLF。字段顺序可变；必须恰好有五个字段，不允许重复/未知字段。

```json
{"device_id":"esp32-01","seq":1,"priority":"urgent","source":"synthetic","samples":[2,4,6,8]}
```

| 字段 | 约束 |
|---|---|
| device_id | 1–32 个 ASCII 字母/数字/下划线/连字符 |
| seq | 0 到 9007199254740991 的十进制整数字面量，无符号、无小数/指数 |
| priority | `normal` 或 `urgent` |
| source | 第一版只允许 `synthetic` |
| samples | 1–8192 个有限 JSON 数字，绝对值不超过 1000000 |

帧最大为 262144 字节（不含 LF）；超长后丢弃到下一 LF，一帧只记一次拒绝。断开时的残帧记 `incomplete_frame`。完整非法帧记 `invalid_message`，队列已满记 `queue_full`。错误不会污染下一帧。最多 16 个连接，各自具有有界接收缓冲区；额外连接直接关闭。没有 ACK，发送方不能根据 `send` 成功断言任务完成。

`task_id` 是一次服务运行中合法消息的本地递增标识。`device_id + seq` 用于追踪，但不用于去重；重启设备/重发都可能出现重复。进程退出后不恢复队列。固件不重传不确定状态的帧。

滑动平均是因果窗口：`y[i] = sum(x[max(0,i-window+1):i+1]) / min(window,i+1)`。输出 `rms = sqrt(mean(y²))` 与 `mean = mean(y)`。例：`[2,4,6,8]`、window=2，得到 `[2,3,5,7]`，mean=4.25、rms=sqrt(21.75)。Python 独立参考使用 `math.fsum`，测试容差 1e-8（合成输入）；较大幅度数据建议相对容差 1e-9、绝对容差 1e-8。

CSV 时间取 CLOCK_MONOTONIC，ns 列是本机单调时间，不是 Unix 时间戳，不能跨机器比较。`received_ns` 在完整帧交给解析器时记录；因此不包含开发板发送和网络传输时间。`wait_ms = started - enqueued`；`compute_ms = finished - started`；`total_ms = finished - received`，均以 ms 输出。状态文件写入、线程唤醒等开销会影响等待；完成时刻在结果计算完毕处记录，后续日志 I/O 不包含在 total 内。

队列容量限制等待任务，不含正在运行的一个任务。任务为固定最大样本缓冲，应用内存量级为 `(capacity + 2) × sizeof(EdgeTask) + 16 × (262144+1)`，另有线程栈、socket 内核缓冲和运行时开销。日志文件随运行时间增长，需要实验后归档；内存有界不意味着磁盘无限。
