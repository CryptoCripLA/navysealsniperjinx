use anyhow::{anyhow, Result};
use serde_json::Value;
use solana_client::rpc_client::RpcClient;
use solana_sdk::pubkey::Pubkey;
use std::str::FromStr;

use crate::common::utils::ParseTx;

#[derive(Debug, Clone)]
pub struct TokenAccountInfo {
    pub mint: String,
    pub decimals: u8,
}

/// Returns true when a Helius-style WS notification contains swap inner instructions.
pub fn ws_notification_has_swap_instructions(json: &Value) -> bool {
    json["params"]["result"]["transaction"]["meta"]["innerInstructions"]
        .as_array()
        .map(|a| !a.is_empty())
        .unwrap_or(false)
}

pub fn parse_swap_from_ws_message(
    json: &Value,
    target: &str,
    sol_address: &str,
    rpc_client: &RpcClient,
) -> Result<ParseTx> {
    let lookup = |ata: &Pubkey| {
        let data = rpc_client
            .get_token_account(ata)?
            .ok_or_else(|| anyhow!("token account {ata} not found"))?;
        Ok(TokenAccountInfo {
            mint: data.mint,
            decimals: data.token_amount.decimals,
        })
    };
    parse_swap_from_ws_message_with_lookup(json, target, sol_address, lookup)
}

pub fn parse_swap_from_ws_message_with_lookup<F>(
    json: &Value,
    target: &str,
    sol_address: &str,
    lookup: F,
) -> Result<ParseTx>
where
    F: Fn(&Pubkey) -> Result<TokenAccountInfo>,
{
    let inner_instructions = json["params"]["result"]["transaction"]["meta"]["innerInstructions"]
        .as_array()
        .ok_or_else(|| anyhow!("no inner instructions"))?;

    for inner_instruction in inner_instructions {
        let instructions = inner_instruction["instructions"]
            .as_array()
            .ok_or_else(|| anyhow!("no instructions array"))?;

        if instructions.len() < 2 {
            continue;
        }

        for instruction in instructions {
            if instruction["parsed"]["type"] != "transfer" {
                continue;
            }
            if instruction["parsed"]["info"]["authority"] != target {
                continue;
            }

            let amount_in = instructions[0]["parsed"]["info"]["amount"]
                .as_str()
                .unwrap_or("0")
                .to_string();
            let in_ata = instructions[0]["parsed"]["info"]["destination"]
                .as_str()
                .ok_or_else(|| anyhow!("missing destination"))?;
            let out_ata = instructions[1]["parsed"]["info"]["source"]
                .as_str()
                .ok_or_else(|| anyhow!("missing source"))?;

            let pubkey_in_ata = Pubkey::from_str(in_ata)?;
            let pubkey_out_ata = Pubkey::from_str(out_ata)?;

            let in_data = lookup(&pubkey_in_ata)?;
            let out_data = lookup(&pubkey_out_ata)?;

            let param_amount_in = amount_in
                .parse::<f64>()
                .map_err(|_| anyhow!("invalid amount"))?;

            let (mint, direction) = if in_data.mint == sol_address {
                (out_data.mint, "buy".to_string())
            } else {
                (in_data.mint, "sell".to_string())
            };

            return Ok(ParseTx {
                type_tx: "swap".to_string(),
                direction: Some(direction),
                amount_in: param_amount_in / 10_f64.powf(in_data.decimals as f64),
                amount_out: 0.0,
                mint,
            });
        }
    }

    Err(anyhow!("no matching transfer for target"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;
    use std::collections::HashMap;

    #[test]
    fn ws_fixture_contains_swap_instructions() {
        let json: Value = serde_json::from_str(include_str!(
            "../../tests/fixtures/ws_swap_notification.json"
        ))
        .unwrap();
        assert!(ws_notification_has_swap_instructions(&json));
    }

    #[test]
    fn parses_fixture_with_mock_lookup() {
        let json: Value = serde_json::from_str(include_str!(
            "../../tests/fixtures/ws_swap_notification.json"
        ))
        .unwrap();
        let sol = "So11111111111111111111111111111111111111112";
        let token_mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v";
        let dest_ata = "H6ARHf6YXhGYeQfUzQNGk6rDNnLBQKrenN712K4AQJEG";
        let source_ata = "8UJgxaiQx5nTrdDgph5FyerMmzgWJrahHUdijBiDdvUJ";
        let target = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1";
        let mut accounts = HashMap::new();
        accounts.insert(
            dest_ata.to_string(),
            TokenAccountInfo {
                mint: sol.into(),
                decimals: 9,
            },
        );
        accounts.insert(
            source_ata.to_string(),
            TokenAccountInfo {
                mint: token_mint.into(),
                decimals: 6,
            },
        );

        let parsed = parse_swap_from_ws_message_with_lookup(
            &json,
            target,
            sol,
            |pk| {
                accounts
                    .get(&pk.to_string())
                    .cloned()
                    .ok_or_else(|| anyhow!("unknown ata"))
            },
        )
        .unwrap();

        assert_eq!(parsed.direction.as_deref(), Some("buy"));
        assert_eq!(parsed.mint, token_mint);
        assert!((parsed.amount_in - 0.01).abs() < f64::EPSILON);
    }
}
