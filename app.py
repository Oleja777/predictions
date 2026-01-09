"""Compatibility wrapper for the package app module."""

from predictions.app import RequestHandler, run, runtime_config_store, service, store, tracker

__all__ = [
    "RequestHandler",
    "run",
    "runtime_config_store",
    "service",
    "store",
    "tracker",
]


if __name__ == "__main__":
    run()
