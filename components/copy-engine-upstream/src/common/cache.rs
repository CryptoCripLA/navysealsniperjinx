use solana_sdk::pubkey::Pubkey;
use spl_token_2022::{
    extension::StateWithExtensionsOwned,
    state::{Account, Mint},
};
use std::collections::HashMap;
use std::sync::RwLock;

pub struct TokenAccountCache {
    accounts: RwLock<HashMap<Pubkey, StateWithExtensionsOwned<Account>>>,
    mints: RwLock<HashMap<Pubkey, StateWithExtensionsOwned<Mint>>>,
}

impl TokenAccountCache {
    pub fn new() -> Self {
        Self {
            accounts: RwLock::new(HashMap::new()),
            mints: RwLock::new(HashMap::new()),
        }
    }

    pub fn get_account(&self, key: &Pubkey) -> Option<StateWithExtensionsOwned<Account>> {
        self.accounts.read().ok()?.get(key).cloned()
    }

    pub fn set_account(&self, key: Pubkey, value: StateWithExtensionsOwned<Account>) {
        if let Ok(mut map) = self.accounts.write() {
            map.insert(key, value);
        }
    }

    pub fn get_mint(&self, key: &Pubkey) -> Option<StateWithExtensionsOwned<Mint>> {
        self.mints.read().ok()?.get(key).cloned()
    }

    pub fn set_mint(&self, key: Pubkey, value: StateWithExtensionsOwned<Mint>) {
        if let Ok(mut map) = self.mints.write() {
            map.insert(key, value);
        }
    }
}

impl Default for TokenAccountCache {
    fn default() -> Self {
        Self::new()
    }
}

lazy_static::lazy_static! {
    pub static ref TOKEN_ACCOUNT_CACHE: TokenAccountCache = TokenAccountCache::new();
    pub static ref TOKEN_MINT_CACHE: TokenAccountCache = TokenAccountCache::new();
}
