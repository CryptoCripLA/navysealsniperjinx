use std::str::FromStr;
use std::sync::Arc;

use serde::Serialize;
use solana_client::rpc_client::RpcClient;
use solana_sdk::pubkey::Pubkey;
use spl_token::amount_to_ui_amount;

use crate::common::utils::create_rpc_client;
use crate::dex::pump_fun::{self, PUMP_PROGRAM};
use crate::dex::raydium;
use crate::engine::pricing::get_position_value_sol;

use super::context::AgentContext;

#[derive(Debug, Clone, Serialize)]
pub struct MintMarketIntel {
    pub mint: String,
    pub venue: String,
    pub liquidity_sol: Option<f64>,
    pub virtual_sol_reserves: Option<f64>,
    pub virtual_token_reserves: Option<f64>,
    pub curve_complete: Option<bool>,
    pub token_supply_ui: Option<f64>,
    pub estimated_sell_sol: Option<f64>,
    pub price_vs_entry_pct: Option<f64>,
    pub top10_holder_pct: Option<f64>,
    pub holder_count_sampled: Option<usize>,
    pub risk_flags: Vec<String>,
}

pub async fn analyze_mint(
    ctx: &AgentContext,
    mint: &str,
    entry_sol: Option<f64>,
    token_balance_raw: Option<u64>,
) -> MintMarketIntel {
    let mut intel = MintMarketIntel {
        mint: mint.to_string(),
        venue: "unknown".into(),
        liquidity_sol: None,
        virtual_sol_reserves: None,
        virtual_token_reserves: None,
        curve_complete: None,
        token_supply_ui: None,
        estimated_sell_sol: None,
        price_vs_entry_pct: None,
        top10_holder_pct: None,
        holder_count_sampled: None,
        risk_flags: Vec::new(),
    };

    let Ok(rpc) = create_rpc_client() else {
        intel.risk_flags.push("rpc_unavailable".into());
        return intel;
    };

    enrich_supply(&rpc, mint, &mut intel);
    enrich_holders(&rpc, mint, &mut intel);
    enrich_pump(&rpc, mint, &mut intel).await;
    if intel.venue == "unknown" {
        enrich_raydium(&rpc, mint, &mut intel).await;
    }

    if let Some(raw) = token_balance_raw {
        if let Ok(est) = get_position_value_sol(rpc.clone(), mint, raw, ctx.config.slippage).await {
            intel.estimated_sell_sol = Some(est);
            if let Some(entry) = entry_sol {
                if entry > 0.0 {
                    intel.price_vs_entry_pct = Some(((est - entry) / entry) * 100.0);
                }
            }
        }
    }

    apply_risk_heuristics(&mut intel);
    intel
}

fn enrich_holders(rpc: &RpcClient, mint: &str, intel: &mut MintMarketIntel) {
    let Ok(mint_pk) = Pubkey::from_str(mint) else {
        return;
    };
    let Ok(largest) = rpc.get_token_largest_accounts(&mint_pk) else {
        return;
    };
    let supply = intel.token_supply_ui.unwrap_or(0.0);
    if supply <= 0.0 {
        return;
    }
    let mut top10: f64 = 0.0;
    let mut count = 0usize;
    for acct in largest.into_iter().take(10) {
        if let Some(ui) = acct.amount.ui_amount {
            top10 += ui;
            count += 1;
        }
    }
    if count > 0 {
        intel.top10_holder_pct = Some((top10 / supply) * 100.0);
        intel.holder_count_sampled = Some(count);
    }
}

fn enrich_supply(rpc: &RpcClient, mint: &str, intel: &mut MintMarketIntel) {
    let Ok(mint_pk) = Pubkey::from_str(mint) else {
        intel.risk_flags.push("invalid_mint".into());
        return;
    };
    if let Ok(supply) = rpc.get_token_supply(&mint_pk) {
        intel.token_supply_ui = Some(supply.ui_amount.unwrap_or(0.0));
    }
}

async fn enrich_pump(rpc: &Arc<RpcClient>, mint: &str, intel: &mut MintMarketIntel) {
    let Ok(mint_pk) = Pubkey::from_str(mint) else {
        return;
    };
    let Ok(program_id) = Pubkey::from_str(PUMP_PROGRAM) else {
        return;
    };
    if let Ok((_, _, curve)) =
        pump_fun::get_bonding_curve_account(rpc.clone(), &mint_pk, &program_id).await
    {
        intel.venue = "pump.fun".into();
        intel.virtual_sol_reserves = Some(amount_to_ui_amount(
            curve.virtual_sol_reserves,
            spl_token::native_mint::DECIMALS,
        ));
        intel.virtual_token_reserves = Some(amount_to_ui_amount(
            curve.virtual_token_reserves,
            6,
        ));
        intel.liquidity_sol = Some(amount_to_ui_amount(
            curve.real_sol_reserves,
            spl_token::native_mint::DECIMALS,
        ));
        intel.curve_complete = Some(curve.complete);
        if curve.complete {
            intel.risk_flags.push("bonding_curve_complete".into());
        }
    }
}

async fn enrich_raydium(rpc: &Arc<RpcClient>, mint: &str, intel: &mut MintMarketIntel) {
    if let Ok((_pool_id, _pool)) = raydium::get_pool_state_by_mint(rpc.clone(), mint).await {
        intel.venue = "raydium".into();
        intel.risk_flags.push("raydium_pool_detected".into());
    }
}

fn apply_risk_heuristics(intel: &mut MintMarketIntel) {
    if intel.venue == "unknown" {
        intel.risk_flags.push("no_swap_venue".into());
    }
    if let Some(liq) = intel.liquidity_sol {
        if liq < 0.5 {
            intel.risk_flags.push("low_liquidity".into());
        }
    }
    if let Some(pct) = intel.top10_holder_pct {
        if pct >= 70.0 {
            intel.risk_flags.push("high_holder_concentration".into());
        }
    }
    if let Some(pct) = intel.price_vs_entry_pct {
        if pct <= -25.0 {
            intel.risk_flags.push("deep_drawdown".into());
        }
    }
    if let Some(supply) = intel.token_supply_ui {
        if supply > 0.0 && supply < 1_000_000.0 {
            intel.risk_flags.push("low_circulating_supply".into());
        }
    }
}

pub async fn analyze_mints(
    ctx: &AgentContext,
    mints: &[(&str, Option<f64>, Option<u64>)],
) -> Vec<MintMarketIntel> {
    let mut out = Vec::with_capacity(mints.len());
    for (mint, entry, bal) in mints {
        out.push(analyze_mint(ctx, mint, *entry, *bal).await);
    }
    out
}
