"""Setup script for the context-m-langchain PyPI package.

Publishes the Context-M LangChain memory adapter to PyPI so users can
install it with:

    pip install context-m-langchain

And use it as a drop-in replacement for ConversationBufferMemory:

    from context_m_langchain import ContextMMemory
    memory = ContextMMemory(user_id="alice")
    # in an agent setup:
    #   agent = initialize_agent(tools, llm, memory=memory)

The adapter calls Context-M's REST API (default http://localhost:8900)
so it works with any deployed Context-M instance — same wire format as
the MCP server, same provenance, same μ=0 ingest. No LLM calls in the
save path; load_memory_variables does a single /v1/search.

Build + publish:

    cd plugins/langchain
    python -m build
    twine upload dist/*
"""
from setuptools import setup, find_packages

setup(
    name="context-m-langchain",
    version = "0.3.0",
    description="LangChain BaseMemory adapter for Context-M — the "
                "Universal Neuro-Symbolic Memory Fabric.",
    long_description=(
        "# Context-M LangChain Adapter\n"
        "Drop-in replacement for `ConversationBufferMemory` that routes "
        "through Context-M's REST API. Gives any LangChain agent access "
        "to Context-M's bi-temporal Trace, cryptographic provenance, "
        "Memory Git (branch/merge/diff/blame), ZK-lite proofs, "
        "federation-ready schema export, and the mu=0 ingest protocol "
        "(zero LLM calls during save).\n\n"
        "## Quickstart\n\n"
        "```python\n"
        "from context_m_langchain import ContextMMemory\n"
        "memory = ContextMMemory(user_id='alice')\n"
        "memory.save_context(\n"
        "    {'input': 'Hi I\\'m Alice'},\n"
        "    {'output': 'Nice to meet you, Alice!'})\n"
        "memory.load_memory_variables(\n"
        "    {'input': 'what\\'s my name?'})\n"
        "```\n\n"
        "## Configuration\n\n"
        "Environment variables:\n"
        "- `CONTEXT_M_REST_URL` — default `http://localhost:8900`\n"
        "- `CONTEXT_M_API_KEY` — default empty (set if REST server requires RBAC)\n"
        "- `CONTEXT_M_USER_ID` — default `\"default\"`\n\n"
        "## What's new in v0.2.0 (2026-08-28)\n\n"
        "- Timeout raised to 10s default (was 5s) for large-context search.\n"
        "- `intent` field surfaced in `load_memory_variables` output.\n"
        "- `save_context` streams through the unmess pipeline by default.\n"
    ),
    long_description_content_type="text/markdown",
    author="Context-M maintainers",
    author_email="maintainers@context-m.dev",
    url="https://github.com/ssmurfgg04-gif/context-m",
    license="Apache-2.0",
    python_requires=">=3.10",
    packages=find_packages(),
    install_requires=[
        # No hard langchain dependency at import time — the adapter
        # duck-types BaseMemory so it works with or without langchain
        # installed. Users who want strict typing can install langchain
        # separately.
    ],
    extras_require={
        "langchain": ["langchain>=0.1.0"],
        "dev": ["pytest>=7.0", "langchain>=0.1.0"],
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: Apache Software License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Software Development :: Libraries :: Python Modules",
        "Topic :: Scientific/Engineering :: Artificial Intelligence",
    ],
)
