from __future__ import annotations

import threading

from crow_os.api import create_app


def test_restart_marks_running_chat_threads_failed_with_transcript_preserved(workspace):
    agent = workspace.chat.create_agent(name="Local test agent", backend="ollama")
    thread = workspace.chat.create_thread(agent["id"], "interrupted on restart")
    workspace.store.execute("UPDATE chat_threads SET status='running' WHERE id=?", (thread["id"],))

    workspace.recover()

    recovered = workspace.chat.thread_detail(thread["id"])
    assert recovered["status"] == "failed"
    assert any(
        message["role"] == "assistant" and "interrupted because the workspace restarted" in message["content"]
        for message in recovered["messages"]
    )
    assert any(
        event["kind"] == "chat_thread_interrupted"
        and event["payload"]["thread_id"] == thread["id"]
        for event in workspace.events()
    )


def test_chat_shutdown_signals_and_joins_active_worker(workspace):
    agent = workspace.chat.create_agent(name="Local test agent", backend="ollama")
    thread = workspace.chat.create_thread(agent["id"], "shutdown test")
    workspace.store.execute("UPDATE chat_threads SET status='running' WHERE id=?", (thread["id"],))

    stop = threading.Event()
    worker = threading.Thread(target=stop.wait, name="test-chat-worker", daemon=True)
    workspace.chat._flags[thread["id"]] = stop
    workspace.chat._threads[thread["id"]] = worker
    worker.start()

    still_running = workspace.chat.shutdown(timeout_s=1.0)

    assert stop.is_set()
    assert not worker.is_alive()
    assert still_running == []
    activity = workspace.store.query_one(
        "SELECT detail FROM chat_activities WHERE thread_id=? ORDER BY id DESC LIMIT 1",
        (thread["id"],),
    )
    assert "workspace server is shutting down" in activity["detail"]


def test_workspace_shutdown_cancels_active_round_and_chat(workspace):
    agent = workspace.chat.create_agent(name="Local test agent", backend="ollama")
    thread = workspace.chat.create_thread(agent["id"], "shutdown recovery")
    workspace.store.execute("UPDATE chat_threads SET status='running' WHERE id=?", (thread["id"],))
    workspace.store.execute(
        """
        INSERT INTO rounds (id, title, ref, commit_sha, runner, status, created_at,
                            env_json, inputs_json, acceptance, sealed)
        VALUES ('round-live', 'active round', 'main', 'abc', 'python', 'running', 'now',
                '{}', '{}', 0, 0)
        """
    )

    result = workspace.shutdown(timeout_s=0.5)

    assert result["rounds_cancel_requested"] == 1
    assert not workspace.runner.flag("round-live").is_set() is False
    assert result["chat_threads_still_running"] == []
