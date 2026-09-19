"""GitGuard -- a safe, agentic git assistant.

The observe->decide->act loop of an AI coding agent, specialized for git and
wrapped in a permission model that classifies every command as read-only,
mutating, or destructive before it runs.
"""

from .classifier import Classification, Tier, classify

__version__ = "0.1.0"
__all__ = ["classify", "Classification", "Tier", "__version__"]
