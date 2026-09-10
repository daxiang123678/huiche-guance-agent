在线浏览链接：https://2ae1ae61caf7489e8721924ff28e0bbb.app.workbuddy.link/
# 高位回撤观测智能体

面向研究与展示的加密资产观测仪表盘，当前只聚焦两项能力：

1. 主流资产距离历史最高价（ATH）的回撤排行；
2. 单个资产近一年日线成交量分布，以及下方支撑位、上方压力位识别。

## 本地运行

项目使用 Python 标准库，无需安装第三方包：

```bash
python app.py
```

打开 http://127.0.0.1:8787 。项目会优先使用 CoinGecko 公共接口；如果部署环境访问 CoinGecko 出现 TLS、网络或限流问题，会自动切换到 Hyperliquid 公开行情接口，避免线上页面因为单一数据源不可达而空白。

## 目录结构

```text
app.py                 HTTP 入口与 API 路由
core/market_data.py    CoinGecko 数据适配与缓存
core/signals.py        ATH 回撤、成交量分布、支撑压力算法
dashboard/index.html   仪表盘页面
```

## 海外部署

可将项目部署到 Render、Railway 等支持 Python Web 服务的平台。启动命令：

```bash
python app.py
```

生产环境请将 `app.py` 中的监听地址改为平台提供的端口配置，并设置 `PORT` 环境变量。CoinGecko 公共接口存在频率和历史范围限制，页面会展示真实返回结果或明确的限流提示。

> 分析结果仅用于研究，不构成投资建议。
