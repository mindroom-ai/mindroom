import { render, screen } from '@testing-library/react'
import { Sidebar } from '../Sidebar'

jest.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ signOut: jest.fn() }) }))

describe('Sidebar', () => {
  it('links support to the support email address in both the mobile and desktop sidebars', () => {
    render(<Sidebar sidebarOpen={false} setSidebarOpen={jest.fn()} />)

    const supportLinks = screen.getAllByRole('link', { name: 'Support' })
    expect(supportLinks.map(link => link.getAttribute('href'))).toEqual([
      'mailto:support@mindroom.chat',
      'mailto:support@mindroom.chat',
    ])
  })
})
