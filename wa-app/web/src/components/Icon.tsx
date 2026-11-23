import type { ReactNode } from 'react'

// Paths are the 24px line icons of the picked design (a-familiar), stroke 1.75, drawn in currentColor.
const PATHS: Record<string, ReactNode> = {
  search: (
    <>
      <circle cx="11" cy="11" r="6.5" />
      <path d="M16 16l4.5 4.5" />
    </>
  ),
  pin: (
    <>
      <path d="M9 3h6l-1 6 3 3v1.5H7V12l3-3z" />
      <path d="M12 13.5V21" />
    </>
  ),
  mute: (
    <>
      <path d="M4 9v6h3.5L12 19V5L7.5 9z" />
      <path d="M16 9.5l5 5M21 9.5l-5 5" />
    </>
  ),
  check: <path d="M5 12.5l4.5 4.5L19 7.5" />,
  checks: (
    <>
      <path d="M2.5 12.5L7 17l9-9.5" />
      <path d="M11.5 15.5l1.5 1.5 8.5-9.5" />
    </>
  ),
  bellOff: (
    <>
      <path d="M8.5 5.5A5.5 5.5 0 0117.5 10v3l1.5 3H8M6.5 9.5V13L5 16h1.5" />
      <path d="M10 19.5a2 2 0 004 0M4 4l16 16" />
    </>
  ),
  archive: (
    <>
      <rect x="3" y="4" width="18" height="4.5" rx="1.2" />
      <path d="M5 8.5V19a1 1 0 001 1h12a1 1 0 001-1V8.5" />
      <path d="M10 12.5h4" />
    </>
  ),
  forward: <path d="M14 6l6 5.5-6 5.5v-3.5c-4.5 0-7.5 1-9.5 4.5.5-5 3.5-8.5 9.5-9z" />,
  image: (
    <>
      <rect x="3.5" y="5" width="17" height="14" rx="2" />
      <circle cx="9" cy="10.5" r="1.5" />
      <path d="M4 17.5l5-4.5 4 3.5 3-2.5 4 3.5" />
    </>
  ),
  video: (
    <>
      <rect x="3" y="6" width="13" height="12" rx="2" />
      <path d="M16 10.5l5-3v9l-5-3" />
    </>
  ),
  file: (
    <>
      <path d="M6 3h8l4 4v13a1 1 0 01-1 1H6a1 1 0 01-1-1V4a1 1 0 011-1z" />
      <path d="M14 3v4h4" />
    </>
  ),
  mic: (
    <>
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5.5 11.5a6.5 6.5 0 0013 0M12 18v3" />
    </>
  ),
  play: <path d="M8 5.5v13l11-6.5z" fill="currentColor" />,
  pause: (
    <>
      <path d="M8.5 5.5v13M15.5 5.5v13" strokeWidth={3} />
    </>
  ),
  send: <path d="M4 12l16-8-6 16-3-7z" />,
  attach: <path d="M19 11.5l-7 7a4.5 4.5 0 01-6.4-6.4l7.5-7.5a3 3 0 014.2 4.2l-7.4 7.4a1.5 1.5 0 01-2.1-2.1l6.7-6.7" />,
  reply: <path d="M10 6L4 11.5 10 17v-3.5c4.5 0 7.5 1 9.5 4.5-.5-5-3.5-8.5-9.5-9z" />,
  copy: (
    <>
      <rect x="8" y="8" width="11" height="11" rx="2" />
      <path d="M16 8V6a1 1 0 00-1-1H6a1 1 0 00-1 1v9a1 1 0 001 1h2" />
    </>
  ),
  sparkle: (
    <>
      <path d="M10 4l1.8 5.2L17 11l-5.2 1.8L10 18l-1.8-5.2L3 11l5.2-1.8z" />
      <path d="M18 3.5v4M16 5.5h4" />
    </>
  ),
  more: (
    <>
      <circle cx="12" cy="5.5" r="1.2" fill="currentColor" />
      <circle cx="12" cy="12" r="1.2" fill="currentColor" />
      <circle cx="12" cy="18.5" r="1.2" fill="currentColor" />
    </>
  ),
  close: <path d="M6 6l12 12M18 6L6 18" />,
  chart: <path d="M5 20V10M12 20V4M19 20v-7" />,
  phone: (
    <path d="M6.5 4h3l1.5 4-2 1.5a11 11 0 005.5 5.5L16 13l4 1.5v3a2 2 0 01-2 2C10.8 19.5 4.5 13.2 4.5 6a2 2 0 012-2z" />
  ),
  chevron: <path d="M9 5l7 7-7 7" />,
  back: <path d="M15 5l-7 7 7 7" />,
  megaphone: (
    <>
      <path d="M4 10v4h3l7 4V6L7 10z" />
      <path d="M17.5 9.5a4 4 0 010 5" />
    </>
  ),
  person: (
    <>
      <circle cx="12" cy="9" r="3.5" />
      <path d="M5 20c.8-3.8 3.5-5.5 7-5.5s6.2 1.7 7 5.5" />
    </>
  ),
  ban: (
    <>
      <circle cx="12" cy="12" r="8" />
      <path d="M6.3 6.3l11.4 11.4" />
    </>
  ),
  chats: <path d="M5 5h14a2 2 0 012 2v8a2 2 0 01-2 2h-6l-4.5 3.5V17H5a2 2 0 01-2-2V7a2 2 0 012-2z" />,
  communities: (
    <>
      <circle cx="9" cy="9" r="3" />
      <circle cx="17" cy="10" r="2.3" />
      <path d="M3.5 19c.5-3 2.7-4.7 5.5-4.7s5 1.7 5.5 4.7" />
      <path d="M15.5 14.6c2.6-.3 4.6 1 5 3.8" />
    </>
  ),
  calls: (
    <path d="M6.5 4h3l1.5 4-2 1.5a11 11 0 005.5 5.5L16 13l4 1.5v3a2 2 0 01-2 2C10.8 19.5 4.5 13.2 4.5 6a2 2 0 012-2z" />
  ),
  settings: (
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M12 3v2.5M12 18.5V21M3 12h2.5M18.5 12H21M5.6 5.6l1.8 1.8M16.6 16.6l1.8 1.8M5.6 18.4l1.8-1.8M16.6 7.4l1.8-1.8" />
    </>
  ),
}

export type IconName = keyof typeof PATHS

export function Icon({ name, size = 20, label }: { name: IconName; size?: number; label?: string }) {
  return (
    <svg
      className="ic"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden={label ? undefined : true}
      role={label ? 'img' : undefined}
      aria-label={label}
    >
      {PATHS[name]}
    </svg>
  )
}
