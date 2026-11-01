import { render, screen } from '@testing-library/react'
import { Header } from '../Header'

jest.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ user: { email: 'owner@example.com' } }) }))
jest.mock('@/components/DarkModeToggle', () => ({ DarkModeToggle: () => null }))

describe('Header', () => {
  it('offers only the sidebar toggle beside the dark mode toggle', () => {
    render(<Header sidebarOpen={false} setSidebarOpen={jest.fn()} />)

    expect(screen.getByText('owner@example.com')).toBeInTheDocument()
    expect(screen.getAllByRole('button').map(button => button.textContent)).toEqual(['Open sidebar'])
  })
})
