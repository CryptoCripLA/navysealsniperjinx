use std::env;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};

pub struct BotControl {
    paused: AtomicBool,
    consecutive_failures: AtomicU32,
    max_failures: u32,
}

impl BotControl {
    pub fn from_env() -> Self {
        let max_failures = env::var("CIRCUIT_BREAKER_MAX_FAILURES")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(5);
        Self::new(max_failures)
    }

    pub fn new(max_failures: u32) -> Self {
        Self {
            paused: AtomicBool::new(false),
            consecutive_failures: AtomicU32::new(0),
            max_failures,
        }
    }

    pub fn is_paused(&self) -> bool {
        self.paused.load(Ordering::Relaxed)
    }

    pub fn pause(&self) {
        self.paused.store(true, Ordering::Relaxed);
    }

    pub fn resume(&self) {
        self.paused.store(false, Ordering::Relaxed);
        self.consecutive_failures.store(0, Ordering::Relaxed);
    }

    pub fn record_success(&self) {
        self.consecutive_failures.store(0, Ordering::Relaxed);
    }

    /// Returns `true` when the circuit breaker trips and auto-pauses the bot.
    pub fn record_failure(&self) -> bool {
        let count = self.consecutive_failures.fetch_add(1, Ordering::Relaxed) + 1;
        if count >= self.max_failures {
            self.paused.store(true, Ordering::Relaxed);
            true
        } else {
            false
        }
    }

    pub fn failure_count(&self) -> u32 {
        self.consecutive_failures.load(Ordering::Relaxed)
    }

    pub fn max_failures(&self) -> u32 {
        self.max_failures
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pause_and_resume() {
        let ctrl = BotControl::new(3);
        assert!(!ctrl.is_paused());
        ctrl.pause();
        assert!(ctrl.is_paused());
        ctrl.resume();
        assert!(!ctrl.is_paused());
    }

    #[test]
    fn circuit_breaker_trips() {
        let ctrl = BotControl::new(2);
        assert!(!ctrl.record_failure());
        assert!(ctrl.record_failure());
        assert!(ctrl.is_paused());
    }
}
