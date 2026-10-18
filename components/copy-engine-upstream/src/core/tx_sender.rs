use std::{env, str::FromStr, sync::Arc, time::Duration};

use anyhow::{anyhow, Result};
use jito_json_rpc_client::jsonrpc_client::rpc_client::RpcClient as JitoRpcClient;
use solana_client::rpc_client::RpcClient;
use solana_sdk::{
    instruction::Instruction,
    signature::Keypair,
    signer::Signer,
    system_transaction,
    transaction::{Transaction, VersionedTransaction},
};
use spl_token::ui_amount_to_amount;
use tracing::{error, info, warn};

use crate::services::{
    bloxroute::BloxrouteClient,
    jito::{self, get_tip_account, get_tip_value, init_tip_accounts, wait_for_bundle_confirmation},
    nozomi::{self, get_tip_value as nozomi_tip, NozomiClient},
    zeroslot::ZeroSlotClient,
};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TxBackend {
    Rpc,
    Jito,
    Nozomi,
    ZeroSlot,
    Bloxroute,
}

impl TxBackend {
    pub fn parse(raw: &str) -> Self {
        match raw.to_lowercase().as_str() {
            "nozomi" => Self::Nozomi,
            "zeroslot" => Self::ZeroSlot,
            "bloxroute" => Self::Bloxroute,
            "jito" => Self::Jito,
            _ => Self::Rpc,
        }
    }

    pub fn from_env() -> Self {
        let raw = env::var("TX_BACKEND").unwrap_or_else(|_| {
            if env::var("USE_JITO")
                .ok()
                .map(|v| v == "true")
                .unwrap_or(true)
            {
                "jito".to_string()
            } else {
                "rpc".to_string()
            }
        });
        Self::parse(&raw)
    }

    fn fallback(&self) -> Option<Self> {
        env::var("TX_BACKEND_FALLBACK")
            .ok()
            .map(|v| Self::parse(&v))
    }
}

fn unit_price() -> u64 {
    env::var("UNIT_PRICE")
        .ok()
        .and_then(|v| u64::from_str(&v).ok())
        .unwrap_or(20_000)
}

fn unit_limit() -> u32 {
    env::var("UNIT_LIMIT")
        .ok()
        .and_then(|v| u32::from_str(&v).ok())
        .unwrap_or(200_000)
}

pub struct TxSender {
    backend: TxBackend,
}

impl TxSender {
    pub fn from_env() -> Self {
        Self {
            backend: TxBackend::from_env(),
        }
    }

    pub async fn send_instructions(
        &self,
        client: &RpcClient,
        keypair: &Keypair,
        mut instructions: Vec<Instruction>,
    ) -> Result<Vec<String>> {
        if !matches!(self.backend, TxBackend::Jito) {
            instructions.insert(
                0,
                solana_sdk::compute_budget::ComputeBudgetInstruction::set_compute_unit_limit(
                    unit_limit(),
                ),
            );
            instructions.insert(
                1,
                solana_sdk::compute_budget::ComputeBudgetInstruction::set_compute_unit_price(
                    unit_price(),
                ),
            );
        }

        let recent_blockhash = client.get_latest_blockhash()?;
        let mut txn = Transaction::new_signed_with_payer(
            &instructions,
            Some(&keypair.pubkey()),
            &[keypair],
            recent_blockhash,
        );

        if env::var("TX_SIMULATE").ok().as_deref() == Some("true") {
            let simulate_result = client.simulate_transaction(&txn)?;
            if let Some(logs) = simulate_result.value.logs {
                for log in logs {
                    info!("{}", log);
                }
            }
            return match simulate_result.value.err {
                Some(err) => Err(anyhow!("{}", err)),
                None => Ok(vec![]),
            };
        }

        match self.send_signed(client, keypair, &mut txn, recent_blockhash).await {
            Ok(sigs) => Ok(sigs),
            Err(err) => {
                if let Some(fallback) = self.backend.fallback() {
                    warn!("tx backend {:?} failed: {} — fallback {:?}", self.backend, err, fallback);
                    TxSender { backend: fallback }
                        .send_signed(client, keypair, &mut txn, recent_blockhash)
                        .await
                } else {
                    Err(err)
                }
            }
        }
    }

    async fn send_signed(
        &self,
        client: &RpcClient,
        keypair: &Keypair,
        txn: &Transaction,
        recent_blockhash: solana_sdk::hash::Hash,
    ) -> Result<Vec<String>> {
        match self.backend {
            TxBackend::Jito => self.send_jito(client, keypair, txn, recent_blockhash).await,
            TxBackend::Nozomi => self.send_nozomi(keypair, txn).await,
            TxBackend::ZeroSlot => self.send_zeroslot(txn).await,
            TxBackend::Bloxroute => self.send_bloxroute(txn).await,
            TxBackend::Rpc => {
                let sig = common::rpc::send_txn(client, txn, true)?;
                Ok(vec![sig.to_string()])
            }
        }
    }

    async fn send_jito(
        &self,
        _client: &RpcClient,
        keypair: &Keypair,
        txn: &Transaction,
        recent_blockhash: solana_sdk::hash::Hash,
    ) -> Result<Vec<String>> {
        init_tip_accounts().await?;
        let tip_account = get_tip_account().await?;
        let mut tip = get_tip_value().await?;
        tip = tip.min(0.1);
        let tip_lamports = ui_amount_to_amount(tip, spl_token::native_mint::DECIMALS);

        let jito_client = Arc::new(JitoRpcClient::new(format!(
            "{}/api/v1/bundles",
            *jito::BLOCK_ENGINE_URL
        )));
        let bundle = vec![
            VersionedTransaction::from(txn.clone()),
            VersionedTransaction::from(system_transaction::transfer(
                keypair,
                &tip_account,
                tip_lamports,
                recent_blockhash,
            )),
        ];
        let bundle_id = jito_client.send_bundle(&bundle).await?;
        info!("jito bundle_id: {}", bundle_id);

        wait_for_bundle_confirmation(
            move |id: String| {
                let client = Arc::clone(&jito_client);
                async move {
                    let response = client.get_bundle_statuses(&[id]).await;
                    let statuses = response.inspect_err(|err| {
                        error!("jito status error: {:?}", err);
                    })?;
                    Ok(statuses.value)
                }
            },
            bundle_id,
            Duration::from_millis(1000),
            Duration::from_secs(10),
        )
        .await
    }

    async fn send_nozomi(&self, keypair: &Keypair, txn: &Transaction) -> Result<Vec<String>> {
        let tip_account = nozomi::get_tip_account()?;
        let tip = nozomi_tip().await?;
        let tip_lamports = ui_amount_to_amount(tip, spl_token::native_mint::DECIMALS);
        let tip_tx = system_transaction::transfer(
            keypair,
            &tip_account,
            tip_lamports,
            txn.message.recent_blockhash,
        );
        let client = NozomiClient::from_env()?;
        let sig = client.send_transaction(txn).await?;
        let _ = client.send_transaction(&tip_tx);
        Ok(vec![sig.to_string()])
    }

    async fn send_zeroslot(&self, txn: &Transaction) -> Result<Vec<String>> {
        let url = crate::services::zeroslot::ZERO_SLOT_URL.clone();
        let client = ZeroSlotClient::new(&url);
        let sig = client
            .send_transaction(txn)
            .await
            .map_err(|e| anyhow!("zeroslot: {}", e))?;
        Ok(vec![sig.to_string()])
    }

    async fn send_bloxroute(&self, txn: &Transaction) -> Result<Vec<String>> {
        let client = BloxrouteClient::from_env()?;
        let sig = client
            .send_transaction(txn)
            .await
            .map_err(|e| anyhow!("bloxroute: {}", e))?;
        Ok(vec![sig.to_string()])
    }
}

#[cfg(test)]
mod tests {
    use super::TxBackend;

    #[test]
    fn parses_tx_backends() {
        assert_eq!(TxBackend::parse("jito"), TxBackend::Jito);
        assert_eq!(TxBackend::parse("NOZOMI"), TxBackend::Nozomi);
        assert_eq!(TxBackend::parse("zeroslot"), TxBackend::ZeroSlot);
        assert_eq!(TxBackend::parse("unknown"), TxBackend::Rpc);
    }
}
