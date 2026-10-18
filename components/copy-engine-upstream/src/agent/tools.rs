use std::str::FromStr;

use anyhow::{anyhow, Result};
use serde::Serialize;
use solana_sdk::{pubkey::Pubkey, signer::Signer};
use tracing::info;

use crate::common::utils::{create_nonblocking_rpc_client, create_rpc_client, import_wallet, AppState};
use crate::core::balance::fetch_token_balance;
use crate::engine::pricing::get_position_value_sol;
use crate::engine::sniper::mirror_swap;
use crate::services::telegram::notify_trade;

use super::context::AgentContext;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ToolName {
    ListOpenPositions,
    GetWalletSolBalance,
    QuoteSellSol,
    MirrorBuy,
    MirrorSell,
}

#[derive(Debug, Clone, Serialize)]
pub struct ToolCall {
    pub name: ToolName,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub mint: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub amount_sol: Option<f64>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ToolResult {
    pub ok: bool,
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub signature: Option<String>,
}

pub fn tool_schemas_json() -> serde_json::Value {
    serde_json::json!([
        {
            "type": "function",
            "function": {
                "name": "quote_sell_sol",
                "description": "Quote SOL received if selling entire open position for mint",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "mint": { "type": "string" }
                    },
                    "required": ["mint"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_wallet_sol_balance",
                "description": "Get wallet SOL balance",
                "parameters": { "type": "object", "properties": {} }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "list_open_positions",
                "description": "Count open positions",
                "parameters": { "type": "object", "properties": {} }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "mirror_buy",
                "description": "Mirror a buy for mint with SOL amount",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "mint": { "type": "string" },
                        "amount_sol": { "type": "number" }
                    },
                    "required": ["mint", "amount_sol"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "mirror_sell",
                "description": "Mirror a sell for mint; amount_sol 1.0 sells entire position",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "mint": { "type": "string" },
                        "amount_sol": { "type": "number" }
                    },
                    "required": ["mint", "amount_sol"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "skip",
                "description": "Skip trade with reason",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reason": { "type": "string" }
                    },
                    "required": ["reason"]
                }
            }
        }
    ])
}

pub async fn get_wallet_sol_balance() -> Result<f64> {
    let rpc = create_rpc_client()?;
    let wallet = import_wallet()?;
    let lamports = rpc.get_balance(&wallet.pubkey())?;
    Ok(lamports as f64 / 1_000_000_000.0)
}

pub async fn quote_sell_sol(ctx: &AgentContext, mint: &str) -> Result<f64> {
    let pos = ctx
        .positions
        .get(mint)
        .ok_or_else(|| anyhow!("no open position for {mint}"))?;
    let rpc = create_rpc_client()?;
    get_position_value_sol(
        rpc,
        mint,
        pos.token_balance_raw,
        ctx.config.slippage,
    )
    .await
}

pub async fn execute_tool(
    ctx: &AgentContext,
    call: &ToolCall,
    notify_label: Option<&str>,
) -> Result<ToolResult> {
    match call.name {
        ToolName::ListOpenPositions => {
            let open = ctx.positions.list_open();
            Ok(ToolResult {
                ok: true,
                message: format!("{} open position(s)", open.len()),
                signature: None,
            })
        }
        ToolName::GetWalletSolBalance => {
            let bal = get_wallet_sol_balance().await?;
            Ok(ToolResult {
                ok: true,
                message: format!("wallet_sol={bal}"),
                signature: None,
            })
        }
        ToolName::QuoteSellSol => {
            let mint = call
                .mint
                .as_deref()
                .ok_or_else(|| anyhow!("quote_sell_sol requires mint"))?;
            let quote = quote_sell_sol(ctx, mint).await?;
            Ok(ToolResult {
                ok: true,
                message: format!("quote_sell_sol={quote}"),
                signature: None,
            })
        }
        ToolName::MirrorBuy => {
            let mint = call
                .mint
                .clone()
                .ok_or_else(|| anyhow!("mirror_buy requires mint"))?;
            let amount = call.amount_sol.unwrap_or(ctx.config.buy_amount_sol);
            mirror_swap_tool(ctx, &mint, amount, "buy", notify_label).await
        }
        ToolName::MirrorSell => {
            let mint = call
                .mint
                .clone()
                .ok_or_else(|| anyhow!("mirror_sell requires mint"))?;
            let amount = call.amount_sol.unwrap_or(1.0);
            mirror_swap_tool(ctx, &mint, amount, "sell", notify_label).await
        }
    }
}

async fn mirror_swap_tool(
    ctx: &AgentContext,
    mint: &str,
    amount_in: f64,
    direction: &str,
    notify_label: Option<&str>,
) -> Result<ToolResult> {
    if ctx.config.dry_run {
        info!("DRY_RUN tool: would {direction} {mint} amount={amount_in}");
        return Ok(ToolResult {
            ok: true,
            message: format!("dry_run {direction} {mint}"),
            signature: None,
        });
    }

    let rpc_client = create_rpc_client()?;
    let rpc_nonblocking_client = create_nonblocking_rpc_client().await?;
    let wallet = import_wallet()?;
    let state = AppState {
        rpc_client: rpc_client.clone(),
        rpc_nonblocking_client,
        wallet: wallet.clone(),
    };

    let res = mirror_swap(state, mint.to_string(), amount_in, direction, &ctx.config).await?;
    let sig = res.first().cloned();

    if let Some(ref signature) = sig {
        if direction == "buy" {
            let mint_pk = Pubkey::from_str(mint)?;
            let (balance_raw, decimals) =
                fetch_token_balance(&rpc_client, &wallet.pubkey(), &mint_pk)?;
            ctx.positions.on_buy(mint, amount_in, balance_raw, decimals);
        } else if direction == "sell" {
            let exit_sol = quote_sell_sol(ctx, mint).await.unwrap_or(0.0);
            if let Some(closed) = ctx.positions.on_sell(mint) {
                if let Some(audit) = &ctx.audit {
                    let _ = audit.record_closed_trade(
                        mint,
                        closed.entry_sol,
                        exit_sol,
                        notify_label.unwrap_or("sell"),
                    );
                }
                info!(
                    "position closed {} | entry_sol={} exit_sol={}",
                    mint, closed.entry_sol, exit_sol
                );
            }
        }
        let label = notify_label.unwrap_or(direction);
        notify_trade(&wallet.pubkey().to_string(), signature, label, mint).await;
    }

    Ok(ToolResult {
        ok: sig.is_some(),
        message: format!("{direction} {mint}"),
        signature: sig,
    })
}

pub fn action_to_tool_call(action: &super::events::TradeAction) -> Option<ToolCall> {
    use super::events::TradeAction;
    match action {
        TradeAction::MirrorBuy { mint, amount_sol } => Some(ToolCall {
            name: ToolName::MirrorBuy,
            mint: Some(mint.clone()),
            amount_sol: Some(*amount_sol),
        }),
        TradeAction::MirrorSell { mint, amount_sol } => Some(ToolCall {
            name: ToolName::MirrorSell,
            mint: Some(mint.clone()),
            amount_sol: Some(*amount_sol),
        }),
        TradeAction::Skip { .. } => None,
    }
}
