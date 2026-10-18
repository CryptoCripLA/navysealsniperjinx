# 数据集卡片

## 名称

Pump.fun Market Lab - 2026-07-31 北京时间完整日

## 摘要

这个数据集包含北京时间完整自然日的一天 Pump.fun 原始市场事件，覆盖两个市场：

- `pump_fun`：bonding-curve / 内盘事件
- `pump_amm`：PumpSwap / AMM / 外盘事件

它适合用于公开研究演示、数据工具样例和成本敏感的市场分析。

## 时间范围

- 北京时间：`2026-07-31 00:00:00` 到 `2026-07-31 23:59:59`
- UTC 时间：`2026-07-30 16:00:00` 到 `2026-07-31 15:59:59`

## 文件结构

按源数据风格进行小时级分区：

```text
dataset/date=YYYY-MM-DD/dataset_YYYY-MM-DD_HH.parquet
```

示例：

```text
pump_amm/date=2026-07-31/pump_amm_2026-07-31_08.parquet
```

## 字段

常见字段包括：

- `event_type`
- `token_mint`
- `slot_number`
- `signature`
- `timestamp`
- `action`
- `token_amount`
- `lamports_amount`
- `fee_lamports`
- `user_wallet`

不同市场还会包含 reserve 值、pool 标识、creator 字段和 LP 字段等。

每个 dataset 的精确字段列表请查看 `data_manifest/pumpfun_market_2026-07-31_manifest.csv`。

## 已知限制

- 这是一天公开样本，不是完整历史归档。
- 这是原始市场数据，不是清洗后的交易信号。
- 如果没有真实执行和成本建模，不应据此声称 live profitability。
- 完整性指每个市场预期的 24 个小时文件都存在，不代表每个上游源都捕获了所有可能的链上事件。

## 适合用途

- 构建读取器和 schema 检查。
- 对比 bonding-curve 与 AMM 活跃度。
- 原型验证成本敏感分析。
- 演示如何在 GitHub 上打包可复现的市场数据。

## 非目标

- 不做实盘交易。
- 不做私有钱包画像。
- 不分发 API key。
- 不提供投资建议。
