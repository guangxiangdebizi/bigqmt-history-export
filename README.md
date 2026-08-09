# BigQMT History Export Skill

面向 Codex 的大QMT历史行情导出 Skill。它不依赖 GUI 点击，不启动网络服务，使用两条本地链路：

1. 在 `XtItClient.exe` 中动态定位并调用 QMT 已注册的 `download_history_data` 原生助手，触发客户端按自身权限下载历史数据。
2. 通过本机 Formula RPC（通常是 `127.0.0.1:58600`）读取缓存行情，批量转换为可恢复、可校验的 Parquet 数据集。

仓库同时包含一个可选的 QMT 内置 Python 文件桥，适合不能或不希望从外部进程调用原生助手的环境。

## 能力

- 无 GUI 定位 `XtItClient.exe` 和 `FormulaLib.dll`
- 按 ASLR 动态解析 `download_history_data`，不写死 PID、模块基址或绝对地址
- 调用 BSON-over-TCP Formula RPC 读取 `getMarketData`
- 导出股票和 ETF 的 1 分钟 OHLCV/成交额
- 支持并发、重试、分片、原子写入和完成回执
- 将新数据按时间戳覆盖合并到现有中性 Parquet 快照
- 生成覆盖率、缺口、缺失标的和验证报告
- 可选安装到旧的 `data/raw/tushare/1min/...` 目录以兼容既有项目
- 提供 QMT 内置 Python 3.6/GBK 文件桥

## 适用范围

- Windows x64
- 已登录并正常运行的大QMT客户端
- 外部批处理 Python 3.10+
- 目标进程与调用脚本权限级别一致

本工具不会提升行情权限。QMT 服务端或账号给出的历史起始时间仍然生效。某个环境实测可读到哪一天，不代表其他账号也有相同范围；每次运行必须先做权限探测。

## 安装 Skill

```powershell
git clone https://github.com/guangxiangdebizi/bigqmt-history-export.git
cd bigqmt-history-export
.\install.ps1
```

默认安装到 `$CODEX_HOME\skills\bigqmt-history-export`；未设置 `CODEX_HOME` 时安装到 `$HOME\.codex\skills\bigqmt-history-export`。

安装外部脚本依赖：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 先做无副作用自检

启动并登录大QMT后运行：

```powershell
$scripts = ".\skills\bigqmt-history-export\scripts"
$py = ".\.venv\Scripts\python.exe"

& $py "$scripts\check_environment.py"
& $py "$scripts\qmt_native_download.py" --locate-only
```

两条命令只检查进程、模块、原生助手签名和本地 RPC 端口，不触发行情下载。

## 单标的端到端验证

先选择一个确定存在、且日期位于当前账号权限范围内的标的：

```powershell
& $py "$scripts\qmt_native_download.py" 600000.SH `
  --period 1m --start 20260701 --end 20260710

& $py "$scripts\qmt_rpc_call.py" getMarketData --params `
  '{"fields":["time","open","high","low","close","volume","amount"],"stockCodes":["600000.SH"],"startTime":"20260701","endTime":"20260710","period":"1m","dividendType":"none","count":-1}'
```

原生返回 `result=1` 只表示请求被 QMT 接受。必须继续确认 RPC `status=0`、返回目标代码且 bar 数大于零。

## 批量导出

universe CSV 至少需要 `ts_code`，也可包含 `name`、`list_status`、`list_date`、`delist_date`：

```powershell
& $py "$scripts\download_qmt_1min.py" --asset stock `
  --universe-csv .\universe_stocks.csv --statuses L `
  --start-date 20250701 --end-date 20260701 `
  --out .\data\raw\qmt `
  --publication-dir .\data\public\china_a_share_1m_ohlcv `
  --concurrency 4 --retries 2
```

ETF 使用 `--asset etf`。先用 `build_etf_universe.py` 从本地基金元数据和已有 ETF 任务清单构造包含上市、退市状态的 universe。

完成后校验：

```powershell
& $py "$scripts\verify_qmt_1min.py" --asset stock --out .\data\raw\qmt
```

只有验证结果同时满足 `status=pass`、`failed=0`，才应发布快照。

## 数据布局

原始层：

```text
data/raw/qmt/
├── 1min/
│   ├── stock/600000_SH/
│   │   ├── part_qmt_<start>_<end>.parquet
│   │   └── _qmt_<start>_<end>_complete.json
│   └── etf/510300_SH/
└── meta/
    ├── qmt_progress.json
    ├── qmt_stock_symbols_latest.csv
    └── qmt_stock_validation.json
```

公开层每个标的一个文件：

```text
data/public/<snapshot>/data/stock_1m/SH/600000.parquet
data/public/<snapshot>/data/etf_1m/SH/510300.parquet
```

公开 schema：`symbol, exchange, timestamp, open, high, low, close, volume, turnover`。QMT 的股票/ETF成交量通常以“手”返回，本工具写入公开层时乘以 100 转成“股”。

## 文件桥

`skills/bigqmt-history-export/scripts/bridge.py` 用于大QMT内置 Python。它是 ASCII 源码并声明 `#coding:gbk`，不导入外部 pandas/requests，也不创建线程。外部程序通过 `D:\QMT_Bridge\cmd` 写入原子 JSON 命令，桥接策略把 CSV 写入 `out`，回执写入 `done`，心跳写入 `heartbeat.json`。

文件桥和外部原生/RPC链路是两个独立方案，不应在同一标的上同时发起重复下载。

## 详细文档

- [Skill 主流程](skills/bigqmt-history-export/SKILL.md)
- [架构与选择](skills/bigqmt-history-export/references/architecture.md)
- [Formula RPC 协议](skills/bigqmt-history-export/references/protocol.md)
- [原生助手定位与调用](skills/bigqmt-history-export/references/native-helper.md)
- [文件桥协议](skills/bigqmt-history-export/references/file-bridge.md)
- [批量导出、校验与发布](skills/bigqmt-history-export/references/batch-export.md)
- [权限探测与故障排查](skills/bigqmt-history-export/references/permissions-troubleshooting.md)

## 安全与公开边界

- 只对用户自己启动并登录的本地 QMT 进程运行。
- 不记录或公开账号、token、订单、资金信息、绝对内存地址和本机用户名路径。
- `helper_rva` 是模块内相对位置，进程重启或 QMT 升级后必须重新定位。
- 原始 QMT 行情的再发布权取决于用户与数据提供方的协议；代码许可证不授予数据再分发权。
- 不把空响应伪装成成功；权限不足、标的不存在和数据未落盘必须分别留证。

## 测试

离线测试不会连接 QMT，也不会下载行情：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## License

代码使用 MIT License。QMT/迅投相关名称和接口归其权利人所有，本仓库不包含客户端二进制、账号、行情数据或授权材料。
