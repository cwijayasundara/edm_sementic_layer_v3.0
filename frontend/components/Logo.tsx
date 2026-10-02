export function Logo({ className = "size-7" }: { className?: string }) {
  return (
    <svg viewBox="0 0 32 32" className={className} aria-hidden>
      <rect width="32" height="32" rx="7" fill="#1d2d50" />
      <path d="M16 6 L26 24 H6 Z" fill="none" stroke="#fff" strokeWidth="2" strokeLinejoin="round" />
      <rect x="9" y="25" width="14" height="2.5" rx="1" fill="#b3123a" />
    </svg>
  );
}
