use std::env;
use std::str::FromStr;
use std::time::Duration;

use anyhow::Result;
use solana_sdk::{pubkey::Pubkey, signer::Signer};
use tracing::warn;

use crate::agent::market::analyze_mint;
use crate::common::utils::{create_rpc_client, import_wallet};
use crate::core::balance::fetch_token_balance;

use super::context::AgentContext;
use super::events::TradeAction;
use super::runner::execute_action;

pub async fn execute_with_retry(
    ctx: &AgentContext,
    action: TradeAction,
    notify_label: Option<&str>,
) -> Result<()> {
    let max_retries = env::var("AGENT_MAX_RETRIES")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(3);
    let mut backoff_ms = 500u64;
    let action = plan_action(ctx, action).await?;

    if matches!(action, TradeAction::Skip { .. }) {
        return execute_action(ctx, action, notify_label).await;
    }

    for attempt in 0..max_retries {
        match execute_action(ctx, action.clone(), notify_label).await {
            Ok(()) => {
                if let TradeAction::MirrorBuy { ref mint, .. } = action {
                    if verify_buy(ctx, mint).await? {
                        return Ok(());
                    }
                    warn!(
                        "buy verify failed {} attempt {}/{}",
                        mint,
                        attempt + 1,
                        max_retries
                    );
                } else {
                    return Ok(());
                }
            }
            Err(err) => {
                if attempt + 1 >= max_retries {
                    return Err(err);
                }
                warn!(
                    "execute attempt {}/{} failed: {}",
                    attempt + 1,
                    max_retries,
                    err
                );
            }
        }
        tokio::time::sleep(Duration::from_millis(backoff_ms)).await;
        backoff_ms = (backoff_ms * 2).min(4_000);
    }

    Err(anyhow::anyhow!("plan exhausted retries"))
}

async fn plan_action(ctx: &AgentContext, action: TradeAction) -> Result<TradeAction> {
    match &action {
        TradeAction::MirrorBuy { mint, .. } => {
            let intel = analyze_mint(ctx, mint, None, None).await;
            if intel.risk_flags.iter().any(|f| {
                matches!(
                    f.as_str(),
                    "high_holder_concentration" | "no_swap_venue" | "low_liquidity"
                )
            }) {
                return Ok(TradeAction::Skip {
                    reason: format!("plan blocked: {}", intel.risk_flags.join(", ")),
                });
            }
            if let Some(pct) = intel.top10_holder_pct {
                if pct >= 85.0 {
                    return Ok(TradeAction::Skip {
                        reason: format!("plan blocked: top10 holders {pct:.0}%"),
                    });
                }
            }
            Ok(action)
        }
        _ => Ok(action),
    }
}

async fn verify_buy(ctx: &AgentContext, mint: &str) -> Result<bool> {
    if ctx.config.dry_run {
        return Ok(ctx.positions.get(mint).is_some());
    }
    if ctx.positions.get(mint).is_none() {
        return Ok(false);
    }
    let rpc = create_rpc_client()?;
    let wallet = import_wallet()?;
    let mint_pk = Pubkey::from_str(mint)?;
    let (balance_raw, _) = fetch_token_balance(&rpc, &wallet.pubkey(), &mint_pk)?;
    Ok(balance_raw > 0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::agent::AgentContext;
    use crate::engine::positions::PositionTracker;
    use crate::engine::strategy::TradeConfig;
    use std::sync::Arc;

    #[tokio::test]
    async fn verify_buy_checks_position() {
        let positions = Arc::new(PositionTracker::new(TradeConfig::default()));
        positions.on_buy("m", 0.1, 100, 6);
        let mut cfg = TradeConfig::default();
        cfg.dry_run = true;
        let ctx = AgentContext::new(positions, cfg);
        assert!(verify_buy(&ctx, "m").await.unwrap());
        assert!(!verify_buy(&ctx, "other").await.unwrap());
    }
}
