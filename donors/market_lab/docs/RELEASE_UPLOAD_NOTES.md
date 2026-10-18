# Release 上传说明

完整一天的原始数据已在本地准备到：

```text
release_assets/full_day_raw/
```

原始目录中包含 48 个 parquet 文件：

- 24 个小时级 `pump_fun` 文件
- 24 个小时级 `pump_amm` 文件

这个目录已被 Git 忽略，因为完整数据约 2.33 GiB，并且部分小时文件超过 GitHub 普通文件 100 MB 限制。

实际上传到 GitHub Release 的文件是两个 zip 包：

```text
release_assets/archives/pump_amm_2026-07-31_full_day_raw.zip
release_assets/archives/pump_fun_2026-07-31_full_day_raw.zip
```

推荐 GitHub 流程：

1. 推送仓库代码、文档、manifest 和样本文件。
2. 创建一个名为 `dataset-2026-07-31` 的 GitHub Release。
3. 按市场压缩为 zip 后上传到 release assets，避免 `.parquet` 直传时触发浏览器或站点附件类型限制。

建议 release 标题：

```text
Pump.fun Market Lab Dataset - 2026-07-31 北京时间完整日
```

建议 release 描述：

```text
北京时间完整一天的 Pump.fun 原始市场数据。

市场：
- pump_fun bonding-curve / 内盘事件
- pump_amm PumpSwap / AMM / 外盘事件

覆盖范围：
- 北京时间：2026-07-31 00:00:00 到 2026-07-31 23:59:59
- UTC：2026-07-30 16:00:00 到 2026-07-31 15:59:59

这是公开研究样本，不是交易信号服务。
```
