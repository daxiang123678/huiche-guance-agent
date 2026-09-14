在线链接：https://2ae1ae61caf7489e8721924ff28e0bbb.app.workbuddy.link/
# 高位回撤观测智能体

面向研究与展示的加密资产观测工具，当前聚焦两项能力：

1. **历史高点回撤观测** —— 主流资产距离历史最高价（ATH）的回撤排行；
2. **成交量分布与支撑压力识别** —— 单个资产近一年的成交量密集区，以及下方支撑位、上方压力位。

同一个作品有两种用法，**共用同一套自研规则引擎**，结论口径完全一致：

- **网页仪表盘**（`app.py` + `dashboard/index.html`）—— 给人看的；
- **技能 / 命令行**（`agent.py` + `skills/gaowei-huiche/SKILL.md`）—— 给 Agent 调用的。

## ⭐ 用到了币安官方的哪几项（逐项核对）

| # | 官方资源 | 用在什么地方 |
|---|---|---|
| 1 | 官方技能 **`binance`**（币安 Skills Hub） | 经官方 CLI 的 `binance-cli request GET <url>` 调官方公开入口取数 |
| 2 | 官方开源数据仓库 **`data.binance.vision`** | 月度日线归档 ZIP，用于历史行情回溯 |
| 3 | 官方公开行情接口 **`data-api.binance.vision`** | 全市场快照 + 月线（算 ATH）+ 日线（算成交分布） |

**算法层与结论层是本项目自研的**（`core/signals.py`）—— 回撤怎么算、成交量分布怎么分桶、
支撑压力位怎么定，这些是作品的价值所在，官方没有提供这类合成结论。

准确的说法是 **数据层用官方资源、算法层和结论层自研**，不说「基于官方 skill 开发」。

> 官方 Skills Hub 里另外 17 个技能**为什么没有接入**，以及每个的实测结论，
> 逐项写在 [AGENT.md](AGENT.md) 的第二节，不做挂名。

## 本地运行

项目只使用 Python 标准库，不需要安装任何第三方包：

```bash
python app.py
```

打开 <http://127.0.0.1:8787> 。

数据取数顺序是：**币安官方公开行情接口优先**，取不到才逐级降级到综合平台数据源。
页面右上角的「数据来源」和页脚会**如实显示这一次实际用的是哪一层**。

## 用法（命令行 / 技能）

四条数据通道：

```bash
# 1. 默认通道：官方公开行情接口，回撤榜前 20，并对回撤最深的 3 个币做成交分布分析
python agent.py

# 2. 官方技能 binance + 官方 CLI 通道，输出纯 JSON
python agent.py --skill --json

# 3. 官方开源数据仓库通道，回溯 24 个月
python agent.py --official --months 24 --top 12

# 4. 综合平台口径（非官方），仅用于对照
python agent.py --global --top 12

# 5. 只看指定币种，并对回撤最深的 3 个做成交分布分析
python agent.py --symbols BTC,ETH,SOL --depth 3
```

也支持**直接说一句自然语言**，会自动选择对应的通道（只在没显式给通道参数时才生效）：

```bash
python agent.py "看看哪些币回撤得最狠"
python agent.py "用官方开源数据看看 BTC 距最高还有多远"
```

`--json` 时 stdout 是纯 JSON，所有进度与说明都走 stderr，方便被其他程序直接消费。

安装为技能：

```bash
npx skills add https://github.com/daxiang123678/huiche-guance-agent
```

## 目录结构

```text
app.py                        HTTP 入口与接口路由
agent.py                      命令行入口（四通道）
core/market_data.py           行情取数层：币安官方优先，综合平台兜底
core/signals.py               自研引擎：回撤、成交量分布、支撑压力
dashboard/index.html          仪表盘页面
skills/gaowei-huiche/SKILL.md 技能定义
AGENT.md                      给评委的速览（含官方资源逐项核对）
项目说明.md                   面向评委的完整项目介绍与操作指南
```

## 文档索引

**随本仓库发布（在仓库里都能搜到）：**

| 文件 | 说明 |
|---|---|
| `README.md` | 本文件 |
| `AGENT.md` | 给评委的速览 + 官方资源逐项核对 + 诚实边界 |
| `项目说明.md` | 完整项目介绍、操作指南、数据来源说明 |
| `skills/gaowei-huiche/SKILL.md` | 技能定义 |

**作者本地自用材料（不随仓库发布，所以在仓库里搜不到，属正常）：**

| 文件 | 说明 |
|---|---|
| `上传到GitHub-操作步骤.md` | 作者自己的上传操作手册 |
| `参赛回复-评委两问.md` | 答辩用的预备回答 |

## 海外部署

可将项目部署到支持 Python Web 服务的平台，启动命令：

```bash
python app.py
```

生产环境请用平台提供的端口配置，并设置 `PORT` 环境变量。

> 分析结果仅用于研究，不构成投资建议。
