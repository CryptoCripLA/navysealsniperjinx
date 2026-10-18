use anyhow::{Context, Result};
use solana_sdk::{commitment_config::CommitmentConfig, signature::Keypair, signer::keypair::keypair_from_seed};
use std::{env, sync::Arc};

#[derive(Clone)]
pub struct AppState {
    pub rpc_client: Arc<solana_client::rpc_client::RpcClient>,
    pub rpc_nonblocking_client: Arc<solana_client::nonblocking::rpc_client::RpcClient>,
    pub wallet: Arc<Keypair>,
}

pub struct ParseTx {
    pub type_tx: String,
    pub direction: Option<String>,
    pub amount_in: f64,
    pub amount_out: f64,
    pub mint: String,
}

pub fn require_env_var(key: &str) -> Result<String> {
    let val = env::var(key).with_context(|| format!("{key} not set"))?;
    let trimmed = val.trim().to_string();
    if trimmed.is_empty() {
        anyhow::bail!("{key} is set but empty — fill it in .env");
    }
    Ok(trimmed)
}

pub fn require_wss_url(key: &str) -> Result<String> {
    let url = require_env_var(key)?;
    if !url.starts_with("ws://") && !url.starts_with("wss://") {
        anyhow::bail!(
            "{key} must start with ws:// or wss:// (Helius example: wss://mainnet.helius-rpc.com/?api-key=YOUR_KEY)"
        );
    }
    Ok(url)
}

pub fn require_https_url(key: &str) -> Result<String> {
    let url = require_env_var(key)?;
    if !url.starts_with("http://") && !url.starts_with("https://") {
        anyhow::bail!(
            "{key} must start with http:// or https:// (Helius example: https://mainnet.helius-rpc.com/?api-key=YOUR_KEY)"
        );
    }
    Ok(url)
}

pub fn import_env_var(key: &str) -> String {
    require_env_var(key).unwrap_or_else(|err| panic!("{err}"))
}

pub fn create_rpc_client() -> Result<Arc<solana_client::rpc_client::RpcClient>> {
    let rpc_https = require_https_url("RPC_ENDPOINT")?;
    let rpc_client = solana_client::rpc_client::RpcClient::new_with_commitment(
        rpc_https,
        CommitmentConfig::processed(),
    );
    Ok(Arc::new(rpc_client))
}

pub async fn create_nonblocking_rpc_client(
) -> Result<Arc<solana_client::nonblocking::rpc_client::RpcClient>> {
    let rpc_https = require_https_url("RPC_ENDPOINT")?;
    let rpc_client = solana_client::nonblocking::rpc_client::RpcClient::new_with_commitment(
        rpc_https,
        CommitmentConfig::processed(),
    );
    Ok(Arc::new(rpc_client))
}

pub fn import_wallet() -> Result<Arc<Keypair>> {
    let priv_key = require_env_var("PRIVATE_KEY")?;
    let bytes = bs58::decode(&priv_key)
        .into_vec()
        .map_err(|err| anyhow::anyhow!("PRIVATE_KEY invalid base58: {err}"))?;

    let keypair = match bytes.len() {
        64 => Keypair::from_bytes(&bytes)
            .map_err(|err| anyhow::anyhow!("PRIVATE_KEY invalid 64-byte keypair: {err}"))?,
        32 => keypair_from_seed(&bytes)
            .map_err(|err| anyhow::anyhow!("PRIVATE_KEY invalid 32-byte seed: {err}"))?,
        n => anyhow::bail!(
            "PRIVATE_KEY decoded to {n} bytes — expected 64 (full keypair) or 32 (seed)"
        ),
    };

    Ok(Arc::new(keypair))
}

pub fn validate_startup_env() -> Result<()> {
    let devnet = env::var("AGENT_NETWORK")
        .map(|v| v.eq_ignore_ascii_case("devnet"))
        .unwrap_or(false);

    if devnet {
        require_env_var("RPC_ENDPOINT")?;
        require_env_var("RPC_WEBSOCKET_ENDPOINT")?;
    } else {
        require_https_url("RPC_ENDPOINT")?;
        require_wss_url("RPC_WEBSOCKET_ENDPOINT")?;
    }

    require_env_var("PRIVATE_KEY")?;
    require_env_var("SOL_PUBKEY")?;
    require_env_var("TARGET_PUBKEY")?;
    require_env_var("JUP_PUBKEY")?;
    import_wallet().context("invalid PRIVATE_KEY")?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::require_env_var;

    #[test]
    fn rejects_empty_env_value() {
        std::env::set_var("TEST_EMPTY_VAR", "   ");
        let err = require_env_var("TEST_EMPTY_VAR").unwrap_err();
        assert!(err.to_string().contains("empty"));
        std::env::remove_var("TEST_EMPTY_VAR");
    }
}
