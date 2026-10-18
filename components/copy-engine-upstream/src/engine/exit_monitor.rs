use std::collections::HashSet;
use std::str::FromStr;
use std::sync::Arc;
use std::time::Duration;

use solana_sdk::pubkey::Pubkey;
use tokio::sync::Mutex;
use tracing::{error, warn};

use crate::agent::{EventBus, TradeEvent};
use crate::common::utils::create_rpc_client;
use crate::engine::pricing::get_position_value_sol;
use crate::engine::strategy::TradeConfig;

pub async fn run_exit_monitor(
    bus: EventBus,
    config: TradeConfig,
    positions: Arc<crate::engine::positions::PositionTracker>,
    exiting: Arc<Mutex<HashSet<String>>>,
    mut shutdown: tokio::sync::watch::Receiver<bool>,
) {
    let poll_secs = config.position_poll_secs;
    tracing::info!("exit monitor started | poll={}s", poll_secs);

    loop {
        tokio::select! {
            changed = shutdown.changed() => {
                if changed.is_ok() && *shutdown.borrow() {
                    tracing::info!("exit monitor stopping");
                    break;
                }
            }
            _ = tokio::time::sleep(Duration::from_secs(poll_secs)) => {}
        }

        if *shutdown.borrow() {
            break;
        }

        let open = positions.list_open();
        if open.is_empty() {
            continue;
        }

        let rpc_client = match create_rpc_client() {
            Ok(c) => c,
            Err(err) => {
                error!("exit monitor rpc init failed: {}", err);
                continue;
            }
        };

        for pos in open {
            {
                let guard = exiting.lock().await;
                if guard.contains(&pos.mint) {
                    continue;
                }
            }

            let value = match get_position_value_sol(
                rpc_client.clone(),
                &pos.mint,
                pos.token_balance_raw,
                config.slippage,
            )
            .await
            {
                Ok(v) => v,
                Err(err) => {
                    warn!("price fetch failed {}: {}", pos.mint, err);
                    continue;
                }
            };

            let snapshot = positions.record_valuation(&pos.mint, value);
            let (entry_sol, peak_value_sol) = snapshot
                .map(|p| (p.entry_sol, p.peak_value_sol))
                .unwrap_or((pos.entry_sol, pos.peak_value_sol));

            exiting.lock().await.insert(pos.mint.clone());

            let event = if let Some(reason) = positions.should_exit(&pos.mint, value) {
                TradeEvent::ExitSignal {
                    mint: pos.mint.clone(),
                    reason,
                }
            } else {
                TradeEvent::PositionValued {
                    mint: pos.mint.clone(),
                    entry_sol,
                    peak_value_sol,
                    current_value_sol: value,
                }
            };

            if let Err(err) = bus.publish(event).await {
                error!("exit monitor publish failed {}: {}", pos.mint, err);
            }

            exiting.lock().await.remove(&pos.mint);
        }
    }
}

pub fn validate_mint(mint: &str) -> bool {
    Pubkey::from_str(mint).is_ok()
}
