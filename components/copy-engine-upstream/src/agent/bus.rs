use std::sync::Arc;

use tokio::sync::mpsc;
use tracing::{error, info};

use super::context::AgentContext;
use super::events::TradeEvent;
use super::runner::process_event;

const DEFAULT_CAPACITY: usize = 256;

#[derive(Clone)]
pub struct EventBus {
    tx: mpsc::Sender<TradeEvent>,
}

impl EventBus {
    pub fn new() -> (Self, mpsc::Receiver<TradeEvent>) {
        let (tx, rx) = mpsc::channel(DEFAULT_CAPACITY);
        (Self { tx }, rx)
    }

    pub async fn publish(&self, event: TradeEvent) -> anyhow::Result<()> {
        self.tx
            .send(event)
            .await
            .map_err(|e| anyhow::anyhow!("event bus closed: {e}"))
    }
}

pub async fn run_orchestrator(mut rx: mpsc::Receiver<TradeEvent>, ctx: AgentContext) {
    info!("agent orchestrator started");
    while let Some(event) = rx.recv().await {
        if let Err(err) = process_event(&ctx, event).await {
            error!("agent process_event failed: {}", err);
        }
    }
    info!("agent orchestrator stopped");
}

pub fn spawn_orchestrator(
    rx: mpsc::Receiver<TradeEvent>,
    ctx: AgentContext,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        run_orchestrator(rx, ctx).await;
    })
}

pub type SharedBus = Arc<EventBus>;
