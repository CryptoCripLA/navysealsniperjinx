use std::collections::HashMap;
use std::sync::Arc;

use anyhow::Result;
use colored::Colorize;
use solana_client::nonblocking::rpc_client::RpcClient;
use solana_sdk::pubkey::Pubkey;
use spl_token_2022::{
    extension::StateWithExtensionsOwned,
    state::{Account, Mint},
};

use crate::common::cache::{TOKEN_ACCOUNT_CACHE, TOKEN_MINT_CACHE};
use crate::common::logger::Logger;

pub struct BatchRpcClient {
    rpc_client: Arc<RpcClient>,
    logger: Logger,
}

impl BatchRpcClient {
    pub fn new(rpc_client: Arc<RpcClient>) -> Self {
        Self {
            rpc_client,
            logger: Logger::new("[BATCH-RPC] => ".cyan().to_string()),
        }
    }

    pub async fn get_multiple_token_accounts(
        &self,
        mint: &Pubkey,
        accounts: &[Pubkey],
    ) -> Result<HashMap<Pubkey, StateWithExtensionsOwned<Account>>> {
        let mut result = HashMap::new();
        let mut accounts_to_fetch = Vec::new();

        for key in accounts {
            if let Some(cached) = TOKEN_ACCOUNT_CACHE.get_account(key) {
                result.insert(*key, cached);
            } else {
                accounts_to_fetch.push(*key);
            }
        }

        if accounts_to_fetch.is_empty() {
            return Ok(result);
        }

        self.logger
            .log(format!("fetching {} token accounts", accounts_to_fetch.len()));
        let fetched = self
            .rpc_client
            .get_multiple_accounts(&accounts_to_fetch)
            .await?;

        for (i, maybe_account) in fetched.iter().enumerate() {
            let key = accounts_to_fetch[i];
            if let Some(account) = maybe_account {
                if account.owner == spl_token::ID {
                    let parsed = StateWithExtensionsOwned::<Account>::unpack(account.data.clone())?;
                    if parsed.base.mint == *mint {
                        TOKEN_ACCOUNT_CACHE.set_account(key, parsed.clone());
                        result.insert(key, parsed);
                    }
                }
            }
        }

        Ok(result)
    }

    pub async fn get_multiple_mints(
        &self,
        mints: &[Pubkey],
    ) -> Result<HashMap<Pubkey, StateWithExtensionsOwned<Mint>>> {
        let mut result = HashMap::new();
        let mut to_fetch = Vec::new();

        for key in mints {
            if let Some(cached) = TOKEN_MINT_CACHE.get_mint(key) {
                result.insert(*key, cached);
            } else {
                to_fetch.push(*key);
            }
        }

        if to_fetch.is_empty() {
            return Ok(result);
        }

        let fetched = self.rpc_client.get_multiple_accounts(&to_fetch).await?;
        for (i, maybe_account) in fetched.iter().enumerate() {
            let key = to_fetch[i];
            if let Some(account) = maybe_account {
                if account.owner == spl_token::ID {
                    let parsed = StateWithExtensionsOwned::<Mint>::unpack(account.data.clone())?;
                    TOKEN_MINT_CACHE.set_mint(key, parsed.clone());
                    result.insert(key, parsed);
                }
            }
        }

        Ok(result)
    }
}

pub fn create_batch_client(rpc_client: Arc<RpcClient>) -> BatchRpcClient {
    BatchRpcClient::new(rpc_client)
}
