import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { UpdateRow } from './components/UpdateRow'
import { announceUpdate, resetUpdate, watchForUpdate } from './update'

afterEach(() => {
  cleanup()
  resetUpdate()
})

describe('update notice', () => {
  it('shows nothing until an update is announced, then the Reload row', () => {
    render(<UpdateRow />)
    expect(screen.queryByText('Update available')).toBeNull()
    act(announceUpdate)
    expect(screen.getByText('Update available')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Reload' })).toBeTruthy()
  })

  it('announces a controller change only when a worker already controlled the page', () => {
    const target = new EventTarget() as EventTarget & { controller: unknown }
    target.controller = null
    Object.defineProperty(navigator, 'serviceWorker', { value: target, configurable: true })
    render(<UpdateRow banner />)
    watchForUpdate()
    act(() => void target.dispatchEvent(new Event('controllerchange'))) // first install: no controller before
    expect(screen.queryByText('Update available')).toBeNull()
    act(() => void target.dispatchEvent(new Event('controllerchange'))) // a later takeover
    expect(screen.getByText('Update available')).toBeTruthy()
  })
})
