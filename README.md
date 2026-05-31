# 选股策略回测系统

基于 [thsdk](https://pypi.org/project/thsdk/)（同花顺数据接口）的选股策略回测工具，BS 架构（浏览器 + 服务端）。

- 用 `wencai_nlp`（问财自然语言）按策略选股，支持历史日期回测
- 以**上证指数 K 线**作为交易日历，天然排除周末/节假日/临时休市，自动推算 T-1/T-2/T-3 与 T+1/T+2/T+3
- 结果表展示选股日股价及后 1/2/3 个交易日的累计涨跌幅（涨红跌绿）
- 复刻通达信「拉升资金」指标，对**连续 3 天拉升资金增加**的股票标注红色「主」徽标（悬停显示提示）
- 流式回测带**进度条**，并可展开查看每日的 wencai 请求与原始返回，便于排查

## 技术栈

- Python ≥ 3.10，依赖管理用 [uv](https://docs.astral.sh/uv/)
- 后端：FastAPI + Uvicorn
- 数据：thsdk、pandas

## 文件结构

| 文件 | 说明 |
| --- | --- |
| `app.py` | FastAPI 后端：REST/流式接口，托管前端 |
| `core.py` | 回测核心：THS 连接、交易日历、选股、行情、拉升资金指标 |
| `config.py` | 账户配置加载（config.json + 环境变量） |
| `static/index.html` | 前端单页：表单、进度条、结果表、调试面板 |
| `config.example.json` | 账户配置模板（复制为 `config.json` 使用） |
| `pyproject.toml` / `uv.lock` | 依赖声明与锁定 |

---

## 在 Windows 计算机上运行

### 一、需要拷贝的文件

整个项目目录，但**排除**以下内容（不可移植或含敏感信息）：

- `.venv/` —— 虚拟环境含平台相关二进制，必须在目标机重建
- `config.json` —— 含账户凭证，到新机器重新配置
- `__pycache__/`

需保留：`app.py`、`core.py`、`config.py`、`static/`、`pyproject.toml`、`uv.lock`、`config.example.json`、`.python-version`。

### 二、安装 uv

无需单独安装 Python，uv 会按 `.python-version` 自动下载 3.10。在 PowerShell 执行：

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

安装后**重开一个终端**使 PATH 生效，验证：

```powershell
uv --version
```

### 三、安装依赖

进入项目目录，按 `uv.lock` 还原环境（自动下载 Python 与全部依赖）：

```powershell
cd 项目路径
uv sync
```

> `uvicorn[standard]` 中的 `uvloop` 仅在 Linux/Mac 安装，Windows 自动跳过并回退到 asyncio，不影响运行。thsdk 为纯 Python 包，无需编译器。

### 四、配置账户（可选）

不配置则使用临时游客账户（可测试，但可能随时失效）。使用自己的同花顺账户：

```powershell
copy config.example.json config.json
```

编辑 `config.json` 填入 `username` / `password`（`mac` 可留空）。也可用环境变量临时覆盖：

```powershell
$env:THS_USERNAME="你的账号"; $env:THS_PASSWORD="你的密码"
```

### 五、启动

```powershell
uv run uvicorn app:app --host 0.0.0.0 --port 8000
```

浏览器打开 **http://localhost:8000**，填写回测日期区间后点「开始回测」。

### 注意事项

- **网络**：thsdk 需连接同花顺行情服务器，确保能联外网；首次运行若 Windows 防火墙弹窗，允许 Python 通过。
- **访问范围**：`--host 127.0.0.1` 仅本机访问；`--host 0.0.0.0` 则同局域网设备可经 `http://本机IP:8000` 访问。
- **端口冲突**：8000 被占用时改用 `--port 8001`。
- **离线内网机器**：目标机无法联公网安装依赖时，可在有网机器 `uv export > requirements.txt` 或 `uv pip download` 预下载后拷入内网；但 thsdk 运行时仍需能连同花顺服务器。

---

## 策略说明

默认策略模板在 `core.py` 的 `DEFAULT_STRATEGY_TEMPLATE`，前端可临时编辑覆盖。日期占位符：

- `{T}` —— 选股日
- `{T1}` / `{T2}` / `{T3}` —— T 往前推的第 1 / 2 / 3 个交易日

运行时占位符替换为「YYYY年M月D日」交给 wencai 识别。
