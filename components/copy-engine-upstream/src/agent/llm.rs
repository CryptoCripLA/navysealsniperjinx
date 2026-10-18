use std::env;
use std::time::Duration;

use anyhow::Result;
use reqwest::Client;
use serde::{Deserialize, Serialize};
use tracing::{info, warn};

use crate::engine::strategy::AgentMode;

use super::context::AgentContext;
use super::events::{TradeAction, TradeEvent};
use super::snapshot::build_snapshot;
use super::tools;

#[derive(Serialize)]
struct ChatRequest {
    model: String,
    messages: Vec<ChatMessage>,
    tools: serde_json::Value,
    temperature: f32,
}

#[derive(Serialize, Deserialize)]
struct ChatMessage {
    role: String,
    content: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    tool_calls: Option<Vec<ToolCallPayload>>,
}

#[derive(Serialize, Deserialize)]
struct ToolCallPayload {
    id: String,
    #[serde(rename = "type")]
    kind: String,
    function: FunctionCall,
}

#[derive(Serialize, Deserialize)]
struct FunctionCall {
    name: String,
    arguments: String,
}

#[derive(Deserialize)]
struct ChatResponse {
    choices: Vec<ChatChoice>,
}

#[derive(Deserialize)]
struct ChatChoice {
    message: ChatMessage,
}

#[derive(Deserialize)]
struct SkipArgs {
    reason: String,
}

#[derive(Deserialize)]
struct TradeArgs {
    mint: String,
    amount_sol: f64,
}

pub fn should_use_llm(ctx: &AgentContext) -> bool {
    ctx.config.agent_mode == AgentMode::Llm
        || (ctx.config.agent_mode == AgentMode::Approve && ctx.config.llm_in_approve)
}

pub async fn refine_action(
    ctx: &AgentContext,
    event: &TradeEvent,
    base: TradeAction,
) -> Result<TradeAction> {
    if !should_use_llm(ctx) {
        return Ok(base);
    }

    let api_url = match env::var("LLM_API_URL") {
        Ok(v) if !v.is_empty() => v,
        _ => {
            warn!("AGENT_MODE=llm but LLM_API_URL unset — using rules action");
            return Ok(base);
        }
    };

    let focus_mint = match event {
        TradeEvent::DetectedSwap { mint, .. }
        | TradeEvent::PositionValued { mint, .. }
        | TradeEvent::ExitSignal { mint, .. } => Some(mint.as_str()),
    };

    let snapshot = build_snapshot(ctx, focus_mint).await.ok();
    let recent = ctx
        .audit
        .as_ref()
        .and_then(|a| a.recent_swaps_json(5).ok());

    let api_key = env::var("LLM_API_KEY").unwrap_or_default();
    let model = env::var("LLM_MODEL").unwrap_or_else(|_| "gpt-4o-mini".into());

    let prompt = format!(
        "Event: {:?}\nRules proposal: {}\nSnapshot: {}\nRecent target swaps: {:?}\n\
         Choose mirror_buy, mirror_sell, or skip via tool call.",
        event,
        base,
        snapshot
            .as_ref()
            .map(|s| serde_json::to_string(s).unwrap_or_default())
            .unwrap_or_default(),
        recent
    );

    let body = ChatRequest {
        model,
        messages: vec![
            ChatMessage {
                role: "system".into(),
                content: "You are a Solana copy-trading agent with rich market context. \
                    Consider liquidity, venue (pump.fun vs raydium), risk_flags, unrealized PnL, \
                    recent target swaps, and pause state. Prefer skip when risk_flags are severe \
                    or liquidity is low. Use tool calls only.".into(),
                tool_calls: None,
            },
            ChatMessage {
                role: "user".into(),
                content: prompt,
                tool_calls: None,
            },
        ],
        tools: tools::tool_schemas_json(),
        temperature: 0.0,
    };

    let client = Client::builder()
        .timeout(Duration::from_secs(15))
        .build()?;
    let mut req = client.post(&api_url).json(&body);
    if !api_key.is_empty() {
        req = req.bearer_auth(api_key);
    }

    let response = match req.send().await {
        Ok(r) => r,
        Err(err) => {
            warn!("LLM request failed: {err} — using rules action");
            return Ok(base);
        }
    };

    if !response.status().is_success() {
        warn!("LLM API error {} — using rules action", response.status());
        return Ok(base);
    }

    let chat: ChatResponse = response.json().await?;
    let message = match chat.choices.first() {
        Some(c) => &c.message,
        None => return Ok(base),
    };

    if let Some(calls) = &message.tool_calls {
        if let Some(action) = parse_tool_calls(calls) {
            info!("LLM selected action via tool call");
            return Ok(action);
        }
    }

    if message.content.contains("skip") {
        return Ok(TradeAction::Skip {
            reason: "llm veto".into(),
        });
    }

    Ok(base)
}

fn parse_tool_calls(calls: &[ToolCallPayload]) -> Option<TradeAction> {
    let call = calls.first()?;
    match call.function.name.as_str() {
        "mirror_buy" => {
            let args: TradeArgs = serde_json::from_str(&call.function.arguments).ok()?;
            Some(TradeAction::MirrorBuy {
                mint: args.mint,
                amount_sol: args.amount_sol,
            })
        }
        "mirror_sell" => {
            let args: TradeArgs = serde_json::from_str(&call.function.arguments).ok()?;
            Some(TradeAction::MirrorSell {
                mint: args.mint,
                amount_sol: args.amount_sol,
            })
        }
        "skip" => {
            let args: SkipArgs = serde_json::from_str(&call.function.arguments).ok()?;
            Some(TradeAction::Skip {
                reason: args.reason,
            })
        }
        _ => None,
    }
}

pub fn parse_llm_decision(raw: &str) -> Result<bool> {
    let trimmed = raw.trim();
    if trimmed.contains("\"skip\"") {
        return Ok(false);
    }
    if trimmed.contains("\"execute\"") {
        return Ok(true);
    }
    Err(anyhow::anyhow!("unrecognized LLM response"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_skip_and_execute() {
        assert!(!parse_llm_decision(r#"{"decision":"skip"}"#).unwrap());
        assert!(parse_llm_decision(r#"{"decision":"execute"}"#).unwrap());
    }

    #[test]
    fn parses_tool_call_buy() {
        let calls = vec![ToolCallPayload {
            id: "1".into(),
            kind: "function".into(),
            function: FunctionCall {
                name: "mirror_buy".into(),
                arguments: r#"{"mint":"abc","amount_sol":0.01}"#.into(),
            },
        }];
        let action = parse_tool_calls(&calls).unwrap();
        assert_eq!(
            action,
            TradeAction::MirrorBuy {
                mint: "abc".into(),
                amount_sol: 0.01
            }
        );
    }
}
