# USAGE.md —— 安装之后，用户说什么，你做什么

> 给 Muse 看。脚本目录：`~/workspace/stock-push/app/scripts/`，数据目录：`~/workspace/stock-push/`。
> 所有脚本输出的数字、时间、来源**不要改**；**不给买卖建议**；查不到就如实说“暂未找到”。
> 渠道（App、微信、飞书、邮件）和排版由你按当前对话渠道自行判断。

| 用户说 | 你做什么 |
|---|---|
| “我的持仓”“总金额”“今天赚了多少” | 运行 `snapshot.py`，把输出（模板 A 的内容顺序）按渠道排版后发给用户。没有持仓就回“还没记录持仓，发我代码、股数、成本”。需要更多字段用 `snapshot.py --json` |
| “买了 100 股 NVDA 成本 180”“卖了 50 股 @190”“改成 200 股”“清仓” | 先把名称换成代码并确认，再运行 `holdings.py buy / sell / set / remove`，把输出里的 `message` 回显给用户。`warnings` 非空先问用户。变更后运行 `snapshot.py --space` 并把 `outbox/space_snapshot.json` 推到 Space，再 `monitor.py ack space_snapshot.json` |
| “撤销” | `holdings.py undo`（只能撤销最近一步；再撤销一次就是重做） |
| “NVDA 跌破 150 提醒我”“茅台涨到 1600 提醒我” | 读 `data/rules.json`，在 `per_symbol.<代码>` 里写入 `price_below` / `price_above`（保持 JSON 合法），回显确认 |
| “涨跌超 3% 就提醒我”“NVDA 单独设 8%” | 改 `rules.json` 的 `defaults.day_move_pct` 或 `per_symbol.<代码>.day_move_pct` |
| “夜盘怎么样” | 读 `state/selftest.json` 的 `features.us_night`：为 `false` 就如实说“夜盘数据源还在验证，暂时给不了可靠夜盘价”；为 `true` 运行 `quotes.py --symbols <代码>`，展示价格并注明“夜盘参考价，价差大、无成交时仅供参考” |
| “XX 为什么涨/跌” | 运行 `news.py check --mode digest --market <市场>`，用模板 B 的内容回答；没有结果就说“暂未找到明确消息”，不要猜 |
| “暂停推送”“今天别发了” | 在 `data/prefs.json` 写 `paused_until`（UTC 时间，如明早）；“恢复”就删掉它 |
| “只收收盘日报”“关掉夜盘”“别发新闻了” | 改 `data/prefs.json` 的 `enable`（snapshot / alerts / reports / night / news），并 `cron.update` 或删除相应任务 |
| “提醒太频繁/太慢/太费额度” | 改 `prefs.json` 的 `monitor_mode`（realtime / balanced / eco），并按 `cron/CRON.md` 更新各监测任务的分钟字段 |
| “换成美元/港币显示总金额” | 改 `prefs.json` 的 `base_currency`（CNY / USD / HKD） |
| “改成绿涨红跌” | 改 `prefs.json` 的 `color_scheme`（red_up / green_up） |
| “免打扰到早上 7 点” | 改 `prefs.json` 的 `quiet_hours`，如 `"23:00-07:00"`（用户时区） |
| “日报合并成一条” | 改 `prefs.json` 的 `report_mode` 为 `merged`，按 `cron/CRON.md` 调整报告任务 |
| “推送到微信/飞书/邮件” | 渠道不归本项目管：确认用户已接好该渠道，之后发送就用它 |
| “更新” | 按 `INSTALL.md` 第 14 节 |
| “卸载” | 按 `INSTALL.md` 第 15 节 |
| “检查一下是不是正常” | 运行 `selftest.py`，用大白话汇报通过、失败和待补测的项目 |

## 日常自动流程（定时任务触发，详见 cron/CRON.md）
1. 监测任务运行 `monitor.py`，第一行 `NOOP` 就结束；`ACTION` 就读 `outbox/` 里的文件发送，成功后 `monitor.py ack <文件名>`。
2. 报告任务运行 `report.py`，流程相同。
3. 发送失败就保留文件、不 ack，下一轮会再次提醒；行情长时间获取失败时，脚本会自己产出一条 `notice` 异常通知。

## 回复风格
- 先给结论和数字，再给来源和时间；每条都带“数据时间 · 来源 · 状态（实时/延迟/收盘）”。
- 夜盘、缓存、休市的数据必须在消息里说明，不要当成实时价。
- 日报和当天第一条提醒末尾带：“以上仅为信息整理，不构成投资建议。”（脚本草稿里已包含，不要删）。
