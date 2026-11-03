"""Snapshot Proof: cooperative SQLite + artifact snapshots, verified by restore and new writes."""

from .core import backup, drill, restore, verify
from .protocol import application_write, initialize
from .safety import ConsistencyError, DestinationError, ProofError, VerificationError

__version__ = "0.1.0"
__all__ = ["backup", "drill", "restore", "verify", "application_write", "initialize",
           "ConsistencyError", "DestinationError", "ProofError", "VerificationError"]
