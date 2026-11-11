# 定时任务清单

安装时（INSTALL.md 第 11 步）按这张表创建。**先 `cron.list` 查 id，存在就 `cron.update`，不存在才新建。**
所有任务都用各市场自己的时区；只为用户**有持仓的市场**创建。平台 cron 最短 1 分钟。

## 提醒速度 → 分钟字段
| 速度 | 盘中/盘前盘后 `{m}` | 夜盘 `{n}` |
|---|---|---|
| 实时 realtime | `*`（每分钟） | `*/5` |
| 均衡 balanced（默认） | `*/5` | `*/15` |
| 省额度 eco | `*/15` | `*/30` |

用户想改速度：`cron.update` 改这几个任务的分钟字段，并把 `data/prefs.json` 的 `monitor_mode` 改成对应值。

## 监测任务（脚本没事就输出 NOOP）
| id | 时区 | cron 表达式 | 命令 | 条件 |
|---|---|---|---|---|
| `stock-push-monitor-asia` | Asia/Shanghai | `{m} 9-16 * * 1-5` | `monitor.py run --market CN,HK` | 持有 A 股或港股（只持有其一就只写那一个市场） |
| `stock-push-monitor-us` | America/New_York | `{m} 4-19 * * 1-5` | `monitor.py run --market US` | 持有美股 |
| `stock-push-monitor-us-night-a` | America/New_York | `{n} 20-23 * * 0-4` | `monitor.py run --market US` | 持有美股，且**夜盘自测已通过** |
| `stock-push-monitor-us-night-b` | America/New_York | `{n} 0-3 * * 1-5` | `monitor.py run --market US` | 同上 |

说明：美股夜盘从周日 20:00 ET 起到周五 20:00 ET 止，所以夜盘任务分成两条（晚上 20–23 点周日到周四；凌晨 0–3 点周一到周五）。脚本自己再判断精确时段，窗口外立刻输出 `NOOP`。

## 报告任务（每个市场每天一次）
| id | 时区 | cron 表达式 | 命令 |
|---|---|---|---|
| `stock-push-report-us-pre` | America/New_York | `30 8 * * 1-5` | `report.py --market US --kind pre` |
| `stock-push-report-us-post` | America/New_York | `30 16 * * 1-5` | `report.py --market US --kind post` |
| `stock-push-report-cn-close` | Asia/Shanghai | `30 15 * * 1-5` | `report.py --market CN --kind close` |
| `stock-push-report-hk-close` | Asia/Hong_Kong | `30 16 * * 1-5` | `report.py --market HK --kind close` |
| `stock-push-report-all`（仅 `report_mode=merged`） | 用户指定，默认 Asia/Shanghai | 默认 `30 16 * * 1-5` | `report.py --market ALL --kind close` |

- 用户没选的日报不建；`merged` 模式只建 `stock-push-report-all`，其中美股部分显示的是上一次收盘或盘后数据。
- 休市日没有官方清单：脚本发现“当日没有成交数据”就自动输出 `NOOP`，不发空日报。

## 交给你的指令（写进每个任务的内容里）
脚本目录写成绝对路径：`~/workspace/stock-push/app/scripts/`。

**监测任务：**
```
运行：python3 ~/workspace/stock-push/app/scripts/monitor.py run --market <市场>
如果输出的第一行是 NOOP：什么都不要做、不要回复、立即结束。
如果是 ACTION：第二行 JSON 的 outbox 列出文件，位于 ~/workspace/stock-push/outbox/。逐个处理：
- type 为 alert / news / notice：读取 draft，按当前渠道排版后发给用户。数字、时间、来源不要改，不要加买卖建议。
  news 的每个 item 带 needs_summary=true，可以用自己的话补一句话概括（不要照抄原文），查不到的不要编。
- type 为 space_snapshot：把其中的 snapshot 和 series 通过 Space 的 action（upsertSnapshot）写入看板。
发送成功后运行：python3 ~/workspace/stock-push/app/scripts/monitor.py ack <文件名>
发送失败：保留文件、不要 ack，下一轮会自动再提醒你。
```

**报告任务：**
```
运行：python3 ~/workspace/stock-push/app/scripts/report.py --market <市场> --kind <pre|post|close>
如果输出的第一行是 NOOP：立即结束，不要回复。
如果是 ACTION：读取 outbox 里的文件，type 为 report，draft 是日报内容，按当前渠道排版后发给用户（数字不改，不加买卖建议）。
发送成功后运行：python3 ~/workspace/stock-push/app/scripts/monitor.py ack <文件名>
```

## 每个交易日大概唤醒多少次（只是算术，不是 token 数）
| 场景（都持有） | 实时 | 均衡 | 省额度 |
|---|---|---|---|
| A 股 + 港股（共用一个任务，9–16 点） | 480 | 96 | 32 |
| 美股盘前到盘后（4–19 点） | 960 | 192 | 64 |
| 美股夜盘（8 小时） | 96 | 32 | 16 |
| 合计（不含最多 4 个报告） | 约 1,540 | 约 320 | 约 110 |

只持有一个市场时按对应行计算。每次唤醒是否消耗额度、消耗多少，**取决于 Muse 对一次“只跑脚本、没事就结束”的任务怎么计费**，见 docs/COST.md。
