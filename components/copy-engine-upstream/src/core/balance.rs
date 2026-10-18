use anyhow::Result;
use solana_client::rpc_client::RpcClient;
use solana_sdk::pubkey::Pubkey;
use spl_associated_token_account::get_associated_token_address;

pub fn fetch_token_balance(rpc: &RpcClient, owner: &Pubkey, mint: &Pubkey) -> Result<(u64, u8)> {
    let ata = get_associated_token_address(owner, mint);
    match rpc.get_token_account_balance(&ata) {
        Ok(balance) => {
            let amount: u64 = balance.amount.parse()?;
            Ok((amount, balance.decimals))
        }
        Err(_) => Ok((0, 0)),
    }
}
