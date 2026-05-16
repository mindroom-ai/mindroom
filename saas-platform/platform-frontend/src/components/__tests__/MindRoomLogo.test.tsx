import { render, screen } from '@testing-library/react'
import { MindRoomLogo } from '../MindRoomLogo'

describe('MindRoomLogo', () => {
  it('renders the shared MindRoom brand asset from public files', () => {
    render(<MindRoomLogo size={40} className="transition-transform" />)

    const logo = screen.getByRole('img', { name: 'MindRoom logo' })

    expect(logo.tagName.toLowerCase()).toBe('img')
    expect(logo).toHaveAttribute('src', '/res/branding/mindroom.svg')
    expect(logo).toHaveAttribute('width', '40')
    expect(logo).toHaveAttribute('height', '40')
    expect(logo).toHaveClass('transition-transform')
  })
})
