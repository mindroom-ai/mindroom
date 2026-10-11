import { act, fireEvent, render, screen } from '@testing-library/react'
import { useRouter } from 'next/navigation'
import DashboardLayout from '../layout'
import { DarkModeProvider } from '@/hooks/useDarkMode'
import { clearSsoCookie } from '@/lib/api'
import { createClient } from '@/lib/supabase/client'

jest.mock('@/lib/supabase/client', () => ({ createClient: jest.fn() }))
jest.mock('@/lib/api', () => ({ clearSsoCookie: jest.fn() }))

// The development flag is read when the auth module loads.
jest.mock('@/hooks/useAuth', () => {
  const originalEnv = process.env
  process.env = { ...originalEnv, NODE_ENV: 'development', NEXT_PUBLIC_DEV_AUTH: 'true' }
  try {
    return jest.requireActual('@/hooks/useAuth')
  } finally {
    process.env = originalEnv
  }
})

it('keeps development dashboard auth local, including sign-out', async () => {
  const router = {
    push: jest.fn(),
    replace: jest.fn(),
    refresh: jest.fn(),
    back: jest.fn(),
    forward: jest.fn(),
    prefetch: jest.fn(),
  }
  const auth = {
    getUser: jest.fn(),
    onAuthStateChange: jest.fn(),
    signOut: jest.fn(),
  }
  jest.mocked(useRouter).mockReturnValue(router)
  jest.mocked(createClient).mockReturnValue({ auth } as unknown as ReturnType<typeof createClient>)

  render(
    <DarkModeProvider>
      <DashboardLayout><p>Dashboard content</p></DashboardLayout>
    </DarkModeProvider>
  )

  expect(screen.getByText('Dashboard content')).toBeInTheDocument()
  expect(screen.getByText('dev@mindroom.local')).toBeInTheDocument()
  expect(auth.getUser).not.toHaveBeenCalled()
  expect(auth.onAuthStateChange).not.toHaveBeenCalled()

  await act(async () => {
    fireEvent.click(screen.getAllByRole('button', { name: 'Sign out' })[0])
  })

  expect(router.push).toHaveBeenCalledWith('/')
  expect(clearSsoCookie).not.toHaveBeenCalled()
  expect(auth.signOut).not.toHaveBeenCalled()
})
