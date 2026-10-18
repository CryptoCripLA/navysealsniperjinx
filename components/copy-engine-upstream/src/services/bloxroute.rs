use crate::error::ClientError;
use anyhow::Result;
use base64::{engine::general_purpose::STANDARD as BASE64, Engine};
use reqwest::Client;
use serde_json::json;
use solana_sdk::{signature::Signature, transaction::Transaction};
use std::{env, str::FromStr, sync::LazyLock};

pub static BLOXROUTE_URL: LazyLock<String> = LazyLock::new(|| {
    env::var("BLOXROUTE_URL").unwrap_or_else(|_| "https://sol.api.blxrbdn.com".to_string())
});

pub struct BloxrouteClient {
    endpoint: String,
    auth: String,
    client: Client,
}

impl BloxrouteClient {
    pub fn from_env() -> Result<Self> {
        let auth = env::var("BLOXROUTE_AUTH")
            .map_err(|_| anyhow::anyhow!("BLOXROUTE_AUTH not set"))?;
        Ok(Self {
            endpoint: BLOXROUTE_URL.clone(),
            auth,
            client: Client::new(),
        })
    }

    pub async fn send_transaction(&self, transaction: &Transaction) -> Result<Signature, ClientError> {
        let wire = bincode::serialize(transaction).map_err(|e| {
            ClientError::Parse("transaction serialization".into(), e.to_string())
        })?;
        let body = json!({
            "transaction": { "content": BASE64.encode(wire) },
            "frontRunningProtection": false,
            "useStakedRPCs": true
        });
        let response = self
            .client
            .post(format!("{}/api/v2/submit", self.endpoint))
            .header("Authorization", &self.auth)
            .header("Content-Type", "application/json")
            .json(&body)
            .send()
            .await
            .map_err(|e| ClientError::Solana("bloxroute request".into(), e.to_string()))?;
        let data: serde_json::Value = response
            .json()
            .await
            .map_err(|e| ClientError::Parse("bloxroute json".into(), e.to_string()))?;
        let sig = data["signature"]
            .as_str()
            .ok_or_else(|| ClientError::Parse("bloxroute response".into(), "no signature".into()))?;
        Signature::from_str(sig).map_err(|e| ClientError::Parse("signature".into(), e.to_string()))
    }
}
