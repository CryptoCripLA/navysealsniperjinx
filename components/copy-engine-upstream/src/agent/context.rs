use std::sync::Arc;

use crate::agent::approval::ApprovalService;
use crate::agent::control::BotControl;
use crate::engine::positions::PositionTracker;
use crate::engine::strategy::TradeConfig;

#[derive(Clone)]
pub struct AgentContext {
    pub positions: Arc<PositionTracker>,
    pub config: TradeConfig,
    pub audit: Option<Arc<crate::agent::audit::AuditLog>>,
    pub approval: Option<Arc<ApprovalService>>,
    pub control: Option<Arc<BotControl>>,
    pub target: Option<String>,
}

impl AgentContext {
    pub fn new(positions: Arc<PositionTracker>, config: TradeConfig) -> Self {
        Self {
            positions,
            config,
            audit: None,
            approval: None,
            control: None,
            target: None,
        }
    }

    pub fn with_audit(mut self, audit: Arc<crate::agent::audit::AuditLog>) -> Self {
        self.audit = Some(audit);
        self
    }

    pub fn with_approval(mut self, approval: Arc<ApprovalService>) -> Self {
        self.approval = Some(approval);
        self
    }

    pub fn with_control(mut self, control: Arc<BotControl>) -> Self {
        self.control = Some(control);
        self
    }

    pub fn with_target(mut self, target: String) -> Self {
        self.target = Some(target);
        self
    }

    pub fn is_paused(&self) -> bool {
        self.control
            .as_ref()
            .map(|c| c.is_paused())
            .unwrap_or(false)
    }
}
