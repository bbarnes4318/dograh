"""Docker and psql access for the planner. Read-only by construction.

Image contents are read with ``docker create`` + ``docker cp`` on a container
that is never started (entrypoint replaced, no network), so reading an
image's migrations can never run its startup ``alembic upgrade head``.

Database reads always run inside ``BEGIN READ ONLY ... ROLLBACK``.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
from collections.abc import Callable, Sequence

Runner = Callable[..., subprocess.CompletedProcess]


def _run(
    cmd: Sequence[str], *, input: bytes | None = None, cwd: str | None = None
) -> bytes:
    proc = subprocess.run(
        list(cmd),
        input=input,
        cwd=cwd,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"{' '.join(cmd[:3])} ... failed ({proc.returncode}): "
            f"{proc.stderr.decode(errors='replace').strip()[:400]}"
        )
    return proc.stdout


class Docker:
    def __init__(self, run: Callable[..., bytes] = _run):
        self.run = run

    def compose_container(self, compose_dir: str, service: str) -> str:
        out = self.run(
            ["docker", "compose", "ps", "-a", "-q", service], cwd=compose_dir
        )
        lines = out.decode().strip().splitlines()
        return lines[0] if lines else ""

    def image_id(self, ref: str) -> str:
        return (
            self.run(["docker", "image", "inspect", "-f", "{{.Id}}", ref])
            .decode()
            .strip()
        )

    def container_image_id(self, container: str) -> str:
        return (
            self.run(["docker", "inspect", "-f", "{{.Image}}", container])
            .decode()
            .strip()
        )

    def container_env(self, container: str, names: Sequence[str]) -> dict[str, str]:
        """Only the allowlisted names; everything else is dropped in memory."""
        raw = self.run(["docker", "inspect", "-f", "{{json .Config.Env}}", container])
        wanted = set(names)
        out: dict[str, str] = {}
        for item in json.loads(raw.decode() or "[]") or []:
            key, _, value = item.partition("=")
            if key in wanted:
                out[key] = value
        return out

    def container_mount_targets(self, container: str) -> list[str]:
        raw = self.run(["docker", "inspect", "-f", "{{json .Mounts}}", container])
        return sorted(
            m.get("Destination", "") for m in json.loads(raw.decode() or "[]") or []
        )

    def container_state(self, container: str) -> dict:
        raw = self.run(
            [
                "docker",
                "inspect",
                "-f",
                (
                    '{"status":"{{.State.Status}}","health":"{{if .State.Health}}{{.State.Health.Status}}{{end}}",'
                    '"restarts":{{.RestartCount}},"image":"{{.Image}}"}'
                ),
                container,
            ]
        )
        return json.loads(raw.decode())

    def _copy_out(self, container: str, path: str) -> bytes | None:
        try:
            return self.run(["docker", "cp", f"{container}:{path}", "-"])
        except RuntimeError as exc:
            if "No such container:path" in str(exc) or "Could not find the file" in str(
                exc
            ):
                return None
            raise

    def read_paths(
        self, source: str, paths: Sequence[str], *, is_image: bool
    ) -> dict[str, str | None]:
        """Text of each path (None when absent) from an image or a container."""
        container = self._probe(source) if is_image else source
        try:
            return {p: _single_file(self._copy_out(container, p)) for p in paths}
        finally:
            if is_image:
                self.run(["docker", "rm", "-f", container])

    def read_dir(
        self, source: str, path: str, *, is_image: bool
    ) -> list[tuple[str, str]]:
        container = self._probe(source) if is_image else source
        try:
            data = self._copy_out(container, path)
        finally:
            if is_image:
                self.run(["docker", "rm", "-f", container])
        if data is None:
            raise RuntimeError(f"{path} not found in {source}")
        return _py_files(data)

    def _probe(self, image: str) -> str:
        # Created, never started: replaced entrypoint, no network.
        return (
            self.run(
                [
                    "docker",
                    "create",
                    "--network",
                    "none",
                    "--entrypoint",
                    "/bin/true",
                    "--label",
                    "hopwhistle.cutover.probe=1",
                    image,
                ]
            )
            .decode()
            .strip()
        )


def _single_file(data: bytes | None) -> str | None:
    if data is None:
        return None
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        files = [m for m in tar.getmembers() if m.isfile()]
        if len(files) != 1:
            return None
        handle = tar.extractfile(files[0])
        return handle.read().decode("utf-8", errors="replace") if handle else None


def _py_files(data: bytes) -> list[tuple[str, str]]:
    out = []
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for member in tar.getmembers():
            # Only regular .py files directly in the versions directory; never
            # extracted to disk, so path tricks in the archive cannot matter.
            if not member.isfile() or not member.name.endswith(".py"):
                continue
            if member.name.count("/") != 1:
                continue
            handle = tar.extractfile(member)
            if handle:
                out.append(
                    (
                        os.path.basename(member.name),
                        handle.read().decode("utf-8", errors="replace"),
                    )
                )
    return out


class Psql:
    """``cmd``: argv that starts psql against the dograh database, e.g.
    ``docker compose exec -T postgres psql -U <user> -d <db>``."""

    def __init__(
        self,
        cmd: Sequence[str],
        cwd: str | None = None,
        run: Callable[..., bytes] = _run,
    ):
        self.cmd = list(cmd)
        self.cwd = cwd
        self.run = run

    def read(self, sql: str) -> list[list[str]]:
        script = f"BEGIN READ ONLY;\n{sql.rstrip().rstrip(';')};\nROLLBACK;\n".encode()
        out = self.run(
            [
                *self.cmd,
                "-X",
                "-q",
                "-A",
                "-t",
                "-F",
                "\t",
                "-v",
                "ON_ERROR_STOP=1",
                "-f",
                "-",
            ],
            input=script,
            cwd=self.cwd,
        )
        return [line.split("\t") for line in out.decode().splitlines() if line != ""]

    def alembic_revisions(self) -> list[str] | None:
        """None when the table does not exist."""
        exists = self.read("SELECT to_regclass('public.alembic_version') IS NOT NULL")
        if not exists or exists[0][0] != "t":
            return None
        return sorted(
            r[0] for r in self.read("SELECT version_num FROM alembic_version")
        )

    def invalid_indexes(self) -> list[str]:
        rows = self.read(
            "SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE NOT i.indisvalid AND n.nspname = 'public' ORDER BY 1"
        )
        return [r[0] for r in rows]

    def count(self, sql: str) -> int:
        rows = self.read(sql)
        return int(rows[0][0]) if rows else 0
