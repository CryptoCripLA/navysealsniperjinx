use std::sync::Arc;

use copy_trading_bot::agent::{control::BotControl, decide, AgentContext, TradeAction, TradeEvent};
use copy_trading_bot::engine::positions::PositionTracker;
use copy_trading_bot::engine::strategy::TradeConfig;

#[test]
fn detected_buy_event_produces_mirror_buy() {
    let ctx = AgentContext::new(
        Arc::new(PositionTracker::new(TradeConfig::default())),
        TradeConfig::default(),
    );
    let action = decide(
        &ctx,
        &TradeEvent::DetectedSwap {
            mint: "TestMint1111111111111111111111111111111111".into(),
            amount_in: 0.05,
            direction: "buy".into(),
        },
    );
    assert!(matches!(action, TradeAction::MirrorBuy { .. }));
}

#[test]
fn paused_bot_skips_copy_trade_in_pipeline() {
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
            mint: "TestMint1111111111111111111111111111111111".into(),
            amount_in: 0.05,
            direction: "buy".into(),
        },
    );
    assert!(matches!(action, TradeAction::Skip { .. }));
}
