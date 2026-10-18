use borsh::{BorshDeserialize, BorshSerialize};
use pump_fun_types::events::{CreateEvent, TradeEvent};
use solana_keypair::Keypair;
use solana_signer::Signer;
use solana_program::instruction::{AccountMeta, Instruction};
use solana_program::pubkey::Pubkey;
use spl_associated_token_account::instruction::{create_associated_token_account_idempotent, create_associated_token_account};
use crate::mew::sol_hook::sol::TOKEN_PROGRAM_ID;
use crate::mew::sol_hook::{sol::{SolHook, SYSTEM_PROGRAM}, utils::decode_b64};
use crate::mew::writing::cc;
use std::io::Cursor;
use std::sync::Arc;
use solana_commitment_config::CommitmentConfig;
use reqwest::header;
use reqwest::header::HeaderMap;
use reqwest::Client;
use crate::mew::config::get_priority_fee_lvl;
use crate::log;

#[derive(Debug, BorshDeserialize, BorshSerialize)]
pub struct BondingCurveAccount {
    pub virtual_token_reserves: u64,
    pub virtual_sol_reserves:   u64,
    pub real_token_reserves:    u64,
    pub real_sol_reserves:      u64,
    pub token_total_supply:     u64,
    pub complete:               bool,
    pub creator:                Pubkey,
}

#[derive(Debug)]
pub enum PumpFunEvent {
    Trade(Option<TradeEvent>),
    Create(Option<CreateEvent>),
    Unknown,
}

pub const PUMP_FUN_ID: Pubkey = Pubkey::from_str_const("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P");
pub const FEE_RECIPIENT: Pubkey = Pubkey::from_str_const("62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV");
pub const EVENT_AUTHORITY: Pubkey = Pubkey::from_str_const("Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1");
pub const GLOBAL_VOLUME_ACCUMULATOR: Pubkey = Pubkey::from_str_const("Hq2wp8uJ9jCPsYgNHex8RtqdvMPfVGoYwjvF1ATiwn2Y");
pub const GLOBAL: Pubkey = Pubkey::from_str_const("4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf");
pub const FEE_PROGRAM: Pubkey = Pubkey::from_str_const("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ");
pub const FEE_CONFIG: Pubkey = Pubkey::from_str_const("8Wf5TiAheLUqBrKXeYg2JtAFFMWtKdG2BSFgqUcPVwTt");

pub const CREATE_DISCRIM: [u8; 8] = [27, 114, 169, 77, 222, 235, 99, 118];
pub const TRADE_DISCRIM: [u8; 8] = [189, 219, 127, 211, 78, 230, 97, 238];
pub const BUY_DISCRIM: [u8; 8] = [102, 6, 61, 18, 1, 218, 235, 234];
pub const SELL_DISCRIM: [u8; 8] = [51, 230, 133, 164, 1, 127, 131, 173];

pub const CREATE_SIG: &str = "G3K";
pub const TRADE_SIG: &str = "vdt";

pub const SEARCH_FOR: &str = "Program data: ";
pub const RAPID_LAUNCH_URI: &str = "https://rapidlaunch.io/";
pub const PUMP_FUN_URI: &str = "https://ipfs.io";
pub const TOTAL_SUPPLY: u64 = 1_000_000_000;

#[derive(Debug, Clone, Default)]
pub struct Socials {
    pub twitter: Option<String>,
    pub telegram: Option<String>,
    pub website: Option<String>,
}

#[derive(Clone)]
pub struct PumpFun {
    pub keypair: Arc<Keypair>,
    pub sol: Arc<SolHook>,
    client: Arc<Client>,
}

impl PumpFun {
    pub fn new(keypair: Arc<Keypair>, sol: Arc<SolHook>) -> Self {
        Self { keypair, sol, client: Arc::new(Client::new()) }
    }

    pub async fn get_market_cap(trade: &TradeEvent, sol_price_usd: f64) -> anyhow::Result<f64> {
        let price = Self::get_price(trade);
        let price_in_usd = price * sol_price_usd;
        Ok(1000000000.0 * price_in_usd)
    }

    pub async fn derive_bonding_curve(mint: &Pubkey) -> anyhow::Result<Pubkey> {
        let (pda, _) = Pubkey::find_program_address(
            &[b"bonding-curve", mint.as_ref()],
            &PUMP_FUN_ID,
        );
        Ok(pda)
    }

    pub async fn fetch_state(&self, bonding_curve: &Pubkey) -> anyhow::Result<BondingCurveAccount> {
        let data = self.sol.rpc_client.get_account_with_commitment(bonding_curve, CommitmentConfig::processed()).await?.value.ok_or(anyhow::anyhow!("account not found"))?.data;
        if data.len() < 8 { anyhow::bail!("account too short"); }
        let mut cur = Cursor::new(&data[8..]);
        let state = BondingCurveAccount::deserialize_reader(&mut cur)?;
        Ok(state)
    }

    pub async fn get_creator(&self, bonding_curve: &Pubkey) -> anyhow::Result<Pubkey> {
        let state = self.fetch_state(bonding_curve).await?;
        Ok(state.creator)
    }

    pub fn parse_logs(logs: std::slice::Iter<'_, String>, sig: Option<&String>) -> Vec<PumpFunEvent> {
        let mut events: Vec<PumpFunEvent> = Vec::new();
        for log in logs {
            let is_program_data = log.contains(SEARCH_FOR);
            if is_program_data {
                let program_data = log.split_at(SEARCH_FOR.len());
                let (_, program_data) = program_data;
                let b64 = match decode_b64(program_data) {
                    Ok(b64) => b64,
                    Err(_) => {
                        continue;
                    }
                };
                if b64[..8] == TRADE_DISCRIM {
                    let mut cur = Cursor::new(&b64[8..]);
                    let dtx = TradeEvent::deserialize_reader(&mut cur);
                    match dtx {
                        Ok(dtx) => {
                            events.push(PumpFunEvent::Trade(Some(dtx)));
                        }
                        Err(e) => {
                            eprintln!("Error deserializing trade event {:?}: {e}", sig.unwrap_or(&"".to_string()));
                        }
                    }
                } else if b64[..8] == CREATE_DISCRIM {
                    let mut cur = Cursor::new(&b64[8..]);
                    let dtx: Result<CreateEvent, _> = CreateEvent::deserialize_reader(&mut cur);
                    match dtx {
                        Ok(dtx) => {
                            events.push(PumpFunEvent::Create(Some(dtx)));
                        }
                        Err(e) => {
                            eprintln!("Error deserializing create event {:?}: {e}", sig.unwrap_or(&"".to_string()));
                        }
                    }
                }
            }
        };
        events
    }

    pub fn get_price(event: &TradeEvent) -> f64 {
        let vsr = event.virtual_sol_reserves as f64 / 1e9;
        let vtr = event.virtual_token_reserves as f64 / 1e6;
        vsr / vtr
    }

    pub fn get_open_price(event: &CreateEvent) -> f64 {
        let vsr = event.virtual_sol_reserves as f64 / 1e9;
        let vtr = event.virtual_token_reserves as f64 / 1e6;
        vsr / vtr
    }

    pub async fn fetch_price(&self, bonding_curve: &Pubkey) -> anyhow::Result<(BondingCurveAccount, f64)> {
        let state = self.fetch_state(bonding_curve).await?;
        let vsr = state.virtual_sol_reserves as f64 / 1e9;
        let vtr = state.virtual_token_reserves as f64 / 1e6;
        let price = vsr / vtr;
        Ok((state, price))
    }

    pub fn user_volume_accu_pda(&self, user: &Pubkey) -> Pubkey {
        let (pda, _) = Pubkey::find_program_address(
            &[b"user_volume_accumulator", user.as_ref()],
            &PUMP_FUN_ID,
        );
        pda
    }

    pub fn creator_vault_pda(&self, creator: &Pubkey) -> Pubkey {
        let (pda, _) = Pubkey::find_program_address(
            &[b"creator-vault", creator.as_ref()],
            &PUMP_FUN_ID,
        );
        pda
    }

    pub fn lamports_to_tokens(&self, lamports: f64, price: f64) -> u64 {
        let lamports = lamports / 1e9;
        let tokens = lamports / price;
        (tokens * 1e6) as u64
    }

    pub async fn check_socials(&self, uri: &str, retries: u32) -> anyhow::Result<Socials> {
        if !uri.starts_with(PUMP_FUN_URI) && !uri.starts_with(RAPID_LAUNCH_URI) {
            eprintln!("{} Socials Check | URI is not a pump.fun URI: {uri}{}", cc::LIGHT_GRAY, cc::RESET);
            return Ok(Socials {
                ..Default::default()
            });
        }

        let mut retry = 0;

        let mut headers = HeaderMap::new();
        headers.insert(header::ACCEPT, header::HeaderValue::from_static("application/json"));

        let mut socials = Socials {
            ..Default::default()
        };

        let req = self.client.get(uri).headers(headers);

        while retry < retries {
            let res = req.try_clone().unwrap().send().await?;
            let data = match res.json::<serde_json::Value>().await {
                Ok(data) => data,
                Err(_) => {
                    retry += 1;
                    tokio::time::sleep(std::time::Duration::from_millis(100)).await;
                    continue;
                }
            };
            socials = Socials {
                twitter: data.get(
                    "twitter"
                ).unwrap_or_default().as_str().map(|s| s.to_string()),
                telegram: data.get(
                    "telegram"
                ).unwrap_or_default().as_str().map(|s| s.to_string()),
                website: data.get(
                    "website"
                ).unwrap_or_default().as_str().map(|s| s.to_string()),
            };
            break;
        }
        Ok(socials)
    }

    pub async fn buy( 
        &self,
        mint: &Pubkey,
        bonding_curve: &Pubkey,
        creator: &Pubkey,
        sol_amount_in: f64,
        slippage: f64,
        price: f64,
        use_idempotent: Option<bool>,
    ) -> anyhow::Result<(Vec<Instruction>, u64)> {
        let buyer = self.keypair.pubkey();
        let program = match self.sol.get_token_program_id(mint).await {
            Ok(program) => program,
            Err(e) => {
                log!(cc::LIGHT_YELLOW, "This is normal when sniping. Error getting token program id: {:?}", e);
                TOKEN_PROGRAM_ID
            }
        };
        let sol_amount = sol_amount_in * 1e9;
        let max_sol_cost = (&sol_amount * &slippage) as u64;
        let token_amount_out = self.lamports_to_tokens(sol_amount, price);

        let mut ixs = vec![];
        if use_idempotent.unwrap_or(false) {
            ixs.push(
                create_associated_token_account_idempotent(
                    &buyer,
                    &buyer,
                    mint,
                    &program,
                )
            );
        } else {
            ixs.push(
                    create_associated_token_account(
                        &buyer,
                        &buyer,
                        mint,
                        &program,
                    )
                );
        };

        let associated_bc: Pubkey;
        let associated_user: Pubkey;
        if program == TOKEN_PROGRAM_ID {
            associated_bc = self.sol.get_ata_for_token(bonding_curve, mint);
            associated_user = self.sol.get_ata_for_token(&buyer, mint);
        } else {
            associated_bc = self.sol.get_ata_for_token2022(bonding_curve, mint);
            associated_user = self.sol.get_ata_for_token2022(&buyer, mint);
        }
        let user_volume_accu = self.user_volume_accu_pda(&buyer);
        let vault = self.creator_vault_pda(creator);

        let accs = vec![
            AccountMeta::new_readonly(GLOBAL, false),                                   // global
            AccountMeta::new(FEE_RECIPIENT, false),                                    // feeRecipient (writable)
            AccountMeta::new_readonly(*mint, false),                                 // mint
            AccountMeta::new(*bonding_curve, false),                               // bondingCurve (writable)
            AccountMeta::new(associated_bc, false),                                // associatedBondingCurve (writable)
            AccountMeta::new(associated_user, false),                               // associatedUser (writable)
            AccountMeta::new(buyer, true),                                          // user (signer, writable)
            AccountMeta::new_readonly(SYSTEM_PROGRAM, false),                       // systemProgram
            AccountMeta::new_readonly(program, false),                            // tokenProgram
            AccountMeta::new(vault, false),                                     // vault (writable)
            AccountMeta::new_readonly(EVENT_AUTHORITY, false),                // eventAuthority
            AccountMeta::new_readonly(PUMP_FUN_ID, false),                  // program
            AccountMeta::new(GLOBAL_VOLUME_ACCUMULATOR, false),           // globalVolumeAccumulator (writable)
            AccountMeta::new(user_volume_accu, false),                  // userVolumeAccumulator (writable)
            AccountMeta::new_readonly(FEE_CONFIG, false),                // feeConfig
            AccountMeta::new_readonly(FEE_PROGRAM, false),              // feeProgram
        ];

        let recent_fees = self.sol.fetch_priority_fee(&get_priority_fee_lvl(), &accs.iter().map(|acc| acc.pubkey).collect::<Vec<Pubkey>>()).await.unwrap();
        log!("Fee: {:?}", recent_fees);
        let mut data = [0u8; 8 + 8 + 8];
        data[..8].copy_from_slice(&BUY_DISCRIM);
        data[8..16].copy_from_slice(&token_amount_out.to_le_bytes());
        data[16..24].copy_from_slice(&max_sol_cost.to_le_bytes());

        ixs.push(Instruction { program_id: PUMP_FUN_ID, accounts: accs, data: data.to_vec() });
        Ok((ixs, recent_fees))
    }

    pub async fn sell(
        &self,
        mint: &Pubkey,
        bonding_curve: &Pubkey,
        creator: &Pubkey,
        sell_pct: u64,
        slippage: f64,
        price: f64,
    ) -> anyhow::Result<(Vec<Instruction>, u64)> {
        let buyer = self.keypair.pubkey();
        let program = self.sol.get_token_program_id(mint).await.unwrap();
        
        let token_balance = self.sol.get_token_balance(&buyer, &mint).await.unwrap();
        let token_balance_raw = (token_balance * 1e6) as u64;
        let token_amount_out = (token_balance_raw * sell_pct.max(1) / 100) as u64;
        let min_sol_output = token_balance * price;
        let min_sol_output = min_sol_output * (1.0 - slippage);
        let min_sol_output = (min_sol_output * 1e9) as u64;

        let mut ixs = vec![];
        let associated_bc: Pubkey;
        let associated_user: Pubkey;
        if program == TOKEN_PROGRAM_ID {
            associated_bc = self.sol.get_ata_for_token(bonding_curve, mint);
            associated_user = self.sol.get_ata_for_token(&buyer, mint);
        } else {
            associated_bc = self.sol.get_ata_for_token2022(bonding_curve, mint);
            associated_user = self.sol.get_ata_for_token2022(&buyer, mint);
        }
        let vault = self.creator_vault_pda(creator);

        let accs = vec![
            AccountMeta::new_readonly(GLOBAL, false),                                   // global
            AccountMeta::new(FEE_RECIPIENT, false),                                    // feeRecipient (writable)
            AccountMeta::new_readonly(*mint, false),                                 // mint
            AccountMeta::new(*bonding_curve, false),                               // bondingCurve (writable)
            AccountMeta::new(associated_bc, false),                                // associatedBondingCurve (writable)
            AccountMeta::new(associated_user, false),                               // associatedUser (writable)
            AccountMeta::new(buyer, true),                                          // user (signer, writable)
            AccountMeta::new_readonly(SYSTEM_PROGRAM, false),                       // systemProgram
            AccountMeta::new(vault, false),                                     // vault (writable)
            AccountMeta::new_readonly(program, false),                            // tokenProgram
            AccountMeta::new_readonly(EVENT_AUTHORITY, false),                // eventAuthority
            AccountMeta::new_readonly(PUMP_FUN_ID, false),                  // program
            AccountMeta::new_readonly(FEE_CONFIG, false),                // feeConfig
            AccountMeta::new_readonly(FEE_PROGRAM, false),              // feeProgram
        ];

        let recent_fees = self.sol.fetch_priority_fee(&get_priority_fee_lvl(), &accs.iter().map(|acc| acc.pubkey).collect::<Vec<Pubkey>>()).await.unwrap();
        log!("Fee: {:?}", recent_fees);
        let mut data = [0u8; 8 + 8 + 8];
        data[..8].copy_from_slice(&SELL_DISCRIM);
        data[8..16].copy_from_slice(&token_amount_out.to_le_bytes());
        data[16..24].copy_from_slice(&min_sol_output.to_le_bytes());

        ixs.push(Instruction { program_id: PUMP_FUN_ID, accounts: accs, data: data.to_vec() });
        Ok((ixs, recent_fees))
    }
}



