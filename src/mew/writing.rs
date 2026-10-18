use std::io::{self, Write, StdoutLock};

#[macro_export]
macro_rules! log {
    // colored, no extra args
    ($color:expr, $fmt:literal) => {{
        let time = chrono::Utc::now().format("%H:%M:%S%.3f").to_string();
        let mut _stderr = ::std::io::stderr().lock();
        let _ = ::std::io::Write::write_fmt(
            &mut _stderr,
            format_args!(concat!("{}{} | {}", "{}", $fmt, "{}", "\n"),
                $crate::mew::writing::cc::LIGHT_GRAY,
                time,
                $crate::mew::writing::cc::RESET,
                $color,
                $crate::mew::writing::cc::RESET
            )
        );
    }};

    // colored, with args
    ($color:expr, $fmt:literal, $($arg:tt)+) => {{
        let time = chrono::Utc::now().format("%H:%M:%S%.3f").to_string();
        let mut _stderr = ::std::io::stderr().lock();
        let _ = ::std::io::Write::write_fmt(
            &mut _stderr,
            format_args!(concat!("{}{} | {}", "{}", $fmt, "{}", "\n"),
                $crate::mew::writing::cc::LIGHT_GRAY,
                time,
                $crate::mew::writing::cc::RESET,
                $color,
                $($arg)+,
                $crate::mew::writing::cc::RESET
            )
        );
    }};

    // default color, no extra args
    ($fmt:literal) => {{
        let time = chrono::Utc::now().format("%H:%M:%S%.3f").to_string();
        let mut _stderr = ::std::io::stderr().lock();
        let _ = ::std::io::Write::write_fmt(
            &mut _stderr,
            format_args!(concat!("{}{} | {}", "{}", $fmt, "{}", "\n"),
                $crate::mew::writing::cc::LIGHT_GRAY,
                time,
                $crate::mew::writing::cc::RESET,
                $crate::mew::writing::cc::LIGHT_GRAY,
                $crate::mew::writing::cc::RESET
            )
        );
    }};

    // default color, with args
    ($fmt:literal, $($arg:tt)+) => {{
        let time = chrono::Utc::now().format("%H:%M:%S%.3f").to_string();
        let mut _stderr = ::std::io::stderr().lock();
        let _ = ::std::io::Write::write_fmt(
            &mut _stderr,
            format_args!(concat!("{}{} | {}", "{}", $fmt, "{}", "\n"),
                $crate::mew::writing::cc::LIGHT_GRAY,
                time,
                $crate::mew::writing::cc::RESET,
                $crate::mew::writing::cc::LIGHT_GRAY,
                $($arg)+,
                $crate::mew::writing::cc::RESET
            )
        );
    }};
}

#[macro_export]
macro_rules! warn {
    ($($arg:tt)*) => {{
        let mut _stderr = ::std::io::stderr().lock();
        let _ = ::std::io::Write::write_fmt(
            &mut _stderr,
            format_args!(
                "{}{}{}\n",
                $crate::mew::writing::cc::ORANGE,
                format_args!($($arg)*),
                $crate::mew::writing::cc::RESET
            )
        );
    }};
}


pub struct Colors<'a> {
    lock: StdoutLock<'a>,
}

pub mod cc {
    pub const RED: &str      = "\x1b[31m";
    pub const GREEN: &str    = "\x1b[32m";
    pub const YELLOW: &str   = "\x1b[33m";
    pub const BLUE: &str        = "\x1b[34m";
    pub const MAGENTA: &str     = "\x1b[35m";
    pub const CYAN: &str        = "\x1b[36m";
    pub const WHITE: &str    = "\x1b[37m";
    pub const BOLD: &str     = "\x1b[1m";
    pub const RESET: &str    = "\x1b[0m";
    pub const BLINK: &str    = "\x1b[5m";
    pub const BLACK: &str    = "\x1b[30m";
    pub const ORANGE: &str   = "\x1b[38;5;208m";
    pub const PURPLE: &str   = "\x1b[38;5;93m";
    pub const DARK_GRAY: &str   = "\x1b[38;5;238m";
    pub const LIGHT_GRAY: &str  = "\x1b[38;5;245m";
    pub const PINK: &str            = "\x1b[38;5;213m";
    pub const BROWN: &str           = "\x1b[38;5;130m";
    pub const LIGHT_GREEN: &str     = "\x1b[92m";
    pub const LIGHT_BLUE: &str  = "\x1b[94m";
    pub const LIGHT_CYAN: &str  = "\x1b[96m";
    pub const LIGHT_RED: &str   = "\x1b[91m";
    pub const LIGHT_MAGENTA: &str   = "\x1b[95m";
    pub const LIGHT_YELLOW: &str    = "\x1b[93m";
    pub const LIGHT_WHITE: &str     = "\x1b[97m";
}

impl<'a> Colors<'a> {
    pub fn new(lock: StdoutLock<'a>) -> Self {
        Self { lock }
    }

    pub fn cprint(&mut self, text: &str, color: &str) {
        let _ = writeln!(self.lock, "{}{}{}", color, text, cc::RESET).unwrap();
    }

    pub fn cinput(&mut self, text: &str, color: &str) -> String {
        let mut input = String::new();
        let _ = writeln!(self.lock, "{}{}{}", color, text, cc::RESET).unwrap();
        io::stdin().read_line(&mut input).unwrap();
        input.trim().to_string()
    }

    pub fn err_print(&mut self, text: &str) {
        let _ = writeln!(self.lock, "{}{}{}", cc::RED, text, cc::RESET).unwrap();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_cprint() {
        let lock: StdoutLock<'static> = io::stdout().lock();
        let mut colors: Colors<'static> = Colors::new(lock);
        colors.cprint(&format!("{}Hello, world!", cc::BOLD), cc::RED);
        colors.cprint("This is your captain speaking!", cc::BLUE);
    }

    #[test]
    fn test_err_print() {
        let lock: StdoutLock<'static> = io::stdout().lock();
        let mut colors: Colors<'static> = Colors::new(lock);
        colors.err_print("This is an error message!");
    }

    // #[test]
    // fn test_cinput() {   
    //     let lock: StdoutLock<'static> = io::stdout().lock();
    //     let mut colors: Colors<'static> = Colors::new(lock);
    //     let input: String = colors.cinput("Enter your name: ", cc::BLUE);
    //     colors.cprint(&format!("Hello, {}!", input), cc::GREEN);
    // }
}