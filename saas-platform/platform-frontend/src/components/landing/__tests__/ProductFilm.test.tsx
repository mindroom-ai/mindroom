import { fireEvent, render, screen } from '@testing-library/react'
import { useDarkMode } from '@/hooks/useDarkMode'
import { ProductFilm } from '../ProductFilm'

jest.mock('@/hooks/useDarkMode', () => ({
  useDarkMode: jest.fn(),
}))

const film = () => screen.getByLabelText('MindRoom product film')

describe('ProductFilm', () => {
  it('plays the light film with player controls in light mode', () => {
    ;(useDarkMode as jest.Mock).mockReturnValue({ isDarkMode: false })
    render(<ProductFilm />)

    expect(film().tagName).toBe('VIDEO')
    expect(film()).toHaveAttribute('src', 'https://github.com/user-attachments/assets/f35866c4-c226-408a-9f1f-b7588f76c564')
    expect(film()).toHaveAttribute('poster', 'https://github.com/user-attachments/assets/88accc8b-6666-494b-a415-6f7effaaf751')
    expect(film()).toHaveAttribute('controls')
    expect(film()).toHaveAttribute('preload', 'none')
  })

  it('switches to the dark film when the page turns dark', () => {
    ;(useDarkMode as jest.Mock).mockReturnValue({ isDarkMode: false })
    const { rerender } = render(<ProductFilm />)

    ;(useDarkMode as jest.Mock).mockReturnValue({ isDarkMode: true })
    rerender(<ProductFilm />)

    expect(film()).toHaveAttribute('src', 'https://github.com/user-attachments/assets/f99556da-f289-49fe-a365-7c2a8bb93c77')
    expect(film()).toHaveAttribute('poster', 'https://github.com/user-attachments/assets/7c8011bd-22b7-4795-8d4d-ab2864c7b118')
  })

  it('keeps the film the reader started when the page turns dark', () => {
    ;(useDarkMode as jest.Mock).mockReturnValue({ isDarkMode: false })
    const { rerender } = render(<ProductFilm />)
    const started = film()
    fireEvent.play(started)

    ;(useDarkMode as jest.Mock).mockReturnValue({ isDarkMode: true })
    rerender(<ProductFilm />)

    expect(film()).toBe(started)
    expect(film()).toHaveAttribute('src', 'https://github.com/user-attachments/assets/f35866c4-c226-408a-9f1f-b7588f76c564')
    expect(film()).toHaveAttribute('poster', 'https://github.com/user-attachments/assets/88accc8b-6666-494b-a415-6f7effaaf751')
  })
})
