///
///  Vacation = Validator Location
/// 

use reqwest::Client;
use solana_commitment_config::CommitmentConfig;
use std::{collections::HashMap, sync::Arc, io::{self}};
use solana_client::rpc_response::RpcContactInfo;
use std::net::SocketAddr;
use crate::mew::sol_hook::sol::SolHook;
use serde_json::Value;
use sqlx::{Pool, Postgres, FromRow};
use sqlx::postgres::{PgPoolOptions};
use std::time::Duration;    
use crate::mew::writing::{cc, Colors};
use crate::mew::config;
use crate::log;
#[derive(Clone)]
pub struct Vacation {
    web_client: Client,
    pub cache: Arc<HashMap<String, SocketAddr>>,
    pub sol_hook: SolHook,
    pub db_pool: Option<Pool<Postgres>>,
    pub regions: Option<Vec<String>>,
}
#[derive(Debug, Clone, FromRow)]
pub struct VacationEntry {
    pub pubkey: String,
    pub city: String,
    pub country: String,
}

impl Vacation {
    pub fn new(sol_hook: SolHook) -> Self {
        Self {
            web_client: Client::new(),
            cache: Arc::new(HashMap::new()),
            sol_hook: sol_hook,
            db_pool: None,
            regions: None,
        }
    }

    pub async fn initialize(mut self, db_url: &str) -> Self {
        let cluster_info = self.sol_hook.rpc_client.get_cluster_nodes()
            .await
            .unwrap();
        let node2tpu = self.map_node_to_tpu(&cluster_info);
        self.cache = Arc::new(node2tpu);
        self.regions = config::get_regions();
        let db_res = self.connect_db(&db_url).await;
        match db_res {
            Ok(_) => {
                self.ensure_table().await.unwrap();
            }
            Err(_) => {
                eprintln!("Failed to connect to database.");
            }
        }
        self
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

    pub async fn get_leaders(&self, num_leaders: u64) -> Vec<(String, SocketAddr)> {
        let slot_now = self.sol_hook.rpc_client.get_slot_with_commitment(CommitmentConfig::processed()).await.unwrap();
        let leaders = self.sol_hook.rpc_client.get_slot_leaders(slot_now, num_leaders).await.unwrap();
        let mut out = Vec::with_capacity(num_leaders as usize);
        for leader in leaders {
            let k = leader.to_string();
            if let Some(addr) = self.cache.get(&k) {
                out.push((k, *addr));
            }
        }
        out
    }

    /// Resolver using free GeoIP providers.
    /// Tries providers in order and returns the first successful result.
    /// Providers used:
    /// - ipwho.is
    /// - ipapi.co
    /// - ip-api.com
    pub async fn get_location(&self, ip: &str) -> anyhow::Result<(String, String)> {
        if ip.is_empty() {
            anyhow::bail!("empty ip");
        }

        if let Some(loc) = self.fetch_ipwho_is(ip).await? { return Ok(loc); }
        if let Some(loc) = self.fetch_geojs(ip).await?     { return Ok(loc); }
        if let Some(loc) = self.fetch_ipapi_co(ip).await?  { return Ok(loc); }
        if let Some(loc) = self.fetch_ip_api(ip).await?    { return Ok(loc); }

        anyhow::bail!(format!("geoip: all providers failed for {ip}"))
    }

    async fn fetch_ipwho_is(&self, ip: &str) -> anyhow::Result<Option<(String, String)>> {
        let url = format!("https://ipwho.is/{ip}?fields=success,message,city,country");
        let resp = match self.web_client
            .get(url)
            .timeout(Duration::from_millis(1500))
            .send()
            .await
        {
            Ok(r) => r,
            Err(_) => return Ok(None),
        };
        let v: Value = match resp.json().await { Ok(v) => v, Err(_) => return Ok(None) };
        let ok = v.get("success").and_then(|b| b.as_bool()).unwrap_or(false);
        if !ok { return Ok(None); }
        let city = v.get("city").and_then(|s| s.as_str()).unwrap_or("").trim();
        let country = v.get("country").and_then(|s| s.as_str()).unwrap_or("").trim();
        if city.is_empty() || country.is_empty() { return Ok(None); }
        Ok(Some((city.to_string(), country.to_string())))
    }

    async fn fetch_ipapi_co(&self, ip: &str) -> anyhow::Result<Option<(String, String)>> {
        let url = format!("https://ipapi.co/{ip}/json/");
        let resp = match self.web_client
            .get(url)
            .timeout(Duration::from_millis(1500))
            .send()
            .await
        {
            Ok(r) => r,
            Err(_) => return Ok(None),
        };
        let v: Value = match resp.json().await { Ok(v) => v, Err(_) => return Ok(None) };
        // ipapi.co returns { error: true, reason: "..." } on failure
        if v.get("error").and_then(|b| b.as_bool()).unwrap_or(false) { return Ok(None); }
        let city = v.get("city").and_then(|s| s.as_str()).unwrap_or("").trim();
        // country_name has full name; country gives ISO code
        let country = v.get("country_name").and_then(|s| s.as_str()).unwrap_or("").trim();
        if city.is_empty() || country.is_empty() { return Ok(None); }
        Ok(Some((city.to_string(), country.to_string())))
    }

    async fn fetch_ip_api(&self, ip: &str) -> anyhow::Result<Option<(String, String)>> {
        // Free tier over HTTP only
        let url = format!("http://ip-api.com/json/{ip}?fields=status,message,city,country");
        let resp = match self.web_client
            .get(url)
            .timeout(Duration::from_millis(1500))
            .send()
            .await
        {
            Ok(r) => r,
            Err(_) => return Ok(None),
        };
        let v: Value = match resp.json().await { Ok(v) => v, Err(_) => return Ok(None) };
        let status = v.get("status").and_then(|s| s.as_str()).unwrap_or("");
        if status != "success" { return Ok(None); }
        let city = v.get("city").and_then(|s| s.as_str()).unwrap_or("").trim();
        let country = v.get("country").and_then(|s| s.as_str()).unwrap_or("").trim();
        if city.is_empty() || country.is_empty() { return Ok(None); }
        Ok(Some((city.to_string(), country.to_string())))
    }

    async fn fetch_geojs(&self, ip: &str) -> anyhow::Result<Option<(String, String)>> {
        // https://get.geojs.io/v1/ip/geo/{ip}.json
        let url = format!("https://get.geojs.io/v1/ip/geo/{ip}.json");
        let resp = match self.web_client
            .get(url)
            .timeout(Duration::from_millis(1500))
            .send()
            .await
        {
            Ok(r) => r,
            Err(_) => return Ok(None),
        };

        // geojs always returns JSON; no explicit "success" flag
        let v: Value = match resp.json().await { Ok(v) => v, Err(_) => return Ok(None) };

        let city    = v.get("city").and_then(|s| s.as_str()).unwrap_or("").trim();
        let country = v.get("country").and_then(|s| s.as_str()).unwrap_or("").trim();

        if city.is_empty() || country.is_empty() {
            return Ok(None);
        }
        Ok(Some((city.to_string(), country.to_string())))
    }

    /// Connect to PostgreSQL and store the pool inside `Vacation`.
    pub async fn connect_db(&mut self, database_url: &str) -> anyhow::Result<()> {
        let pool = PgPoolOptions::new()
            .max_connections(5)
            .connect(database_url)
            .await?;
        self.db_pool = Some(pool);
        Ok(())
    }

    fn db(&self) -> anyhow::Result<&Pool<Postgres>> {
        match &self.db_pool {
            Some(p) => Ok(p),
            None => anyhow::bail!("database is not connected; call connect_db() first"),
        }
    }

    pub async fn ensure_table(&self) -> anyhow::Result<()> {
        let sql = r#"
            CREATE TABLE IF NOT EXISTS vacation (
                pubkey  TEXT PRIMARY KEY,
                city    TEXT NOT NULL,
                country TEXT NOT NULL
            )
        "#;
        sqlx::query(sql).execute(self.db()?).await?;
        Ok(())
    }

    pub async fn upsert_vacation(
        &self,
        pubkey: &str,
        city: &str,
        country: &str,
    ) -> anyhow::Result<()> {
        let sql = r#"
            INSERT INTO vacation (pubkey, city, country)
            VALUES ($1, $2, $3)
            ON CONFLICT(pubkey) DO UPDATE SET city = excluded.city, country = excluded.country
        "#;
        sqlx::query(sql)
            .bind(pubkey)
            .bind(city)
            .bind(country)
            .execute(self.db()?)
            .await?;
        Ok(())
    }

    pub async fn get_vacation(&self, pubkey: &str) -> anyhow::Result<Option<VacationEntry>> {
        let sqlq = r#"SELECT pubkey, city, country FROM vacation WHERE pubkey = $1"#;
        let res = sqlx::query_as::<_, VacationEntry>(sqlq)
            .bind(pubkey)
            .fetch_optional(self.db()?)
            .await?;
        Ok(res)
    }
    
    pub async fn get_vacation_all(&self) -> anyhow::Result<Vec<VacationEntry>> {
        let sqlq = r#"SELECT pubkey, city, country FROM vacation"#;
        let res = sqlx::query_as::<_, VacationEntry>(sqlq)
            .fetch_all(self.db()?)
            .await?;
        Ok(res)
    }

    /// Delay is in milliseconds.
    pub async fn fill(&self, delay: Option<u64>) -> anyhow::Result<()> {
        let delay = delay.unwrap_or(1);
        let mut colors = Colors::new(io::stdout().lock());
        let all = self.get_vacation_all().await?;
        let all_pubs = all.iter()
            .map(|x| x.pubkey.clone())
            .collect::<Vec<String>>();
    
        let cluster_info = &self.cache;
        for (pubkey, addr) in cluster_info.iter() {
            if all_pubs.contains(pubkey) {
                continue;
            }
            let ip = addr.ip().to_string();
            match self.get_location(&ip).await {
                Ok(loc) => {
                    colors.cprint(&format!("Indexing {} @ {} -> {:?}", pubkey, ip, loc), cc::MAGENTA);
                    self.upsert_vacation(pubkey, &loc.0, &loc.1).await?;
                }
                Err(e) => colors.cprint(&format!("geoip failed for {}: {}", ip, e), cc::RED),
            }
            tokio::time::sleep(Duration::from_millis(delay)).await;
        }
        Ok(())
    }

    /// Get the upcoming leaders and their vacation.
    /// Returns a vector of tuples containing:
    /// - Slot number
    /// - City name
    /// - Country name
    /// 
    /// The vector is sorted by slot number.
    /// 
    /// # Errors
    /// 
    /// Returns an error if:
    /// - The database connection fails
    /// - The RPC client fails to get the current slot
    pub async fn get_upcoming_leaders_vacation(&self, num_leaders: u64) -> anyhow::Result<Vec<(u64, String, String)>> {
        let leaders = self.get_leaders(num_leaders).await;
        let mut out = Vec::new();
        let mut slot: u64 = self.sol_hook.rpc_client.get_slot_with_commitment(CommitmentConfig::processed()).await.unwrap();
        for (pubkey, _) in leaders {
            let loc = self.get_vacation(&pubkey).await?;
            if let Some(loc) = loc {
                out.push((slot, loc.city, loc.country));
            } else {
                log!(cc::RED, "Unknown vacation for {}", pubkey);
                out.push((slot, "Unknown".to_string(), "Unknown".to_string()));
            }
            slot += 1;
        }
        Ok(out)
    }

    pub async fn is_leader_active_region(&self) -> anyhow::Result<(bool, Vec<(u64, String, String)>)> {
        let regions = self.regions.clone().unwrap_or_default();
        let leaders = self.get_upcoming_leaders_vacation(2).await?; // 2 slots ahead
        let is_region = leaders.iter()
            .all(|(_, city, country)| regions.contains(city) || regions.contains(country));
        Ok((is_region, leaders))
    }
}