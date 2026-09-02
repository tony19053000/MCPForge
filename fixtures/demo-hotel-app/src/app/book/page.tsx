import { BookingForm } from "@/components/BookingForm";
import { findRoom, listRooms } from "@/lib/rooms";

export default async function BookPage({
  searchParams,
}: {
  searchParams: Promise<{ room?: string }>;
}) {
  const { room: requested } = await searchParams;
  const room = (requested ? findRoom(requested) : undefined) ?? listRooms()[0]!;

  return (
    <main>
      <h1>Book {room.name}</h1>
      <p>
        Sleeps {room.capacity} · ${room.pricePerNight} per night
      </p>
      <BookingForm roomId={room.id} maxGuests={room.capacity} />
    </main>
  );
}
