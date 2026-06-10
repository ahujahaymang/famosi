"""
Sliding-window text chunker using tiktoken token counts.

``chunk_text`` splits a document into overlapping windows of at most
``max_tokens`` tokens with ``overlap`` tokens of context carried over from
the previous window.  This matches the ingestion strategy described in the
design (Section 6.3):

  - Chunk size:  512 tokens
  - Overlap:     50  tokens
  - Encoding:    cl100k_base  (used by text-embedding-3-small)

Requirements: 15.2
"""

from __future__ import annotations

import tiktoken

# tiktoken encoding used by text-embedding-3-small and GPT-4 family models
_ENCODING_NAME = "cl100k_base"


def chunk_text(
    text: str,
    max_tokens: int = 512,
    overlap: int = 50,
) -> list[str]:
    """
    Split ``text`` into overlapping token-bounded windows.

    The algorithm:
    1. Encode the entire text into a token list using ``cl100k_base``.
    2. Advance a window of ``max_tokens`` tokens, stepping by
       ``(max_tokens - overlap)`` tokens each iteration so consecutive
       windows share ``overlap`` tokens of context.
    3. Decode each token window back to a UTF-8 string.
    4. Strip leading/trailing whitespace and skip any empty strings.

    Parameters
    ----------
    text:
        The raw document text to chunk.
    max_tokens:
        Maximum number of tokens per chunk (inclusive).  Default 512.
    overlap:
        Number of tokens shared between consecutive chunks.  Default 50.
        Must be strictly less than ``max_tokens``.

    Returns
    -------
    A list of non-empty text strings.  Returns ``[]`` for empty input or
    input that encodes to zero tokens.

    Raises
    ------
    ValueError:
        If ``overlap >= max_tokens`` (would produce infinite loops or empty
        windows).
    """
    if overlap >= max_tokens:
        raise ValueError(
            f"overlap ({overlap}) must be strictly less than max_tokens ({max_tokens})."
        )

    if not text or not text.strip():
        return []

    enc = tiktoken.get_encoding(_ENCODING_NAME)
    tokens: list[int] = enc.encode(text)

    if not tokens:
        return []

    step = max_tokens - overlap
    chunks: list[str] = []

    start = 0
    while start < len(tokens):
        end = start + max_tokens
        window_tokens = tokens[start:end]
        chunk_str = enc.decode(window_tokens).strip()
        if chunk_str:
            chunks.append(chunk_str)
        start += step

    return chunks
