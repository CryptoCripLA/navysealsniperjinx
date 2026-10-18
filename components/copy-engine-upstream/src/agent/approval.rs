use std::collections::HashMap;
use std::env;
use std::time::Duration;

use anyhow::Result;
use rand::Rng;
use tokio::sync::{oneshot, Mutex};
use tracing::{info, warn};

use crate::services::telegram::TelegramNotifier;

use super::events::TradeAction;

pub struct ApprovalService {
    pending: Mutex<HashMap<String, oneshot::Sender<bool>>>,
    timeout_secs: u64,
}

impl ApprovalService {
    pub fn from_env() -> Self {
        let timeout_secs = env::var("AGENT_APPROVAL_TIMEOUT_SECS")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(60);
        Self {
            pending: Mutex::new(HashMap::new()),
            timeout_secs,
        }
    }

    pub async fn request(&self, action: TradeAction) -> TradeAction {
        match &action {
            TradeAction::Skip { .. } => return action,
            _ => {}
        }

        let Some(notifier) = TelegramNotifier::from_env() else {
            warn!("approve mode: telegram not configured — skipping trade");
            return TradeAction::Skip {
                reason: "telegram not configured for approval".into(),
            };
        };

        let id = new_request_id();
        let (tx, rx) = oneshot::channel();
        self.pending.lock().await.insert(id.clone(), tx);

        let text = format!(
            "Trade approval required\n{action}\n\nTap Approve or Reject (expires in {}s)",
            self.timeout_secs
        );
        if let Err(err) = notifier.send_approval_request(&id, &text).await {
            self.pending.lock().await.remove(&id);
            warn!("approval telegram send failed: {}", err);
            return TradeAction::Skip {
                reason: format!("approval notify failed: {err}"),
            };
        }

        info!("approval requested id={}", id);
        match tokio::time::timeout(Duration::from_secs(self.timeout_secs), rx).await {
            Ok(Ok(true)) => {
                info!("approval granted id={}", id);
                action
            }
            Ok(Ok(false)) => TradeAction::Skip {
                reason: "rejected via telegram".into(),
            },
            Ok(Err(_)) => TradeAction::Skip {
                reason: "approval channel closed".into(),
            },
            Err(_) => {
                self.pending.lock().await.remove(&id);
                TradeAction::Skip {
                    reason: format!("approval timeout after {}s", self.timeout_secs),
                }
            }
        }
    }

    pub async fn resolve_callback(&self, id: &str, approved: bool) -> Result<()> {
        if let Some(tx) = self.pending.lock().await.remove(id) {
            let _ = tx.send(approved);
        }
        Ok(())
    }
}

fn new_request_id() -> String {
    let n: u32 = rand::thread_rng().gen();
    format!("{:08x}", n)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn skip_actions_pass_through() {
        let svc = ApprovalService::from_env();
        let action = TradeAction::Skip {
            reason: "test".into(),
        };
        let out = svc.request(action.clone()).await;
        assert_eq!(out, action);
    }
}
