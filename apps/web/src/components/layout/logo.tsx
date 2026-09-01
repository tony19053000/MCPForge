export function Logo({ className }: { className?: string }) {
  return (
    <span className={className}>
      <span className="font-semibold tracking-tight text-text">MCP</span>
      <span className="font-semibold tracking-tight text-accent">Forge</span>
    </span>
  );
}
