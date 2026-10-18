#[derive(Debug, Clone)]
pub enum TradeEvent {
    DetectedSwap {
        mint: String,
        amount_in: f64,
        direction: String,
    },
    PositionValued {
        mint: String,
        entry_sol: f64,
        peak_value_sol: f64,
        current_value_sol: f64,
    },
    ExitSignal {
        mint: String,
        reason: &'static str,
    },
}

#[derive(Debug, Clone, PartialEq)]
pub enum TradeAction {
    MirrorBuy { mint: String, amount_sol: f64 },
    MirrorSell { mint: String, amount_sol: f64 },
    Skip { reason: String },
}

impl std::fmt::Display for TradeAction {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            TradeAction::MirrorBuy { mint, amount_sol } => {
                write!(f, "mirror_buy {mint} {amount_sol}")
            }
            TradeAction::MirrorSell { mint, amount_sol } => {
                write!(f, "mirror_sell {mint} {amount_sol}")
            }
            TradeAction::Skip { reason } => write!(f, "skip: {reason}"),
        }
    }
}
