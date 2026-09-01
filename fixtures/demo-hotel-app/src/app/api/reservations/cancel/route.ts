import { NextResponse } from "next/server";
import { BookingError, cancelReservation } from "@/lib/reservations";

export async function POST(request: Request) {
  try {
    const { reservationId } = await request.json();
    return NextResponse.json(cancelReservation(reservationId));
  } catch (error) {
    if (error instanceof BookingError) {
      return NextResponse.json({ error: error.message }, { status: 400 });
    }
    return NextResponse.json({ error: "Unexpected error" }, { status: 500 });
  }
}
