import { Link, useLocation } from 'react-router'
import { Icon, type IconName } from './Icon'

const TABS: { to: string; label: string; icon: IconName; owns: (path: string) => boolean }[] = [
  { to: '/', label: 'Chats', icon: 'chats', owns: (p) => p === '/' || p.startsWith('/archived') || p.startsWith('/chat') },
  { to: '/communities', label: 'Communities', icon: 'communities', owns: (p) => p.startsWith('/communities') },
  { to: '/calls', label: 'Calls', icon: 'calls', owns: (p) => p.startsWith('/calls') },
  { to: '/settings', label: 'Settings', icon: 'settings', owns: (p) => p.startsWith('/settings') },
]

export function TabBar() {
  const { pathname } = useLocation()
  return (
    <nav className="tabs" aria-label="Main">
      {TABS.map((tab) => (
        <Link key={tab.to} to={tab.to} className="tab" aria-current={tab.owns(pathname) ? 'page' : undefined}>
          <Icon name={tab.icon} size={24} />
          <span>{tab.label}</span>
        </Link>
      ))}
    </nav>
  )
}
