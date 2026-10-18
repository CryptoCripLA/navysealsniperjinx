use std::env;

use crate::common::constants::{
    BUY_AMOUNT_SOL, MAX_CONCURRENT_TRADES, STOP_LOSS_PERCENTAGE, TAKE_PROFIT_PERCENTAGE,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AgentMode {
    Rules,
    Llm,
    Approve,
}

impl AgentMode {
    pub fn from_env() -> Self {
        match env::var("AGENT_MODE")
            .unwrap_or_else(|_| "rules".into())
            .to_lowercase()
            .as_str()
        {
            "llm" => Self::Llm,
            "approve" => Self::Approve,
            _ => Self::Rules,
        }
    }
}

#[derive(Clone)]
pub struct TradeConfig {
    pub buy_amount_sol: f64,
    pub max_concurrent_trades: usize,
    pub stop_loss_pct: f64,
    pub take_profit_pct: f64,
    pub slippage: u64,
    pub use_jito: bool,
    pub position_poll_secs: u64,
    pub dry_run: bool,
    pub agent_mode: AgentMode,
    pub positions_db_path: Option<String>,
    pub mint_whitelist: Vec<String>,
    pub mint_blacklist: Vec<String>,
    pub min_target_buy_sol: f64,
    pub copy_sells: bool,
    pub auto_approve: bool,
    pub trailing_stop_pct: f64,
    pub mint_sl_overrides: std::collections::HashMap<String, f64>,
    pub llm_in_approve: bool,
}

impl Default for TradeConfig {
    fn default() -> Self {
        Self {
            buy_amount_sol: BUY_AMOUNT_SOL,
            max_concurrent_trades: MAX_CONCURRENT_TRADES,
            stop_loss_pct: STOP_LOSS_PERCENTAGE,
            take_profit_pct: TAKE_PROFIT_PERCENTAGE,
            slippage: 5,
            use_jito: true,
            position_poll_secs: 5,
            dry_run: false,
            agent_mode: AgentMode::Rules,
            positions_db_path: Some("positions.db".into()),
            mint_whitelist: Vec::new(),
            mint_blacklist: Vec::new(),
            min_target_buy_sol: 0.0,
            copy_sells: true,
            auto_approve: true,
            trailing_stop_pct: 0.0,
            mint_sl_overrides: std::collections::HashMap::new(),
            llm_in_approve: false,
        }
    }
}

impl TradeConfig {
    pub fn from_env() -> Self {
        let mut cfg = Self::default();
        if let Ok(v) = env::var("SLIPPAGE").and_then(|s| s.parse().map_err(|_| env::VarError::NotPresent)) {
            cfg.slippage = v;
        }
        if let Ok(v) = env::var("USE_JITO").and_then(|s| s.parse().map_err(|_| env::VarError::NotPresent)) {
            cfg.use_jito = v;
        }
        if let Ok(v) = env::var("BUY_AMOUNT_SOL").and_then(|s| s.parse().map_err(|_| env::VarError::NotPresent)) {
            cfg.buy_amount_sol = v;
        }
        if let Ok(v) = env::var("MAX_CONCURRENT_TRADES").and_then(|s| s.parse().map_err(|_| env::VarError::NotPresent)) {
            cfg.max_concurrent_trades = v;
        }
        if let Ok(v) = env::var("STOP_LOSS_PERCENTAGE").and_then(|s| s.parse().map_err(|_| env::VarError::NotPresent)) {
            cfg.stop_loss_pct = v;
        }
        if let Ok(v) = env::var("TAKE_PROFIT_PERCENTAGE").and_then(|s| s.parse().map_err(|_| env::VarError::NotPresent)) {
            cfg.take_profit_pct = v;
        }
        if let Ok(v) = env::var("POSITION_POLL_SECS").and_then(|s| s.parse().map_err(|_| env::VarError::NotPresent)) {
            cfg.position_poll_secs = v;
        }
        if let Ok(v) = env::var("DRY_RUN").and_then(|s| s.parse().map_err(|_| env::VarError::NotPresent)) {
            cfg.dry_run = v;
        }
        cfg.agent_mode = AgentMode::from_env();
        cfg.positions_db_path = env::var("POSITIONS_DB_PATH").ok().and_then(|p| {
            if p.is_empty() {
                None
            } else {
                Some(p)
            }
        });
        cfg.mint_whitelist = parse_csv_env("MINT_WHITELIST");
        cfg.mint_blacklist = parse_csv_env("MINT_BLACKLIST");
        if let Ok(v) = env::var("MIN_TARGET_BUY_SOL").and_then(|s| s.parse().map_err(|_| env::VarError::NotPresent)) {
            cfg.min_target_buy_sol = v;
        }
        if let Ok(v) = env::var("COPY_SELLS").and_then(|s| s.parse().map_err(|_| env::VarError::NotPresent)) {
            cfg.copy_sells = v;
        }
        if let Ok(v) = env::var("AGENT_AUTO_APPROVE").and_then(|s| s.parse().map_err(|_| env::VarError::NotPresent)) {
            cfg.auto_approve = v;
        }
        if let Ok(v) = env::var("TRAILING_STOP_PERCENTAGE").and_then(|s| s.parse().map_err(|_| env::VarError::NotPresent)) {
            cfg.trailing_stop_pct = v;
        }
        cfg.mint_sl_overrides = parse_mint_sl_overrides("MINT_STOP_LOSS_OVERRIDES");
        if let Ok(v) = env::var("AGENT_LLM_IN_APPROVE").and_then(|s| s.parse().map_err(|_| env::VarError::NotPresent)) {
            cfg.llm_in_approve = v;
        }
        cfg
    }

    pub fn stop_loss_for_mint(&self, mint: &str) -> f64 {
        self.mint_sl_overrides
            .get(mint)
            .copied()
            .unwrap_or(self.stop_loss_pct)
    }

    pub fn should_stop_loss_for_mint(&self, mint: &str, entry: f64, current: f64) -> bool {
        let pct = self.stop_loss_for_mint(mint);
        entry > 0.0 && ((entry - current) / entry * 100.0) >= pct
    }

    pub fn should_trailing_stop(&self, peak: f64, current: f64) -> bool {
        if self.trailing_stop_pct <= 0.0 || peak <= 0.0 {
            return false;
        }
        current <= peak * (1.0 - self.trailing_stop_pct / 100.0)
    }

    pub fn mint_allowed(&self, mint: &str) -> bool {
        if self.mint_blacklist.iter().any(|m| m == mint) {
            return false;
        }
        if self.mint_whitelist.is_empty() {
            return true;
        }
        self.mint_whitelist.iter().any(|m| m == mint)
    }

    pub fn should_stop_loss(&self, entry: f64, current: f64) -> bool {
        entry > 0.0 && ((entry - current) / entry * 100.0) >= self.stop_loss_pct
    }

    pub fn should_take_profit(&self, entry: f64, current: f64) -> bool {
        entry > 0.0 && ((current - entry) / entry * 100.0) >= self.take_profit_pct
    }
}

fn parse_csv_env(key: &str) -> Vec<String> {
    env::var(key)
        .ok()
        .map(|s| {
            s.split(',')
                .map(|p| p.trim().to_string())
                .filter(|p| !p.is_empty())
                .collect()
        })
        .unwrap_or_default()
}

fn parse_mint_sl_overrides(key: &str) -> std::collections::HashMap<String, f64> {
    let mut map = std::collections::HashMap::new();
    if let Ok(raw) = env::var(key) {
        for part in raw.split(',') {
            let part = part.trim();
            if part.is_empty() {
                continue;
            }
            if let Some((mint, pct)) = part.split_once(':') {
                if let Ok(v) = pct.trim().parse::<f64>() {
                    map.insert(mint.trim().to_string(), v);
                }
            }
        }
    }
    map
}

#[cfg(test)]
mod tests {
    use super::TradeConfig;

    #[test]
    fn stop_loss_triggers_on_drop() {
        let cfg = TradeConfig {
            stop_loss_pct: 20.0,
            take_profit_pct: 50.0,
            ..TradeConfig::default()
        };
        assert!(cfg.should_stop_loss(1.0, 0.79));
        assert!(!cfg.should_stop_loss(1.0, 0.85));
    }

    #[test]
    fn take_profit_triggers_on_gain() {
        let cfg = TradeConfig {
            stop_loss_pct: 20.0,
            take_profit_pct: 50.0,
            ..TradeConfig::default()
        };
        assert!(cfg.should_take_profit(1.0, 1.51));
        assert!(!cfg.should_take_profit(1.0, 1.2));
    }
}
