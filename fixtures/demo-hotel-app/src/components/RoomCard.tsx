import Link from "next/link";
import type { Room } from "@/lib/types";

/**
 * Presentational, and deliberately server-renderable: it takes data and renders
 * a link, not a callback. A server component cannot pass an event handler to a
 * child, which is what an earlier version tried to do.
 */
export function RoomCard({ room }: { room: Room }) {
  return (
    <article>
      <h3>{room.name}</h3>
      <p>Sleeps {room.capacity}</p>
      <p>${room.pricePerNight} per night</p>
      <ul>
        {room.amenities.map((a) => (
          <li key={a}>{a}</li>
        ))}
      </ul>
      <Link href={`/book?room=${room.id}`}>Select this room</Link>
    </article>
  );
}
