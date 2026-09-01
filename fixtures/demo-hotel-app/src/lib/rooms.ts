import type { Room } from "@/lib/types";

/**
 * The hotel's inventory. A real app would read this from a database; the shape
 * is what matters for the demo.
 */
const ROOMS: Room[] = [
  {
    id: "std-queen",
    name: "Standard Queen",
    capacity: 2,
    pricePerNight: 120,
    amenities: ["wifi", "tv"],
  },
  {
    id: "deluxe-king",
    name: "Deluxe King",
    capacity: 2,
    pricePerNight: 195,
    amenities: ["wifi", "tv", "balcony", "minibar"],
  },
  {
    id: "family-suite",
    name: "Family Suite",
    capacity: 4,
    pricePerNight: 260,
    amenities: ["wifi", "tv", "kitchenette", "sofa-bed"],
  },
];

export function listRooms(): Room[] {
  return ROOMS;
}

export function findRoom(roomId: string): Room | undefined {
  return ROOMS.find((room) => room.id === roomId);
}

/** Search the inventory. This is the business logic a WebMCP tool should call. */
export function searchRooms(params: { guests?: number; maxPrice?: number }): Room[] {
  return ROOMS.filter((room) => {
    if (params.guests !== undefined && room.capacity < params.guests) return false;
    if (params.maxPrice !== undefined && room.pricePerNight > params.maxPrice) return false;
    return true;
  });
}
