use crate::common::utils::AppState;
use tracing::warn;

use crate::dex::raydium;
use crate::engine::strategy::TradeConfig;
use crate::engine::swap::{pump_swap, raydium_swap};
use anyhow::Result;

pub async fn mirror_swap(
    state: AppState,
    mint: String,
    amount_in: f64,
    direction: &str,
    config: &TradeConfig,
) -> Result<Vec<String>> {
    let in_type = "qty";
    if let Ok(sigs) = pump_swap(
        state.clone(),
        amount_in,
        direction,
        in_type,
        config.slippage,
        config.use_jito,
        &mint,
    )
    .await
    {
        return Ok(sigs);
    }
    warn!("pump.fun swap failed for {mint}, trying raydium fallback");
    let (pool_id, pool_state) =
        raydium::get_pool_state_by_mint(state.rpc_client.clone(), &mint).await?;
    raydium_swap(
        state,
        amount_in,
        direction,
        in_type,
        config.slippage,
        config.use_jito,
        pool_id,
        pool_state,
    )
    .await
}
