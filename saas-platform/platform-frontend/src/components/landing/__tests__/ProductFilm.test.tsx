import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useDarkMode } from '@/hooks/useDarkMode'
import { ProductFilm } from '../ProductFilm'

jest.mock('@/hooks/useDarkMode', () => ({
  useDarkMode: jest.fn(),
}))

const film = () => screen.getByLabelText('MindRoom product film')

describe('ProductFilm', () => {
  it('shows the light film behind a play button in light mode', () => {
    ;(useDarkMode as jest.Mock).mockReturnValue({ isDarkMode: false })
    render(<ProductFilm />)

    expect(film().tagName).toBe('VIDEO')
    expect(film()).toHaveAttribute('src', 'https://github.com/user-attachments/assets/fcc38c9c-61ba-4db4-b026-4f947c931381')
    expect(film()).toHaveAttribute('poster', 'https://github.com/user-attachments/assets/1e6ecd17-024a-497d-8c12-041dbeaf5438')
    expect(film()).not.toHaveAttribute('controls')
    expect(film()).toHaveAttribute('preload', 'none')
    expect(screen.getByRole('button', { name: 'Play the MindRoom film' })).toBeInTheDocument()
  })

  it('starts playback when the play button is pressed', () => {
    ;(useDarkMode as jest.Mock).mockReturnValue({ isDarkMode: false })
    const play = jest.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined)
    render(<ProductFilm />)

    fireEvent.click(screen.getByRole('button', { name: 'Play the MindRoom film' }))

    expect(play).toHaveBeenCalledTimes(1)
    play.mockRestore()
  })

  it('falls back to the player controls when the browser refuses to play', async () => {
    ;(useDarkMode as jest.Mock).mockReturnValue({ isDarkMode: false })
    const play = jest.spyOn(HTMLMediaElement.prototype, 'play').mockRejectedValue(new DOMException('Blocked', 'NotAllowedError'))
    render(<ProductFilm />)

    fireEvent.click(screen.getByRole('button', { name: 'Play the MindRoom film' }))

    await waitFor(() => expect(film()).toHaveAttribute('controls'))
    play.mockRestore()
  })

  it('moves keyboard focus to the player when the play button is pressed', () => {
    ;(useDarkMode as jest.Mock).mockReturnValue({ isDarkMode: false })
    const play = jest.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined)
    render(<ProductFilm />)
    const button = screen.getByRole('button', { name: 'Play the MindRoom film' })
    button.focus()

    fireEvent.click(button)
    fireEvent.play(film())

    expect(film()).toHaveFocus()
    play.mockRestore()
  })

  it('draws the play button and player focus rings inside the rounded frame', () => {
    ;(useDarkMode as jest.Mock).mockReturnValue({ isDarkMode: false })
    render(<ProductFilm />)

    // The frame clips overflow, so a ring drawn outside either element would be invisible.
    expect(screen.getByRole('button', { name: 'Play the MindRoom film' })).toHaveClass('focus-visible:outline-offset-[-6px]')
    expect(film()).toHaveClass('focus-visible:outline-offset-[-6px]')
  })

  it('shows the player controls once the film plays', () => {
    ;(useDarkMode as jest.Mock).mockReturnValue({ isDarkMode: false })
    render(<ProductFilm />)

    fireEvent.play(film())

    expect(film()).toHaveAttribute('controls')
    expect(screen.queryByRole('button', { name: 'Play the MindRoom film' })).not.toBeInTheDocument()
  })

  it('switches to the dark film when the page turns dark', () => {
    ;(useDarkMode as jest.Mock).mockReturnValue({ isDarkMode: false })
    const { rerender } = render(<ProductFilm />)

    ;(useDarkMode as jest.Mock).mockReturnValue({ isDarkMode: true })
    rerender(<ProductFilm />)

    expect(film()).toHaveAttribute('src', 'https://github.com/user-attachments/assets/67ca35c1-e66c-40c7-8295-740d57ceec92')
    expect(film()).toHaveAttribute('poster', 'https://github.com/user-attachments/assets/320881ba-a36d-4c00-949d-f4f56dc3d15c')
  })

  it('keeps the film the reader started when the page turns dark', () => {
    ;(useDarkMode as jest.Mock).mockReturnValue({ isDarkMode: false })
    const { rerender } = render(<ProductFilm />)
    const started = film()
    fireEvent.play(started)

    ;(useDarkMode as jest.Mock).mockReturnValue({ isDarkMode: true })
    rerender(<ProductFilm />)

    expect(film()).toBe(started)
    expect(film()).toHaveAttribute('src', 'https://github.com/user-attachments/assets/fcc38c9c-61ba-4db4-b026-4f947c931381')
    expect(film()).toHaveAttribute('poster', 'https://github.com/user-attachments/assets/1e6ecd17-024a-497d-8c12-041dbeaf5438')
  })
})
