use std::collections::HashMap;
use std::sync::{Arc, RwLock};
use std::time::{SystemTime, UNIX_EPOCH};

use tracing::info;

use crate::engine::position_store::PositionStore;
use crate::engine::strategy::TradeConfig;

#[derive(Debug, Clone)]
pub struct Position {
    pub mint: String,
    pub entry_sol: f64,
    pub peak_value_sol: f64,
    pub token_balance_raw: u64,
    pub decimals: u8,
    pub opened_at: u64,
}

pub struct PositionTracker {
    open: RwLock<HashMap<String, Position>>,
    config: TradeConfig,
    store: Option<Arc<PositionStore>>,
}

impl PositionTracker {
    pub fn new(config: TradeConfig) -> Self {
        Self {
            open: RwLock::new(HashMap::new()),
            config,
            store: None,
        }
    }

    pub fn with_store(config: TradeConfig, store: Arc<PositionStore>) -> Self {
        Self {
            open: RwLock::new(HashMap::new()),
            config,
            store: Some(store),
        }
    }

    pub fn load_from_store(&self) -> anyhow::Result<usize> {
        let Some(store) = &self.store else {
            return Ok(0);
        };
        let loaded = store.load_all()?;
        let count = loaded.len();
        if let Ok(mut map) = self.open.write() {
            for pos in loaded {
                map.insert(pos.mint.clone(), pos);
            }
        }
        if count > 0 {
            info!("restored {} open position(s) from disk", count);
        }
        Ok(count)
    }

    pub fn on_buy(
        &self,
        mint: &str,
        entry_sol: f64,
        token_balance_raw: u64,
        decimals: u8,
    ) {
        let opened_at = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        let pos = Position {
            mint: mint.to_string(),
            entry_sol,
            peak_value_sol: entry_sol,
            token_balance_raw,
            decimals,
            opened_at,
        };
        self.persist_position(&pos);
    }

    pub fn on_sell(&self, mint: &str) -> Option<Position> {
        let removed = self.open.write().ok()?.remove(mint);
        if removed.is_some() {
            if let Some(store) = &self.store {
                if let Err(err) = store.remove(mint) {
                    tracing::warn!("position remove failed {}: {}", mint, err);
                }
            }
        }
        removed
    }

    pub fn record_valuation(&self, mint: &str, current_value_sol: f64) -> Option<Position> {
        let mut map = self.open.write().ok()?;
        let pos = map.get_mut(mint)?;
        if current_value_sol > pos.peak_value_sol {
            pos.peak_value_sol = current_value_sol;
        }
        let snapshot = pos.clone();
        drop(map);
        self.persist_position(&snapshot);
        Some(snapshot)
    }

    fn persist_position(&self, pos: &Position) {
        if let Ok(mut map) = self.open.write() {
            map.insert(pos.mint.clone(), pos.clone());
        }
        if let Some(store) = &self.store {
            if let Err(err) = store.upsert(pos) {
                tracing::warn!("position persist failed {}: {}", pos.mint, err);
            }
        }
    }

    pub fn get(&self, mint: &str) -> Option<Position> {
        self.open.read().ok()?.get(mint).cloned()
    }

    pub fn open_count(&self) -> usize {
        self.open.read().map(|m| m.len()).unwrap_or(0)
    }

    pub fn can_open_new(&self) -> bool {
        self.open_count() < self.config.max_concurrent_trades
    }

    pub fn should_exit(&self, mint: &str, current_value_sol: f64) -> Option<&'static str> {
        let map = self.open.read().ok()?;
        let pos = map.get(mint)?;
        if self.config.should_take_profit(pos.entry_sol, current_value_sol) {
            return Some("take_profit");
        }
        if self
            .config
            .should_stop_loss_for_mint(mint, pos.entry_sol, current_value_sol)
        {
            return Some("stop_loss");
        }
        if self
            .config
            .should_trailing_stop(pos.peak_value_sol, current_value_sol)
        {
            return Some("trailing_stop");
        }
        None
    }

    pub fn list_open(&self) -> Vec<Position> {
        self.open
            .read()
            .map(|m| m.values().cloned().collect())
            .unwrap_or_default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::engine::position_store::PositionStore;

    #[test]
    fn tracks_open_and_close() {
        let tracker = PositionTracker::new(TradeConfig::default());
        tracker.on_buy("mint1", 0.1, 1_000_000, 6);
        assert_eq!(tracker.open_count(), 1);
        assert!(tracker.get("mint1").is_some());
        tracker.on_sell("mint1");
        assert_eq!(tracker.open_count(), 0);
    }

    #[test]
    fn respects_max_concurrent() {
        let mut cfg = TradeConfig::default();
        cfg.max_concurrent_trades = 1;
        let tracker = PositionTracker::new(cfg);
        tracker.on_buy("a", 0.1, 100, 6);
        assert!(!tracker.can_open_new());
    }

    #[test]
    fn should_exit_take_profit() {
        let tracker = PositionTracker::new(TradeConfig::default());
        tracker.on_buy("mint1", 1.0, 1_000_000, 6);
        assert_eq!(tracker.should_exit("mint1", 1.6), Some("take_profit"));
        assert_eq!(tracker.should_exit("mint1", 1.1), None);
    }

    #[test]
    fn should_exit_stop_loss() {
        let tracker = PositionTracker::new(TradeConfig::default());
        tracker.on_buy("mint1", 1.0, 1_000_000, 6);
        assert_eq!(tracker.should_exit("mint1", 0.7), Some("stop_loss"));
    }

    #[test]
    fn trailing_stop_from_peak() {
        let mut cfg = TradeConfig::default();
        cfg.trailing_stop_pct = 10.0;
        cfg.take_profit_pct = 100.0;
        let tracker = PositionTracker::new(cfg);
        tracker.on_buy("mint1", 1.0, 1_000_000, 6);
        tracker.record_valuation("mint1", 1.2);
        assert_eq!(tracker.should_exit("mint1", 1.15), None);
        assert_eq!(tracker.should_exit("mint1", 1.07), Some("trailing_stop"));
    }

    #[test]
    fn per_mint_sl_override() {
        let mut cfg = TradeConfig::default();
        cfg.stop_loss_pct = 20.0;
        cfg.mint_sl_overrides.insert("strict".into(), 5.0);
        let tracker = PositionTracker::new(cfg);
        tracker.on_buy("strict", 1.0, 1_000_000, 6);
        assert_eq!(tracker.should_exit("strict", 0.94), Some("stop_loss"));
    }

    #[test]
    fn loads_from_store_on_startup() {
        let dir = std::env::temp_dir().join(format!("pos_tracker_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("tracker.db");
        {
            let store = Arc::new(PositionStore::open(&path).unwrap());
            store
                .upsert(&Position {
                    mint: "restored".into(),
                    entry_sol: 0.2,
                    peak_value_sol: 0.2,
                    token_balance_raw: 500,
                    decimals: 6,
                    opened_at: 1,
                })
                .unwrap();
        }
        let store = Arc::new(PositionStore::open(&path).unwrap());
        let tracker = PositionTracker::with_store(TradeConfig::default(), store);
        assert_eq!(tracker.load_from_store().unwrap(), 1);
        assert_eq!(tracker.open_count(), 1);
        let _ = std::fs::remove_dir_all(dir);
    }
}
