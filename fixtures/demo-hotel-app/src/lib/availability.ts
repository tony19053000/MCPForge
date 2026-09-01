import { findRoom } from "@/lib/rooms";
import { getReservationsForRoom } from "@/lib/reservations";
import type { Availability } from "@/lib/types";

export function nightsBetween(checkIn: string, checkOut: string): number {
  const start = new Date(checkIn).getTime();
  const end = new Date(checkOut).getTime();
  if (Number.isNaN(start) || Number.isNaN(end) || end <= start) return 0;
  return Math.round((end - start) / 86_400_000);
}

/** Whether a room is free for a date range, and what it would cost. */
export function checkAvailability(
  roomId: string,
  checkIn: string,
  checkOut: string,
): Availability {
  const room = findRoom(roomId);
  const nights = nightsBetween(checkIn, checkOut);

  if (!room || nights === 0) {
    return { roomId, checkIn, checkOut, available: false, nights: 0, totalPrice: 0 };
  }

  const clash = getReservationsForRoom(roomId).some((reservation) => {
    if (reservation.status === "CANCELLED") return false;
    return reservation.checkIn < checkOut && checkIn < reservation.checkOut;
  });

  return {
    roomId,
    checkIn,
    checkOut,
    available: !clash,
    nights,
    totalPrice: clash ? 0 : nights * room.pricePerNight,
  };
}
