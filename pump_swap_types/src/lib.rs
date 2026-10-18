#![allow(unexpected_cfgs, deprecated)]

use anchor_lang::prelude::*;
use anchor_gen::generate_cpi_crate;

generate_cpi_crate!("pump_swap.json");