import { checkAvailability } from "@/lib/availability";
import { findRoom } from "@/lib/rooms";
import type { Reservation } from "@/lib/types";

/** In-memory store. A real app would use a database. */
const RESERVATIONS = new Map<string, Reservation>();

export function getReservationsForRoom(roomId: string): Reservation[] {
  return [...RESERVATIONS.values()].filter((r) => r.roomId === roomId);
}

export function getReservation(id: string): Reservation | undefined {
  return RESERVATIONS.get(id);
}

export function listReservations(): Reservation[] {
  return [...RESERVATIONS.values()];
}

export class BookingError extends Error {}

/**
 * Create a reservation. WRITE — this changes state and takes the guest's money,
 * so a generated WebMCP tool for it must require human approval.
 */
export function createReservation(input: {
  roomId: string;
  guestName: string;
  guestEmail: string;
  checkIn: string;
  checkOut: string;
  guests: number;
}): Reservation {
  const room = findRoom(input.roomId);
  if (!room) throw new BookingError(`Unknown room: ${input.roomId}`);
  if (input.guests > room.capacity) {
    throw new BookingError(`${room.name} sleeps ${room.capacity}, not ${input.guests}`);
  }

  const availability = checkAvailability(input.roomId, input.checkIn, input.checkOut);
  if (!availability.available) {
    throw new BookingError("Those dates are not available");
  }

  const reservation: Reservation = {
    id: `res_${Math.random().toString(36).slice(2, 10)}`,
    roomId: input.roomId,
    guestName: input.guestName,
    guestEmail: input.guestEmail,
    checkIn: input.checkIn,
    checkOut: input.checkOut,
    guests: input.guests,
    totalPrice: availability.totalPrice,
    status: "CONFIRMED",
    createdAt: new Date().toISOString(),
  };
  RESERVATIONS.set(reservation.id, reservation);
  return reservation;
}

/**
 * Cancel a reservation. DESTRUCTIVE — irreversible for the guest, so a
 * generated tool must require explicit confirmation.
 */
export function cancelReservation(id: string): Reservation {
  const reservation = RESERVATIONS.get(id);
  if (!reservation) throw new BookingError(`Unknown reservation: ${id}`);
  if (reservation.status === "CANCELLED") throw new BookingError("Already cancelled");

  const cancelled: Reservation = { ...reservation, status: "CANCELLED" };
  RESERVATIONS.set(id, cancelled);
  return cancelled;
}
