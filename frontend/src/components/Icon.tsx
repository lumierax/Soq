import type { CSSProperties } from 'react';

const paths: Record<string, React.ReactNode> = {
  pulse: <><path d="M2 12h4l3-8 6 16 3-8h4" /></>,
  chart: <><path d="M4 19V5M4 19h16M8 14l4-5 4 3 5-7" /></>,
  coins: <><ellipse cx="9" cy="6" rx="6" ry="3" /><path d="M3 6v5c0 1.7 2.7 3 6 3 1.3 0 2.5-.2 3.5-.6M3 11v5c0 1.7 2.7 3 6 3" /><ellipse cx="17" cy="14" rx="4" ry="2" /><path d="M13 14v5c0 1.1 1.8 2 4 2s4-.9 4-2v-5" /></>,
  bolt: <path d="M13 2 4 14h7l-1 8 10-12h-7l0-8Z" />,
  bot: <><rect x="4" y="7" width="16" height="13" rx="4" /><path d="M12 3v4M9 15h6M1 11v5M23 11v5" /><circle cx="8" cy="11" r=".7" /><circle cx="16" cy="11" r=".7" /></>,
  settings: <><path d="m12 3 2 3 4-.1.5 3.8 3.2 2.3-3.2 2.3L18 18l-4-.1-2 3-2-3-4 .1-.5-3.8L2.3 12l3.2-2.3L6 6l4 .1 2-3Z" /><circle cx="12" cy="12" r="3" /></>,
  search: <><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 5 5" /></>,
  star: <path d="m12 2 3.1 6.3 6.9 1-5 4.9 1.2 6.8-6.2-3.2L5.8 21 7 14.2l-5-4.9 6.9-1L12 2Z" />,
  arrow: <><path d="M6 17 18 5M6 5h12v12" /></>,
  down: <path d="m6 9 6 6 6-6" />,
  close: <><path d="m6 6 12 12M6 18 18 6" /></>,
  refresh: <><path d="M20 7v5h-5M4 17v-5h5" /><path d="M5.6 8a7 7 0 0 1 11.8-2L20 9M4 15l2.6 3A7 7 0 0 0 18.4 16" /></>,
  shield: <><path d="m12 2 9 4v6c0 5-9 10-9 10S3 17 3 12V6l9-4Z" /><path d="m8 12 3 3 5-6" /></>,
  clock: <><circle cx="12" cy="12" r="9" /><path d="M12 6v6l4 2" /></>,
  wallet: <><path d="M20 7H5a2 2 0 0 1 0-4h13v4M5 7H3v12a2 2 0 0 0 2 2h15V7Z" /><path d="M16 12h6v5h-6v-5Z" /></>,
  play: <path d="m8 4 12 8-12 8V4Z" />,
  pause: <><path d="M7 4v16M17 4v16" /></>,
  check: <path d="m5 12 4 4L19 6" />,
  flask: <><path d="M8 3h8M10 3v7L4 20c-.3.5.1 1 1 1h14c.9 0 1.3-.5 1-1l-6-10V3M7 15h10" /></>,
  trend: <><path d="m3 17 6-6 4 4 8-11M15 4h6v6" /></>,
  grid: <><rect x="3" y="3" width="7" height="7" rx="2" /><rect x="14" y="3" width="7" height="7" rx="2" /><rect x="3" y="14" width="7" height="7" rx="2" /><rect x="14" y="14" width="7" height="7" rx="2" /></>,
  eye: <><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7S2 12 2 12Z" /><circle cx="12" cy="12" r="3" /></>,
  info: <><circle cx="12" cy="12" r="9" /><path d="M12 11v6M12 7h.01" /></>,
};

export default function Icon({ name, className = '', style }: { name: string; className?: string; style?: CSSProperties }) {
  return <svg aria-hidden="true" className={`icon ${className}`} style={style} viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round">{paths[name] ?? paths.chart}</svg>;
}
