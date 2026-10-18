use anyhow::{anyhow, Result};
use raydium_amm::math::U128;
use solana_client::rpc_client::RpcClient;
use solana_sdk::pubkey::Pubkey;
use spl_token::amount_to_ui_amount;
use std::str::FromStr;
use std::sync::Arc;
use tracing::debug;

use crate::dex::pump_fun::{self, BondingCurveAccount, PUMP_PROGRAM};
use crate::dex::raydium::{self, AMM_PROGRAM};

pub fn quote_pump_sell_lamports(curve: &BondingCurveAccount, token_amount: u64) -> u64 {
    if token_amount == 0 || curve.virtual_token_reserves == 0 {
        return 0;
    }
    U128::from(token_amount)
        .checked_mul(U128::from(curve.virtual_sol_reserves))
        .and_then(|v| v.checked_div(U128::from(curve.virtual_token_reserves)))
        .map(|v| v.as_u64())
        .unwrap_or(0)
}

pub async fn quote_pump_sell_sol(
    rpc: Arc<RpcClient>,
    mint: &str,
    token_amount_raw: u64,
) -> Result<f64> {
    let mint_pk = Pubkey::from_str(mint)?;
    let program_id = Pubkey::from_str(PUMP_PROGRAM)?;
    let (_, _, curve) = pump_fun::get_bonding_curve_account(rpc, &mint_pk, &program_id).await?;
    if curve.complete {
        return Err(anyhow!("pump curve complete"));
    }
    let lamports = quote_pump_sell_lamports(&curve, token_amount_raw);
    Ok(amount_to_ui_amount(lamports, spl_token::native_mint::DECIMALS))
}

pub async fn quote_raydium_sell_sol(
    rpc: Arc<RpcClient>,
    mint: &str,
    token_amount_raw: u64,
    slippage: u64,
) -> Result<f64> {
    if token_amount_raw == 0 {
        return Ok(0.0);
    }
    let (pool_id, pool_state) = raydium::get_pool_state_by_mint(rpc.clone(), mint).await?;
    let native_mint = spl_token::native_mint::ID;
    let amm_program = Pubkey::from_str(AMM_PROGRAM)?;
    let user_input_token = if pool_state.coin_vault_mint == native_mint {
        pool_state.pc_vault
    } else {
        pool_state.coin_vault
    };
    let slippage_bps = slippage * 100;
    let swap_info = amm_cli::calculate_swap_info(
        &rpc,
        amm_program,
        pool_id,
        user_input_token,
        token_amount_raw,
        slippage_bps,
        true,
    )?;
    Ok(amount_to_ui_amount(
        swap_info.other_amount_threshold,
        spl_token::native_mint::DECIMALS,
    ))
}

pub async fn get_position_value_sol(
    rpc: Arc<RpcClient>,
    mint: &str,
    token_amount_raw: u64,
    slippage: u64,
) -> Result<f64> {
    if token_amount_raw == 0 {
        return Ok(0.0);
    }
    match quote_pump_sell_sol(rpc.clone(), mint, token_amount_raw).await {
        Ok(v) => {
            debug!("pump quote {} -> {} SOL", mint, v);
            return Ok(v);
        }
        Err(err) => debug!("pump quote failed for {}: {}", mint, err),
    }
    quote_raydium_sell_sol(rpc, mint, token_amount_raw, slippage).await
}
