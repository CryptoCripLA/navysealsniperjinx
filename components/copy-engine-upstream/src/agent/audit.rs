use std::path::Path;
use std::sync::Mutex;

use anyhow::{Context, Result};
use rusqlite::{params, Connection};
use serde::Serialize;
use serde_json::Value;

#[derive(Debug, Clone, Serialize)]
pub struct PnlSummary {
    pub total_closed: usize,
    pub realized_pnl_sol: f64,
    pub wins: usize,
    pub losses: usize,
    pub win_rate_pct: f64,
    pub total_decisions: usize,
    pub successful_executions: usize,
    pub skipped: usize,
}

pub struct AuditLog {
    conn: Mutex<Connection>,
}

impl AuditLog {
    pub fn open(path: impl AsRef<Path>) -> Result<Self> {
        let conn = Connection::open(path.as_ref())
            .with_context(|| format!("open audit db {:?}", path.as_ref()))?;
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS agent_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                event_json TEXT NOT NULL,
                snapshot_json TEXT,
                policy_action TEXT NOT NULL,
                final_action TEXT NOT NULL,
                outcome TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS recent_target_swaps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                mint TEXT NOT NULL,
                direction TEXT NOT NULL,
                amount_in REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS closed_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                mint TEXT NOT NULL,
                entry_sol REAL NOT NULL,
                exit_sol REAL NOT NULL,
                pnl_sol REAL NOT NULL,
                reason TEXT NOT NULL
            );",
        )?;
        Ok(Self {
            conn: Mutex::new(conn),
        })
    }

    pub fn record_decision(
        &self,
        event_json: &str,
        snapshot_json: Option<&str>,
        policy_action: &str,
        final_action: &str,
        outcome: &str,
    ) -> Result<()> {
        let ts = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs() as i64)
            .unwrap_or(0);
        let conn = self.conn.lock().map_err(|_| anyhow::anyhow!("audit lock"))?;
        conn.execute(
            "INSERT INTO agent_decisions (ts, event_json, snapshot_json, policy_action, final_action, outcome)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![
                ts,
                event_json,
                snapshot_json,
                policy_action,
                final_action,
                outcome
            ],
        )?;
        Ok(())
    }

    pub fn record_target_swap(&self, mint: &str, direction: &str, amount_in: f64) -> Result<()> {
        let ts = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs() as i64)
            .unwrap_or(0);
        let conn = self.conn.lock().map_err(|_| anyhow::anyhow!("audit lock"))?;
        conn.execute(
            "INSERT INTO recent_target_swaps (ts, mint, direction, amount_in) VALUES (?1, ?2, ?3, ?4)",
            params![ts, mint, direction, amount_in],
        )?;
        Ok(())
    }

    pub fn recent_swaps_json(&self, limit: usize) -> Result<Value> {
        let conn = self.conn.lock().map_err(|_| anyhow::anyhow!("audit lock"))?;
        let mut stmt = conn.prepare(
            "SELECT mint, direction, amount_in, ts FROM recent_target_swaps ORDER BY id DESC LIMIT ?1",
        )?;
        let rows = stmt.query_map(params![limit as i64], |row| {
            Ok(serde_json::json!({
                "mint": row.get::<_, String>(0)?,
                "direction": row.get::<_, String>(1)?,
                "amount_in": row.get::<_, f64>(2)?,
                "ts": row.get::<_, i64>(3)?,
            }))
        })?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row?);
        }
        Ok(Value::Array(out))
    }

    pub fn record_closed_trade(
        &self,
        mint: &str,
        entry_sol: f64,
        exit_sol: f64,
        reason: &str,
    ) -> Result<()> {
        let ts = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs() as i64)
            .unwrap_or(0);
        let pnl_sol = exit_sol - entry_sol;
        let conn = self.conn.lock().map_err(|_| anyhow::anyhow!("audit lock"))?;
        conn.execute(
            "INSERT INTO closed_trades (ts, mint, entry_sol, exit_sol, pnl_sol, reason)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![ts, mint, entry_sol, exit_sol, pnl_sol, reason],
        )?;
        Ok(())
    }

    pub fn pnl_summary(&self) -> Result<PnlSummary> {
        let conn = self.conn.lock().map_err(|_| anyhow::anyhow!("audit lock"))?;

        let total_closed: i64 = conn.query_row(
            "SELECT COUNT(*) FROM closed_trades",
            [],
            |row| row.get(0),
        )?;
        let realized_pnl_sol: f64 = conn
            .query_row(
                "SELECT COALESCE(SUM(pnl_sol), 0) FROM closed_trades",
                [],
                |row| row.get(0),
            )
            .unwrap_or(0.0);
        let wins: i64 = conn.query_row(
            "SELECT COUNT(*) FROM closed_trades WHERE pnl_sol > 0",
            [],
            |row| row.get(0),
        )?;
        let losses: i64 = conn.query_row(
            "SELECT COUNT(*) FROM closed_trades WHERE pnl_sol <= 0",
            [],
            |row| row.get(0),
        )?;

        let total_decisions: i64 = conn.query_row(
            "SELECT COUNT(*) FROM agent_decisions",
            [],
            |row| row.get(0),
        )?;
        let successful_executions: i64 = conn.query_row(
            "SELECT COUNT(*) FROM agent_decisions WHERE outcome = 'ok' AND final_action NOT LIKE 'skip%'",
            [],
            |row| row.get(0),
        )?;
        let skipped: i64 = conn.query_row(
            "SELECT COUNT(*) FROM agent_decisions WHERE final_action LIKE 'skip%' OR policy_action LIKE 'skip%'",
            [],
            |row| row.get(0),
        )?;

        let win_rate_pct = if total_closed > 0 {
            (wins as f64 / total_closed as f64) * 100.0
        } else {
            0.0
        };

        Ok(PnlSummary {
            total_closed: total_closed as usize,
            realized_pnl_sol,
            wins: wins as usize,
            losses: losses as usize,
            win_rate_pct,
            total_decisions: total_decisions as usize,
            successful_executions: successful_executions as usize,
            skipped: skipped as usize,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn records_decision_and_swap() {
        let dir = std::env::temp_dir().join(format!("audit_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("audit.db");
        let log = AuditLog::open(&path).unwrap();
        log.record_decision("{}", None, "skip", "skip", "ok").unwrap();
        log.record_target_swap("mint", "buy", 0.1).unwrap();
        log.record_closed_trade("mint", 1.0, 1.2, "take_profit").unwrap();
        let recent = log.recent_swaps_json(5).unwrap();
        assert_eq!(recent.as_array().unwrap().len(), 1);
        let pnl = log.pnl_summary().unwrap();
        assert_eq!(pnl.total_closed, 1);
        assert!((pnl.realized_pnl_sol - 0.2).abs() < f64::EPSILON);
        let _ = std::fs::remove_dir_all(dir);
    }
}
