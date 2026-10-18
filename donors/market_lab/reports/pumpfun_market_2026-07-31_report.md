# Pump.fun Market Lab - 2026-07-31 数据报告

这份报告描述北京时间完整自然日的一天 Pump.fun 原始市场数据。

## 覆盖范围

- 北京日期：`2026-07-31 00:00:00` 到 `2026-07-31 23:59:59`
- 包含 dataset：`pump_fun` bonding-curve / 内盘市场，`pump_amm` PumpSwap / AMM / 外盘市场
- 每个 dataset 的小时文件数：`24`

## 汇总

| Dataset | 找到小时数 | 行数 | 大小 | 备注 |
|---|---:|---:|---:|---|
| `pump_fun` | 24/24 | 3,342,790 | 0.40 GiB | 完整 |
| `pump_amm` | 24/24 | 17,484,206 | 1.84 GiB | 完整 |

## 安全说明

- 这个仓库是公开研究样本，不是交易信号服务。
- 原始市场数据用于可复现分析和数据工具演示。
- 如果没有建模滑点、优先费、协议费、平台费、失败交易和基础设施成本，不要从样本推断实盘盈利能力。
- 不包含 API key、私有钱包或私有策略标签。

## 文件

- `data_manifest/pumpfun_market_2026-07-31_manifest.csv`：小时级文件清单。
- `reports/pumpfun_market_2026-07-31_report.md`：当前生成报告。
- `data_sample/`：用于快速本地测试的小型 parquet 样本。
- 完整一天的原始数据建议作为 GitHub Release assets 上传，因为它不适合放进普通 Git 历史。
