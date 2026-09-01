# demo-hotel-app

A small but **real** Next.js App Router hotel application. Not a mock: the
business logic works, and the workflows are genuine.

MCPForge uses it two ways:

- as the **demo project**, so the pipeline can be exercised without connecting a
  private repository
- as the **test fixture** every later phase indexes, analyzes and generates against

## Workflows a WebMCP tool should expose

| Workflow | Function | Risk |
|---|---|---|
| Search rooms | `searchRooms` in `src/lib/rooms.ts` | READ |
| Check availability | `checkAvailability` in `src/lib/availability.ts` | READ |
| Create a reservation | `createReservation` in `src/lib/reservations.ts` | WRITE |
| Cancel a reservation | `cancelReservation` in `src/lib/reservations.ts` | DESTRUCTIVE |

A generated tool must **call these functions**, not reimplement them. That is
the property `F5-02` asserts with an AST check.
