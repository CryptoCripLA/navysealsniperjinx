use std::sync::Arc;
use std::time::Duration;

use tracing::{error, info, warn};

use crate::agent::{
    approval::ApprovalService, bot_status::{
        build_bot_status, format_config_text, format_market_text, format_pnl_text,
        format_positions_text, format_status_text,
    }, control::BotControl, AgentContext,
};

use super::telegram::TelegramNotifier;

pub struct TelegramBotContext {
    pub agent: AgentContext,
    pub control: Arc<BotControl>,
    pub target: String,
    pub approval: Arc<ApprovalService>,
}

pub fn spawn_telegram_bot(ctx: TelegramBotContext) {
    tokio::spawn(async move {
        let Some(notifier) = TelegramNotifier::from_env() else {
            warn!("telegram bot: not configured");
            return;
        };
        info!("telegram bot started (commands + approval callbacks)");
        let mut offset = 0i64;
        loop {
            match notifier.get_updates(offset).await {
                Ok(updates) => {
                    for update in updates {
                        if let Some(update_id) = update["update_id"].as_i64() {
                            offset = update_id + 1;
                        }
                        if let Some(cb) = update.get("callback_query") {
                            handle_callback(&notifier, &ctx.approval, cb).await;
                            continue;
                        }
                        if let Some(msg) = update.get("message") {
                            handle_message(&notifier, &ctx, msg).await;
                        }
                    }
                }
                Err(err) => error!("telegram getUpdates failed: {}", err),
            }
            tokio::time::sleep(Duration::from_millis(400)).await;
        }
    });
}

async fn handle_callback(
    notifier: &TelegramNotifier,
    approval: &ApprovalService,
    cb: &serde_json::Value,
) {
    let callback_id = cb["id"].as_str().unwrap_or("");
    let data = cb["data"].as_str().unwrap_or("");
    let (approved, request_id) = if let Some(id) = data.strip_prefix("approve:") {
        (true, id)
    } else if let Some(id) = data.strip_prefix("reject:") {
        (false, id)
    } else {
        return;
    };
    let label = if approved { "approved" } else { "rejected" };
    let _ = approval.resolve_callback(request_id, approved).await;
    let _ = notifier.answer_callback(callback_id, label).await;
    info!("telegram approval {} id={}", label, request_id);
}

async fn handle_message(notifier: &TelegramNotifier, ctx: &TelegramBotContext, msg: &serde_json::Value) {
    if !notifier.is_authorized_chat(msg) {
        return;
    }
    let Some(text) = msg["text"].as_str() else {
        return;
    };
    let command = text.split_whitespace().next().unwrap_or("");
    let arg = text.split_whitespace().nth(1).unwrap_or("");

    let reply = match command {
        "/start" | "/help" => help_text(),
        "/status" => {
            let report = build_bot_status(&ctx.agent, &ctx.control, &ctx.target).await;
            format_status_text(&report)
        }
        "/pause" => {
            ctx.control.pause();
            "Bot paused — new copy trades skipped; exits still run.".into()
        }
        "/resume" => {
            ctx.control.resume();
            "Bot resumed — copy trading active.".into()
        }
        "/positions" => {
            let report = build_bot_status(&ctx.agent, &ctx.control, &ctx.target).await;
            format_positions_text(&report)
        }
        "/pnl" => {
            let report = build_bot_status(&ctx.agent, &ctx.control, &ctx.target).await;
            format_pnl_text(&report)
        }
        "/config" => format_config_text(&ctx.agent),
        "/target" => format!("Target wallet: {}", ctx.target),
        "/market" => {
            if arg.is_empty() {
                "Usage: /market <mint_pubkey>".into()
            } else {
                format_market_text(&ctx.agent, arg).await
            }
        }
        _ if command.starts_with("/market ") => {
            let mint = command.strip_prefix("/market ").unwrap_or("").trim();
            if mint.is_empty() {
                "Usage: /market <mint_pubkey>".into()
            } else {
                format_market_text(&ctx.agent, mint).await
            }
        }
        _ => return,
    };

    if let Err(err) = notifier.send_message(&reply).await {
        warn!("telegram command reply failed: {}", err);
    }
}

fn help_text() -> String {
    [
        "Solana copy-trading bot commands:",
        "/status — bot state + wallet + PnL headline",
        "/positions — open positions with market intel",
        "/pnl — realized PnL + decision stats",
        "/market <mint> — liquidity, venue, risk flags",
        "/config — trading thresholds",
        "/target — monitored wallet",
        "/pause — stop new mirrors (exits still run)",
        "/resume — resume copy trading",
        "/help — this message",
    ]
    .join("\n")
}
