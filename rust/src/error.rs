//! Errors, named after the Python exceptions they stand for, so messages
//! read the same as the Python tool's.

use std::fmt;

#[derive(Clone, Debug, PartialEq)]
pub enum Error {
    /// Python ``KeyError``: an unknown id, port or element type.
    Key(String),
    /// Python ``ValueError``: invalid input.
    Value(String),
    /// Python ``TypeError``.
    Type(String),
    /// ``RoutingError``: the sheet could not be routed.
    Routing(String),
    /// Input / output.
    Io(String),
}

impl Error {
    pub fn message(&self) -> &str {
        match self {
            Error::Key(m) | Error::Value(m) | Error::Type(m) | Error::Routing(m) | Error::Io(m) => m,
        }
    }

    pub fn kind(&self) -> &'static str {
        match self {
            Error::Key(_) => "KeyError",
            Error::Value(_) => "ValueError",
            Error::Type(_) => "TypeError",
            Error::Routing(_) => "RoutingError",
            Error::Io(_) => "OSError",
        }
    }

    /// The same error with ``where: `` in front of its message.
    pub fn context(self, at: &str) -> Error {
        match self {
            Error::Key(m) => Error::Key(format!("{at}: {m}")),
            Error::Value(m) => Error::Value(format!("{at}: {m}")),
            Error::Type(m) => Error::Type(format!("{at}: {m}")),
            other => other,
        }
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.kind(), self.message())
    }
}

impl std::error::Error for Error {}

pub type Result<T> = std::result::Result<T, Error>;

pub fn key_err<T>(msg: impl Into<String>) -> Result<T> {
    Err(Error::Key(msg.into()))
}

pub fn value_err<T>(msg: impl Into<String>) -> Result<T> {
    Err(Error::Value(msg.into()))
}
