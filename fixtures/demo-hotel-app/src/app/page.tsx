import { RoomCard } from "@/components/RoomCard";
import { listRooms } from "@/lib/rooms";

export default function HomePage() {
  const rooms = listRooms();
  return (
    <main>
      <h1>Seaside Hotel</h1>
      <p>{rooms.length} room types available</p>
      {rooms.map((room) => (
        <RoomCard key={room.id} room={room} />
      ))}
    </main>
  );
}
