use anyhow::Result;
use solana_client::rpc_client::RpcClient;
use solana_sdk::{instruction::Instruction, signature::Keypair};
use tokio::time::Instant;
use tracing::info;

use crate::core::tx_sender::TxSender;

pub async fn new_signed_and_send(
    client: &RpcClient,
    keypair: &Keypair,
    instructions: Vec<Instruction>,
    _use_jito: bool,
) -> Result<Vec<String>> {
    let start_time = Instant::now();
    let sender = TxSender::from_env();
    let txs = sender
        .send_instructions(client, keypair, instructions)
        .await?;
    info!("tx elapsed: {:?}", start_time.elapsed());
    Ok(txs)
}
