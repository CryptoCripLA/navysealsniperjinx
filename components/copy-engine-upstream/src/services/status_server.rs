use std::env;
use std::net::SocketAddr;
use std::sync::Arc;

use axum::{routing::get, Json, Router};
use serde::Serialize;
use tracing::info;

use crate::agent::bot_status::build_bot_status;
use crate::agent::control::BotControl;
use crate::agent::AgentContext;

#[derive(Clone)]
pub struct StatusState {
    pub ctx: AgentContext,
    pub control: Arc<BotControl>,
    pub target_pubkey: String,
}

#[derive(Serialize)]
pub struct HealthResponse {
    pub ok: bool,
}

pub fn build_router(state: StatusState) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/status", get(status))
        .route("/pnl", get(pnl))
        .with_state(state)
}

async fn health() -> Json<HealthResponse> {
    Json(HealthResponse { ok: true })
}

async fn status(
    axum::extract::State(state): axum::extract::State<StatusState>,
) -> Json<crate::agent::bot_status::BotStatusReport> {
    Json(
        build_bot_status(&state.ctx, &state.control, &state.target_pubkey)
            .await,
    )
}

async fn pnl(
    axum::extract::State(state): axum::extract::State<StatusState>,
) -> Json<serde_json::Value> {
    let report = build_bot_status(&state.ctx, &state.control, &state.target_pubkey).await;
    Json(serde_json::json!({
        "ok": true,
        "pnl": report.pnl,
        "paused": report.paused,
    }))
}

pub async fn run_status_server(state: StatusState) -> anyhow::Result<()> {
    let port = env::var("PORT")
        .or_else(|_| env::var("STATUS_PORT"))
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(8080);
    let addr = SocketAddr::from(([0, 0, 0, 0], port));
    let app = build_router(state);
    info!("status server listening on http://{addr}/status");
    let listener = tokio::net::TcpListener::bind(addr).await?;
    axum::serve(listener, app).await?;
    Ok(())
}

pub fn spawn_status_server(ctx: AgentContext, control: Arc<BotControl>, target_pubkey: String) {
    let state = StatusState {
        ctx,
        control,
        target_pubkey,
    };
    tokio::spawn(async move {
        if let Err(err) = run_status_server(state).await {
            tracing::error!("status server error: {}", err);
        }
    });
}
