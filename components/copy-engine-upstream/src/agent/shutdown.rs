use std::sync::Arc;

use tokio::sync::watch;
use tracing::info;

#[derive(Clone)]
pub struct Shutdown {
    tx: watch::Sender<bool>,
    rx: watch::Receiver<bool>,
}

impl Shutdown {
    pub fn new() -> Arc<Self> {
        let (tx, rx) = watch::channel(false);
        Arc::new(Self { tx, rx })
    }

    pub fn subscribe(&self) -> watch::Receiver<bool> {
        self.rx.clone()
    }

    pub fn trigger(&self) {
        let _ = self.tx.send(true);
    }

    pub fn is_triggered(&self) -> bool {
        *self.rx.borrow()
    }
}

pub fn spawn_shutdown_listener(shutdown: Arc<Shutdown>) {
    tokio::spawn(async move {
        tokio::select! {
            _ = tokio::signal::ctrl_c() => {
                info!("ctrl-c received — shutting down");
            }
            _ = wait_sigterm() => {
                info!("sigterm received — shutting down");
            }
        }
        shutdown.trigger();
    });
}

async fn wait_sigterm() {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{signal, SignalKind};
        if let Ok(mut stream) = signal(SignalKind::terminate()) {
            stream.recv().await;
        }
    }
    #[cfg(not(unix))]
    {
        std::future::pending::<()>().await;
    }
}
