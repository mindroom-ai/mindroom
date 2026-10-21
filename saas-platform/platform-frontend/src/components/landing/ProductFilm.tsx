'use client'

import { useEffect, useRef, useState } from 'react'
import { Play } from 'lucide-react'
import { useDarkMode } from '@/hooks/useDarkMode'

// The films are GitHub attachments so the video files stay out of Git history; next.config's media-src allows their hosts.
const FILMS = {
  light: {
    src: 'https://github.com/user-attachments/assets/fcc38c9c-61ba-4db4-b026-4f947c931381',
    poster: 'https://github.com/user-attachments/assets/1e6ecd17-024a-497d-8c12-041dbeaf5438',
  },
  dark: {
    src: 'https://github.com/user-attachments/assets/67ca35c1-e66c-40c7-8295-740d57ceec92',
    poster: 'https://github.com/user-attachments/assets/320881ba-a36d-4c00-949d-f4f56dc3d15c',
  },
}
// Both films run 49 seconds; update this with them.
const FILM_LENGTH = '0:49'

export function ProductFilm() {
  const { isDarkMode } = useDarkMode()
  const videoRef = useRef<HTMLVideoElement>(null)
  // A film the reader started keeps its theme, so a theme switch never restarts it.
  const [startedFilm, setStartedFilm] = useState<typeof FILMS.light | null>(null)
  const film = startedFilm ?? (isDarkMode ? FILMS.dark : FILMS.light)
  const started = startedFilm !== null
  // The play button disappears once the film starts, so keyboard focus follows the reader to the player.
  const focusPlayerOnStart = useRef(false)

  useEffect(() => {
    if (started && focusPlayerOnStart.current) videoRef.current?.focus()
  }, [started])

  return (
    <div className="relative overflow-hidden rounded-2xl bg-gray-100 shadow-[0_24px_60px_-24px_rgba(47,33,97,0.3)] dark:bg-gray-900 dark:shadow-none dark:outline dark:outline-1 dark:outline-white/10">
      <video
        ref={videoRef}
        src={film.src}
        poster={film.poster}
        controls={started}
        tabIndex={started ? 0 : -1}
        playsInline
        preload="none"
        aria-label="MindRoom product film"
        onPlay={() => setStartedFilm(film)}
        className="block aspect-video w-full focus-visible:outline-offset-[-6px]"
      />
      {!started && (
        <button
          type="button"
          // If the browser refuses to play, hand the reader the native controls instead of a dead button.
          onClick={(event) => {
            focusPlayerOnStart.current = event.currentTarget === document.activeElement
            videoRef.current?.play().catch(() => setStartedFilm(film))
          }}
          className="group absolute inset-0 flex items-center justify-center rounded-2xl bg-gradient-to-t from-gray-950/25 via-transparent to-transparent focus-visible:outline-offset-[-6px]"
        >
          <span className="flex items-center gap-2 rounded-full bg-brand-600 py-2 pl-2.5 pr-4 text-xs font-semibold sm:gap-3 sm:py-3 sm:pl-4 sm:pr-6 sm:text-sm text-white shadow-[0_12px_30px_-10px_rgba(17,24,39,0.55)] dark:shadow-none transition-transform duration-300 ease-out group-hover:scale-105 group-active:scale-100 dark:bg-brand-400 dark:text-brand-950">
            <span className="flex h-7 w-7 items-center justify-center rounded-full bg-white/20 sm:h-9 sm:w-9">
              <Play className="h-4 w-4 translate-x-px fill-current" />
            </span>
            Play the MindRoom film
            <span aria-hidden="true" className="font-normal tabular-nums">{FILM_LENGTH}</span>
          </span>
        </button>
      )}
    </div>
  )
}
