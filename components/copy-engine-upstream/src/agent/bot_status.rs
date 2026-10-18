use serde::Serialize;

use super::audit::PnlSummary;
use super::context::AgentContext;
use super::control::BotControl;
use super::market::{analyze_mint, MintMarketIntel};
use super::tools::get_wallet_sol_balance;

#[derive(Debug, Clone, Serialize)]
pub struct PositionStatusRow {
    pub mint: String,
    pub entry_sol: f64,
    pub peak_value_sol: f64,
    pub token_balance_raw: u64,
    pub estimated_value_sol: Option<f64>,
    pub unrealized_pnl_pct: Option<f64>,
    pub market: MintMarketIntel,
}

#[derive(Debug, Clone, Serialize)]
pub struct BotStatusReport {
    pub ok: bool,
    pub paused: bool,
    pub circuit_failures: u32,
    pub circuit_max: u32,
    pub target_pubkey: String,
    pub agent_mode: String,
    pub dry_run: bool,
    pub wallet_sol: f64,
    pub open_positions: usize,
    pub max_concurrent_trades: usize,
    pub positions: Vec<PositionStatusRow>,
    pub pnl: Option<PnlSummary>,
    pub recent_target_swaps: Option<serde_json::Value>,
}

pub async fn build_bot_status(
    ctx: &AgentContext,
    control: &BotControl,
    target_pubkey: &str,
) -> BotStatusReport {
    let wallet_sol = get_wallet_sol_balance().await.unwrap_or(0.0);
    let open = ctx.positions.list_open();
    let open_count = open.len();

    let mut positions = Vec::with_capacity(open_count);
    for pos in &open {
        let market = analyze_mint(
            ctx,
            &pos.mint,
            Some(pos.entry_sol),
            Some(pos.token_balance_raw),
        )
        .await;
        let estimated_value_sol = market.estimated_sell_sol;
        let unrealized_pnl_pct = market.price_vs_entry_pct;
        positions.push(PositionStatusRow {
            mint: pos.mint.clone(),
            entry_sol: pos.entry_sol,
            peak_value_sol: pos.peak_value_sol,
            token_balance_raw: pos.token_balance_raw,
            estimated_value_sol,
            unrealized_pnl_pct,
            market,
        });
    }

    let pnl = ctx.audit.as_ref().and_then(|a| a.pnl_summary().ok());
    let recent_target_swaps = ctx
        .audit
        .as_ref()
        .and_then(|a| a.recent_swaps_json(8).ok());

    BotStatusReport {
        ok: true,
        paused: control.is_paused(),
        circuit_failures: control.failure_count(),
        circuit_max: control.max_failures(),
        target_pubkey: target_pubkey.to_string(),
        agent_mode: format!("{:?}", ctx.config.agent_mode),
        dry_run: ctx.config.dry_run,
        wallet_sol,
        open_positions: open_count,
        max_concurrent_trades: ctx.config.max_concurrent_trades,
        positions,
        pnl,
        recent_target_swaps,
    }
}

pub fn format_status_text(report: &BotStatusReport) -> String {
    let pnl_line = report
        .pnl
        .as_ref()
        .map(|p| {
            format!(
                "realized_pnl={:.4} SOL | closed={} | win_rate={:.0}%",
                p.realized_pnl_sol, p.total_closed, p.win_rate_pct
            )
        })
        .unwrap_or_else(|| "pnl=n/a".into());

    format!(
        "Bot status\npaused={}\ncircuit={}/{}\ntarget={}\nagent={}\ndry_run={}\nwallet={:.4} SOL\nopen={}/{}\n{}",
        report.paused,
        report.circuit_failures,
        report.circuit_max,
        report.target_pubkey,
        report.agent_mode,
        report.dry_run,
        report.wallet_sol,
        report.open_positions,
        report.max_concurrent_trades,
        pnl_line,
    )
}

pub fn format_positions_text(report: &BotStatusReport) -> String {
    if report.positions.is_empty() {
        return "No open positions.".into();
    }
    let mut lines = vec![format!("Open positions ({})", report.positions.len())];
    for p in &report.positions {
        let val = p
            .estimated_value_sol
            .map(|v| format!("{v:.4}"))
            .unwrap_or_else(|| "?".into());
        let pnl = p
            .unrealized_pnl_pct
            .map(|v| format!("{v:+.1}%"))
            .unwrap_or_else(|| "?".into());
        let flags = if p.market.risk_flags.is_empty() {
            "ok".into()
        } else {
            p.market.risk_flags.join(",")
        };
        lines.push(format!(
            "• {} | entry={:.4} peak={:.4} est={} pnl={} | {} | flags={}",
            p.mint, p.entry_sol, p.peak_value_sol, val, pnl, p.market.venue, flags
        ));
    }
    lines.join("\n")
}

pub fn format_pnl_text(report: &BotStatusReport) -> String {
    let Some(pnl) = &report.pnl else {
        return "PnL data unavailable (no audit log).".into();
    };
    format!(
        "PnL summary\nrealized={:.4} SOL\nclosed_trades={}\nwins={} losses={}\nwin_rate={:.1}%\ndecisions={} executed={} skipped={}",
        pnl.realized_pnl_sol,
        pnl.total_closed,
        pnl.wins,
        pnl.losses,
        pnl.win_rate_pct,
        pnl.total_decisions,
        pnl.successful_executions,
        pnl.skipped,
    )
}

pub fn format_config_text(ctx: &AgentContext) -> String {
    format!(
        "Config\nbuy_cap={} SOL\nmax_trades={}\nslippage={}%\nSL={}% TP={}%\ntrailing={}%\ncopy_sells={}\nmin_target_buy={} SOL",
        ctx.config.buy_amount_sol,
        ctx.config.max_concurrent_trades,
        ctx.config.slippage,
        ctx.config.stop_loss_pct,
        ctx.config.take_profit_pct,
        ctx.config.trailing_stop_pct,
        ctx.config.copy_sells,
        ctx.config.min_target_buy_sol,
    )
}

pub async fn format_market_text(ctx: &AgentContext, mint: &str) -> String {
    let pos = ctx.positions.get(mint);
    let entry = pos.as_ref().map(|p| p.entry_sol);
    let bal = pos.as_ref().map(|p| p.token_balance_raw);
    let intel = analyze_mint(ctx, mint, entry, bal).await;
    let est = intel
        .estimated_sell_sol
        .map(|v| format!("{v:.4}"))
        .unwrap_or_else(|| "n/a".into());
    format!(
        "Market: {mint}\nvenue={}\nliquidity={:?} SOL\nsupply={:?}\ntop10_holders={:?}%\nest_sell={} SOL\nvs_entry={:?}%\nflags={}",
        intel.venue,
        intel.liquidity_sol,
        intel.token_supply_ui,
        intel.top10_holder_pct,
        est,
        intel.price_vs_entry_pct,
        if intel.risk_flags.is_empty() {
            "none".into()
        } else {
            intel.risk_flags.join(", ")
        }
    )
}
