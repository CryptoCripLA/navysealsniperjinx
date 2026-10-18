pub mod approval;
pub mod audit;
pub mod bot_status;
pub mod bus;
pub mod context;
pub mod control;
pub mod events;
pub mod llm;
pub mod market;
pub mod planner;
pub mod policy;
pub mod runner;
pub mod shutdown;
pub mod snapshot;
pub mod tools;

pub use approval::ApprovalService;
pub use audit::{AuditLog, PnlSummary};
pub use bot_status::BotStatusReport;
pub use bus::{spawn_orchestrator, EventBus, SharedBus};
pub use context::AgentContext;
pub use control::BotControl;
pub use events::{TradeAction, TradeEvent};
pub use policy::decide;
pub use runner::{
    build_agent_context, build_context, build_context_with_audit, execute_action,
    handle_trade_event, process_event,
};
pub use shutdown::Shutdown;
