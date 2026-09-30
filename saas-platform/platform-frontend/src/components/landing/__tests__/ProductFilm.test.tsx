import { render, screen } from '@testing-library/react'
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
    expect(film()).toHaveAttribute('src', 'https://github.com/user-attachments/assets/383e9556-af82-4b4c-bdd4-8ba894481eec')
    expect(film()).toHaveAttribute('poster', 'https://github.com/user-attachments/assets/e3ce6dae-b730-41cc-a1e9-a7292e8e5766')
    expect(film()).toHaveAttribute('controls')
    expect(film()).toHaveAttribute('preload', 'none')
  })

  it('switches to the dark film when the page turns dark', () => {
    ;(useDarkMode as jest.Mock).mockReturnValue({ isDarkMode: false })
    const { rerender } = render(<ProductFilm />)

    ;(useDarkMode as jest.Mock).mockReturnValue({ isDarkMode: true })
    rerender(<ProductFilm />)

    expect(film()).toHaveAttribute('src', 'https://github.com/user-attachments/assets/2c3227cf-0cea-475f-8c27-959a21504348')
    expect(film()).toHaveAttribute('poster', 'https://github.com/user-attachments/assets/70e6d774-5f5c-4d2c-862d-a992136dfd73')
  })
})
