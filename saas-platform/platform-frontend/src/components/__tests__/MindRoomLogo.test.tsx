import { render, screen } from '@testing-library/react'
import { MindRoomLogo } from '../MindRoomLogo'

describe('MindRoomLogo', () => {
  it('renders an inline logo without requesting a missing optimized image asset', () => {
    render(<MindRoomLogo size={40} className="text-orange-500" />)

    const logo = screen.getByRole('img', { name: 'MindRoom logo' })

    expect(logo.tagName.toLowerCase()).toBe('svg')
    expect(logo).toHaveAttribute('width', '40')
    expect(logo).toHaveAttribute('height', '40')
    expect(document.querySelector('img[src*="logo.png"]')).not.toBeInTheDocument()
  })
})
