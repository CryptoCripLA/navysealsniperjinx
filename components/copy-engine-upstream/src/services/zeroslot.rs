use crate::common::utils::import_env_var;
use crate::error::ClientError;
use anyhow::{anyhow, Result};
use base64::{engine::general_purpose::STANDARD as BASE64, Engine};
use rand::{seq::IteratorRandom, thread_rng};
use serde_json::{json, Value};
use solana_sdk::{pubkey::Pubkey, signature::Signature, transaction::Transaction};
use std::{str::FromStr, sync::LazyLock};

pub static ZERO_SLOT_URL: LazyLock<String> = LazyLock::new(|| import_env_var("ZERO_SLOT_URL"));

pub fn get_tip_account() -> Result<Pubkey> {
    let accounts = [
        "6fQaVhYZA4w3MBSXjJ81Vf6W1EDYeUPXpgVQ6UQyU1Av",
        "4HiwLEP2Bzqj3hM2ENxJuzhcPCdsafwiet3oGkMkuQY4",
        "7toBU3inhmrARGngC7z6SjyP85HgGMmCTEwGNRAcYnEK",
        "8mR3wB1nh4D6J9RUCugxUpc6ya8w38LPxZ3ZjcBhgzws",
        "6SiVU5WEwqfFapRuYCndomztEwDjvS5xgtEof3PLEGm9",
    ];
    let mut rng = thread_rng();
    let acc = accounts
        .iter()
        .choose(&mut rng)
        .ok_or_else(|| anyhow!("zeroslot: no tip accounts available"))?;
    Ok(Pubkey::from_str(acc)?)
}

pub async fn get_tip_value() -> Result<f64> {
    let tip_value = std::env::var("ZERO_SLOT_TIP_VALUE").unwrap_or_else(|_| "0.001".to_string());
    f64::from_str(&tip_value).map_err(|_| anyhow!("invalid ZERO_SLOT_TIP_VALUE"))
}

pub const MAX_RETRIES: u8 = 3;

#[derive(Debug, Clone)]
pub struct TransactionConfig {
    pub skip_preflight: bool,
    pub encoding: String,
}

impl Default for TransactionConfig {
    fn default() -> Self {
        Self {
            skip_preflight: true,
            encoding: "base64".to_string(),
        }
    }
}

#[derive(Clone, Debug)]
pub struct ZeroSlotClient {
    endpoint: String,
    client: reqwest::Client,
    config: TransactionConfig,
}

impl ZeroSlotClient {
    pub fn new(endpoint: &str) -> Self {
        Self {
            endpoint: endpoint.to_string(),
            client: reqwest::Client::new(),
            config: TransactionConfig::default(),
        }
    }

    pub async fn send_transaction(
        &self,
        transaction: &Transaction,
    ) -> Result<Signature, ClientError> {
        let wire_transaction = bincode::serialize(transaction).map_err(|e| {
            ClientError::Parse("transaction serialization".into(), e.to_string())
        })?;
        let encoded_tx = BASE64.encode(&wire_transaction);

        for retry in 0..MAX_RETRIES {
            match self.try_send_transaction(&encoded_tx).await {
                Ok(signature) => {
                    return Signature::from_str(&signature).map_err(|e| {
                        ClientError::Parse("invalid signature".into(), e.to_string())
                    });
                }
                Err(e) => {
                    if retry == MAX_RETRIES - 1 {
                        return Err(e);
                    }
                }
            }
        }

        Err(ClientError::Other("max retries exceeded".into()))
    }

    async fn try_send_transaction(&self, encoded_tx: &str) -> Result<String, ClientError> {
        let params = json!([
            encoded_tx,
            {
                "skipPreflight": self.config.skip_preflight,
                "encoding": self.config.encoding,
            }
        ]);
        let response = self.send_request("sendTransaction", params).await?;
        response["result"]
            .as_str()
            .map(|s| s.to_string())
            .ok_or_else(|| {
                ClientError::Parse("invalid response".into(), "missing result".into())
            })
    }

    async fn send_request(&self, method: &str, params: Value) -> Result<Value, ClientError> {
        let request_body = json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params
        });
        let response = self
            .client
            .post(&self.endpoint)
            .header("Content-Type", "application/json")
            .json(&request_body)
            .send()
            .await
            .map_err(|e| ClientError::Solana("request failed".into(), e.to_string()))?;
        let response_data: Value = response
            .json()
            .await
            .map_err(|e| ClientError::Parse("invalid json".into(), e.to_string()))?;
        if let Some(error) = response_data.get("error") {
            return Err(ClientError::Solana("rpc error".into(), error.to_string()));
        }
        Ok(response_data)
    }
}
