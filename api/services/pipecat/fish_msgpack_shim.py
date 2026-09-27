"""Make pipecat's Fish Audio TTS importable inside the stock dograh-api image.

Mounted at ``/app/api/services/pipecat/fish_msgpack_shim.py``.

Why this exists
---------------
``pipecat.services.fish.tts`` imports ``ormsgpack`` at module scope. The
published ``dograhai/dograh-api`` image installs pipecat *without* the
``[fish]`` extra (see dograh's ``api/Dockerfile``: the extras list stops at
``smallest``), so ``ormsgpack`` is not in the venv and that import raises
``ImportError`` before the service class is ever reachable.

The module itself IS in the image — extras only pull wheels, they don't strip
source — so the single missing piece is the msgpack codec.

``msgpack`` is already pinned in dograh's ``api/requirements.txt``
(``msgpack==1.1.2``) and, for every message shape on Fish's wire protocol
(start / text / flush / stop / audio / finish), produces byte-identical output:

    msgpack.packb(obj, use_bin_type=True) == ormsgpack.packb(obj)

so we register a thin shim under the name ``ormsgpack`` before importing
pipecat's service. That keeps Fish Audio a pure volume-mount patch — no image
rebuild, no extra wheel in the container, nothing to redo on the next
``docker compose pull``.

If the real ``ormsgpack`` is present (someone rebuilt the image with
``pipecat[fish]``), it wins and the shim is never installed.
"""

from __future__ import annotations

import sys
import types

SHIM_MARKER = "HOPWHISTLE_FISH_TTS_V1"


def _install_ormsgpack_shim() -> str:
    """Ensure ``sys.modules['ormsgpack']`` exists. Returns the backend used."""
    if "ormsgpack" in sys.modules:
        return "preexisting"

    try:
        import ormsgpack  # noqa: F401

        return "ormsgpack"
    except ModuleNotFoundError:
        pass

    import msgpack

    shim = types.ModuleType("ormsgpack")
    shim.__doc__ = f"msgpack-backed ormsgpack stand-in ({SHIM_MARKER})."
    shim.HOPWHISTLE_FISH_SHIM = True  # type: ignore[attr-defined]

    def packb(obj, *, default=None, option=None):  # noqa: ANN001, ANN202
        # use_bin_type=True is msgpack 1.x's default and matches ormsgpack's
        # str/bin encoding exactly. Stated explicitly so a future msgpack
        # default flip can't silently change the wire format.
        return msgpack.packb(obj, use_bin_type=True, default=default)

    def unpackb(data, *, ext_hook=None, option=None):  # noqa: ANN001, ANN202
        # raw=False decodes msgpack str -> Python str (ormsgpack's behaviour);
        # audio payloads arrive as msgpack bin and stay bytes either way.
        return msgpack.unpackb(data, raw=False, strict_map_key=False)

    shim.packb = packb  # type: ignore[attr-defined]
    shim.unpackb = unpackb  # type: ignore[attr-defined]

    sys.modules["ormsgpack"] = shim
    return "msgpack-shim"


MSGPACK_BACKEND = _install_ormsgpack_shim()

# Import only after the shim is registered.
from pipecat.services.fish.tts import (  # noqa: E402
    FishAudioTTSService,
    FishAudioTTSSettings,
)

__all__ = [
    "FishAudioTTSService",
    "FishAudioTTSSettings",
    "MSGPACK_BACKEND",
    "SHIM_MARKER",
]
