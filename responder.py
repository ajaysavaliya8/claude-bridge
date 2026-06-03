"""Autonomous responder daemon — one per peer, next to that peer's project.

Long-polls the broker for questions addressed to this peer and answers them with
no human attending the terminal:

* text-only question  -> headless ``claude -p`` inside ``PROJECT_DIR`` (read-only
  tools, resumed session so context accumulates across questions);
* image-bearing question -> the Anthropic vision API (see :mod:`vision`), because
  the headless Read tool does not reliably see images.

Every question is handled in its own try/except: one bad question (or a crash in
``claude``) is turned into an error answer and the loop keeps going. A single
``claude`` run is bounded by a wall-clock timeout so one hung CLI cannot pin the
whole peer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import sys
from typing import Any

import httpx

from broker_client import BrokerClient, BrokerError
from config import Config
from prompts import (
    RESPONDER_SYSTEM_PROMPT,
    build_note_prompt,
    build_question_prompt,
    build_retrieval_prompt,
)

logger = logging.getLogger("claude_bridge.responder")


class ClaudeError(RuntimeError):
    """A headless ``claude`` run failed. Subclasses distinguish failure modes so
    the session self-heal only fires when discarding context is actually warranted."""


class ClaudeExitError(ClaudeError):
    """``claude`` exited non-zero — the signature of an invalid/unknown ``--resume``
    session id (among other crashes). The only failure that justifies clearing the
    accumulated session and retrying fresh."""


# Generic, non-revealing message returned to the asking peer on internal failure.
# Details (stderr, paths, tracebacks) are logged locally, never sent cross-peer.
_GENERIC_ERROR = (
    "The peer '{peer}' could not answer this question due to an internal error. "
    "Its operator should check the responder logs (request_id={rid})."
)


class Responder:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.peer = config.require_peer_self()
        self.project_dir = config.require_project_dir()
        self._session_file = config.session_state_dir / f"{self.peer}.session"
        self._lock_fd: int | None = None
        self._stop = asyncio.Event()

    # -- single-instance lock (one responder per peer) ------------------- #
    def acquire_lock(self) -> None:
        """Take an OS advisory lock so two responders can't race one peer's
        session file. The lock is released automatically when the process dies,
        so a crash never leaves a stale lock blocking restart."""
        lock_path = self.config.session_state_dir / f"{self.peer}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise RuntimeError(
                f"another responder for peer '{self.peer}' already holds {lock_path}; "
                f"only one responder per peer may run"
            ) from exc
        self._lock_fd = fd

    # -- session persistence (per peer) ---------------------------------- #
    def _load_session(self) -> str | None:
        try:
            sid = self._session_file.read_text(encoding="utf-8").strip()
            return sid or None
        except FileNotFoundError:
            return None

    def _save_session(self, session_id: str) -> None:
        if not session_id:
            return
        self._session_file.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: a crash mid-write can never leave a truncated session id.
        tmp = self._session_file.parent / (self._session_file.name + ".tmp")
        tmp.write_text(session_id, encoding="utf-8")
        os.replace(tmp, self._session_file)

    def _clear_session(self) -> None:
        self._session_file.unlink(missing_ok=True)

    # -- headless claude -------------------------------------------------- #
    async def _run_claude(
        self, prompt: str, *, resume: str | None, model: str | None = None
    ) -> dict[str, Any]:
        """Run ``claude -p`` in the project dir and return the parsed JSON.

        The prompt is delivered on stdin (not argv) to dodge OS command-line
        length limits and quoting issues with large questions. The run is bounded
        by ``claude_timeout_seconds`` and the child is killed on timeout. A
        result flagged ``is_error`` (e.g. max-turns exhaustion) is raised, not
        returned as if it were a real answer.
        """
        claude_bin = shutil.which(self.config.claude_bin) or self.config.claude_bin
        argv: list[str] = [
            claude_bin,
            "-p",
            "--output-format",
            "json",
            "--allowedTools",
            self.config.allowed_tools,
            "--max-turns",
            str(self.config.max_turns),
            "--append-system-prompt",
            RESPONDER_SYSTEM_PROMPT,
        ]
        if model:
            argv += ["--model", model]
        if resume:
            argv += ["--resume", resume]

        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(self.project_dir),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=prompt.encode("utf-8")),
                timeout=self.config.claude_timeout_seconds,
            )
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await proc.wait()
            except ProcessLookupError:  # pragma: no cover - already gone
                pass
            # A timeout is NOT a session problem — raise the base type so the
            # self-heal does not wipe an otherwise-valid accumulating session.
            raise ClaudeError(
                f"claude timed out after {self.config.claude_timeout_seconds}s and was killed"
            )

        if proc.returncode != 0:
            # Non-zero exit is where an invalid/unknown --resume id surfaces.
            raise ClaudeExitError(
                f"claude exited {proc.returncode}: {stderr.decode('utf-8', 'replace')[:2000]}"
            )
        try:
            data = json.loads(stdout.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ClaudeError(
                f"could not parse claude JSON output: {stdout.decode('utf-8', 'replace')[:2000]}"
            ) from exc

        # `claude -p --output-format json` exits 0 even on non-fatal failures
        # (error_max_turns, error_during_execution): is_error=true with a partial
        # or empty result. Surface that instead of returning a confident wrong answer.
        if data.get("is_error") is True or str(data.get("subtype", "")).startswith("error"):
            raise ClaudeError(
                f"claude returned an error result (subtype={data.get('subtype')}): "
                f"{str(data.get('result', ''))[:500]}"
            )
        return data

    async def _run_session_turn(self, prompt: str, *, model: str | None = None) -> dict[str, Any]:
        """Run a claude turn that accumulates context: resume the saved session,
        and if a resumed run fails (e.g. a stale/invalid session id), self-heal by
        clearing the session and retrying once from scratch. Persists the new id."""
        resume = self._load_session()
        try:
            result = await self._run_claude(prompt, resume=resume, model=model)
        except ClaudeExitError:
            # Only a non-zero exit on a RESUMED run (the invalid-session signature)
            # justifies discarding context. Timeouts, max-turns, parse errors, etc.
            # propagate so a transient blip never wipes a valid accumulating session.
            if not resume:
                raise
            logger.warning("resumed claude run exited non-zero; clearing session and retrying fresh")
            self._clear_session()
            result = await self._run_claude(prompt, resume=None, model=model)
        session_id = result.get("session_id")
        if isinstance(session_id, str):
            self._save_session(session_id)
        return result

    async def _answer_text_question(
        self, sender: str, question: str
    ) -> tuple[str, dict[str, Any], float | None]:
        result = await self._run_session_turn(build_question_prompt(sender, question))
        answer = str(result.get("result", "")).strip() or "(no answer produced)"
        cost = result.get("total_cost_usd")
        meta = {
            "path": "headless-text",
            "session_id": result.get("session_id"),
            "num_turns": result.get("num_turns"),
            "duration_ms": result.get("duration_ms"),
            "subtype": result.get("subtype"),
        }
        return answer, meta, (float(cost) if isinstance(cost, (int, float)) else None)

    async def _retrieve_context(self, sender: str, question: str) -> str:
        """Best-effort, throwaway text-only retrieval to ground an image answer.

        Run as a FRESH claude (no --resume, never persisted) so this scratchpad
        turn cannot pollute the accumulating text-question session. Failures are
        swallowed: the vision call can still read the image alone.
        """
        try:
            result = await self._run_claude(
                build_retrieval_prompt(sender, question),
                resume=None,
                model=self.config.retrieval_model,
            )
            return str(result.get("result", "")).strip()
        except Exception as exc:  # noqa: BLE001 - retrieval is optional
            logger.warning("context retrieval failed (continuing without it): %s", exc)
            return ""

    async def _answer_image_question(
        self, client: BrokerClient, sender: str, question: str, attachment_ids: list[str]
    ) -> tuple[str, dict[str, Any], float | None]:
        from vision import answer_with_vision  # lazy: only image questions need the SDK

        images: list[tuple[bytes, str]] = []
        for aid in attachment_ids:
            data, media_type = await client.download_attachment(aid)
            images.append((data, media_type))

        context = await self._retrieve_context(sender, question)
        api_key = self.config.require_anthropic_key()
        answer = await asyncio.to_thread(
            answer_with_vision,
            api_key=api_key,
            model=self.config.vision_model,
            sender=sender,
            question=question,
            context=context,
            images=images,
        )
        meta = {"path": "vision-api", "model": self.config.vision_model, "images": len(images)}
        return answer, meta, None

    async def _handle_note(self, sender: str, note: str) -> None:
        """Fire-and-forget note: optionally fold it into the resumed session so
        the resident expert is aware of it later, then log it."""
        if self.config.inject_notes:
            try:
                await self._run_session_turn(build_note_prompt(sender, note))
            except Exception as exc:  # noqa: BLE001 - note injection is best-effort
                logger.warning("note injection failed: %s", exc)
        logger.info("note from %s: %s", sender, note[:200])

    # -- per-message dispatch -------------------------------------------- #
    async def handle_message(self, client: BrokerClient, msg: dict[str, Any]) -> None:
        request_id = msg["request_id"]
        sender = msg.get("sender", "unknown")
        kind = msg.get("kind", "request")
        question = msg.get("question", "")
        attachment_ids = msg.get("attachment_ids", []) or []

        if kind == "note":
            await self._handle_note(sender, question)
            # Ack so the lifecycle closes and a sender can confirm via /reply.
            await client.answer(
                request_id=request_id, answer="noted", meta={"path": "note-ack"}
            )
            return

        try:
            if attachment_ids:
                logger.info("answering image question %s from %s", request_id, sender)
                answer, meta, cost = await self._answer_image_question(
                    client, sender, question, attachment_ids
                )
            else:
                logger.info("answering text question %s from %s", request_id, sender)
                answer, meta, cost = await self._answer_text_question(sender, question)
            if cost is not None:
                logger.info("request %s cost_usd=%.4f", request_id, cost)
            await client.answer(
                request_id=request_id,
                answer=answer,
                is_error=False,
                cost_usd=cost,
                meta=meta,
            )
        except Exception:  # noqa: BLE001 - never let one question kill the loop
            # Log full detail locally; return only a generic message to the peer
            # so internal paths/stderr never leak across the bridge.
            logger.exception("failed to answer %s", request_id)
            await client.answer(
                request_id=request_id,
                answer=_GENERIC_ERROR.format(peer=self.peer, rid=request_id),
                is_error=True,
                meta={"path": "error"},
            )

    # -- main loop ------------------------------------------------------- #
    async def run(self) -> None:
        self._install_signal_handlers()
        logger.info(
            "responder for peer '%s' polling %s (project: %s)",
            self.peer,
            self.config.broker_url,
            self.project_dir,
        )
        async with BrokerClient(
            self.config.broker_url,
            default_timeout=self.config.http_timeout_seconds,
        ) as client:
            while not self._stop.is_set():
                try:
                    msg = await self._poll_or_stop(client)
                except (BrokerError, httpx.HTTPError) as exc:
                    logger.warning("poll error (retrying): %s", exc)
                    await asyncio.sleep(self.config.responder_poll_loop_pause)
                    continue
                except Exception as exc:  # noqa: BLE001 - defensive: keep looping
                    logger.warning("unexpected poll error (retrying): %s", exc)
                    await asyncio.sleep(self.config.responder_poll_loop_pause)
                    continue

                if msg is None:
                    continue  # 204 or shutdown signal: loop condition re-checks
                await self.handle_message(client, msg)
        logger.info("responder for peer '%s' stopped", self.peer)

    async def _poll_or_stop(self, client: BrokerClient) -> dict[str, Any] | None:
        """Long-poll, but wake immediately if a shutdown signal arrives.

        If poll and stop complete in the same cycle, the already-delivered message
        WINS — the broker has already popped it from its queue and marked it
        'delivered', so dropping it here would lose the question until a broker
        restart. run() answers it, then exits on the next ``_stop`` check.
        """
        poll_task = asyncio.ensure_future(client.poll(self.peer, wait=self.config.poll_wait_seconds))
        stop_task = asyncio.ensure_future(self._stop.wait())
        done, _pending = await asyncio.wait(
            {poll_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        # Salvage a successfully completed poll even if stop also fired.
        if poll_task in done and not poll_task.cancelled() and poll_task.exception() is None:
            stop_task.cancel()
            return poll_task.result()
        poll_task.cancel()
        stop_task.cancel()
        if self._stop.is_set():
            return None
        # Poll finished by raising and stop was not set: re-raise to run()'s handler.
        return poll_task.result()

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
            if sig is None:
                continue
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except (NotImplementedError, RuntimeError, ValueError):
                # Windows event loop doesn't support add_signal_handler; SIGINT
                # still raises KeyboardInterrupt and is caught in main().
                pass


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if sys.platform == "win32":
        # Subprocess support requires the Proactor loop; force it in case a
        # parent set the Selector policy.
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    config = Config.from_env()
    responder = Responder(config)
    responder.acquire_lock()
    try:
        asyncio.run(responder.run())
    except KeyboardInterrupt:  # pragma: no cover
        logger.info("responder stopped (interrupt)")


if __name__ == "__main__":
    main()
