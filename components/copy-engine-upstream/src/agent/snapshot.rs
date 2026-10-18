use serde::Serialize;

use super::audit::PnlSummary;
use super::context::AgentContext;
use super::market::{analyze_mint, analyze_mints, MintMarketIntel};
use super::tools::get_wallet_sol_balance;

#[derive(Debug, Clone, Serialize)]
pub struct PositionView {
    pub mint: String,
    pub entry_sol: f64,
    pub peak_value_sol: f64,
    pub token_balance_raw: u64,
    pub decimals: u8,
    pub estimated_value_sol: Option<f64>,
    pub unrealized_pnl_pct: Option<f64>,
    pub market: MintMarketIntel,
}

#[derive(Debug, Clone, Serialize)]
pub struct MarketSnapshot {
    pub wallet_sol: f64,
    pub paused: bool,
    pub target_pubkey: Option<String>,
    pub open_positions: Vec<PositionView>,
    pub open_count: usize,
    pub max_concurrent_trades: usize,
    pub buy_cap_sol: f64,
    pub stop_loss_pct: f64,
    pub take_profit_pct: f64,
    pub trailing_stop_pct: f64,
    pub dry_run: bool,
    pub agent_mode: String,
    pub recent_target_swaps: Option<serde_json::Value>,
    pub pnl: Option<PnlSummary>,
    pub learning_hint: Option<String>,
    pub focus_mint_intel: Option<MintMarketIntel>,
}

pub async fn build_snapshot(
    ctx: &AgentContext,
    focus_mint: Option<&str>,
) -> anyhow::Result<MarketSnapshot> {
    let wallet_sol = get_wallet_sol_balance().await.unwrap_or(0.0);
    let open = ctx.positions.list_open();
    let mint_specs: Vec<(&str, Option<f64>, Option<u64>)> = open
        .iter()
        .map(|p| (p.mint.as_str(), Some(p.entry_sol), Some(p.token_balance_raw)))
        .collect();
    let market_rows = analyze_mints(ctx, &mint_specs).await;

    let open_positions: Vec<PositionView> = open
        .into_iter()
        .zip(market_rows.into_iter())
        .map(|(p, market)| {
            let estimated_value_sol = market.estimated_sell_sol;
            let unrealized_pnl_pct = market.price_vs_entry_pct;
            PositionView {
                mint: p.mint,
                entry_sol: p.entry_sol,
                peak_value_sol: p.peak_value_sol,
                token_balance_raw: p.token_balance_raw,
                decimals: p.decimals,
                estimated_value_sol,
                unrealized_pnl_pct,
                market,
            }
        })
        .collect();
    let open_count = open_positions.len();

    let recent_target_swaps = ctx
        .audit
        .as_ref()
        .and_then(|a| a.recent_swaps_json(8).ok());
    let pnl = ctx.audit.as_ref().and_then(|a| a.pnl_summary().ok());
    let learning_hint = pnl.as_ref().map(|p| {
        if p.total_closed >= 5 {
            format!(
                "closed={} win_rate={:.0}% realized_pnl={:.4} SOL",
                p.total_closed, p.win_rate_pct, p.realized_pnl_sol
            )
        } else {
            "insufficient closed trade history".into()
        }
    });

    let focus_mint_intel = if let Some(mint) = focus_mint {
        let pos = ctx.positions.get(mint);
        Some(
            analyze_mint(
                ctx,
                mint,
                pos.as_ref().map(|p| p.entry_sol),
                pos.as_ref().map(|p| p.token_balance_raw),
            )
            .await,
        )
    } else {
        None
    };

    Ok(MarketSnapshot {
        wallet_sol,
        paused: ctx.is_paused(),
        target_pubkey: ctx.target.clone(),
        open_positions,
        open_count,
        max_concurrent_trades: ctx.config.max_concurrent_trades,
        buy_cap_sol: ctx.config.buy_amount_sol,
        stop_loss_pct: ctx.config.stop_loss_pct,
        take_profit_pct: ctx.config.take_profit_pct,
        trailing_stop_pct: ctx.config.trailing_stop_pct,
        dry_run: ctx.config.dry_run,
        agent_mode: format!("{:?}", ctx.config.agent_mode),
        recent_target_swaps,
        pnl,
        learning_hint,
        focus_mint_intel,
    })
}
