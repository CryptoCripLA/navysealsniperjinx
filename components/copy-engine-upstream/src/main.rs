use copy_trading_bot::agent::{
    build_agent_context, shutdown::{spawn_shutdown_listener, Shutdown}, spawn_orchestrator,
    ApprovalService, AuditLog, BotControl, EventBus, TradeEvent,
};
use copy_trading_bot::common::constants::PUMP_PROGRAM;
use copy_trading_bot::common::utils::{require_env_var, require_https_url, require_wss_url, validate_startup_env};
use copy_trading_bot::engine::exit_monitor::run_exit_monitor;
use copy_trading_bot::engine::position_store::PositionStore;
use copy_trading_bot::engine::positions::PositionTracker;
use copy_trading_bot::engine::strategy::TradeConfig;
use copy_trading_bot::ray_parse::tx_parse::parse_swap_from_ws_message;
use copy_trading_bot::services::status_server::spawn_status_server;
use copy_trading_bot::services::telegram::TelegramNotifier;
use copy_trading_bot::services::telegram_bot::{spawn_telegram_bot, TelegramBotContext};
use dotenv::dotenv;
use futures_util::{SinkExt, StreamExt};
use solana_client::rpc_client::RpcClient;
use std::collections::HashSet;
use std::env;
use std::sync::Arc;
use tokio::sync::Mutex;
use tokio_tungstenite::{connect_async, tungstenite::Message as WsMessage};
use tracing::{error, info, warn};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    dotenv().ok();
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .init();

    validate_startup_env()?;

    let sol_address = require_env_var("SOL_PUBKEY")?;
    let rpc_https_url = require_https_url("RPC_ENDPOINT")?;
    let unwanted_key = require_env_var("JUP_PUBKEY")?;
    let target = require_env_var("TARGET_PUBKEY")?;
    let ws_url = require_wss_url("RPC_WEBSOCKET_ENDPOINT")?;

    let rpc_client = Arc::new(RpcClient::new(rpc_https_url));
    let config = TradeConfig::from_env();

    let positions = if let Some(db_path) = &config.positions_db_path {
        let store = Arc::new(PositionStore::open(db_path)?);
        let tracker = PositionTracker::with_store(config.clone(), store);
        tracker.load_from_store()?;
        Arc::new(tracker)
    } else {
        Arc::new(PositionTracker::new(config.clone()))
    };

    let audit_path = env::var("AGENT_AUDIT_DB_PATH").unwrap_or_else(|_| "agent_audit.db".into());
    let audit = Arc::new(AuditLog::open(&audit_path)?);
    let approval = Arc::new(ApprovalService::from_env());
    let control = Arc::new(BotControl::from_env());
    let shutdown = Shutdown::new();
    spawn_shutdown_listener(shutdown.clone());

    let agent = build_agent_context(
        positions.clone(),
        config.clone(),
        audit,
        approval.clone(),
        control.clone(),
        target.clone(),
    );

    if TelegramNotifier::from_env().is_some() {
        spawn_telegram_bot(TelegramBotContext {
            agent: agent.clone(),
            control: control.clone(),
            target: target.clone(),
            approval,
        });
    }

    let exiting = Arc::new(Mutex::new(HashSet::<String>::new()));

    let (bus, rx) = EventBus::new();
    spawn_orchestrator(rx, agent.clone());
    spawn_status_server(agent.clone(), control.clone(), target.clone());

    info!(
        "bot ready | target={} | max_trades={} | slippage={}% | poll={}s | dry_run={} | agent={:?} | paused={}",
        target,
        config.max_concurrent_trades,
        config.slippage,
        config.position_poll_secs,
        config.dry_run,
        config.agent_mode,
        control.is_paused(),
    );

    {
        let bus_exit = bus.clone();
        let config_exit = config.clone();
        let positions_exit = positions.clone();
        let exiting = exiting.clone();
        let shutdown_exit = shutdown.subscribe();
        tokio::spawn(async move {
            run_exit_monitor(
                bus_exit,
                config_exit,
                positions_exit,
                exiting,
                shutdown_exit,
            )
            .await;
        });
    }

    let mut shutdown_ws = shutdown.subscribe();
    while !shutdown.is_triggered() {
        info!("connecting websocket: {}", ws_url);
        tokio::select! {
            _ = shutdown_ws.changed() => {
                if shutdown.is_triggered() {
                    info!("shutdown requested — stopping websocket loop");
                    break;
                }
            }
            result = run_ws_loop(
                &ws_url,
                &target,
                &unwanted_key,
                &sol_address,
                rpc_client.clone(),
                bus.clone(),
                shutdown.clone(),
            ) => {
                match result {
                    Ok(()) => warn!("websocket loop ended, reconnecting"),
                    Err(err) => error!("websocket error: {}, reconnecting in 3s", err),
                }
            }
        }
        if shutdown.is_triggered() {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_secs(3)).await;
    }

    info!("bot shutdown complete");
    Ok(())
}

async fn run_ws_loop(
    ws_url: &str,
    target: &str,
    unwanted_key: &str,
    sol_address: &str,
    rpc_client: Arc<RpcClient>,
    bus: EventBus,
    shutdown: Arc<Shutdown>,
) -> anyhow::Result<()> {
    let (ws_stream, _) = connect_async(ws_url).await?;
    let (mut write, mut read) = ws_stream.split();

    let subscription_message = serde_json::json!({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "transactionSubscribe",
        "params": [
            {
                "failed": false,
                "accountInclude": [
                    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
                    PUMP_PROGRAM,
                    target
                ],
                "accountExclude": [unwanted_key],
            },
            {
                "commitment": "processed",
                "encoding": "jsonParsed",
                "transactionDetails": "full",
                "maxSupportedTransactionVersion": 0
            }
        ]
    });

    write
        .send(subscription_message.to_string().into())
        .await?;
    info!("subscribed to target wallet trades");

    let mut shutdown_rx = shutdown.subscribe();
    while !shutdown.is_triggered() {
        tokio::select! {
            _ = shutdown_rx.changed() => {
                if shutdown.is_triggered() {
                    return Ok(());
                }
            }
            msg = read.next() => {
                let Some(msg) = msg else {
                    return Ok(());
                };
                let msg = msg?;
                let WsMessage::Text(text) = msg else {
                    continue;
                };

                let json: serde_json::Value = match serde_json::from_str(&text) {
                    Ok(v) => v,
                    Err(err) => {
                        warn!("invalid ws json: {}", err);
                        continue;
                    }
                };

                let target_owned = target.to_string();
                let sol_owned = sol_address.to_string();
                let rpc = rpc_client.clone();
                let parsed = tokio::task::spawn_blocking(move || {
                    parse_swap_from_ws_message(&json, &target_owned, &sol_owned, &rpc)
                })
                .await??;

                let direction = parsed.direction.unwrap_or_else(|| "buy".to_string());
                info!(
                    "detected {} {} amount={} mint={}",
                    direction, target, parsed.amount_in, parsed.mint
                );

                let event = TradeEvent::DetectedSwap {
                    mint: parsed.mint,
                    amount_in: parsed.amount_in,
                    direction,
                };

                if let Err(err) = bus.publish(event).await {
                    error!("event bus publish failed: {}", err);
                }
            }
        }
    }

    Ok(())
}
