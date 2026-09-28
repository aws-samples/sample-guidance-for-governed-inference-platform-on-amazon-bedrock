# ABOUTME: Governed Inference Platform - Deploy and manage a governed AI inference platform on Amazon Bedrock
# ABOUTME: Main package for enterprise deployment of Claude Code using Amazon Bedrock

"""Governed Inference Platform - Enterprise deployment tool."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("governed-inference-platform")
except PackageNotFoundError:
    __version__ = "0.0.0"

__all__ = ["__version__"]
