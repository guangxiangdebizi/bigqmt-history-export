---
name: bigqmt-history-export
description: 通过大QMT XtItClient进程内原生函数和本地Formula RPC无GUI导出历史行情，支持股票/ETF 1分钟数据、权限探测、可恢复批量下载、Parquet合并、文件桥、校验和发布。用户要求从大QMT拉历史数据、替换Tushare分钟接口、逆向QMT本地接口、排查历史权限、构建或修复QMT文件桥时使用。
---

# BigQMT History Export

将实时运行状态作为最高优先级证据。先确认 QMT 进程、模块、RPC 端口和账号数据权限，再执行下载；禁止用 GUI 点击作为批量数据通道，禁止把原生函数返回 `1` 等同于已经取得行情。

## 选择链路

优先使用外部原生/RPC链路：

1. `scripts/qmt_native_download.py` 在 `XtItClient.exe` 中动态定位 `download_history_data` 并触发下载。
2. `scripts/qmt_rpc_call.py` 通过本机 Formula RPC 读取 `getMarketData`。
3. `scripts/download_qmt_1min.py` 组合两者完成批量、断点、Parquet和公开快照覆盖。

满足下列任一条件时改用 `scripts/bridge.py`：

- 外部进程没有读取/写入 QMT 进程内存的权限。
- QMT 版本使原生签名定位失效，但内置 Python 的 `download_history_data` 与 `C.get_market_data_ex` 可用。
- 用户明确要求文件系统命令/回执模式。

不要同时用两条链路处理同一标的和日期范围。架构选择与信任边界见 [references/architecture.md](references/architecture.md)。

## 外部环境

- 要求 Windows x64、大QMT已登录、`XtItClient.exe` 正常运行。
- 外部脚本使用 Python 3.10+，安装 `pandas`、`numpy`、`pyarrow`、`pymongo`。
- 脚本和 QMT 使用相同权限级别；不要先以管理员运行一边、普通用户运行另一边。
- 内置文件桥保持 Python 3.6 语法、`#coding:gbk`、ASCII源文本，不导入外部第三方包，不启动线程，不阻塞 QMT 共享策略线程。

## 阶段一：无副作用预检

先运行只定位、不下载的检查：

```powershell
$py = ".\.venv\Scripts\python.exe"
& $py scripts\check_environment.py
& $py scripts\qmt_native_download.py --locate-only
```

必须确认：

- 只发现一个 `XtItClient.exe`，或显式传入正确 `--pid`。
- 目标进程加载了 `FormulaLib.dll`。
- 动态扫描得到唯一 `helper_rva`。
- Formula RPC 端口可连接；默认 `127.0.0.1:58600`，但应以本机 `formulaserver.ini` 为准。

进程重启或 QMT 升级后重新定位。不要复用旧 PID、模块基址、函数绝对地址或旧进程的 RVA 假设。原生定位细节见 [references/native-helper.md](references/native-helper.md)。

## 阶段二：证明一条最小端到端链路

选择一个流动性正常的标的和一个很短、确定处于权限范围内的日期：

```powershell
& $py scripts\qmt_native_download.py 600000.SH `
  --period 1m --start 20260701 --end 20260710

& $py scripts\qmt_rpc_call.py getMarketData --params `
  '{"fields":["time","open","high","low","close","volume","amount"],"stockCodes":["600000.SH"],"startTime":"20260701","endTime":"20260710","period":"1m","dividendType":"none","count":-1}'
```

把链路判为成功前同时证明：

1. 原生调用返回 `result=1`。
2. RPC返回 `status=0`。
3. `params.result[0]` 等于请求代码。
4. `params.result[1]` 能解析成时间戳和字段/值向量。
5. 行数大于零、时间范围符合请求、OHLC合理、成交量非负。

Formula RPC帧、flags、BSON和结果布局见 [references/protocol.md](references/protocol.md)。

## 阶段三：探测权限边界

对同一标的逐步向前移动起始日期，每次只改日期。同步查看 QMT 日志中的“最大起始时间”、产品类型和权限提示。

将以下情况分开记录：

- 原生返回 `0`：请求未被助手接受。
- 原生返回 `1`、RPC为空：常见于权限下限、标的无数据或尚未落盘。
- RPC状态非零：协议、处理函数或参数错误。
- 起始日被截断：账号/数据产品限制，不是 Python 参数问题。

不得宣称本地逆向能绕过服务端行情授权。一个环境观察到的日期下限只是该环境的证据，不是工具的固定能力。权限验证矩阵见 [references/permissions-troubleshooting.md](references/permissions-troubleshooting.md)。

## 阶段四：批量导出

universe CSV 最少包含 `ts_code`。推荐字段：

```text
ts_code,name,list_status,list_date,delist_date,exchange,symbol
600000.SH,浦发银行,L,19991110,,SH,600000
```

先用 `--limit 2` 和单周范围做批量烟雾测试，再扩到完整 universe：

```powershell
& $py scripts\download_qmt_1min.py --asset stock `
  --universe-csv .\universe_stocks.csv --statuses L `
  --start-date 20250701 --end-date 20260701 `
  --out .\data\raw\qmt `
  --publication-dir .\data\public\china_a_share_1m_ohlcv `
  --concurrency 4 --retries 2 --progress-every 25
```

批量约束：

- 保持固定 `start/end` 以便回执命中断点；重启续跑时不要无意改变结束时间。
- 默认只跳过同时存在、行数一致且 `status=complete` 的 Parquet与回执。
- 先写临时文件再 `os.replace`；Windows共享冲突使用退避重试。
- 并发从 2 或 4 开始，确认 QMT 稳定后再提高；不要超过脚本允许的 16。
- 用 `--num-shards/--shard-index` 做多进程分片时，确保所有分片使用同一固定范围且写不同标的。
- 当天尚未收盘时，把结束时间和“日内快照”写入元数据。

ETF先运行 `scripts/build_etf_universe.py`，保留上市/退市元数据，再以 `--asset etf` 导出。不要把基金联接、场外基金或股票代码误归类为ETF。

目录、schema、单位换算、断点语义和快照覆盖规则见 [references/batch-export.md](references/batch-export.md)。

## 阶段五：验证和发布

批次结束后运行：

```powershell
& $py scripts\verify_qmt_1min.py --asset stock --out .\data\raw\qmt
```

只在 `status=pass`、`failed=0`、清单标的数与预期一致时继续。随后按需要：

- 用 `scripts/build_1min_dataset.py` 构建中性快照和覆盖报告。
- 用 `scripts/install_qmt_raw_compat.py` 创建旧目录硬链接；跨卷时才复制。
- 检查公开目录不存在账号、token、绝对内存地址、QMT安装路径、日志或原始命令回执。
- 发布后从远端重新读取 README、summary和若干 Parquet 元数据，不以本地命令退出码代替远端验证。

## 文件桥流程

将 `scripts/bridge.py` 作为大QMT内置 Python 策略部署。外部程序：

1. 先把命令写到同目录临时文件，刷新并关闭后原子重命名为 `cmd/<id>.json`。
2. 读取 `heartbeat.json` 和 `state/ready.json` 确认桥接存活。
3. 等待 `done/<id>.json`，再读取其中的 `result.output` CSV。
4. 根据 `ok`、`rows`、`empty`、首末时间和尝试次数判定结果。

桥接策略一次只处理一个历史请求，避免阻塞所有 QMT 内置策略。命令格式、状态机和部署约束见 [references/file-bridge.md](references/file-bridge.md)。

## 不变量

- 只操作用户自己的本地 QMT 进程与账号有权访问的数据。
- 不通过 GUI 自动化完成数据通道。
- 不写死绝对内存地址，不跨进程复用地址。
- 不把“请求接受”当作“数据可用”。
- 不把空响应写成完成数据。
- 不覆盖早期历史，除非新数据在同一时间戳上通过验证并明确具有更高来源优先级。
- QMT股票/ETF成交量通常是“手”；写入本Skill的公开 schema 时乘100变为“股”。在换用其他市场或品种前重新验证单位。
- 保留原始分片、完成回执、批次清单、验证报告和来源边界，确保可重放。

## 资源索引

- [references/architecture.md](references/architecture.md)：链路选择、组件和信任边界。
- [references/protocol.md](references/protocol.md)：Formula RPC帧、BSON、flags和响应解析。
- [references/native-helper.md](references/native-helper.md)：Boost.Python字符串引用、MSVC x64 ABI和远程调用。
- [references/file-bridge.md](references/file-bridge.md)：文件桥命令、回执、心跳和部署。
- [references/batch-export.md](references/batch-export.md)：批量、schema、目录、恢复、验证和发布。
- [references/permissions-troubleshooting.md](references/permissions-troubleshooting.md)：权限探测、错误分类和恢复顺序。
