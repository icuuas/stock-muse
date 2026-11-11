# 数据源：实测结论与未验证项

> 实测时间：**2026-11-10**，由 Muse 在自己的云电脑里完成。结论只代表那一天；这些都是免费或非官方接口，可能随时变化。
> 安装时 `scripts/selftest.py` 会重新实测，并据此开启或关闭功能。

## 已实测可用
| 数据 | 来源 | 备注 |
|---|---|---|
| 美股盘中价 | Finnhub `/quote`（免费 key） | 字段 `c,d,dp,t,pc`；限 60 次/分钟；**没有盘前盘后价**；免费档没有 K 线 |
| 美股盘前盘后价 | Nasdaq `/api/quote/{sym}/info` | 字段 `lastSalePrice, lastTradeTimestamp, isRealTime` |
| 日线、分时、前收盘 | Yahoo chart（`includePrePost=true`） | 前收盘用日线（早于当前交易日的最近一根），不用 meta 的 previousClose |
| 港股、A 股行情 | 新浪 `hq.sinajs.cn`（必须带 `Referer: finance.sina.com.cn`，GBK 解码，多只代码可一次请求）+ Yahoo（`600519.SS`、`000001.SZ`、`0700.HK`） | 实测两者现价、最高最低、昨收完全一致；新浪时间戳观察到 15:34、16:30、16:08 等，按原样展示 |
| 美股新闻 | Finnhub `company-news` | 一天 222 条里标题含 Nvidia 的只有 128 条，**必须先关键词过滤** |
| 美股公告 | SEC EDGAR `data.sec.gov/submissions/CIK….json` | 必须带正常的 User-Agent；只有日期，没有时分 |
| A 股公告 | 东方财富公告 API | 只有日期；24 小时内可能 0 条 |
| 汇率 | Yahoo `CNY=X` | 与新浪在岸价有差异（6.6622 对 6.6724），所以**汇率只用 Yahoo，不混用** |
| 安装方式 | GitHub clone / zip / raw、pip、运行下载的脚本 | 三种下载都通，没有弹审批；本项目不需要 pip |

## 实测不可用，已禁用
- 腾讯行情 `qt.gtimg.cn`：超时。
- 东方财富行情 `push2`：302 转到 `push2delay`，偶发 502，不稳定。
- 巨潮资讯查询接口：GET 返回 500，POST 超时。
- AKShare：未安装，底层也是上述源。
- 上交所、港交所的休市日历页：404，没有可用的休市日清单（脚本用“当日有无成交数据”反推）。
- 港交所披露易：JSF 表单，curl 的 GET 和 POST 都只返回搜索页，拿不到公告。
- A50 指数期货：没有可用源，本项目不提供。

## 没有验证过（功能默认关闭，自测通过才开启）
1. **美股夜盘**（美东 20:00–04:00）：测试时夜盘还没开始，12 个候选源里没有一个被证明能拿到夜盘数据。Robinhood 是唯一候选，且是未公开接口。
2. 港股、A 股**盘中是否实时**：测试时已收盘，只拿到收盘快照。
3. 港股公告（需浏览器流程）。
4. 港股、A 股新闻（Yahoo search news 是否可用）。
5. 汇率的更新频率；`HKD=X`、`HKDCNY=X` 两个汇率对。
6. 全新用户的初始环境（Python、curl、git）；私有仓库；恶意脚本的拦截策略。

## 代码里按假设编写、靠自测校验的字段布局
这些解析在没有联网的环境里只用模拟数据测过，**真实返回格式以自测为准**：
- 新浪美股 `gb_` 的盘后字段位置（第 21、24 项）和时间字段的时区（按北京时间解析）。
- Nasdaq `marketStatus` 的取值写法。
- Robinhood 夜盘字段（`last_extended_hours_trade_price`、`bid_price`、`ask_price`、`venue_last_non_reg_trade_time`、`updated_at`）。
- 东方财富公告的字段名（`art_code`、`title`、`notice_date`）。
- Yahoo search 的 `news` 字段结构。

如果自测里某一项失败，对应功能会自动关闭，并在消息里如实说明，不会用错误数据凑数。
