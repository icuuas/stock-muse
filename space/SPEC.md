# Space「持仓看板」说明

给 Muse 创建 Space 时参考。目标：用户随时打开，看到最新持仓、盈亏和异动，**不需要再问 Muse**。

## 基本要求
- 名称“持仓看板”，slug `stock-push-board`，**fullstack Space，私有，不分享**。
- 数据存 Space 自己的库里；**API key 不得出现在前端**。
- Space **读不了云电脑里的文件**，所以数据由你推进去（见下）。

## 数据流
```
cron 任务 → monitor.py / snapshot.py --space → outbox/space_snapshot.json
          → 你把其中的 snapshot、series 写进 Space（action: upsertSnapshot）
          → monitor.py ack space_snapshot.json
```
- 监测任务按 `prefs.space_refresh_minutes`（默认 10 分钟）产出一次，**只在开盘时段**。
- 用户变更持仓后，立刻运行 `snapshot.py --space` 并推送一次。
- 建议的 action：`upsertSnapshot`（整份快照）、`getportfolio`（读取）、`appendAlert`（可选：记录最近 20 条异动）。

## 写入的数据（outbox/space_snapshot.json）
```json
{
  "type": "space_snapshot", "needs_push": true, "created": "2026-11-10T21:42:00Z",
  "snapshot": {
    "generated": "...", "base_currency": "CNY",
    "totals": {"value": 0, "day_pnl": 0, "day_pct": 0, "pnl": 0, "pnl_pct": 0, "partial": false, "missing": []},
    "fx": {"pairs": {}, "to_base": {}, "time": "...", "source": "Yahoo", "ok": true, "stale": false},
    "markets": {"US": {"label": "美股", "session_label": "盘后", "latest_time": "...", "sources": ["nasdaq"], "status_labels": ["实时"]}},
    "positions": [{"symbol": "NVDA.US", "name": "英伟达", "market": "US", "currency": "USD", "shares": 100, "cost": 150,
                   "price": 182.35, "price_time": "...", "status_label": "实时", "day_change_pct": 7.2,
                   "value": 18235, "value_base": 131292, "pnl_pct": 21.6, "weight": 32.0,
                   "source": "nasdaq", "stale": false, "holiday_hint": false, "from_cache": false}],
    "notes": ["…"]
  },
  "series": {"NVDA.US": [["2026-11-10T21:40:00Z", 182.1]]}
}
```

## 页面
1. **顶部总览**：总金额、今日盈亏（金额和百分比）、累计浮盈亏、汇率与取数时间、各市场状态徽标（盘中/盘后/夜盘/已收盘/休市）。
2. **持仓表**：代码｜名称｜现价｜今日%｜数量｜成本｜市值｜浮盈%｜仓位｜来源与状态。按折算市值降序。
3. **异动榜**：最近 20 条提醒（可选）。
4. **走势小图**：用 `series` 里缓存的点自己画，没有 K 线接口。
5. **提示区**：显示 `snapshot.notes`（缺数据、休市、夜盘不可用、两源价差等）。

## 规则
- 涨跌颜色跟 `prefs.json` 的 `color_scheme`：`red_up` 红涨绿跌，`green_up` 绿涨红跌。
- 数据时间超过 20 分钟且该市场正在交易：该行置灰，写“数据过期”。
- `from_cache=true` 的行标“缓存”。`holiday_hint=true` 的行标“今日可能休市”。
- 夜盘价标“参考价”，**不得写成实时**。
- 页脚固定：“数据可能有延迟，仅供参考，不构成投资建议。”
- 持仓数量、成本属于敏感信息：页面不得有“分享”入口，不得对外公开。

## 校验
写入一条测试数据，刷新页面能读回；然后清掉测试数据。Space 从写入到页面显示的延迟，请在自测里记录并告诉用户。
