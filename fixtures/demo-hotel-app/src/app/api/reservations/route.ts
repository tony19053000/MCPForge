import { NextResponse } from "next/server";
import { BookingError, createReservation, listReservations } from "@/lib/reservations";

export async function GET() {
  return NextResponse.json(listReservations());
}

export async function POST(request: Request) {
  try {
    const body = await request.json();
    return NextResponse.json(createReservation(body), { status: 201 });
  } catch (error) {
    if (error instanceof BookingError) {
      return NextResponse.json({ error: error.message }, { status: 400 });
    }
    return NextResponse.json({ error: "Unexpected error" }, { status: 500 });
  }
}
