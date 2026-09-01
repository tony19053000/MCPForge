import { NextResponse } from "next/server";
import { searchRooms } from "@/lib/rooms";

export async function GET(request: Request) {
  const params = new URL(request.url).searchParams;
  const guests = params.get("guests");
  const maxPrice = params.get("maxPrice");

  return NextResponse.json(
    searchRooms({
      guests: guests ? Number(guests) : undefined,
      maxPrice: maxPrice ? Number(maxPrice) : undefined,
    }),
  );
}
