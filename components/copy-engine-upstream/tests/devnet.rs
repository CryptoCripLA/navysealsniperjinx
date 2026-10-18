//! Devnet integration checks — run with `cargo test --test devnet -- --ignored`

#[test]
#[ignore = "requires devnet RPC and funded wallet in env"]
fn devnet_env_profile_accepts_devnet_urls() {
    std::env::set_var("AGENT_NETWORK", "devnet");
    std::env::set_var("RPC_ENDPOINT", "https://api.devnet.solana.com");
    std::env::set_var("RPC_WEBSOCKET_ENDPOINT", "wss://api.devnet.solana.com");
    // PRIVATE_KEY and other required vars must be present for full validation.
    let _ = copy_trading_bot::common::utils::require_env_var("RPC_ENDPOINT");
}
