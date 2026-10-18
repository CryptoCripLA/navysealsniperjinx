use anyhow::Result;
use reqwest::Client;
use std::env;

pub struct TelegramNotifier {
    client: Client,
    token: String,
    chat_id: String,
}

impl TelegramNotifier {
    pub fn from_env() -> Option<Self> {
        let token = env::var("TELEGRAM_BOT_TOKEN").ok()?;
        let chat_id = env::var("TELEGRAM_CHAT_ID").ok()?;
        if token.is_empty() || chat_id.is_empty() {
            return None;
        }
        Some(Self {
            client: Client::new(),
            token,
            chat_id,
        })
    }

    pub async fn send_message(&self, text: &str) -> Result<()> {
        let url = format!("https://api.telegram.org/bot{}/sendMessage", self.token);
        self.client
            .post(&url)
            .json(&serde_json::json!({
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": true
            }))
            .send()
            .await?
            .error_for_status()?;
        Ok(())
    }

    pub async fn send_approval_request(&self, request_id: &str, text: &str) -> Result<()> {
        let url = format!("https://api.telegram.org/bot{}/sendMessage", self.token);
        self.client
            .post(&url)
            .json(&serde_json::json!({
                "chat_id": self.chat_id,
                "text": text,
                "reply_markup": {
                    "inline_keyboard": [[
                        {"text": "Approve", "callback_data": format!("approve:{request_id}")},
                        {"text": "Reject", "callback_data": format!("reject:{request_id}")}
                    ]]
                }
            }))
            .send()
            .await?
            .error_for_status()?;
        Ok(())
    }

    pub fn is_authorized_chat(&self, message: &serde_json::Value) -> bool {
        match &message["chat"]["id"] {
            serde_json::Value::String(s) => s == &self.chat_id,
            serde_json::Value::Number(n) => n
                .as_i64()
                .map(|id| id.to_string() == self.chat_id)
                .unwrap_or(false),
            _ => false,
        }
    }

    pub async fn get_updates(&self, offset: i64) -> Result<Vec<serde_json::Value>> {
        let url = format!("https://api.telegram.org/bot{}/getUpdates", self.token);
        let response = self
            .client
            .get(&url)
            .query(&[("timeout", "25"), ("offset", &offset.to_string())])
            .send()
            .await?
            .json::<serde_json::Value>()
            .await?;
        let empty = vec![];
        Ok(response["result"].as_array().unwrap_or(&empty).clone())
    }

    pub async fn answer_callback(&self, callback_id: &str, text: &str) -> Result<()> {
        let url = format!("https://api.telegram.org/bot{}/answerCallbackQuery", self.token);
        self.client
            .post(&url)
            .json(&serde_json::json!({
                "callback_query_id": callback_id,
                "text": text
            }))
            .send()
            .await?
            .error_for_status()?;
        Ok(())
    }
}

pub async fn notify_circuit_breaker(failures: u32) {
    if let Some(notifier) = TelegramNotifier::from_env() {
        let msg = format!(
            "circuit breaker tripped\nconsecutive failures={}\nbot auto-paused — use /resume after review",
            failures
        );
        let _ = notifier.send_message(&msg).await;
    }
}

pub async fn notify_trade(wallet: &str, signature: &str, direction: &str, mint: &str) {
    if let Some(notifier) = TelegramNotifier::from_env() {
        let msg = format!(
            "copy trade\nwallet: {}\ndir: {}\nmint: {}\ntx: https://solscan.io/tx/{}",
            wallet, direction, mint, signature
        );
        let _ = notifier.send_message(&msg).await;
    }
}
