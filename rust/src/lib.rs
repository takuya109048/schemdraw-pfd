//! Grid-based PFD / P&ID generator: auto layout and fewest-bend orthogonal
//! pipe routing.  A Rust port of the Python ``schemdraw_pfd`` package that
//! lays out and routes every sheet exactly as the Python tool does.

// The port follows the Python code closely, statement by statement, so that
// it lays out every sheet exactly the same; these lints would reshape it.
#![allow(
    clippy::collapsible_if,
    clippy::type_complexity,
    clippy::needless_range_loop,
    clippy::manual_range_contains,
    clippy::too_many_arguments
)]

pub mod elements;
pub mod error;
pub mod expand;
pub mod flow;
pub mod freeze;
pub mod geometry;
pub mod layout;
pub mod levels;
pub mod model;
pub mod netlist;
pub mod pool;
pub mod pyjson;
pub mod pyval;
pub mod render;
pub mod revise;
pub mod router;
pub mod style;
pub mod svg;
pub mod symmetry;

pub use error::{Error, Result};
pub use model::{Diagram, Flowsheet};
