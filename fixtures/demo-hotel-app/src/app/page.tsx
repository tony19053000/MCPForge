import { RoomCard } from "@/components/RoomCard";
import { listRooms } from "@/lib/rooms";

export default function HomePage() {
  const rooms = listRooms();
  return (
    <main>
      <h1>Seaside Hotel</h1>
      {rooms.map((room) => (
        <RoomCard key={room.id} room={room} onSelect={() => {}} />
      ))}
    </main>
  );
}
