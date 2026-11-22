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
  archive: (
    <>
      <rect x="3" y="4" width="18" height="4.5" rx="1.2" />
      <path d="M5 8.5V19a1 1 0 001 1h12a1 1 0 001-1V8.5" />
      <path d="M10 12.5h4" />
    </>
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

export function Icon({ name, size = 20 }: { name: IconName; size?: number }) {
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
      aria-hidden="true"
    >
      {PATHS[name]}
    </svg>
  )
}
