use crate::common::utils::import_env_var;
use anyhow::{anyhow, Result};
use base64::{engine::general_purpose::STANDARD as BASE64, Engine};
use rand::{seq::IteratorRandom, thread_rng};
use reqwest::Client;
use serde_json::json;
use solana_sdk::{pubkey::Pubkey, signature::Signature, transaction::Transaction};
use std::{str::FromStr, sync::LazyLock};

pub static NOZOMI_URL: LazyLock<String> = LazyLock::new(|| import_env_var("NOZOMI_URL"));

pub fn get_tip_account() -> Result<Pubkey> {
    let accounts = [
        "TEMPaMeCRFAS9EKF53Jd6KpHxgL47uWLcpFArU1Fanq",
        "noz3jAjPiHuBPqiSPkkugaJDkJscPuRhYnSpbi8UvC4",
        "noz3str9KXfpKknefHji8L1mPgimezaiUyCHYMDv1GE",
        "noz6uoYCDijhu1V7cutCpwxNiSovEwLdRHPwmgCGDNo",
        "noz9EPNcT7WH6Sou3sr3GGjHQYVkN3DNirpbvDkv9YJ",
    ];
    let mut rng = thread_rng();
    let acc = accounts
        .iter()
        .choose(&mut rng)
        .ok_or_else(|| anyhow!("nozomi: no tip accounts available"))?;
    Ok(Pubkey::from_str(acc)?)
}

pub async fn get_tip_value() -> Result<f64> {
    let tip_value = std::env::var("NOZOMI_TIP_VALUE").unwrap_or_else(|_| "0.001".to_string());
    f64::from_str(&tip_value).map_err(|_| anyhow!("invalid NOZOMI_TIP_VALUE"))
}

pub struct NozomiClient {
    endpoint: String,
    http: Client,
}

impl NozomiClient {
    pub fn from_env() -> Result<Self> {
        Ok(Self {
            endpoint: NOZOMI_URL.clone(),
            http: Client::new(),
        })
    }

    pub async fn send_transaction(&self, txn: &Transaction) -> Result<Signature> {
        let wire = bincode::serialize(txn).map_err(|e| anyhow!("serialize: {}", e))?;
        let encoded = BASE64.encode(wire);
        let body = json!([
            encoded,
            { "skipPreflight": true, "encoding": "base64" }
        ]);
        let response = self
            .http
            .post(&self.endpoint)
            .header("Content-Type", "application/json")
            .json(&json!({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendTransaction",
                "params": body
            }))
            .send()
            .await?
            .json::<serde_json::Value>()
            .await?;
        let sig = response["result"]
            .as_str()
            .ok_or_else(|| anyhow!("nozomi: missing result"))?;
        Signature::from_str(sig).map_err(|e| anyhow!("nozomi: bad sig: {}", e))
    }
}
