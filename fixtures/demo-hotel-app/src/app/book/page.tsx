import { BookingForm } from "@/components/BookingForm";

export default function BookPage() {
  return (
    <main>
      <h1>Book a room</h1>
      <BookingForm roomId="std-queen" />
    </main>
  );
}
