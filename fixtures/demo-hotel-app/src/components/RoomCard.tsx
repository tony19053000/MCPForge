import type { Room } from "@/lib/types";

export function RoomCard({ room, onSelect }: { room: Room; onSelect: (id: string) => void }) {
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
      <button type="button" onClick={() => onSelect(room.id)}>
        Select
      </button>
    </article>
  );
}
