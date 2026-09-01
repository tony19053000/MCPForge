"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { ActivityTimeline } from "@/components/workspace/activity-timeline";
import type { ChatEvent, EventDto, TurnDto } from "@/lib/api/types";

/**
 * The workspace conversation — 04_FRONTEND_SPEC.md §2.2.
 *
 * Conversation turns and activity share one chronological column: the run is
 * the conversation, not a sidebar to it.
 */

export interface ChatTransport {
  chat(sessionId: string, message: string, signal?: AbortSignal): AsyncGenerator<ChatEvent>;
}

export function Chat({
  sessionId,
  transport,
  initialTurns = [],
}: {
  sessionId: string;
  transport: ChatTransport;
  initialTurns?: readonly TurnDto[];
}) {
  const [turns, setTurns] = useState<TurnDto[]>([...initialTurns]);
  const [events, setEvents] = useState<EventDto[]>([]);
  const [streaming, setStreaming] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => () => abortRef.current?.abort(), []);

  const send = useCallback(async () => {
    const message = draft.trim();
    if (!message || busy) return;

    setDraft("");
    setError(null);
    setBusy(true);
    setStreaming("");
    setTurns((prev) => [
      ...prev,
      { id: `local-${Date.now()}`, role: "user", text: message, origin: "HUMAN" },
    ]);

    const controller = new AbortController();
    abortRef.current = controller;
    let collected = "";

    try {
      for await (const event of transport.chat(sessionId, message, controller.signal)) {
        if (event.type === "delta") {
          collected += event.text;
          setStreaming(collected);
        } else if (event.type === "activity") {
          setEvents((prev) => [
            ...prev,
            {
              id: event.id,
              kind: event.kind,
              label: event.label,
              detail: {},
              origin: "SYSTEM",
              created_at: new Date().toISOString(),
            },
          ]);
        } else if (event.type === "error") {
          setError(event.message);
        }
      }
      if (collected.trim()) {
        setTurns((prev) => [
          ...prev,
          {
            id: `assistant-${Date.now()}`,
            role: "assistant",
            text: collected.trim(),
            origin: "SYSTEM",
          },
        ]);
      }
    } catch (e) {
      if (!controller.signal.aborted) {
        setError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setStreaming("");
      setBusy(false);
      abortRef.current = null;
    }
  }, [draft, busy, sessionId, transport]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex-1 overflow-y-auto p-6">
        <ol className="flex flex-col gap-5">
          {turns.map((turn) => (
            <li key={turn.id} className="flex flex-col gap-1">
              <span className="text-xs font-medium uppercase tracking-wide text-subtle">
                {turn.role === "user" ? "You" : "MCPForge"}
              </span>
              <p className="whitespace-pre-wrap text-sm leading-relaxed text-text">{turn.text}</p>
            </li>
          ))}

          {streaming ? (
            <li className="flex flex-col gap-1">
              <span className="text-xs font-medium uppercase tracking-wide text-subtle">
                MCPForge
              </span>
              <p className="whitespace-pre-wrap text-sm leading-relaxed text-text">
                {streaming}
                <span aria-hidden="true" className="ml-0.5 inline-block animate-pulse">
                  ▍
                </span>
              </p>
            </li>
          ) : null}
        </ol>

        {events.length > 0 ? (
          <div className="mt-6 border-t border-border pt-4">
            <ActivityTimeline events={events} />
          </div>
        ) : null}

        {error ? (
          <p role="alert" className="mt-4 rounded-control bg-danger-subtle p-3 text-sm text-text">
            {error}
          </p>
        ) : null}
      </div>

      <div className="border-t border-border p-4">
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            void send();
          }}
        >
          <label htmlFor="composer" className="sr-only">
            Message MCPForge
          </label>
          <input
            id="composer"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            disabled={busy}
            placeholder="Make my booking workflow WebMCP compatible."
            className="min-w-0 flex-1 rounded-control border border-border-strong bg-surface px-3 py-2 text-sm text-text placeholder:text-subtle"
          />
          <Button type="submit" disabled={busy || draft.trim().length === 0}>
            {busy ? "Working…" : "Send"}
          </Button>
        </form>
        <p aria-live="polite" className="sr-only">
          {busy ? "MCPForge is responding" : "Ready"}
        </p>
      </div>
    </div>
  );
}
