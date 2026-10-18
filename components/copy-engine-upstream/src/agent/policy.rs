use crate::engine::strategy::AgentMode;

use super::context::AgentContext;
use super::events::{TradeAction, TradeEvent};

pub fn decide(ctx: &AgentContext, event: &TradeEvent) -> TradeAction {
    match event {
        TradeEvent::DetectedSwap {
            mint,
            amount_in,
            direction,
        } => decide_detected_swap(ctx, mint, *amount_in, direction),
        TradeEvent::PositionValued {
            mint,
            entry_sol,
            peak_value_sol,
            current_value_sol,
        } => decide_position_valued(ctx, mint, *entry_sol, *peak_value_sol, *current_value_sol),
        TradeEvent::ExitSignal { mint, reason: _ } => TradeAction::MirrorSell {
            mint: mint.clone(),
            amount_sol: 1.0,
        },
    }
}

fn decide_detected_swap(
    ctx: &AgentContext,
    mint: &str,
    amount_in: f64,
    direction: &str,
) -> TradeAction {
    if ctx.is_paused() {
        return TradeAction::Skip {
            reason: "bot paused".into(),
        };
    }

    if learning_throttle(ctx) {
        return TradeAction::Skip {
            reason: "learning throttle: recent win rate too low".into(),
        };
    }

    if !ctx.config.mint_allowed(mint) {
        return TradeAction::Skip {
            reason: format!("mint {mint} blocked by list policy"),
        };
    }

    if direction == "buy" {
        if amount_in < ctx.config.min_target_buy_sol {
            return TradeAction::Skip {
                reason: format!(
                    "target buy {amount_in} SOL below min {}",
                    ctx.config.min_target_buy_sol
                ),
            };
        }
        if !ctx.positions.can_open_new() {
            return TradeAction::Skip {
                reason: format!("max open positions ({})", ctx.config.max_concurrent_trades),
            };
        }
        let amount = amount_in.min(ctx.config.buy_amount_sol);
        TradeAction::MirrorBuy {
            mint: mint.to_string(),
            amount_sol: amount,
        }
    } else {
        if !ctx.config.copy_sells {
            return TradeAction::Skip {
                reason: "copy sells disabled".into(),
            };
        }
        TradeAction::MirrorSell {
            mint: mint.to_string(),
            amount_sol: amount_in,
        }
    }
}

fn decide_position_valued(
    ctx: &AgentContext,
    mint: &str,
    entry_sol: f64,
    peak_value_sol: f64,
    current_value_sol: f64,
) -> TradeAction {
    if ctx.config.should_take_profit(entry_sol, current_value_sol) {
        return TradeAction::MirrorSell {
            mint: mint.to_string(),
            amount_sol: 1.0,
        };
    }
    if ctx
        .config
        .should_stop_loss_for_mint(mint, entry_sol, current_value_sol)
    {
        return TradeAction::MirrorSell {
            mint: mint.to_string(),
            amount_sol: 1.0,
        };
    }
    if ctx
        .config
        .should_trailing_stop(peak_value_sol, current_value_sol)
    {
        return TradeAction::MirrorSell {
            mint: mint.to_string(),
            amount_sol: 1.0,
        };
    }
    TradeAction::Skip {
        reason: "hold".into(),
    }
}

fn learning_throttle(ctx: &AgentContext) -> bool {
    let Some(audit) = &ctx.audit else {
        return false;
    };
    let Ok(summary) = audit.pnl_summary() else {
        return false;
    };
    summary.total_closed >= 5 && summary.win_rate_pct < 35.0
}

pub fn apply_approve_gate(ctx: &AgentContext, action: TradeAction) -> TradeAction {
    if ctx.config.agent_mode != AgentMode::Approve {
        return action;
    }
    if ctx.config.auto_approve {
        return action;
    }
    match action {
        TradeAction::Skip { reason } => TradeAction::Skip { reason },
        _ => TradeAction::Skip {
            reason: "awaiting telegram approval".into(),
        },
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use crate::engine::positions::PositionTracker;
    use crate::engine::strategy::TradeConfig;

    use super::*;
    use crate::agent::control::BotControl;

    #[test]
    fn paused_bot_skips_detected_swap() {
        let control = Arc::new(BotControl::new(3));
        control.pause();
        let ctx = AgentContext::new(
            Arc::new(PositionTracker::new(TradeConfig::default())),
            TradeConfig::default(),
        )
        .with_control(control);
        let action = decide(
            &ctx,
            &TradeEvent::DetectedSwap {
                mint: "mint".into(),
                amount_in: 0.1,
                direction: "buy".into(),
            },
        );
        assert!(matches!(action, TradeAction::Skip { .. }));
    }

    #[test]
    fn exits_still_run_when_paused() {
        let control = Arc::new(BotControl::new(3));
        control.pause();
        let ctx = AgentContext::new(
            Arc::new(PositionTracker::new(TradeConfig::default())),
            TradeConfig::default(),
        )
        .with_control(control);
        let action = decide(
            &ctx,
            &TradeEvent::PositionValued {
                mint: "mint".into(),
                entry_sol: 1.0,
                peak_value_sol: 1.6,
                current_value_sol: 1.6,
            },
        );
        assert!(matches!(action, TradeAction::MirrorSell { .. }));
    }

    #[test]
    fn skips_buy_when_at_capacity() {
        let mut cfg = TradeConfig::default();
        cfg.max_concurrent_trades = 1;
        let positions = Arc::new(PositionTracker::new(cfg.clone()));
        positions.on_buy("existing", 0.1, 100, 6);
        let ctx = AgentContext::new(positions, cfg);
        let action = decide(
            &ctx,
            &TradeEvent::DetectedSwap {
                mint: "new".into(),
                amount_in: 0.05,
                direction: "buy".into(),
            },
        );
        assert!(matches!(action, TradeAction::Skip { .. }));
    }

    #[test]
    fn caps_buy_amount() {
        let mut cfg = TradeConfig::default();
        cfg.buy_amount_sol = 0.01;
        let ctx = AgentContext::new(Arc::new(PositionTracker::new(cfg.clone())), cfg);
        let action = decide(
            &ctx,
            &TradeEvent::DetectedSwap {
                mint: "mint".into(),
                amount_in: 1.0,
                direction: "buy".into(),
            },
        );
        assert_eq!(
            action,
            TradeAction::MirrorBuy {
                mint: "mint".into(),
                amount_sol: 0.01
            }
        );
    }

    #[test]
    fn position_valued_take_profit() {
        let ctx = AgentContext::new(
            Arc::new(PositionTracker::new(TradeConfig::default())),
            TradeConfig::default(),
        );
        let action = decide(
            &ctx,
            &TradeEvent::PositionValued {
                mint: "mint".into(),
                entry_sol: 1.0,
                peak_value_sol: 1.6,
                current_value_sol: 1.6,
            },
        );
        assert_eq!(
            action,
            TradeAction::MirrorSell {
                mint: "mint".into(),
                amount_sol: 1.0
            }
        );
    }

    #[test]
    fn position_valued_trailing_stop() {
        let mut cfg = TradeConfig::default();
        cfg.trailing_stop_pct = 10.0;
        cfg.take_profit_pct = 100.0;
        let ctx = AgentContext::new(Arc::new(PositionTracker::new(cfg.clone())), cfg);
        let action = decide(
            &ctx,
            &TradeEvent::PositionValued {
                mint: "mint".into(),
                entry_sol: 1.0,
                peak_value_sol: 1.2,
                current_value_sol: 1.07,
            },
        );
        assert_eq!(
            action,
            TradeAction::MirrorSell {
                mint: "mint".into(),
                amount_sol: 1.0
            }
        );
    }

    #[test]
    fn blacklist_blocks_mint() {
        let mut cfg = TradeConfig::default();
        cfg.mint_blacklist = vec!["bad".into()];
        let ctx = AgentContext::new(Arc::new(PositionTracker::new(cfg.clone())), cfg);
        let action = decide(
            &ctx,
            &TradeEvent::DetectedSwap {
                mint: "bad".into(),
                amount_in: 0.1,
                direction: "buy".into(),
            },
        );
        assert!(matches!(action, TradeAction::Skip { .. }));
    }

    #[test]
    fn approve_gate_blocks_without_auto() {
        let mut cfg = TradeConfig::default();
        cfg.agent_mode = AgentMode::Approve;
        cfg.auto_approve = false;
        let ctx = AgentContext::new(Arc::new(PositionTracker::new(cfg.clone())), cfg);
        let gated = apply_approve_gate(
            &ctx,
            TradeAction::MirrorBuy {
                mint: "m".into(),
                amount_sol: 0.01,
            },
        );
        assert!(matches!(gated, TradeAction::Skip { .. }));
    }
}
