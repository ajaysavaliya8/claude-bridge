"""Vision answers via the Anthropic Messages API.

This is the reliable path for image questions. The headless ``claude -p`` Read
tool does NOT deliver images to the model as real vision input (it hallucinates
their contents — anthropics/claude-code #35866, still open). So image-bearing
questions are answered here, by sending the image as a proper base64 vision
content block, exactly the path the API and clipboard-paste use.

The call is synchronous; the responder offloads it with ``asyncio.to_thread`` so
the poll loop is never blocked.
"""

from __future__ import annotations

import base64

from prompts import build_vision_text


def answer_with_vision(
    *,
    api_key: str,
    model: str,
    sender: str,
    question: str,
    context: str,
    images: list[tuple[bytes, str]],
    max_tokens: int = 1024,
) -> str:
    """Answer an image-bearing question.

    Args:
        api_key: Anthropic API key.
        model: Vision-capable model id (e.g. ``claude-sonnet-4-6``).
        sender: Name of the asking peer (for framing only).
        question: The peer's question text.
        context: Project facts retrieved read-only beforehand (may be empty).
        images: List of ``(raw_bytes, media_type)`` pairs already validated.
        max_tokens: Response cap.

    Returns:
        The model's answer text.
    """
    import anthropic  # imported lazily so the broker/tests need no SDK

    client = anthropic.Anthropic(api_key=api_key)

    content: list[dict[str, object]] = []
    for data, media_type in images:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.standard_b64encode(data).decode("ascii"),
                },
            }
        )
    content.append({"type": "text", "text": build_vision_text(sender, question, context)})

    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": content}],
    )

    parts = [block.text for block in message.content if getattr(block, "type", None) == "text"]
    return "\n".join(parts).strip() or "(the vision model returned no text)"
