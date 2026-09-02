"use client";

import { useState } from "react";

export function BookingForm({ roomId, maxGuests = 4 }: { roomId: string; maxGuests?: number }) {
  const [guestName, setGuestName] = useState("");
  const [guestEmail, setGuestEmail] = useState("");
  const [checkIn, setCheckIn] = useState("");
  const [checkOut, setCheckOut] = useState("");
  const [guests, setGuests] = useState(2);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    const response = await fetch("/api/reservations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ roomId, guestName, guestEmail, checkIn, checkOut, guests }),
    });
    if (!response.ok) {
      const body = await response.json();
      setError(body.error ?? "Booking failed");
    }
  }

  return (
    <form onSubmit={submit}>
      <input value={guestName} onChange={(e) => setGuestName(e.target.value)} placeholder="Name" />
      <input value={guestEmail} onChange={(e) => setGuestEmail(e.target.value)} placeholder="Email" />
      <input type="date" value={checkIn} onChange={(e) => setCheckIn(e.target.value)} />
      <input type="date" value={checkOut} onChange={(e) => setCheckOut(e.target.value)} />
      <input
        type="number"
        min={1}
        max={maxGuests}
        value={guests}
        onChange={(e) => setGuests(Number(e.target.value))}
      />
      <button type="submit">Book</button>
      {error ? <p role="alert">{error}</p> : null}
    </form>
  );
}
