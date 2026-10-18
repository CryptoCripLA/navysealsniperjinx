use std::path::Path;
use std::sync::Mutex;

use anyhow::{Context, Result};
use rusqlite::{params, Connection};

use super::positions::Position;

pub struct PositionStore {
    conn: Mutex<Connection>,
}

impl PositionStore {
    pub fn open(path: impl AsRef<Path>) -> Result<Self> {
        let conn = Connection::open(path.as_ref())
            .with_context(|| format!("open positions db {:?}", path.as_ref()))?;
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS positions (
                mint TEXT PRIMARY KEY,
                entry_sol REAL NOT NULL,
                token_balance_raw INTEGER NOT NULL,
                decimals INTEGER NOT NULL,
                opened_at INTEGER NOT NULL,
                peak_value_sol REAL NOT NULL DEFAULT 0
            );",
        )?;
        let _ = conn.execute(
            "ALTER TABLE positions ADD COLUMN peak_value_sol REAL NOT NULL DEFAULT 0",
            [],
        );
        Ok(Self {
            conn: Mutex::new(conn),
        })
    }

    pub fn load_all(&self) -> Result<Vec<Position>> {
        let conn = self.conn.lock().map_err(|_| anyhow::anyhow!("db lock poisoned"))?;
        let mut stmt = conn.prepare(
            "SELECT mint, entry_sol, token_balance_raw, decimals, opened_at, peak_value_sol FROM positions",
        )?;
        let rows = stmt.query_map([], |row| {
            let peak: f64 = row.get(5).unwrap_or(0.0);
            let entry: f64 = row.get(1)?;
            Ok(Position {
                mint: row.get(0)?,
                entry_sol: entry,
                peak_value_sol: if peak > 0.0 { peak } else { entry },
                token_balance_raw: row.get::<_, i64>(2)? as u64,
                decimals: row.get::<_, i64>(3)? as u8,
                opened_at: row.get::<_, i64>(4)? as u64,
            })
        })?;
        rows.collect::<Result<Vec<_>, _>>().map_err(Into::into)
    }

    pub fn upsert(&self, pos: &Position) -> Result<()> {
        let conn = self.conn.lock().map_err(|_| anyhow::anyhow!("db lock poisoned"))?;
        conn.execute(
            "INSERT INTO positions (mint, entry_sol, token_balance_raw, decimals, opened_at, peak_value_sol)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)
             ON CONFLICT(mint) DO UPDATE SET
                entry_sol = excluded.entry_sol,
                token_balance_raw = excluded.token_balance_raw,
                decimals = excluded.decimals,
                opened_at = excluded.opened_at,
                peak_value_sol = excluded.peak_value_sol",
            params![
                pos.mint,
                pos.entry_sol,
                pos.token_balance_raw as i64,
                pos.decimals as i64,
                pos.opened_at as i64,
                pos.peak_value_sol,
            ],
        )?;
        Ok(())
    }

    pub fn remove(&self, mint: &str) -> Result<()> {
        let conn = self.conn.lock().map_err(|_| anyhow::anyhow!("db lock poisoned"))?;
        conn.execute("DELETE FROM positions WHERE mint = ?1", params![mint])?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn persists_and_loads_positions() {
        let dir = std::env::temp_dir().join(format!("pos_store_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("test.db");
        let store = PositionStore::open(&path).unwrap();
        let pos = Position {
            mint: "mint_a".into(),
            entry_sol: 0.5,
            peak_value_sol: 0.6,
            token_balance_raw: 1_000_000,
            decimals: 6,
            opened_at: 1_700_000_000,
        };
        store.upsert(&pos).unwrap();
        let loaded = store.load_all().unwrap();
        assert_eq!(loaded.len(), 1);
        assert_eq!(loaded[0].peak_value_sol, 0.6);
        store.remove("mint_a").unwrap();
        assert!(store.load_all().unwrap().is_empty());
        let _ = std::fs::remove_dir_all(dir);
    }
}
