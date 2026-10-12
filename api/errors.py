"""
The one error shape every router raises: `{"detail": {"code": <stable id>, "message": <text>}}`.

The web client (web/composables) and the assist runner branch on `code`, never on `message`, so a
code is part of the API contract and a message is free to change.
"""
from fastapi import HTTPException


def coded(status_code: int, code: str, message: str) -> HTTPException:
    """An HTTPException whose detail carries a machine-readable `code` beside the human `message`."""
    return HTTPException(status_code, {"code": code, "message": message})
