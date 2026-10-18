#![allow(unexpected_cfgs, deprecated, ambiguous_glob_imports)]

use anchor_lang::prelude::*;
use anchor_gen::generate_cpi_crate;

generate_cpi_crate!("pump_swap.json");