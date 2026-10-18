use std::sync::Arc;

use anyhow::Result;
use tracing::{info, warn};

use crate::engine::positions::PositionTracker;
use crate::engine::strategy::{AgentMode, TradeConfig};
use crate::services::telegram::notify_circuit_breaker;

use super::approval::ApprovalService;
use super::audit::AuditLog;
use super::control::BotControl;
use super::context::AgentContext;
use super::events::{TradeAction, TradeEvent};
use super::llm::refine_action;
use super::planner::execute_with_retry;
use super::policy::{apply_approve_gate, decide};
use super::snapshot::build_snapshot;
use super::tools::{self, ToolCall, ToolName};

pub async fn process_event(ctx: &AgentContext, event: TradeEvent) -> Result<()> {
    if let TradeEvent::DetectedSwap {
        ref mint,
        amount_in,
        ref direction,
    } = event
    {
        if let Some(audit) = &ctx.audit {
            let _ = audit.record_target_swap(mint, direction, amount_in);
        }
    }

    let focus_mint = event_mint(&event);
    let snapshot = build_snapshot(ctx, focus_mint).await.ok();
    let snapshot_json = snapshot
        .as_ref()
        .and_then(|s| serde_json::to_string(s).ok());

    let policy_action = decide(ctx, &event);
    let llm_action = refine_action(ctx, &event, policy_action.clone()).await?;
    let approved = resolve_approval(ctx, llm_action).await;
    let notify_label = match &event {
        TradeEvent::PositionValued { .. } | TradeEvent::ExitSignal { .. } => {
            Some("auto_exit".to_string())
        }
        _ => None,
    };

    let outcome = execute_with_retry(ctx, approved.clone(), notify_label.as_deref()).await;

    if let Some(audit) = &ctx.audit {
        let event_json = serde_json::to_string(&format!("{event:?}")).unwrap_or_default();
        let _ = audit.record_decision(
            &event_json,
            snapshot_json.as_deref(),
            &policy_action.to_string(),
            &approved.to_string(),
            if outcome.is_ok() { "ok" } else { "err" },
        );
    }

    if outcome.is_ok() {
        if let Some(control) = &ctx.control {
            control.record_success();
        }
    } else if let Some(control) = &ctx.control {
        if control.record_failure() {
            warn!(
                "circuit breaker tripped after {} failures",
                control.failure_count()
            );
            notify_circuit_breaker(control.failure_count()).await;
        }
    }

    outcome
}

fn event_mint(event: &TradeEvent) -> Option<&str> {
    match event {
        TradeEvent::DetectedSwap { mint, .. }
        | TradeEvent::PositionValued { mint, .. }
        | TradeEvent::ExitSignal { mint, .. } => Some(mint.as_str()),
    }
}

async fn resolve_approval(ctx: &AgentContext, action: TradeAction) -> TradeAction {
    if ctx.config.agent_mode != AgentMode::Approve || ctx.config.auto_approve {
        return action;
    }
    match &ctx.approval {
        Some(svc) => svc.request(action).await,
        None => apply_approve_gate(ctx, action),
    }
}

pub async fn execute_action(
    ctx: &AgentContext,
    action: TradeAction,
    notify_label: Option<&str>,
) -> Result<()> {
    match action {
        TradeAction::Skip { reason } => {
            info!("agent skip: {}", reason);
            Ok(())
        }
        TradeAction::MirrorBuy { mint, amount_sol } => {
            let call = ToolCall {
                name: ToolName::MirrorBuy,
                mint: Some(mint),
                amount_sol: Some(amount_sol),
            };
            tools::execute_tool(ctx, &call, notify_label).await?;
            Ok(())
        }
        TradeAction::MirrorSell { mint, amount_sol } => {
            let call = ToolCall {
                name: ToolName::MirrorSell,
                mint: Some(mint),
                amount_sol: Some(amount_sol),
            };
            tools::execute_tool(ctx, &call, notify_label).await?;
            Ok(())
        }
    }
}

pub async fn handle_trade_event(ctx: &AgentContext, event: TradeEvent) -> Result<()> {
    process_event(ctx, event).await
}

pub fn build_context(
    positions: Arc<PositionTracker>,
    config: TradeConfig,
) -> AgentContext {
    AgentContext::new(positions, config)
}

pub fn build_context_with_audit(
    positions: Arc<PositionTracker>,
    config: TradeConfig,
    audit: Arc<AuditLog>,
) -> AgentContext {
    AgentContext::new(positions, config).with_audit(audit)
}

pub fn build_agent_context(
    positions: Arc<PositionTracker>,
    config: TradeConfig,
    audit: Arc<AuditLog>,
    approval: Arc<ApprovalService>,
    control: Arc<BotControl>,
    target_pubkey: String,
) -> AgentContext {
    AgentContext::new(positions, config)
        .with_audit(audit)
        .with_approval(approval)
        .with_control(control)
        .with_target(target_pubkey)
}
