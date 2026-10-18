use std::{collections::HashMap, net::SocketAddr};
use solana_client::rpc_response::{RpcContactInfo};
use solana_tpu_client::tpu_client::{TpuClient as BlockingTpuClient, TpuClientConfig};
use solana_quic_client::{new_quic_connection_cache, QuicConnectionCache, QuicConnectionManager, QuicConfig, QuicPool};
use solana_connection_cache::connection_cache::NewConnectionConfig;
use solana_connection_cache::client_connection::ClientConnection;
use solana_client::rpc_client::RpcClient as BlockingRpcClient;
use solana_streamer::streamer::StakedNodes;
use std::{net::IpAddr, sync::{Arc, RwLock}};
use std::time::{Duration, Instant};
use solana_keypair::Keypair;
use solana_signature::Signature;
use solana_program::{instruction::Instruction};
use solana_signer::Signer;
use solana_client::nonblocking::rpc_client::RpcClient;
use solana_transaction::versioned::VersionedTransaction;
use solana_message::{VersionedMessage, v0::Message as V0Message};
use crate::mew::sol_hook::compute_budget::{ix_set_compute_unit_limit, ix_set_compute_unit_price};

const SPRAY_MS: u64 = 100;

type QuicTpuClient = BlockingTpuClient<QuicPool, QuicConnectionManager, QuicConfig>;

pub struct SolTpu {
    pub tpu: QuicTpuClient,
    pub cache: Arc<QuicConnectionCache>,
    pub cluster_info: Vec<RpcContactInfo>,
}

fn make_quic_cache() -> anyhow::Result<Arc<QuicConnectionCache>> {
    let staked = Arc::new(RwLock::new(StakedNodes::default()));
    let cache = new_quic_connection_cache(
        "solhook-quic",
        &Keypair::new(),
        IpAddr::from([0,0,0,0]),
        &staked,
        100,
    )?;
    Ok(Arc::new(cache))
}

impl SolTpu {
    pub async fn new(rpc_url: &str, websocket_url: &str) -> anyhow::Result<Self> {
        let rpc = std::sync::Arc::new(
            BlockingRpcClient::new_with_commitment(
                rpc_url.to_string(),
                solana_commitment_config::CommitmentConfig::processed(),
            )
        );

        let quic_mgr = QuicConnectionManager::new_with_connection_config(QuicConfig::new()?);

        let cfg = TpuClientConfig { fanout_slots: 100 };

        let tpu = QuicTpuClient::new("solhook-tpu", rpc.clone(), websocket_url, cfg, quic_mgr)?;
        let cache = make_quic_cache()?;
        let cluster_info = rpc.clone().get_cluster_nodes()?;
        Ok(Self { tpu, cache, cluster_info })
    }

    pub fn new_with_cache(rpc_url: &str, websocket_url: &str) -> anyhow::Result<Self> {
        let rpc   = Arc::new(BlockingRpcClient::new(rpc_url.to_string()));
        let cache = make_quic_cache()?;
        let cfg   = TpuClientConfig { fanout_slots: 100 };
        let tpu   = QuicTpuClient::new_with_connection_cache(rpc.clone(), websocket_url, cfg, cache.clone())?;
        let cluster_info = rpc.clone().get_cluster_nodes()?;
        Ok(Self { tpu, cache, cluster_info })
    }

    pub fn contact_best_tpu(&self, ci: &RpcContactInfo) -> Option<SocketAddr> {
        ci.tpu_quic
            .as_ref()
            .or(ci.tpu_forwards_quic.as_ref())
            .or(ci.tpu.as_ref())
            .or(ci.tpu_forwards.as_ref())
            .and_then(|s| s.to_string().parse().ok())
    }

    pub fn map_node_to_tpu(&self, nodes: &[RpcContactInfo]) -> HashMap<String, SocketAddr> {
        let mut m = HashMap::new();
        for n in nodes {
            if let Some(addr) = self.contact_best_tpu(n) {
                m.insert(n.pubkey.clone(), addr);
            }
        }
        m
    }

    pub async fn get_leader_tpus(
        &self,
        rpc: &RpcClient,
        num_leaders: u64,
    ) -> anyhow::Result<Vec<(String, SocketAddr)>> {
        let nodes = self.cluster_info.clone();
        let node2tpu = self.map_node_to_tpu(&nodes);
        let slot_now = rpc.get_slot().await?;
        let leaders = rpc.get_slot_leaders(slot_now, num_leaders).await?;
    
        let mut out = Vec::with_capacity(num_leaders as usize);
        for leader in leaders {
            let k = leader.to_string();
            if let Some(addr) = node2tpu.get(&k) {
                out.push((k, *addr));
            }
        }
        anyhow::ensure!(!out.is_empty(), "no tpu addresses for current/next leaders");
        Ok(out)
    }

    pub async fn spray_to_targets(
        &self,
        tpu_cache: &solana_quic_client::QuicConnectionCache,
        rpc: &RpcClient,
        wire: Vec<u8>,
    ) -> anyhow::Result<()> {
        let start_time = Instant::now();
        let targets = self.get_leader_tpus(rpc, 2).await?;
        println!("Elapsed: {:?} | Targets: {:?}", start_time.elapsed(), targets.len());
    
        let deadline = Instant::now() + Duration::from_millis(SPRAY_MS);    
        while Instant::now() < deadline {
            for (_, addr) in targets.iter() {
                let conn = tpu_cache.get_connection(addr);
                let _ = conn.send_data_async(wire.clone());
            }
        }
        Ok(())
    }

    pub fn try_send_vtx(
        &self,
        vtx: &solana_transaction::versioned::VersionedTransaction,
    ) -> solana_transaction_error::TransportResult<()> {
        let wire = bincode::serialize(vtx).expect("serialize");
        self.tpu.try_send_wire_transaction(wire)
    }

    pub async fn send(
        &self,
        rpc: &solana_client::nonblocking::rpc_client::RpcClient,
        mut ixs: Vec<Instruction>,
        payer: &Keypair,
        fee: u64,
        compute_budget: u32,
    ) -> anyhow::Result<Signature> {
        use solana_commitment_config::CommitmentConfig;

        ixs.insert(0, ix_set_compute_unit_price(fee));
        ixs.insert(1, ix_set_compute_unit_limit(compute_budget));

        let bh = rpc
            .get_latest_blockhash_with_commitment(CommitmentConfig::processed())
            .await?
            .0;

        let start_slot = rpc
            .get_slot_with_commitment(CommitmentConfig::processed())
            .await?;

        let msg = VersionedMessage::V0(V0Message::try_compile(&payer.pubkey(), &ixs, &[], bh)?);
        let tx  = VersionedTransaction::try_new(msg, &[payer])?;
        let sig = tx.signatures[0];

        let wire = bincode::serialize(&tx)?;
        self.spray_to_targets(&self.cache, rpc, wire).await?;

        let mut tries = 0usize;
        while !rpc
            .confirm_transaction_with_commitment(&sig, CommitmentConfig::confirmed())
            .await?
            .value
        {
            tokio::time::sleep(Duration::from_millis(80)).await;
            tries += 1;
            if tries > 50 {
                anyhow::bail!("tx not confirmed in time: {sig}");
            }
        }

        let end_slot = rpc
            .get_slot_with_commitment(CommitmentConfig::processed())
            .await?;
        println!("Blocks diff: n+{}", end_slot - start_slot);
        Ok(sig)
    }
}

#[cfg(test)]
mod tests {
    use crate::mew::sol_hook::sol::SolHook;
    use crate::mew::sol_hook::pump_fun::PumpFun;
    use crate::mew::config::config;
    use std::str::FromStr;
    use solana_program::pubkey::Pubkey;

    use super::*;

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn test_tpu_pump_buy() {
        let cfg = config();
    
        let sol = SolHook::new(cfg.rpc_url.clone());
        let tpu = SolTpu::new_with_cache(&cfg.rpc_url, &cfg.ws_url).expect("TPU init");
    
        let keypair = Keypair::from_base58_string(
            ""
        );
        let pump = PumpFun::new(Arc::new(keypair.insecure_clone()), Arc::new(sol.clone()));
    
        let mint = Pubkey::from_str("AhMLyXcAzkpHbfH2AxMEdiUCzSmsdtbWSvh953Y9pump").unwrap();
        let bonding_curve = Pubkey::from_str("usAdAyXTZHo8U5JH4ykNi2Cr6fbWyFYXkkhKqFeSXA6").unwrap();
    
        let (state, price) = pump.fetch_price(&bonding_curve).await.unwrap();
    
        let (ixs, recent_fees) = pump.buy(
            &mint,
            &bonding_curve,
            &state.creator,
            0.001,
            1.3,
            price,
            Some(false),
        ).await.unwrap();
    
        let sig = tpu
            .send(&sol.rpc_client, ixs, &keypair, recent_fees, 70_000)
            .await
            .unwrap();
    
        println!("TPU Sig: {sig}");
    }
}