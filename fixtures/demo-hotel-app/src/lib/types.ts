export interface Room {
  id: string;
  name: string;
  capacity: number;
  pricePerNight: number;
  amenities: string[];
}

export interface Availability {
  roomId: string;
  checkIn: string;
  checkOut: string;
  available: boolean;
  nights: number;
  totalPrice: number;
}

export interface Reservation {
  id: string;
  roomId: string;
  guestName: string;
  guestEmail: string;
  checkIn: string;
  checkOut: string;
  guests: number;
  totalPrice: number;
  status: "CONFIRMED" | "CANCELLED";
  createdAt: string;
}
