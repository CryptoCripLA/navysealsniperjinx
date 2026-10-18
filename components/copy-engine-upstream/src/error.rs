use std::fmt;

#[derive(Debug)]
pub enum ClientError {
    Parse(String, String),
    Solana(String, String),
    Other(String),
}

impl fmt::Display for ClientError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ClientError::Parse(ctx, msg) => write!(f, "parse error ({ctx}): {msg}"),
            ClientError::Solana(ctx, msg) => write!(f, "solana error ({ctx}): {msg}"),
            ClientError::Other(msg) => write!(f, "{msg}"),
        }
    }
}

impl std::error::Error for ClientError {}
